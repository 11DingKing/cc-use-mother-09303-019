"""领域常量与聚合状态回溯（reducer）。

所有聚合都通过 ``apply(state, event)`` 从事件流重建当前状态；
聚合自身不提供任何修改方法——状态变更只能由服务层追加新事件完成，
从而保证撤销、重复、部分替代、申诉复核的全过程可追溯。
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any

# ---- 角色 ----------------------------------------------------------------
ROLE_REVIEWER = "reviewer"   # 资格审核员
ROLE_ISSUER = "issuer"       # 培训机构（证据签发方）
ROLE_TEACHER = "teacher"     # 参训教师
ROLE_ADMIN = "admin"         # 引导数据管理员（具备审核员全部权限）

REVIEWER_ROLES = {ROLE_REVIEWER, ROLE_ADMIN}

# ---- 证据生命周期 --------------------------------------------------------
EV_SUBMITTED = "EvidenceSubmitted"
EV_DUPLICATE_LINKED = "EvidenceDuplicateLinked"
EV_ACCEPTED = "EvidenceAccepted"
EV_REJECTED = "EvidenceRejected"
EV_REVOKED = "EvidenceRevoked"
EV_REINSTATED = "EvidenceReinstated"

ST_SUBMITTED = "SUBMITTED"
ST_DUPLICATE = "DUPLICATE"
ST_ACCEPTED = "ACCEPTED"
ST_REJECTED = "REJECTED"
ST_REVOKED = "REVOKED"

# 允许的状态迁移：刻意保持稀少，任何迁移都以事件留痕。
EVIDENCE_TRANSITIONS: dict[str, set[str]] = {
    ST_SUBMITTED: {ST_DUPLICATE, ST_ACCEPTED, ST_REJECTED},
    ST_ACCEPTED: {ST_REVOKED},
    ST_REVOKED: {ST_ACCEPTED},
    ST_DUPLICATE: set(),
    ST_REJECTED: set(),
}

# ---- 规则集状态 ----------------------------------------------------------
RULESET_DRAFT = "DRAFT"
RULESET_PUBLISHED = "PUBLISHED"
RULESET_DEPRECATED = "DEPRECATED"

# ---- 申诉状态 ------------------------------------------------------------
APPEAL_OPEN = "OPEN"
APPEAL_UPHELD = "UPHELD"        # 维持原判定
APPEAL_OVERTURNED = "OVERTURNED"  # 撤销原判定并形成新判定

# ---- 判定触发来源 --------------------------------------------------------
TRIGGER_INITIAL = "INITIAL"
TRIGGER_APPEAL = "APPEAL_REVIEW"
TRIGGER_REVOCATION = "POST_REVOCATION"


class Aggregate:
    """从事件流惰性重建的聚合基类。"""

    aggregate_type: str = ""

    def __init__(self, aggregate_id: str, events: list[dict[str, Any]]) -> None:
        self.id = aggregate_id
        self.version = 0
        self.state: dict[str, Any] = {}
        for event in events:
            self.when(event)

    def when(self, event: dict[str, Any]) -> None:
        self.state = apply(self.state, event["event_type"], event["data"])
        self.version = event["version"]

    @property
    def exists(self) -> bool:
        return bool(self.state)


def apply(state: dict[str, Any] | None, event_type: str, data: dict[str, Any]) -> dict[str, Any]:
    """纯函数式 reducer：返回应用事件后的新状态。"""
    state = deepcopy(state) if state else {}

    if event_type == "IssuerRegistered":
        state.update(
            issuer_id=data["issuer_id"],
            name=data["name"],
            kind=data["kind"],
            org_id=data["org_id"],
            contact=data.get("contact", ""),
            status="TRUSTED",
        )
    elif event_type == "IssuerStatusChanged":
        state["status"] = data["status"]

    elif event_type == "GoalRegistered":
        state.update(
            goal_id=data["goal_id"],
            title=data["title"],
            description=data.get("description", ""),
        )

    elif event_type == "TrainingUnitRegistered":
        state.update(
            unit_id=data["unit_id"],
            code=data["code"],
            title=data["title"],
            category=data["category"],
            goal_id=data["goal_id"],
            default_hours=data["default_hours"],
            active=True,
        )
    elif event_type == "TrainingUnitRetired":
        state["active"] = False

    elif event_type == "RuleSetRegistered":
        state.update(
            ruleset_id=data["ruleset_id"],
            name=data["name"],
            version=data["version"],
            goals=deepcopy(data["goals"]),
            substitutions=deepcopy(data.get("substitutions", [])),
            status=RULESET_DRAFT,
            effective_from=None,
        )
    elif event_type == "RuleSetPublished":
        state["status"] = RULESET_PUBLISHED
        state["effective_from"] = data["effective_from"]
    elif event_type == "RuleSetDeprecated":
        state["status"] = RULESET_DEPRECATED

    elif event_type == "TeacherRegistered":
        state.update(
            teacher_id=data["teacher_id"], name=data["name"], org_id=data.get("org_id", "")
        )

    elif event_type == EV_SUBMITTED:
        state.update(
            evidence_id=data["evidence_id"],
            teacher_id=data["teacher_id"],
            unit_id=data["unit_id"],
            issuer_id=data["issuer_id"],
            hours=data["hours"],
            issued_on=data["issued_on"],
            external_ref=data.get("external_ref", ""),
            status=ST_SUBMITTED,
            history=[],
        )
    elif event_type == EV_DUPLICATE_LINKED:
        state["status"] = ST_DUPLICATE
        state["duplicate_of"] = data["duplicate_of_evidence_id"]
        state["history"].append({"event": EV_DUPLICATE_LINKED, **data})
    elif event_type == EV_ACCEPTED:
        state["status"] = ST_ACCEPTED
        if data.get("hours") is not None:
            state["hours"] = data["hours"]
        state["history"].append({"event": EV_ACCEPTED, **data})
    elif event_type == EV_REJECTED:
        state["status"] = ST_REJECTED
        state["history"].append({"event": EV_REJECTED, **data})
    elif event_type == EV_REVOKED:
        state["status"] = ST_REVOKED
        state["history"].append({"event": EV_REVOKED, **data})
    elif event_type == EV_REINSTATED:
        state["status"] = ST_ACCEPTED
        state["history"].append({"event": EV_REINSTATED, **data})

    elif event_type == "EvaluationRecorded":
        state.update(
            evaluation_id=data["evaluation_id"],
            teacher_id=data["teacher_id"],
            ruleset_id=data["ruleset_id"],
            ruleset_version=data["ruleset_version"],
            result=data["result"],
            goals=deepcopy(data["goals"]),
            gaps=deepcopy(data.get("gaps", [])),
            contributions=deepcopy(data["contributions"]),
            evidence_basis=deepcopy(data["evidence_basis"]),
            explanation=deepcopy(data["explanation"]),
            trigger=data.get("trigger", TRIGGER_INITIAL),
            previous_evaluation_id=data.get("previous_evaluation_id"),
            decided_by=data["decided_by"],
            decided_at=data["decided_at"],
            status="CURRENT",
        )
    elif event_type == "EvaluationSuperseded":
        state["status"] = "SUPERSEDED"
        state["superseded_by"] = data["by_evaluation_id"]
        state["superseded_reason"] = data.get("reason", "")

    elif event_type == "AppealOpened":
        state.update(
            appeal_id=data["appeal_id"],
            evaluation_id=data["evaluation_id"],
            teacher_id=data["teacher_id"],
            reason=data["reason"],
            status=APPEAL_OPEN,
            reviews=[],
        )
    elif event_type == "AppealReviewed":
        state["status"] = data["decision"]
        state["reviews"].append(deepcopy(data))

    else:  # pragma: no cover - 未知事件属于编程错误
        raise ValueError(f"未知事件类型：{event_type}")

    return state
