"""应用服务层：登记、证据流转、判定、申诉与权限隔离查询。"""
from __future__ import annotations

import re
import uuid
from datetime import date
from typing import Any

from .domain import aggregates as agg
from .domain.engine import evaluate
from .errors import Conflict, NotFound, PermissionDenied, ValidationError
from .event_store import EventStore, iso, utcnow
from .identity import Principal
from .repository import AggregateRepo

AG_ISSUER = "issuer"
AG_UNIT = "training_unit"
AG_GOAL = "goal"
AG_RULESET = "ruleset"
AG_TEACHER = "teacher"
AG_EVIDENCE = "evidence"
AG_EVALUATION = "evaluation"
AG_APPEAL = "appeal"

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
CATEGORIES = {"线上研修", "企业实践", "联合教研"}


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


def _require_date(value: str, field: str) -> str:
    if not isinstance(value, str) or not _DATE_RE.match(value):
        raise ValidationError(f"{field} 必须是 YYYY-MM-DD 日期")
    try:
        date.fromisoformat(value)
    except ValueError as exc:
        raise ValidationError(f"{field} 不是合法日期") from exc
    return value


def _require_positive_number(value: Any, field: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0:
        raise ValidationError(f"{field} 必须是正数")
    return float(value)


class RegistryService:
    """引导数据登记：签发方、能力目标、培训单元、规则版本、教师。"""

    def __init__(self, store: EventStore, repo: AggregateRepo) -> None:
        self.store = store
        self.repo = repo

    # ---- 签发方 ---------------------------------------------------------

    def register_issuer(
        self, p: Principal, *, name: str, kind: str, org_id: str, contact: str = ""
    ) -> dict[str, Any]:
        p.require_reviewer()
        if not name or not org_id:
            raise ValidationError("签发方名称与所属机构不能为空")
        issuer_id = _new_id("iss")
        with self.store.connection:
            state = self.repo.append(
                AG_ISSUER, issuer_id, "IssuerRegistered",
                {
                    "issuer_id": issuer_id,
                    "name": name,
                    "kind": kind,
                    "org_id": org_id,
                    "contact": contact,
                },
                p,
            ).state
        return state

    def change_issuer_status(
        self, p: Principal, issuer_id: str, status: str
    ) -> dict[str, Any]:
        p.require_reviewer()
        if status not in {"TRUSTED", "SUSPENDED"}:
            raise ValidationError("签发方状态只能是 TRUSTED / SUSPENDED")
        issuer = self.repo.require(AG_ISSUER, issuer_id)
        if issuer.state["status"] == status:
            raise Conflict("签发方已处于该状态")
        with self.store.connection:
            issuer = self.repo.append(
                AG_ISSUER, issuer_id, "IssuerStatusChanged",
                {"issuer_id": issuer_id, "status": status}, p,
                expected_version=issuer.version,
            )
        return issuer.state

    # ---- 能力目标 -------------------------------------------------------

    def register_goal(
        self, p: Principal, *, goal_id: str, title: str, description: str = ""
    ) -> dict[str, Any]:
        p.require_reviewer()
        if not goal_id or not title:
            raise ValidationError("能力目标编号与名称不能为空")
        if self.repo.exists(AG_GOAL, goal_id):
            raise Conflict(f"能力目标已登记：{goal_id}")
        with self.store.connection:
            state = self.repo.append(
                AG_GOAL, goal_id, "GoalRegistered",
                {"goal_id": goal_id, "title": title, "description": description}, p,
            ).state
        return state

    # ---- 培训单元 -------------------------------------------------------

    def register_training_unit(
        self,
        p: Principal,
        *,
        code: str,
        title: str,
        category: str,
        goal_id: str,
        default_hours: float,
    ) -> dict[str, Any]:
        p.require_reviewer()
        if not code or not title:
            raise ValidationError("单元编码与名称不能为空")
        if category not in CATEGORIES:
            raise ValidationError(f"培训类别必须是：{'、'.join(sorted(CATEGORIES))}")
        if not self.repo.exists(AG_GOAL, goal_id):
            raise ValidationError(f"能力目标未登记：{goal_id}")
        default_hours = _require_positive_number(default_hours, "默认学时")
        for unit in self.repo.list_snapshots(AG_UNIT):
            if unit["code"] == code:
                raise Conflict(f"单元编码已存在：{code}")
        unit_id = _new_id("unit")
        with self.store.connection:
            state = self.repo.append(
                AG_UNIT, unit_id, "TrainingUnitRegistered",
                {
                    "unit_id": unit_id,
                    "code": code,
                    "title": title,
                    "category": category,
                    "goal_id": goal_id,
                    "default_hours": default_hours,
                },
                p,
            ).state
        return state

    def retire_training_unit(self, p: Principal, unit_id: str) -> dict[str, Any]:
        p.require_reviewer()
        unit = self.repo.require(AG_UNIT, unit_id)
        if not unit.state["active"]:
            raise Conflict("培训单元已停用")
        with self.store.connection:
            unit = self.repo.append(
                AG_UNIT, unit_id, "TrainingUnitRetired",
                {"unit_id": unit_id}, p, expected_version=unit.version,
            )
        return unit.state

    # ---- 规则版本 -------------------------------------------------------

    def register_ruleset(
        self,
        p: Principal,
        *,
        name: str,
        version: str,
        goals: list[dict[str, Any]],
        substitutions: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        p.require_reviewer()
        if not name or not version:
            raise ValidationError("规则集名称与版本不能为空")
        self._validate_ruleset(goals or [], substitutions or [])
        for rs in self.repo.list_snapshots(AG_RULESET):
            if rs["name"] == name and rs["version"] == version:
                raise Conflict(f"规则版本已存在：{name}@{version}")
        ruleset_id = _new_id("rs")
        with self.store.connection:
            state = self.repo.append(
                AG_RULESET, ruleset_id, "RuleSetRegistered",
                {
                    "ruleset_id": ruleset_id,
                    "name": name,
                    "version": version,
                    "goals": goals,
                    "substitutions": substitutions or [],
                },
                p,
            ).state
        return state

    def _validate_ruleset(
        self, goals: list[dict[str, Any]], substitutions: list[dict[str, Any]]
    ) -> None:
        if not goals:
            raise ValidationError("规则集至少包含一个能力目标")
        goal_ids: set[str] = set()
        for item in goals:
            gid = item.get("goal_id")
            if not gid:
                raise ValidationError("能力目标缺少 goal_id")
            if not self.repo.exists(AG_GOAL, gid):
                raise ValidationError(f"规则引用了未登记的能力目标：{gid}")
            if gid in goal_ids:
                raise ValidationError(f"规则集中能力目标重复：{gid}")
            goal_ids.add(gid)
            _require_positive_number(item.get("required_hours"), f"目标 {gid} 的 required_hours")
            count = item.get("required_evidence_count", 1)
            if not isinstance(count, int) or isinstance(count, bool) or count < 1:
                raise ValidationError(f"目标 {gid} 的 required_evidence_count 必须是 >=1 的整数")
        seen_pairs: set[tuple[str, str]] = set()
        for rule in substitutions:
            uid = rule.get("unit_id")
            gid = rule.get("goal_id")
            sid = rule.get("substitution_id") or _new_id("sub")
            rule["substitution_id"] = sid
            unit = self.repo.load(AG_UNIT, uid)
            if not unit.exists:
                raise ValidationError(f"替代规则引用了未登记的培训单元：{uid}")
            if gid not in goal_ids:
                raise ValidationError(f"替代规则目标 {gid} 不在规则集目标内")
            if unit.state["goal_id"] == gid:
                raise ValidationError("培训单元直接覆盖该目标，无需配置替代关系")
            pair = (uid, gid)
            if pair in seen_pairs:
                raise ValidationError(f"同一单元对同一目标只能有一条替代规则：{uid}->{gid}")
            seen_pairs.add(pair)
            ratio = rule.get("ratio", 1.0)
            if not isinstance(ratio, (int, float)) or isinstance(ratio, bool) or not 0 < ratio <= 1:
                raise ValidationError(f"替代规则 {sid} 的 ratio 必须在 (0, 1] 区间")
            if "max_hours" in rule and rule["max_hours"] is not None:
                _require_positive_number(rule["max_hours"], f"替代规则 {sid} 的 max_hours")

    def publish_ruleset(self, p: Principal, ruleset_id: str, effective_from: str | None = None) -> dict[str, Any]:
        p.require_reviewer()
        rs = self.repo.require(AG_RULESET, ruleset_id)
        if rs.state["status"] != agg.RULESET_DRAFT:
            raise Conflict("只有 DRAFT 状态的规则集可以发布")
        effective_from = effective_from or date.today().isoformat()
        _require_date(effective_from, "effective_from")
        with self.store.connection:
            rs = self.repo.append(
                AG_RULESET, ruleset_id, "RuleSetPublished",
                {"ruleset_id": ruleset_id, "effective_from": effective_from},
                p, expected_version=rs.version,
            )
        return rs.state

    def deprecate_ruleset(self, p: Principal, ruleset_id: str) -> dict[str, Any]:
        p.require_reviewer()
        rs = self.repo.require(AG_RULESET, ruleset_id)
        if rs.state["status"] != agg.RULESET_PUBLISHED:
            raise Conflict("只有已发布的规则集可以废止")
        with self.store.connection:
            rs = self.repo.append(
                AG_RULESET, ruleset_id, "RuleSetDeprecated",
                {"ruleset_id": ruleset_id}, p, expected_version=rs.version,
            )
        return rs.state

    def applicable_ruleset(self, as_of: str | None = None) -> dict[str, Any]:
        as_of = as_of or date.today().isoformat()
        candidates = [
            rs
            for rs in self.repo.list_snapshots(AG_RULESET)
            if rs["status"] == agg.RULESET_PUBLISHED and rs["effective_from"] <= as_of
        ]
        if not candidates:
            raise NotFound("当前日期没有已发布且生效的规则版本")
        return sorted(candidates, key=lambda r: (r["effective_from"], r["ruleset_id"]))[-1]

    # ---- 教师 -----------------------------------------------------------

    def register_teacher(self, p: Principal, *, teacher_id: str, name: str, org_id: str = "") -> dict[str, Any]:
        p.require_reviewer()
        if not teacher_id or not name:
            raise ValidationError("教师工号与姓名不能为空")
        if self.repo.exists(AG_TEACHER, teacher_id):
            raise Conflict(f"教师已登记：{teacher_id}")
        with self.store.connection:
            state = self.repo.append(
                AG_TEACHER, teacher_id, "TeacherRegistered",
                {"teacher_id": teacher_id, "name": name, "org_id": org_id}, p,
            ).state
        return state


class EvidenceService:
    """证据提交、验证、撤销与撤销后的再判定。"""

    def __init__(self, store: EventStore, repo: AggregateRepo, registries: RegistryService) -> None:
        self.store = store
        self.repo = repo
        self.registries = registries

    def submit(
        self,
        p: Principal,
        *,
        teacher_id: str,
        unit_id: str,
        issuer_id: str,
        hours: float,
        issued_on: str,
        external_ref: str = "",
    ) -> dict[str, Any]:
        p.require_role(agg.ROLE_TEACHER)
        if p.actor_id != teacher_id:
            raise PermissionDenied("教师只能提交本人的培训证明")
        teacher = self.repo.require(AG_TEACHER, teacher_id)
        unit = self.repo.require(AG_UNIT, unit_id)
        if not unit.state["active"]:
            raise ValidationError("该培训单元已停用，不再接收新证明")
        issuer = self.repo.require(AG_ISSUER, issuer_id)
        if issuer.state["status"] != "TRUSTED":
            raise ValidationError("签发方未处于受信状态，其证明暂不受理")
        hours = _require_positive_number(hours, "学时")
        _require_date(issued_on, "颁发日期")

        duplicate_of = self._find_duplicate(teacher_id, unit_id, issuer_id, issued_on, hours, external_ref)
        evidence_id = _new_id("ev")
        submitted = {
            "evidence_id": evidence_id,
            "teacher_id": teacher_id,
            "unit_id": unit_id,
            "issuer_id": issuer_id,
            "hours": hours,
            "issued_on": issued_on,
            "external_ref": external_ref,
        }
        with self.store.connection:
            ev = self.repo.append(AG_EVIDENCE, evidence_id, agg.EV_SUBMITTED, submitted, p)
            if duplicate_of:
                ev = self.repo.append(
                    AG_EVIDENCE, evidence_id, agg.EV_DUPLICATE_LINKED,
                    {
                        "evidence_id": evidence_id,
                        "duplicate_of_evidence_id": duplicate_of,
                        "note": "与在先证明的签发方、单元、颁发信息一致",
                    },
                    p, expected_version=ev.version,
                )
        return ev.state

    def _find_duplicate(
        self, teacher_id: str, unit_id: str, issuer_id: str,
        issued_on: str, hours: float, external_ref: str,
    ) -> str | None:
        live = {agg.ST_SUBMITTED, agg.ST_ACCEPTED}
        earliest: tuple[str, str] | None = None  # (issued_on, evidence_id)
        for ev in self.repo.list_snapshots(AG_EVIDENCE):
            if ev.get("teacher_id") != teacher_id or ev["status"] not in live:
                continue
            if ev["unit_id"] != unit_id or ev["issuer_id"] != issuer_id:
                continue
            if external_ref and ev.get("external_ref") == external_ref:
                return ev["evidence_id"]  # 同一外部凭证号：确定性命中
            if (
                not external_ref
                and ev["issued_on"] == issued_on
                and float(ev["hours"]) == float(hours)
            ):
                cand = (ev["issued_on"], ev["evidence_id"])
                earliest = cand if earliest is None else min(earliest, cand)
        return earliest[1] if earliest else None

    def _can_verify(self, p: Principal, evidence: dict[str, Any]) -> None:
        if p.role in agg.REVIEWER_ROLES:
            return
        if p.role == agg.ROLE_ISSUER:
            issuer = self.repo.load(AG_ISSUER, evidence["issuer_id"])
            if issuer.exists and issuer.state["org_id"] == p.org_id:
                return
        raise PermissionDenied("只有资格审核员或证据签发机构可以验证该证明")

    def verify(
        self,
        p: Principal,
        evidence_id: str,
        *,
        decision: str,
        note: str = "",
        hours: float | None = None,
    ) -> dict[str, Any]:
        if decision not in {"ACCEPT", "REJECT"}:
            raise ValidationError("验证结论必须是 ACCEPT / REJECT")
        ev = self.repo.require(AG_EVIDENCE, evidence_id)
        self._can_verify(p, ev.state)
        if ev.state["status"] != agg.ST_SUBMITTED:
            raise Conflict("只有待验证的证明可以给出验证结论")
        if decision == "ACCEPT":
            if hours is not None:
                hours = _require_positive_number(hours, "核定时数")
            with self.store.connection:
                ev = self.repo.append(
                    AG_EVIDENCE, evidence_id, agg.EV_ACCEPTED,
                    {"evidence_id": evidence_id, "hours": hours, "note": note},
                    p, expected_version=ev.version,
                )
        else:
            if not note:
                raise ValidationError("拒绝证明必须填写理由")
            with self.store.connection:
                ev = self.repo.append(
                    AG_EVIDENCE, evidence_id, agg.EV_REJECTED,
                    {"evidence_id": evidence_id, "note": note},
                    p, expected_version=ev.version,
                )
        return ev.state

    def revoke(self, p: Principal, evidence_id: str, *, reason: str) -> dict[str, Any]:
        if not reason:
            raise ValidationError("撤销证明必须填写原因")
        ev = self.repo.require(AG_EVIDENCE, evidence_id)
        self._can_verify(p, ev.state)
        if ev.state["status"] != agg.ST_ACCEPTED:
            raise Conflict("只有已通过验证的证明可以被撤销")
        with self.store.connection:
            ev = self.repo.append(
                AG_EVIDENCE, evidence_id, agg.EV_REVOKED,
                {"evidence_id": evidence_id, "reason": reason},
                p, expected_version=ev.version,
            )
        return ev.state

    def reinstate(self, p: Principal, evidence_id: str, *, note: str = "") -> dict[str, Any]:
        p.require_reviewer()
        ev = self.repo.require(AG_EVIDENCE, evidence_id)
        if ev.state["status"] != agg.ST_REVOKED:
            raise Conflict("只有已撤销的证明可以恢复")
        with self.store.connection:
            ev = self.repo.append(
                AG_EVIDENCE, evidence_id, agg.EV_REINSTATED,
                {"evidence_id": evidence_id, "note": note},
                p, expected_version=ev.version,
            )
        return ev.state


class EvaluationService:
    """按规则版本形成可解释、可追溯的资格判定。"""

    def __init__(self, store: EventStore, repo: AggregateRepo, registries: RegistryService) -> None:
        self.store = store
        self.repo = repo
        self.registries = registries

    def _teacher_evidences(self, teacher_id: str) -> list[dict[str, Any]]:
        return [
            ev for ev in self.repo.list_snapshots(AG_EVIDENCE)
            if ev.get("teacher_id") == teacher_id
        ]

    def _units_index(self) -> dict[str, dict[str, Any]]:
        return {u["unit_id"]: u for u in self.repo.list_snapshots(AG_UNIT)}

    def current_evaluation(self, teacher_id: str) -> dict[str, Any] | None:
        for state in self.repo.list_snapshots(AG_EVALUATION):
            if state["teacher_id"] == teacher_id and state["status"] == "CURRENT":
                return state
        return None

    def evaluate(
        self,
        p: Principal,
        teacher_id: str,
        *,
        ruleset_id: str | None = None,
        trigger: str = agg.TRIGGER_INITIAL,
        previous_evaluation_id: str | None = None,
        reason: str = "",
    ) -> dict[str, Any]:
        p.require_reviewer()
        self.repo.require(AG_TEACHER, teacher_id)
        if ruleset_id is None:
            ruleset = self.registries.applicable_ruleset()
        else:
            ruleset = self.repo.require(AG_RULESET, ruleset_id).state
            if ruleset["status"] not in {agg.RULESET_PUBLISHED}:
                raise Conflict("判定只能依据已发布的规则版本")

        result = evaluate(
            ruleset=ruleset,
            units=self._units_index(),
            evidences=self._teacher_evidences(teacher_id),
        )
        evaluation_id = _new_id("eval")
        now = iso(utcnow())
        payload = {
            "evaluation_id": evaluation_id,
            "teacher_id": teacher_id,
            "ruleset_id": ruleset["ruleset_id"],
            "ruleset_version": f"{ruleset['name']}@{ruleset['version']}",
            "result": result["result"],
            "goals": result["goals"],
            "gaps": result["gaps"],
            "contributions": result["contributions"],
            "evidence_basis": result["evidence_basis"],
            "explanation": {
                "summary": result["summary_text"],
                "gaps": result["gaps"],
            },
            "trigger": trigger,
            "previous_evaluation_id": previous_evaluation_id,
            "reason": reason,
            "decided_by": p.actor_id,
            "decided_at": now,
        }
        with self.store.connection:
            previous = self.current_evaluation(teacher_id)
            if previous is not None:
                self.repo.append(
                    AG_EVALUATION, previous["evaluation_id"], "EvaluationSuperseded",
                    {
                        "by_evaluation_id": evaluation_id,
                        "reason": reason or f"被新一轮判定（{trigger}）替代",
                    },
                    p,
                )
            state = self.repo.append(
                AG_EVALUATION, evaluation_id, "EvaluationRecorded", payload, p
            ).state
        return state


class AppealService:
    """教师申诉与复核：全程只追加，复核可形成新判定。"""

    def __init__(
        self, store: EventStore, repo: AggregateRepo, evaluations: EvaluationService
    ) -> None:
        self.store = store
        self.repo = repo
        self.evaluations = evaluations

    def open_appeal(self, p: Principal, *, teacher_id: str, reason: str) -> dict[str, Any]:
        p.require_role(agg.ROLE_TEACHER)
        if p.actor_id != teacher_id:
            raise PermissionDenied("教师只能就本人的判定提出申诉")
        if not reason:
            raise ValidationError("申诉必须说明理由")
        current = self.evaluations.current_evaluation(teacher_id)
        if current is None:
            raise NotFound("该教师尚无判定结果，无法申诉")
        appeal_id = _new_id("ap")
        with self.store.connection:
            state = self.repo.append(
                AG_APPEAL, appeal_id, "AppealOpened",
                {
                    "appeal_id": appeal_id,
                    "evaluation_id": current["evaluation_id"],
                    "teacher_id": teacher_id,
                    "reason": reason,
                },
                p,
            ).state
        return state

    def review_appeal(
        self,
        p: Principal,
        appeal_id: str,
        *,
        decision: str,
        note: str,
        ruleset_id: str | None = None,
    ) -> dict[str, Any]:
        p.require_reviewer()
        if decision not in {agg.APPEAL_UPHELD, agg.APPEAL_OVERTURNED}:
            raise ValidationError("复核结论必须是 UPHELD / OVERTURNED")
        if not note:
            raise ValidationError("复核必须填写意见")
        appeal = self.repo.require(AG_APPEAL, appeal_id)
        if appeal.state["status"] != agg.APPEAL_OPEN:
            raise Conflict("该申诉已完成复核")

        review = {
            "appeal_id": appeal_id,
            "decision": decision,
            "note": note,
            "reviewed_by": p.actor_id,
            "reviewed_at": iso(utcnow()),
            "new_evaluation_id": None,
        }
        with self.store.connection:
            if decision == agg.APPEAL_OVERTURNED:
                current = self.evaluations.current_evaluation(appeal.state["teacher_id"])
                new_eval = self.evaluations.evaluate(
                    p,
                    appeal.state["teacher_id"],
                    ruleset_id=ruleset_id,
                    trigger=agg.TRIGGER_APPEAL,
                    previous_evaluation_id=current["evaluation_id"] if current else appeal.state["evaluation_id"],
                    reason=f"申诉 {appeal_id} 复核撤销原判定",
                )
                review["new_evaluation_id"] = new_eval["evaluation_id"]
            state = self.repo.append(
                AG_APPEAL, appeal_id, "AppealReviewed", review, p,
                expected_version=appeal.version,
            ).state
        return state


class QueryService:
    """面向前端的读模型；所有查询都带机构/角色隔离。"""

    def __init__(self, store: EventStore, repo: AggregateRepo) -> None:
        self.store = store
        self.repo = repo

    # ---- 权限判定 -------------------------------------------------------

    @staticmethod
    def _ensure_teacher_access(p: Principal, teacher_id: str) -> None:
        if p.role in agg.REVIEWER_ROLES:
            return
        if p.role == agg.ROLE_TEACHER and p.actor_id == teacher_id:
            return
        raise PermissionDenied("无权访问该教师的资料")

    def _issuer_org(self, issuer_id: str) -> str:
        issuer = self.repo.load(AG_ISSUER, issuer_id)
        return issuer.state["org_id"] if issuer.exists else ""

    def _evidence_visible(self, p: Principal, ev: dict[str, Any]) -> bool:
        if p.role in agg.REVIEWER_ROLES:
            return True
        if p.role == agg.ROLE_TEACHER:
            return ev["teacher_id"] == p.actor_id
        if p.role == agg.ROLE_ISSUER:
            return self._issuer_org(ev["issuer_id"]) == p.org_id
        return False

    # ---- 目录（对所有已认证角色可见）------------------------------------

    def catalog(self) -> dict[str, Any]:
        return {
            "goals": self.repo.list_snapshots(AG_GOAL),
            "training_units": self.repo.list_snapshots(AG_UNIT),
            "issuers": [
                {k: v for k, v in issuer.items() if k != "history"}
                for issuer in self.repo.list_snapshots(AG_ISSUER)
            ],
            "rulesets": [
                {
                    "ruleset_id": rs["ruleset_id"],
                    "name": rs["name"],
                    "version": rs["version"],
                    "status": rs["status"],
                    "effective_from": rs["effective_from"],
                    "goal_count": len(rs["goals"]),
                    "substitution_count": len(rs.get("substitutions", [])),
                }
                for rs in self.repo.list_snapshots(AG_RULESET)
            ],
        }

    # ---- 证据组合 -------------------------------------------------------

    def list_evidences(self, p: Principal, teacher_id: str | None = None) -> list[dict[str, Any]]:
        if p.role == agg.ROLE_TEACHER:
            teacher_id = p.actor_id
        result = []
        for ev in self.repo.list_snapshots(AG_EVIDENCE):
            if teacher_id and ev.get("teacher_id") != teacher_id:
                continue
            if self._evidence_visible(p, ev):
                result.append(self._enrich_evidence(ev))
        return result

    def get_evidence(self, p: Principal, evidence_id: str) -> dict[str, Any]:
        ev = self.repo.require(AG_EVIDENCE, evidence_id)
        if not self._evidence_visible(p, ev.state):
            raise PermissionDenied("无权访问该证明")
        return self._enrich_evidence(ev.state)

    def _enrich_evidence(self, ev: dict[str, Any]) -> dict[str, Any]:
        unit = self.repo.load(AG_UNIT, ev["unit_id"]).state
        issuer = self.repo.load(AG_ISSUER, ev["issuer_id"]).state
        out = dict(ev)
        out["unit"] = {"code": unit["code"], "title": unit["title"], "category": unit["category"]}
        out["issuer"] = {"name": issuer["name"], "kind": issuer["kind"], "org_id": issuer["org_id"]}
        return out

    def evidence_history(self, p: Principal, evidence_id: str) -> list[dict[str, Any]]:
        ev = self.repo.require(AG_EVIDENCE, evidence_id)
        if not self._evidence_visible(p, ev.state):
            raise PermissionDenied("无权访问该证明的历史")
        return self.store.events_for(AG_EVIDENCE, evidence_id)

    # ---- 教师档案与判定 -------------------------------------------------

    def portfolio(self, p: Principal, teacher_id: str) -> dict[str, Any]:
        self._ensure_teacher_access(p, teacher_id)
        teacher = self.repo.require(AG_TEACHER, teacher_id).state
        evidences = [
            self._enrich_evidence(ev)
            for ev in self.repo.list_snapshots(AG_EVIDENCE)
            if ev.get("teacher_id") == teacher_id
        ]
        current = self.current_evaluation(p, teacher_id)
        status_counts: dict[str, int] = {}
        for ev in evidences:
            status_counts[ev["status"]] = status_counts.get(ev["status"], 0) + 1
        return {
            "teacher": teacher,
            "evidence_count": len(evidences),
            "status_counts": status_counts,
            "evidences": evidences,
            "current_evaluation": current,
        }

    def current_evaluation(self, p: Principal, teacher_id: str) -> dict[str, Any] | None:
        self._ensure_teacher_access(p, teacher_id)
        for state in self.repo.list_snapshots(AG_EVALUATION):
            if state["teacher_id"] == teacher_id and state["status"] == "CURRENT":
                return state
        return None

    def evaluation_history(self, p: Principal, teacher_id: str) -> list[dict[str, Any]]:
        self._ensure_teacher_access(p, teacher_id)
        states = [
            state for state in self.repo.list_snapshots(AG_EVALUATION)
            if state["teacher_id"] == teacher_id
        ]
        return sorted(states, key=lambda s: s["decided_at"])

    def get_appeal(self, p: Principal, appeal_id: str) -> dict[str, Any]:
        appeal = self.repo.require(AG_APPEAL, appeal_id).state
        self._ensure_teacher_access(p, appeal["teacher_id"])
        return appeal

    def list_appeals(self, p: Principal, teacher_id: str | None = None) -> list[dict[str, Any]]:
        result = []
        for appeal in self.repo.list_snapshots(AG_APPEAL):
            if p.role == agg.ROLE_TEACHER and appeal["teacher_id"] != p.actor_id:
                continue
            if teacher_id and appeal["teacher_id"] != teacher_id:
                continue
            result.append(appeal)
        return result
