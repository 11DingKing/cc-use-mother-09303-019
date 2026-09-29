"""身份认证与机构数据隔离所需的账户基础设施。

业务历史走事件溯源（只追加）；账户/令牌属于基础设施，单独建表。
"""
from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass

from .errors import AuthenticationError, PermissionDenied
from .domain.aggregates import (
    ROLE_ADMIN,
    ROLE_ISSUER,
    ROLE_REVIEWER,
    ROLE_TEACHER,
    REVIEWER_ROLES,
)

ACCOUNT_SCHEMA = """
CREATE TABLE IF NOT EXISTS auth_account (
    actor_id   TEXT PRIMARY KEY,
    role       TEXT NOT NULL,
    org_id     TEXT NOT NULL DEFAULT '',
    secret_hash TEXT NOT NULL,
    token_hash TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""

VALID_ROLES = {ROLE_TEACHER, ROLE_ISSUER, ROLE_REVIEWER, ROLE_ADMIN}


@dataclass(frozen=True)
class Principal:
    """已认证调用方。"""

    actor_id: str
    role: str
    org_id: str

    def require_role(self, *roles: str) -> None:
        if self.role not in roles:
            raise PermissionDenied(f"当前角色无权执行该操作，需要：{'、'.join(roles)}")

    def require_reviewer(self) -> None:
        self.require_role(*REVIEWER_ROLES)


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthService:
    def __init__(self, conn: sqlite3.Connection) -> None:
        self._conn = conn
        self._conn.executescript(ACCOUNT_SCHEMA)

    def create_account(
        self, actor_id: str, role: str, secret: str, org_id: str = ""
    ) -> None:
        if role not in VALID_ROLES:
            raise ValueError(f"非法角色：{role}")
        if not actor_id or not secret:
            raise ValueError("账户标识与密钥不能为空")
        from .event_store import iso, utcnow

        try:
            self._conn.execute(
                """
                INSERT INTO auth_account(actor_id, role, org_id, secret_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (actor_id, role, org_id, _hash(secret), iso(utcnow())),
            )
            self._conn.commit()
        except sqlite3.IntegrityError as exc:
            raise PermissionDenied("账户已存在") from exc

    def ensure_bootstrap_admin(self, secret: str) -> None:
        row = self._conn.execute(
            "SELECT 1 FROM auth_account WHERE actor_id = 'admin'"
        ).fetchone()
        if row is None:
            self.create_account(ROLE_ADMIN, ROLE_ADMIN, secret)  # type: ignore[arg-type]

    def issue_token(self, actor_id: str, secret: str) -> str:
        row = self._conn.execute(
            "SELECT actor_id, role, org_id, secret_hash FROM auth_account WHERE actor_id = ?",
            (actor_id,),
        ).fetchone()
        if row is None or not secrets.compare_digest(row["secret_hash"], _hash(secret)):
            raise AuthenticationError("账户标识或密钥错误")
        token = secrets.token_urlsafe(24)
        self._conn.execute(
            "UPDATE auth_account SET token_hash = ? WHERE actor_id = ?",
            (_hash(token), actor_id),
        )
        self._conn.commit()
        return token

    def authenticate(self, token: str | None) -> Principal:
        if not token:
            raise AuthenticationError("缺少 Bearer 令牌")
        row = self._conn.execute(
            "SELECT actor_id, role, org_id FROM auth_account WHERE token_hash = ?",
            (_hash(token),),
        ).fetchone()
        if row is None:
            raise AuthenticationError("令牌无效或已过期")
        return Principal(actor_id=row["actor_id"], role=row["role"], org_id=row["org_id"])

    def revoke_token(self, actor_id: str) -> None:
        self._conn.execute(
            "UPDATE auth_account SET token_hash = '' WHERE actor_id = ?", (actor_id,)
        )
        self._conn.commit()
