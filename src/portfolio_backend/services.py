"""应用服务层：用例编排、权限校验与历史追加。

所有写操作都在一个事务内完成两件事：

1. 向 ``events`` 只追加日志写入事件（含撤销、驳回、重复登记、
   替代认定说明、申诉提交与复核结论）；
2. 物化当前状态行。状态行可重建、可修正，事件日志不可改。

权限隔离（机构间资料互不可见）：

- 参训教师：仅本人证据组合、本人申诉。
- 资格审核员（培训机构）：仅本机构在册教师的资料；可发起判定、
  处理本机构教师的申诉复核。
- 签发方管理员：仅本签发方签发的证据；可验证、驳回、撤销。
- 平台管理员：规则集/规则版本/单元/机构/授权等全局登记，可审计。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from dataclasses import dataclass

from .database import Database
from .domain import (
    AppealStatus,
    Conflict,
    EvidenceStatus,
    NotFound,
    PermissionDenied,
    Role,
    RuleError,
    iso,
    require_slug,
    utcnow,
)
from .engine import evaluate


@dataclass(frozen=True)
class Principal:
    """请求主体（谁在操作）。"""

    user_id: str
    role: Role
    org_id: str | None = None
    name: str = ""

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Principal":
        return cls(
            user_id=row["user_id"],
            role=Role(row["role"]),
            org_id=row["org_id"],
            name=row["name"],
        )


class Service:
    def __init__(self, db: Database) -> None:
        self.db = db

    # ======================================================================
    # 基础查询与主体
    # ======================================================================

    def principal(self, user_id: str) -> Principal:
        row = self.db.conn.execute(
            "SELECT * FROM users WHERE user_id = ? AND is_active = 1", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"用户不存在或已停用：{user_id}")
        return Principal.from_row(row)

    def _get_user(self, conn, user_id: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM users WHERE user_id = ?", (user_id,)).fetchone()
        if row is None:
            raise NotFound(f"用户不存在：{user_id}")
        return row

    # ======================================================================
    # 机构与人员（平台管理员）
    # ======================================================================

    def create_organization(self, principal: Principal, org_id: str, name: str, kind: str) -> dict:
        self._require(principal, Role.ADMIN)
        require_slug(org_id, "机构编号")
        if kind not in ("培训机构", "签发方"):
            raise RuleError("机构类型必须是 培训机构 或 签发方")
        with self.db.tx() as conn:
            exists = conn.execute("SELECT 1 FROM organizations WHERE org_id = ?", (org_id,)).fetchone()
            if exists:
                raise Conflict(f"机构已存在：{org_id}")
            conn.execute(
                "INSERT INTO organizations (org_id, name, kind, created_at) VALUES (?, ?, ?, ?)",
                (org_id, name, kind, iso(utcnow())),
            )
            self.db.append_event(
                conn,
                event_type="organization.created",
                payload={"org_id": org_id, "name": name, "kind": kind},
                actor_id=principal.user_id,
                org_id=org_id,
            )
        return {"org_id": org_id, "name": name, "kind": kind}

    def create_user(
        self,
        principal: Principal,
        user_id: str,
        name: str,
        role: Role,
        org_id: str | None = None,
    ) -> dict:
        self._require(principal, Role.ADMIN)
        require_slug(user_id, "用户编号")
        if role in (Role.ORG_REVIEWER, Role.ISSUER_STAFF) and not org_id:
            raise RuleError("机构角色必须归属一个机构")
        with self.db.tx() as conn:
            if conn.execute("SELECT 1 FROM users WHERE user_id = ?", (user_id,)).fetchone():
                raise Conflict(f"用户已存在：{user_id}")
            if org_id and not conn.execute(
                "SELECT 1 FROM organizations WHERE org_id = ?", (org_id,)
            ).fetchone():
                raise NotFound(f"机构不存在：{org_id}")
            conn.execute(
                "INSERT INTO users (user_id, name, role, org_id, created_at) VALUES (?, ?, ?, ?, ?)",
                (user_id, name, role.value, org_id, iso(utcnow())),
            )
            self.db.append_event(
                conn,
                event_type="user.created",
                payload={"user_id": user_id, "name": name, "role": role.value, "org_id": org_id},
                actor_id=principal.user_id,
            )
        return {"user_id": user_id, "name": name, "role": role.value, "org_id": org_id}

    # ======================================================================
    # 培训单元与签发授权（平台管理员）
    # ======================================================================

    def register_unit(
        self, principal: Principal, unit_code: str, unit_name: str, category: str
    ) -> dict:
        self._require(principal, Role.ADMIN)
        require_slug(unit_code, "培训单元编号")
        with self.db.tx() as conn:
            if conn.execute("SELECT 1 FROM training_units WHERE unit_code = ?", (unit_code,)).fetchone():
                raise Conflict(f"培训单元已存在：{unit_code}")
            conn.execute(
                "INSERT INTO training_units (unit_code, unit_name, category, created_at) "
                "VALUES (?, ?, ?, ?)",
                (unit_code, unit_name, category, iso(utcnow())),
            )
            self.db.append_event(
                conn,
                event_type="unit.registered",
                payload={"unit_code": unit_code, "unit_name": unit_name, "category": category},
                actor_id=principal.user_id,
            )
        return {"unit_code": unit_code, "unit_name": unit_name, "category": category}

    def authorize_issuer(
        self, principal: Principal, issuer_id: str, unit_code: str
    ) -> dict:
        self._require(principal, Role.ADMIN)
        with self.db.tx() as conn:
            self._require_org_kind(conn, issuer_id, "签发方")
            if not conn.execute(
                "SELECT 1 FROM training_units WHERE unit_code = ?", (unit_code,)
            ).fetchone():
                raise NotFound(f"培训单元不存在：{unit_code}")
            try:
                conn.execute(
                    "INSERT INTO issuer_authorizations (issuer_id, unit_code, authorized_at, authorized_by) "
                    "VALUES (?, ?, ?, ?)",
                    (issuer_id, unit_code, iso(utcnow()), principal.user_id),
                )
            except sqlite3.IntegrityError:
                raise Conflict("该签发授权已存在")
            self.db.append_event(
                conn,
                event_type="issuer.authorized",
                payload={"issuer_id": issuer_id, "unit_code": unit_code},
                actor_id=principal.user_id,
                org_id=issuer_id,
            )
        return {"issuer_id": issuer_id, "unit_code": unit_code}

    # ======================================================================
    # 规则集、规则版本、能力目标、替代关系（平台管理员）
    # ======================================================================

    def create_rule_set(self, principal: Principal, rule_set: str, name: str) -> dict:
        self._require(principal, Role.ADMIN)
        require_slug(rule_set, "规则集编号")
        with self.db.tx() as conn:
            if conn.execute("SELECT 1 FROM rule_sets WHERE rule_set = ?", (rule_set,)).fetchone():
                raise Conflict(f"规则集已存在：{rule_set}")
            conn.execute(
                "INSERT INTO rule_sets (rule_set, name, created_at) VALUES (?, ?, ?)",
                (rule_set, name, iso(utcnow())),
            )
            self.db.append_event(
                conn,
                event_type="ruleset.created",
                payload={"rule_set": rule_set, "name": name},
                actor_id=principal.user_id,
            )
        return {"rule_set": rule_set, "name": name}

    def create_rule_version(
        self, principal: Principal, rule_set: str, note: str = ""
    ) -> dict:
        """创建草稿版本。草稿可反复修改；一旦发布即冻结，不可再改。"""
        self._require(principal, Role.ADMIN)
        with self.db.tx() as conn:
            rs = self._get_rule_set(conn, rule_set)
            version = (rs["current_version"] or 0) + 1
            conn.execute(
                "INSERT INTO rule_versions (rule_set, version, status, note, created_at, created_by) "
                "VALUES (?, ?, 'draft', ?, ?, ?)",
                (rule_set, version, note, iso(utcnow()), principal.user_id),
            )
            self.db.append_event(
                conn,
                event_type="ruleversion.drafted",
                payload={"rule_set": rule_set, "version": version, "note": note},
                actor_id=principal.user_id,
            )
        return {"rule_set": rule_set, "version": version, "status": "draft"}

    def _get_rule_set(self, conn, rule_set: str) -> sqlite3.Row:
        row = conn.execute("SELECT * FROM rule_sets WHERE rule_set = ?", (rule_set,)).fetchone()
        if row is None:
            raise NotFound(f"规则集不存在：{rule_set}")
        return row

    def _get_draft(self, conn, rule_set: str, version: int) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM rule_versions WHERE rule_set = ? AND version = ?",
            (rule_set, version),
        ).fetchone()
        if row is None:
            raise NotFound(f"规则版本不存在：{rule_set}@v{version}")
        if row["status"] != "draft":
            raise Conflict(f"规则版本已发布并冻结：{rule_set}@v{version}")
        return row

    def add_goal(
        self,
        principal: Principal,
        rule_set: str,
        version: int,
        goal_code: str,
        goal_name: str,
        required_hours: float,
        sort_order: int = 0,
    ) -> dict:
        self._require(principal, Role.ADMIN)
        require_slug(goal_code, "能力目标编号")
        if required_hours <= 0:
            raise RuleError("目标学时必须为正数")
        with self.db.tx() as conn:
            self._get_draft(conn, rule_set, version)
            try:
                conn.execute(
                    "INSERT INTO goals (rule_set, version, goal_code, goal_name, required_hours, sort_order) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (rule_set, version, goal_code, goal_name, required_hours, sort_order),
                )
            except sqlite3.IntegrityError:
                raise Conflict(f"能力目标已存在：{goal_code}")
            self.db.append_event(
                conn,
                event_type="goal.added",
                payload={
                    "rule_set": rule_set,
                    "version": version,
                    "goal_code": goal_code,
                    "goal_name": goal_name,
                    "required_hours": required_hours,
                },
                actor_id=principal.user_id,
            )
        return {"goal_code": goal_code, "goal_name": goal_name, "required_hours": required_hours}

    def map_unit_goal(
        self,
        principal: Principal,
        rule_set: str,
        version: int,
        unit_code: str,
        goal_code: str,
        weight: float = 1.0,
    ) -> dict:
        """登记“培训单元直连能力目标”映射（判定的核心依据）。"""
        self._require(principal, Role.ADMIN)
        if weight <= 0:
            raise RuleError("覆盖权重必须为正数")
        with self.db.tx() as conn:
            self._get_draft(conn, rule_set, version)
            self._require_unit(conn, unit_code)
            self._require_goal(conn, rule_set, version, goal_code)
            try:
                conn.execute(
                    "INSERT INTO unit_goals (rule_set, version, unit_code, goal_code, weight) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (rule_set, version, unit_code, goal_code, weight),
                )
            except sqlite3.IntegrityError:
                raise Conflict("该单元—目标映射已存在")
            self.db.append_event(
                conn,
                event_type="unitgoal.mapped",
                payload={
                    "rule_set": rule_set,
                    "version": version,
                    "unit_code": unit_code,
                    "goal_code": goal_code,
                    "weight": weight,
                },
                actor_id=principal.user_id,
            )
        return {"unit_code": unit_code, "goal_code": goal_code, "weight": weight}

    def add_substitution(
        self,
        principal: Principal,
        rule_set: str,
        version: int,
        from_unit: str,
        to_unit: str,
        ratio: float,
        cap_ratio: float,
    ) -> dict:
        """登记替代关系。

        ratio: from_unit 1 学时折算为 to_unit 的学时比例；
        cap_ratio: 该替代对每个目标的计入上限占目标要求学时的比例
        （< 1 即结构性部分替代；证据超出上限时超出部分被截顶）。
        """
        self._require(principal, Role.ADMIN)
        if not 0 < ratio <= 1:
            raise RuleError("折算比例必须在 (0, 1] 区间")
        if not 0 <= cap_ratio <= 1:
            raise RuleError("封顶比例必须在 [0, 1] 区间")
        if from_unit == to_unit:
            raise RuleError("替代单元与目标单元不能相同")
        with self.db.tx() as conn:
            self._get_draft(conn, rule_set, version)
            self._require_unit(conn, from_unit)
            self._require_unit(conn, to_unit)
            try:
                conn.execute(
                    "INSERT INTO substitutions (rule_set, version, from_unit, to_unit, ratio, cap_ratio) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (rule_set, version, from_unit, to_unit, ratio, cap_ratio),
                )
            except sqlite3.IntegrityError:
                raise Conflict("该替代关系已存在")
            self.db.append_event(
                conn,
                event_type="substitution.added",
                payload={
                    "rule_set": rule_set,
                    "version": version,
                    "from_unit": from_unit,
                    "to_unit": to_unit,
                    "ratio": ratio,
                    "cap_ratio": cap_ratio,
                },
                actor_id=principal.user_id,
            )
        return {
            "from_unit": from_unit,
            "to_unit": to_unit,
            "ratio": ratio,
            "cap_ratio": cap_ratio,
        }

    def publish_rule_version(
        self, principal: Principal, rule_set: str, version: int
    ) -> dict:
        """发布即冻结：已发布版本的目标、映射、替代关系永不改变。"""
        self._require(principal, Role.ADMIN)
        with self.db.tx() as conn:
            self._get_draft(conn, rule_set, version)
            goals = conn.execute(
                "SELECT COUNT(*) AS n FROM goals WHERE rule_set = ? AND version = ?",
                (rule_set, version),
            ).fetchone()["n"]
            if goals == 0:
                raise RuleError("规则版本至少要包含一个能力目标才能发布")
            now = iso(utcnow())
            conn.execute(
                "UPDATE rule_versions SET status = 'published', published_at = ?, published_by = ? "
                "WHERE rule_set = ? AND version = ?",
                (now, principal.user_id, rule_set, version),
            )
            conn.execute(
                "UPDATE rule_sets SET current_version = ? WHERE rule_set = ?",
                (version, rule_set),
            )
            self.db.append_event(
                conn,
                event_type="ruleversion.published",
                payload={"rule_set": rule_set, "version": version, "frozen": True},
                actor_id=principal.user_id,
            )
        return {"rule_set": rule_set, "version": version, "status": "published"}

    def get_rule_version(self, rule_set: str, version: int) -> dict:
        conn = self.db.conn
        rv = conn.execute(
            "SELECT * FROM rule_versions WHERE rule_set = ? AND version = ?",
            (rule_set, version),
        ).fetchone()
        if rv is None:
            raise NotFound(f"规则版本不存在：{rule_set}@v{version}")
        goals = [
            dict(r)
            for r in conn.execute(
                "SELECT goal_code, goal_name, required_hours, sort_order FROM goals "
                "WHERE rule_set = ? AND version = ? ORDER BY sort_order, goal_code",
                (rule_set, version),
            )
        ]
        maps = [
            dict(r)
            for r in conn.execute(
                "SELECT unit_code, goal_code, weight FROM unit_goals "
                "WHERE rule_set = ? AND version = ?",
                (rule_set, version),
            )
        ]
        subs = [
            dict(r)
            for r in conn.execute(
                "SELECT from_unit, to_unit, ratio, cap_ratio FROM substitutions "
                "WHERE rule_set = ? AND version = ?",
                (rule_set, version),
            )
        ]
        return {
            "rule_set": rule_set,
            "version": version,
            "status": rv["status"],
            "note": rv["note"],
            "published_at": rv["published_at"],
            "goals": goals,
            "unit_goals": maps,
            "substitutions": subs,
        }

    # ======================================================================
    # 教师注册（绑定冻结的规则版本）
    # ======================================================================

    def enroll_teacher(
        self,
        principal: Principal,
        teacher_id: str,
        org_id: str,
        rule_set: str,
        rule_version: int | None = None,
    ) -> dict:
        self._require(principal, Role.ADMIN)
        with self.db.tx() as conn:
            teacher = self._get_user(conn, teacher_id)
            if Role(teacher["role"]) is not Role.TEACHER:
                raise RuleError("只有参训教师可以注册资格档案")
            self._require_org_kind(conn, org_id, "培训机构")
            rv = conn.execute(
                "SELECT * FROM rule_versions WHERE rule_set = ? AND version = ?",
                (rule_set, rule_version or self._get_rule_set(conn, rule_set)["current_version"]),
            ).fetchone()
            if rv is None:
                raise NotFound("指定的规则版本不存在")
            if rv["status"] != "published":
                raise RuleError("只能按已发布的规则版本建档")
            version = rv["version"]
            try:
                conn.execute(
                    "INSERT INTO enrollments (teacher_id, org_id, rule_set, rule_version, enrolled_at) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (teacher_id, org_id, rule_set, version, iso(utcnow())),
                )
            except sqlite3.IntegrityError:
                raise Conflict("该教师已建档")
            self.db.append_event(
                conn,
                event_type="teacher.enrolled",
                payload={
                    "teacher_id": teacher_id,
                    "org_id": org_id,
                    "rule_set": rule_set,
                    "rule_version": version,
                },
                actor_id=principal.user_id,
                teacher_id=teacher_id,
                org_id=org_id,
            )
        return {
            "teacher_id": teacher_id,
            "org_id": org_id,
            "rule_set": rule_set,
            "rule_version": version,
        }

    # ======================================================================
    # 证据：提交（去重）、验证、驳回、撤销——全程追加历史
    # ======================================================================

    @staticmethod
    def _fingerprint(teacher_id: str, issuer_id: str, unit_code: str,
                     issued_on: str, hours: float, external_ref: str) -> str:
        raw = "|".join(
            [teacher_id, issuer_id, unit_code, issued_on, f"{hours:g}", external_ref.strip().lower()]
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def submit_evidence(
        self,
        principal: Principal,
        teacher_id: str,
        unit_code: str,
        issuer_id: str,
        hours: float,
        issued_on: str,
        external_ref: str = "",
    ) -> dict:
        """教师提交证明。命中去重指纹时登记为“重复提交”，不进入组合。"""
        if principal.role is Role.TEACHER:
            if principal.user_id != teacher_id:
                raise PermissionDenied("教师只能提交本人的证明")
        elif principal.role is Role.ORG_REVIEWER:
            self._assert_teacher_in_org(teacher_id, principal.org_id)
        else:
            raise PermissionDenied("该角色不能提交证明")
        if hours <= 0:
            raise RuleError("学时必须为正数")

        with self.db.tx() as conn:
            enrollment = self._get_enrollment(conn, teacher_id)
            self._require_unit(conn, unit_code)
            self._require_org_kind(conn, issuer_id, "签发方")
            fp = self._fingerprint(teacher_id, issuer_id, unit_code, issued_on, hours, external_ref)
            duplicate = conn.execute(
                "SELECT evidence_id FROM evidences WHERE teacher_id = ? AND fingerprint = ? "
                "AND status <> '重复提交' ORDER BY submitted_at LIMIT 1",
                (teacher_id, fp),
            ).fetchone()
            evidence_id = f"ev_{uuid.uuid4().hex[:12]}"
            now = iso(utcnow())
            if duplicate is not None:
                # 重复提交也登记留痕：只追加历史，组合不重复计入。
                conn.execute(
                    """INSERT INTO evidences
                       (evidence_id, teacher_id, unit_code, issuer_id, hours, issued_on,
                        external_ref, fingerprint, status, duplicate_of,
                        submitted_rule_set, submitted_rule_version, submitted_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, '重复提交', ?, ?, ?, ?)""",
                    (evidence_id, teacher_id, unit_code, issuer_id, hours, issued_on,
                     external_ref, fp, duplicate["evidence_id"],
                     enrollment["rule_set"], enrollment["rule_version"], now),
                )
                self.db.append_event(
                    conn,
                    event_type="evidence.duplicate_submitted",
                    payload={
                        "evidence_id": evidence_id,
                        "duplicate_of": duplicate["evidence_id"],
                        "fingerprint": fp,
                        "unit_code": unit_code,
                        "hours": hours,
                    },
                    actor_id=principal.user_id,
                    teacher_id=teacher_id,
                    evidence_id=evidence_id,
                )
                return {
                    "evidence_id": evidence_id,
                    "status": EvidenceStatus.DUPLICATE.value,
                    "duplicate_of": duplicate["evidence_id"],
                }

            conn.execute(
                """INSERT INTO evidences
                   (evidence_id, teacher_id, unit_code, issuer_id, hours, issued_on,
                    external_ref, fingerprint, status, submitted_rule_set,
                    submitted_rule_version, submitted_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, '提交', ?, ?, ?)""",
                (evidence_id, teacher_id, unit_code, issuer_id, hours, issued_on,
                 external_ref, fp, enrollment["rule_set"], enrollment["rule_version"], now),
            )
            self.db.append_event(
                conn,
                event_type="evidence.submitted",
                payload={
                    "evidence_id": evidence_id,
                    "unit_code": unit_code,
                    "issuer_id": issuer_id,
                    "hours": hours,
                    "issued_on": issued_on,
                    "external_ref": external_ref,
                    "fingerprint": fp,
                },
                actor_id=principal.user_id,
                teacher_id=teacher_id,
                evidence_id=evidence_id,
            )
        return {"evidence_id": evidence_id, "status": EvidenceStatus.SUBMITTED.value}

    def verify_evidence(self, principal: Principal, evidence_id: str, note: str = "") -> dict:
        """签发方验证证明：必须是该签发方且已获该单元授权。"""
        with self.db.tx() as conn:
            ev = self._get_evidence(conn, evidence_id)
            self._require_issuer_access(principal, ev)
            if ev["status"] != EvidenceStatus.SUBMITTED.value:
                raise Conflict(f"证据当前状态为 {ev['status']}，不能验证")
            authorized = conn.execute(
                "SELECT 1 FROM issuer_authorizations WHERE issuer_id = ? AND unit_code = ?",
                (principal.org_id, ev["unit_code"]),
            ).fetchone()
            if authorized is None:
                raise PermissionDenied(
                    f"签发方 {principal.org_id} 未被授权签发单元 {ev['unit_code']} 的证明"
                )
            now = iso(utcnow())
            conn.execute(
                "UPDATE evidences SET status = '验证', verified_at = ? WHERE evidence_id = ?",
                (now, evidence_id),
            )
            self.db.append_event(
                conn,
                event_type="evidence.verified",
                payload={"evidence_id": evidence_id, "note": note, "unit_code": ev["unit_code"]},
                actor_id=principal.user_id,
                teacher_id=ev["teacher_id"],
                evidence_id=evidence_id,
                org_id=principal.org_id,
            )
        return {"evidence_id": evidence_id, "status": EvidenceStatus.VERIFIED.value}

    def reject_evidence(self, principal: Principal, evidence_id: str, reason: str) -> dict:
        with self.db.tx() as conn:
            ev = self._get_evidence(conn, evidence_id)
            self._require_issuer_access(principal, ev)
            if ev["status"] != EvidenceStatus.SUBMITTED.value:
                raise Conflict(f"证据当前状态为 {ev['status']}，不能驳回")
            now = iso(utcnow())
            conn.execute(
                "UPDATE evidences SET status = '验证不通过', rejected_at = ? WHERE evidence_id = ?",
                (now, evidence_id),
            )
            self.db.append_event(
                conn,
                event_type="evidence.rejected",
                payload={"evidence_id": evidence_id, "reason": reason},
                actor_id=principal.user_id,
                teacher_id=ev["teacher_id"],
                evidence_id=evidence_id,
                org_id=principal.org_id,
            )
        return {"evidence_id": evidence_id, "status": EvidenceStatus.REJECTED.value}

    def revoke_evidence(self, principal: Principal, evidence_id: str, reason: str) -> dict:
        """撤销证明：原记录保留并标记撤销，历史以事件追加，判定立即排除。"""
        with self.db.tx() as conn:
            ev = self._get_evidence(conn, evidence_id)
            self._require_issuer_access(principal, ev)
            if ev["status"] not in (
                EvidenceStatus.SUBMITTED.value,
                EvidenceStatus.VERIFIED.value,
            ):
                raise Conflict(f"证据当前状态为 {ev['status']}，不能撤销")
            now = iso(utcnow())
            conn.execute(
                "UPDATE evidences SET status = '撤销', revoked_at = ? WHERE evidence_id = ?",
                (now, evidence_id),
            )
            self.db.append_event(
                conn,
                event_type="evidence.revoked",
                payload={"evidence_id": evidence_id, "reason": reason, "previous_status": ev["status"]},
                actor_id=principal.user_id,
                teacher_id=ev["teacher_id"],
                evidence_id=evidence_id,
                org_id=principal.org_id,
            )
        return {"evidence_id": evidence_id, "status": EvidenceStatus.REVOKED.value}

    # ======================================================================
    # 组合与判定
    # ======================================================================

    def _load_evaluation_inputs(
        self, conn, teacher_id: str
    ) -> tuple[sqlite3.Row, list[dict], list[dict], list[dict], list[dict], list[dict]]:
        enrollment = self._get_enrollment(conn, teacher_id)
        rule_set, version = enrollment["rule_set"], enrollment["rule_version"]
        goals = conn.execute(
            "SELECT goal_code, goal_name, required_hours, sort_order FROM goals "
            "WHERE rule_set = ? AND version = ? ORDER BY sort_order, goal_code",
            (rule_set, version),
        ).fetchall()
        direct_maps = conn.execute(
            "SELECT unit_code, goal_code, weight FROM unit_goals WHERE rule_set = ? AND version = ?",
            (rule_set, version),
        ).fetchall()
        substitutions = conn.execute(
            "SELECT from_unit, to_unit, ratio, cap_ratio FROM substitutions "
            "WHERE rule_set = ? AND version = ?",
            (rule_set, version),
        ).fetchall()
        evidences = conn.execute(
            """SELECT e.rowid AS submitted_seq, e.*, tu.unit_name FROM evidences e
               JOIN training_units tu ON tu.unit_code = e.unit_code
               WHERE e.teacher_id = ? ORDER BY e.rowid""",
            (teacher_id,),
        ).fetchall()
        credits = conn.execute(
            """SELECT ac.evidence_id, ac.goal_code, ac.hours, ac.reason
               FROM appeal_credits ac
               JOIN appeals a ON a.appeal_id = ac.appeal_id
               WHERE a.teacher_id = ? AND a.status = '申诉成立'""",
            (teacher_id,),
        ).fetchall()
        return enrollment, [dict(r) for r in goals], [dict(r) for r in direct_maps], [dict(r) for r in substitutions], [dict(r) for r in evidences], [dict(r) for r in credits]

    def evaluate_teacher(
        self, principal: Principal, teacher_id: str, *, persist: bool = True
    ) -> dict:
        """按教师绑定的已冻结规则版本判定，返回缺口与逐证据贡献解释。"""
        self._assert_can_read_teacher(principal, teacher_id)
        with self.db.tx() as conn:
            enrollment, goals, maps, subs, evidences, credits = self._load_evaluation_inputs(
                conn, teacher_id
            )
            rule_set, version = enrollment["rule_set"], enrollment["rule_version"]
            result = evaluate(
                teacher_id=teacher_id,
                rule_set=rule_set,
                rule_version=version,
                goals=goals,
                direct_maps=maps,
                substitutions=subs,
                evidences=evidences,
                appeal_credits=credits,
            )
            payload = result.to_dict()
            evaluation_id = f"eval_{uuid.uuid4().hex[:12]}"
            if persist:
                conn.execute(
                    """INSERT INTO evaluations
                       (evaluation_id, teacher_id, rule_set, rule_version, decided_at, decision, result_json)
                       VALUES (?, ?, ?, ?, ?, ?, ?)""",
                    (
                        evaluation_id,
                        teacher_id,
                        rule_set,
                        version,
                        result.decided_at,
                        result.decision.value,
                        json.dumps(payload, ensure_ascii=False, sort_keys=True),
                    ),
                )
            self.db.append_event(
                conn,
                event_type="evaluation.decided",
                payload={
                    "evaluation_id": evaluation_id if persist else None,
                    "decision": result.decision.value,
                    "rule_set": rule_set,
                    "rule_version": version,
                    "gap_count": len(result.gaps),
                    "gaps": list(result.gaps),
                },
                actor_id=principal.user_id,
                teacher_id=teacher_id,
            )
        payload["evaluation_id"] = evaluation_id if persist else None
        return payload

    def list_evaluations(self, principal: Principal, teacher_id: str) -> list[dict]:
        """教师的全部历史判定（每次判定都追加留痕，不覆盖旧结论）。"""
        self._assert_can_read_teacher(principal, teacher_id)
        rows = self.db.conn.execute(
            """SELECT evaluation_id, rule_set, rule_version, decided_at, decision, result_json
               FROM evaluations WHERE teacher_id = ?
               ORDER BY rowid""",
            (teacher_id,),
        ).fetchall()
        return [
            {
                "evaluation_id": r["evaluation_id"],
                "rule_set": r["rule_set"],
                "rule_version": r["rule_version"],
                "decided_at": r["decided_at"],
                "decision": r["decision"],
                "result": json.loads(r["result_json"]),
            }
            for r in rows
        ]

    def get_portfolio(self, principal: Principal, teacher_id: str) -> dict:
        """形成可追溯证据组合：注册信息、证据清单（含状态）与完整历史。"""
        self._assert_can_read_teacher(principal, teacher_id)
        conn = self.db.conn
        enrollment = self._get_enrollment(conn, teacher_id)
        evidences = [
            self._evidence_public(r)
            for r in conn.execute(
                """SELECT e.*, tu.unit_name FROM evidences e
                   JOIN training_units tu ON tu.unit_code = e.unit_code
                   WHERE e.teacher_id = ? ORDER BY e.submitted_at, e.evidence_id""",
                (teacher_id,),
            )
        ]
        history = [
            {
                "seq": r["seq"],
                "event_id": r["event_id"],
                "event_type": r["event_type"],
                "occurred_at": r["occurred_at"],
                "actor_id": r["actor_id"],
                "evidence_id": r["evidence_id"],
                "payload": json.loads(r["payload"]),
            }
            for r in conn.execute(
                "SELECT * FROM events WHERE teacher_id = ? ORDER BY seq", (teacher_id,)
            )
        ]
        appeals = [
            dict(r)
            for r in conn.execute(
                "SELECT appeal_id, evidence_id, status, reason, created_at, decided_at, "
                "decided_by, decision_note FROM appeals WHERE teacher_id = ? ORDER BY created_at",
                (teacher_id,),
            )
        ]
        last_eval = conn.execute(
            "SELECT evaluation_id, decision, decided_at FROM evaluations "
            "WHERE teacher_id = ? ORDER BY decided_at DESC, rowid DESC LIMIT 1",
            (teacher_id,),
        ).fetchone()
        return {
            "teacher_id": teacher_id,
            "enrollment": dict(enrollment),
            "rule_version": {
                "rule_set": enrollment["rule_set"],
                "version": enrollment["rule_version"],
            },
            "evidence_count": len(evidences),
            "evidences": evidences,
            "appeals": appeals,
            "last_evaluation": dict(last_eval) if last_eval else None,
            "history": history,
        }

    # ======================================================================
    # 申诉与复核：只追加
    # ======================================================================

    def file_appeal(
        self,
        principal: Principal,
        teacher_id: str,
        reason: str,
        evidence_id: str | None = None,
    ) -> dict:
        if principal.role is Role.TEACHER and principal.user_id != teacher_id:
            raise PermissionDenied("教师只能为本人提起申诉")
        if principal.role is not Role.TEACHER:
            raise PermissionDenied("申诉只能由教师本人提起")
        with self.db.tx() as conn:
            self._get_enrollment(conn, teacher_id)
            if evidence_id:
                ev = self._get_evidence(conn, evidence_id)
                if ev["teacher_id"] != teacher_id:
                    raise PermissionDenied("证据不属于该教师")
            appeal_id = f"ap_{uuid.uuid4().hex[:12]}"
            now = iso(utcnow())
            conn.execute(
                """INSERT INTO appeals (appeal_id, teacher_id, evidence_id, status, reason, created_at)
                   VALUES (?, ?, ?, '申诉中', ?, ?)""",
                (appeal_id, teacher_id, evidence_id, reason, now),
            )
            self.db.append_event(
                conn,
                event_type="appeal.filed",
                payload={
                    "appeal_id": appeal_id,
                    "reason": reason,
                    "evidence_id": evidence_id,
                },
                actor_id=principal.user_id,
                teacher_id=teacher_id,
                evidence_id=evidence_id,
            )
        return {"appeal_id": appeal_id, "status": AppealStatus.OPEN.value}

    def review_appeal(
        self,
        principal: Principal,
        appeal_id: str,
        uphold: bool,
        decision_note: str,
        credits: list[dict] | None = None,
    ) -> dict:
        """机构审核员复核申诉。

        成立时可通过 credits 追加目标学时认定：
        [{evidence_id, goal_code, hours, reason?}]。
        认定是追加历史，不回改证据原始状态。
        """
        if principal.role is not Role.ORG_REVIEWER:
            raise PermissionDenied("只有所属培训机构的资格审核员可以复核申诉")
        credits = credits or []
        with self.db.tx() as conn:
            appeal = conn.execute(
                "SELECT * FROM appeals WHERE appeal_id = ?", (appeal_id,)
            ).fetchone()
            if appeal is None:
                raise NotFound(f"申诉不存在：{appeal_id}")
            self._assert_teacher_in_org(appeal["teacher_id"], principal.org_id)
            if appeal["status"] != AppealStatus.OPEN.value:
                raise Conflict("该申诉已复核，结论不可更改（如需更正请发起新申诉）")
            if uphold:
                enrollment = self._get_enrollment(conn, appeal["teacher_id"])
                goal_codes = {
                    r["goal_code"]
                    for r in conn.execute(
                        "SELECT goal_code FROM goals WHERE rule_set = ? AND version = ?",
                        (enrollment["rule_set"], enrollment["rule_version"]),
                    )
                }
                for c in credits:
                    if c["goal_code"] not in goal_codes:
                        raise RuleError(f"认定目标不在规则版本中：{c['goal_code']}")
                    if float(c["hours"]) <= 0:
                        raise RuleError("认定学时必须为正数")
                    credit_ev = self._get_evidence(conn, c["evidence_id"])
                    if credit_ev["teacher_id"] != appeal["teacher_id"]:
                        raise RuleError("认定证据不属于该教师")
            status = AppealStatus.UPHELD.value if uphold else AppealStatus.REJECTED.value
            now = iso(utcnow())
            conn.execute(
                "UPDATE appeals SET status = ?, decided_at = ?, decided_by = ?, decision_note = ? "
                "WHERE appeal_id = ?",
                (status, now, principal.user_id, decision_note, appeal_id),
            )
            for c in credits:
                conn.execute(
                    "INSERT INTO appeal_credits (appeal_id, evidence_id, goal_code, hours, reason) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (
                        appeal_id,
                        c["evidence_id"],
                        c["goal_code"],
                        float(c["hours"]),
                        c.get("reason", decision_note),
                    ),
                )
            self.db.append_event(
                conn,
                event_type="appeal.reviewed",
                payload={
                    "appeal_id": appeal_id,
                    "upheld": uphold,
                    "status": status,
                    "decision_note": decision_note,
                    "credits": credits,
                },
                actor_id=principal.user_id,
                teacher_id=appeal["teacher_id"],
                evidence_id=appeal["evidence_id"],
            )
        return {"appeal_id": appeal_id, "status": status, "credits": credits}

    # ======================================================================
    # 审计与机构视角列表（权限内）
    # ======================================================================

    def list_teachers_for_reviewer(self, principal: Principal) -> list[dict]:
        if principal.role is not Role.ORG_REVIEWER:
            raise PermissionDenied("仅资格审核员可查看本机构教师名册")
        rows = self.db.conn.execute(
            """SELECT u.user_id, u.name, e.rule_set, e.rule_version, e.enrolled_at
               FROM enrollments e JOIN users u ON u.user_id = e.teacher_id
               WHERE e.org_id = ? ORDER BY u.user_id""",
            (principal.org_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_evidence_for_issuer(self, principal: Principal) -> list[dict]:
        if principal.role is not Role.ISSUER_STAFF:
            raise PermissionDenied("仅签发方管理员可查看待办证明")
        rows = self.db.conn.execute(
            """SELECT e.evidence_id, e.teacher_id, e.unit_code, tu.unit_name,
                      e.hours, e.issued_on, e.status, e.submitted_at
               FROM evidences e JOIN training_units tu ON tu.unit_code = e.unit_code
               WHERE e.issuer_id = ? ORDER BY e.submitted_at""",
            (principal.org_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_events(self, principal: Principal, teacher_id: str | None = None) -> list[dict]:
        """平台管理员可全量审计；机构审核员限本机构教师；教师限本人。"""
        conn = self.db.conn
        sql = "SELECT * FROM events"
        params: tuple = ()
        if principal.role is Role.ADMIN:
            if teacher_id:
                sql += " WHERE teacher_id = ?"
                params = (teacher_id,)
        elif principal.role is Role.ORG_REVIEWER:
            ids = {
                r["teacher_id"]
                for r in conn.execute(
                    "SELECT teacher_id FROM enrollments WHERE org_id = ?", (principal.org_id,)
                )
            }
            if teacher_id:
                if teacher_id not in ids:
                    raise PermissionDenied("该教师不属于你的机构")
                sql += " WHERE teacher_id = ?"
                params = (teacher_id,)
            else:
                if not ids:
                    return []
                sql += f" WHERE teacher_id IN ({','.join('?' * len(ids))})"
                params = tuple(sorted(ids))
        elif principal.role is Role.TEACHER:
            sql += " WHERE teacher_id = ?"
            params = (principal.user_id,)
        else:
            # 签发方：按证据所属签发机构过滤（事件携带 org_id）
            sql += " WHERE org_id = ? OR (org_id IS NULL AND evidence_id IN " \
                   "(SELECT evidence_id FROM evidences WHERE issuer_id = ?))"
            params = (principal.org_id, principal.org_id)
        rows = conn.execute(sql + " ORDER BY seq", params).fetchall()
        return [
            {
                "seq": r["seq"],
                "event_id": r["event_id"],
                "event_type": r["event_type"],
                "occurred_at": r["occurred_at"],
                "actor_id": r["actor_id"],
                "teacher_id": r["teacher_id"],
                "evidence_id": r["evidence_id"],
                "org_id": r["org_id"],
                "payload": json.loads(r["payload"]),
            }
            for r in rows
        ]

    # ======================================================================
    # 内部辅助
    # ======================================================================

    @staticmethod
    def _require(principal: Principal, role: Role) -> None:
        if principal.role is not role:
            raise PermissionDenied(f"该操作仅 {role.value} 可执行")

    @staticmethod
    def _require_org_kind(conn, org_id: str, kind: str) -> None:
        row = conn.execute(
            "SELECT kind FROM organizations WHERE org_id = ?", (org_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"机构不存在：{org_id}")
        if row["kind"] != kind:
            raise RuleError(f"机构 {org_id} 不是{kind}")

    @staticmethod
    def _require_unit(conn, unit_code: str) -> None:
        if not conn.execute(
            "SELECT 1 FROM training_units WHERE unit_code = ?", (unit_code,)
        ).fetchone():
            raise NotFound(f"培训单元不存在：{unit_code}")

    @staticmethod
    def _require_goal(conn, rule_set: str, version: int, goal_code: str) -> None:
        if not conn.execute(
            "SELECT 1 FROM goals WHERE rule_set = ? AND version = ? AND goal_code = ?",
            (rule_set, version, goal_code),
        ).fetchone():
            raise NotFound(f"能力目标不存在：{goal_code}")

    @staticmethod
    def _get_evidence(conn, evidence_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM evidences WHERE evidence_id = ?", (evidence_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"证据不存在：{evidence_id}")
        return row

    @staticmethod
    def _get_enrollment(conn, teacher_id: str) -> sqlite3.Row:
        row = conn.execute(
            "SELECT * FROM enrollments WHERE teacher_id = ?", (teacher_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"教师尚未建立资格档案：{teacher_id}")
        return row

    def _assert_teacher_in_org(self, teacher_id: str, org_id: str | None) -> None:
        row = self.db.conn.execute(
            "SELECT org_id FROM enrollments WHERE teacher_id = ?", (teacher_id,)
        ).fetchone()
        if row is None:
            raise NotFound(f"教师尚未建立资格档案：{teacher_id}")
        if row["org_id"] != org_id:
            raise PermissionDenied("机构间资料隔离：该教师不属于你的机构")

    def _assert_can_read_teacher(self, principal: Principal, teacher_id: str) -> None:
        if principal.role is Role.TEACHER:
            if principal.user_id != teacher_id:
                raise PermissionDenied("教师只能查看本人的证据组合")
        elif principal.role is Role.ORG_REVIEWER:
            self._assert_teacher_in_org(teacher_id, principal.org_id)
        elif principal.role is Role.ADMIN:
            return
        else:
            raise PermissionDenied("该角色不能查看教师证据组合")

    @staticmethod
    def _require_issuer_access(principal: Principal, ev: sqlite3.Row) -> None:
        if principal.role is not Role.ISSUER_STAFF:
            raise PermissionDenied("只有签发方管理员可以验证/驳回/撤销证明")
        if principal.org_id != ev["issuer_id"]:
            raise PermissionDenied("机构间资料隔离：证明由其他签发方签发")

    @staticmethod
    def _evidence_public(r: sqlite3.Row) -> dict:
        return {
            "evidence_id": r["evidence_id"],
            "unit_code": r["unit_code"],
            "unit_name": r["unit_name"],
            "issuer_id": r["issuer_id"],
            "hours": r["hours"],
            "issued_on": r["issued_on"],
            "external_ref": r["external_ref"],
            "status": r["status"],
            "duplicate_of": r["duplicate_of"],
            "submitted_at": r["submitted_at"],
            "verified_at": r["verified_at"],
            "rejected_at": r["rejected_at"],
            "revoked_at": r["revoked_at"],
        }
