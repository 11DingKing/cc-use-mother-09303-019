"""SQLite 持久化：schema 初始化与只追加事件日志。

历史可追溯性由两层保证：

1. ``events`` 表对 UPDATE/DELETE 设置触发器，直接拒绝修改与删除，
   撤销证明、重复提交登记、部分替代认定、申诉复核等全部以新事件追加。
2. 每条事件携带前一条事件的哈希（prev_hash）与自身哈希，形成散列链，
   ``Database.verify_chain`` 可在审计时重算校验是否被绕过触发器篡改。

当前状态列（如 evidences.status）是事件流的物化视图，仅由服务层在
追加事件的同一事务内更新。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

from .domain import iso, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS organizations (
    org_id     TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    kind       TEXT NOT NULL CHECK (kind IN ('培训机构', '签发方')),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
    user_id    TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    role       TEXT NOT NULL CHECK (role IN ('参训教师', '资格审核员', '签发方管理员', '平台管理员')),
    org_id     TEXT REFERENCES organizations(org_id),
    is_active  INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS training_units (
    unit_code  TEXT PRIMARY KEY,
    unit_name  TEXT NOT NULL,
    category   TEXT NOT NULL,
    active     INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL
);

-- 签发方被授权可签发哪些培训单元的证明（签发方验证不变量）
CREATE TABLE IF NOT EXISTS issuer_authorizations (
    issuer_id      TEXT NOT NULL REFERENCES organizations(org_id),
    unit_code      TEXT NOT NULL REFERENCES training_units(unit_code),
    authorized_at  TEXT NOT NULL,
    authorized_by  TEXT NOT NULL,
    PRIMARY KEY (issuer_id, unit_code)
);

CREATE TABLE IF NOT EXISTS rule_sets (
    rule_set        TEXT PRIMARY KEY,
    name            TEXT NOT NULL,
    current_version INTEGER,
    created_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS rule_versions (
    rule_set     TEXT NOT NULL REFERENCES rule_sets(rule_set),
    version      INTEGER NOT NULL,
    status       TEXT NOT NULL CHECK (status IN ('draft', 'published')),
    note         TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    created_by   TEXT NOT NULL,
    published_at TEXT,
    published_by TEXT,
    PRIMARY KEY (rule_set, version)
);

CREATE TABLE IF NOT EXISTS goals (
    rule_set       TEXT NOT NULL,
    version        INTEGER NOT NULL,
    goal_code      TEXT NOT NULL,
    goal_name      TEXT NOT NULL,
    required_hours REAL NOT NULL CHECK (required_hours > 0),
    sort_order     INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (rule_set, version, goal_code),
    FOREIGN KEY (rule_set, version) REFERENCES rule_versions(rule_set, version)
);

CREATE TABLE IF NOT EXISTS unit_goals (
    rule_set  TEXT NOT NULL,
    version   INTEGER NOT NULL,
    unit_code TEXT NOT NULL,
    goal_code TEXT NOT NULL,
    weight    REAL NOT NULL DEFAULT 1.0 CHECK (weight > 0),
    PRIMARY KEY (rule_set, version, unit_code, goal_code),
    FOREIGN KEY (rule_set, version, goal_code) REFERENCES goals(rule_set, version, goal_code),
    FOREIGN KEY (unit_code) REFERENCES training_units(unit_code)
);

-- 替代关系：from_unit 学时按 ratio 折算替代 to_unit；
-- 每个目标的替代总量封顶为 required_hours * cap_ratio（部分替代）。
CREATE TABLE IF NOT EXISTS substitutions (
    rule_set  TEXT NOT NULL,
    version   INTEGER NOT NULL,
    from_unit TEXT NOT NULL,
    to_unit   TEXT NOT NULL,
    ratio     REAL NOT NULL CHECK (ratio > 0 AND ratio <= 1),
    cap_ratio REAL NOT NULL CHECK (cap_ratio >= 0 AND cap_ratio <= 1),
    PRIMARY KEY (rule_set, version, from_unit, to_unit),
    CHECK (from_unit <> to_unit),
    FOREIGN KEY (rule_set, version) REFERENCES rule_versions(rule_set, version)
);

-- 教师注册到培训机构，并指定其资格判定适用的规则版本（冻结快照）
CREATE TABLE IF NOT EXISTS enrollments (
    teacher_id    TEXT PRIMARY KEY REFERENCES users(user_id),
    org_id        TEXT NOT NULL REFERENCES organizations(org_id),
    rule_set      TEXT NOT NULL,
    rule_version  INTEGER NOT NULL,
    enrolled_at   TEXT NOT NULL,
    FOREIGN KEY (rule_set, rule_version) REFERENCES rule_versions(rule_set, version)
);

CREATE TABLE IF NOT EXISTS evidences (
    evidence_id            TEXT PRIMARY KEY,
    teacher_id             TEXT NOT NULL REFERENCES users(user_id),
    unit_code              TEXT NOT NULL REFERENCES training_units(unit_code),
    issuer_id              TEXT NOT NULL REFERENCES organizations(org_id),
    hours                  REAL NOT NULL CHECK (hours > 0),
    issued_on              TEXT NOT NULL,
    external_ref           TEXT NOT NULL DEFAULT '',
    fingerprint            TEXT NOT NULL,
    status                 TEXT NOT NULL CHECK (status IN
                               ('提交', '验证', '撤销', '验证不通过', '重复提交')),
    duplicate_of           TEXT REFERENCES evidences(evidence_id),
    submitted_rule_set     TEXT NOT NULL,
    submitted_rule_version INTEGER NOT NULL,
    submitted_at           TEXT NOT NULL,
    verified_at            TEXT,
    rejected_at            TEXT,
    revoked_at             TEXT
);

CREATE INDEX IF NOT EXISTS idx_evidences_teacher ON evidences(teacher_id);
CREATE INDEX IF NOT EXISTS idx_evidences_fp ON evidences(teacher_id, fingerprint);

-- 只追加事件日志：撤销/驳回/重复登记/替代认定/申诉全部追加，永不修改
CREATE TABLE IF NOT EXISTS events (
    seq         INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id    TEXT NOT NULL UNIQUE,
    event_type  TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    actor_id    TEXT NOT NULL,
    teacher_id  TEXT,
    evidence_id TEXT,
    org_id      TEXT,
    payload     TEXT NOT NULL,
    prev_hash   TEXT NOT NULL,
    hash        TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_teacher ON events(teacher_id);
CREATE INDEX IF NOT EXISTS idx_events_evidence ON events(evidence_id);

CREATE TABLE IF NOT EXISTS appeals (
    appeal_id   TEXT PRIMARY KEY,
    teacher_id  TEXT NOT NULL REFERENCES users(user_id),
    evidence_id TEXT REFERENCES evidences(evidence_id),
    status      TEXT NOT NULL CHECK (status IN ('申诉中', '申诉成立', '申诉驳回')),
    reason      TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    decided_at  TEXT,
    decided_by  TEXT REFERENCES users(user_id),
    decision_note TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS appeal_credits (
    credit_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    appeal_id   TEXT NOT NULL REFERENCES appeals(appeal_id),
    evidence_id TEXT NOT NULL REFERENCES evidences(evidence_id),
    goal_code   TEXT NOT NULL,
    hours       REAL NOT NULL CHECK (hours > 0),
    reason      TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS evaluations (
    evaluation_id TEXT PRIMARY KEY,
    teacher_id    TEXT NOT NULL REFERENCES users(user_id),
    rule_set      TEXT NOT NULL,
    rule_version  INTEGER NOT NULL,
    decided_at    TEXT NOT NULL,
    decision      TEXT NOT NULL CHECK (decision IN ('合格', '不合格', '待判定')),
    result_json   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_evaluations_teacher ON evaluations(teacher_id);

-- 事件日志禁止更新与删除（SQLite 触发器在具体语句上抛出）
CREATE TRIGGER IF NOT EXISTS trg_events_no_update
BEFORE UPDATE ON events
BEGIN
    SELECT RAISE(ABORT, '事件日志只允许追加，禁止更新');
END;

CREATE TRIGGER IF NOT EXISTS trg_events_no_delete
BEFORE DELETE ON events
BEGIN
    SELECT RAISE(ABORT, '事件日志只允许追加，禁止删除');
END;
"""

GENESIS_HASH = "0" * 64


class Database:
    """SQLite 连接封装。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        # 后端按请求线程提供服务；用一把可重入锁串行化所有数据库访问，
        # 配合 BEGIN IMMEDIATE，避免跨线程共用连接与写写竞争。
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    def close(self) -> None:
        with self.lock:
            self.conn.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """序列化写事务；事件追加与状态物化必须在同一事务内完成。"""
        with self.lock:
            try:
                self.conn.execute("BEGIN IMMEDIATE")
                yield self.conn
                self.conn.commit()
            except Exception:
                self.conn.rollback()
                raise

    # -- 只追加事件日志 ---------------------------------------------------

    def append_event(
        self,
        conn: sqlite3.Connection,
        *,
        event_type: str,
        payload: dict[str, Any],
        actor_id: str,
        event_id: str | None = None,
        teacher_id: str | None = None,
        evidence_id: str | None = None,
        org_id: str | None = None,
        occurred_at: datetime | None = None,
    ) -> str:
        import uuid

        eid = event_id or f"evt_{uuid.uuid4().hex}"
        ts = iso(occurred_at or utcnow())
        row = conn.execute("SELECT hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        prev_hash = row["hash"] if row else GENESIS_HASH
        body = json.dumps(
            {
                "event_id": eid,
                "event_type": event_type,
                "occurred_at": ts,
                "actor_id": actor_id,
                "teacher_id": teacher_id,
                "evidence_id": evidence_id,
                "org_id": org_id,
                "payload": payload,
                "prev_hash": prev_hash,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
        conn.execute(
            """INSERT INTO events
               (event_id, event_type, occurred_at, actor_id, teacher_id,
                evidence_id, org_id, payload, prev_hash, hash)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                eid,
                event_type,
                ts,
                actor_id,
                teacher_id,
                evidence_id,
                org_id,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                prev_hash,
                digest,
            ),
        )
        return eid

    def verify_chain(self) -> dict[str, Any]:
        """重算全部事件哈希链，供审计调用。"""
        rows = self.conn.execute("SELECT * FROM events ORDER BY seq").fetchall()
        prev = GENESIS_HASH
        for row in rows:
            if row["prev_hash"] != prev:
                return {"ok": False, "broken_at": row["event_id"], "reason": "prev_hash 断链"}
            body = json.dumps(
                {
                    "event_id": row["event_id"],
                    "event_type": row["event_type"],
                    "occurred_at": row["occurred_at"],
                    "actor_id": row["actor_id"],
                    "teacher_id": row["teacher_id"],
                    "evidence_id": row["evidence_id"],
                    "org_id": row["org_id"],
                    "payload": json.loads(row["payload"]),
                    "prev_hash": prev,
                },
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            if hashlib.sha256(body.encode("utf-8")).hexdigest() != row["hash"]:
                return {"ok": False, "broken_at": row["event_id"], "reason": "哈希校验失败"}
            prev = row["hash"]
        return {"ok": True, "event_count": len(rows)}
