"""领域值对象、枚举与错误。

与领域契约 ``domain/contract.json`` 对应：

- 五个业务状态：提交 → 验证 → 组合 → 判定 → 申诉
- 四个不变量：证据组合、签发方验证、替代规则、资格解释
"""
from __future__ import annotations

import dataclasses
import enum
import re
from datetime import datetime, timezone

CASE_ID = "09303-019"
SCHEMA_VERSION = 1

# ---------------------------------------------------------------------------
# 角色（契约 actors）
# ---------------------------------------------------------------------------


class Role(str, enum.Enum):
    TEACHER = "参训教师"
    ORG_REVIEWER = "资格审核员"   # 教师所属培训机构的审核员
    ISSUER_STAFF = "签发方管理员"  # 证据签发方内部人员
    ADMIN = "平台管理员"


# ---------------------------------------------------------------------------
# 证据与判定状态（契约 states：提交/验证/组合/判定/申诉）
# ---------------------------------------------------------------------------


class EvidenceStatus(str, enum.Enum):
    SUBMITTED = "提交"        # 已登记，等待签发方验证
    VERIFIED = "验证"         # 签发方验证通过
    REVOKED = "撤销"          # 签发方撤销（记录保留，不物理删除）
    REJECTED = "验证不通过"    # 签发方核验否认
    DUPLICATE = "重复提交"    # 命中去重指纹，未进入组合


class Decision(str, enum.Enum):
    PENDING = "待判定"
    QUALIFIED = "合格"
    NOT_QUALIFIED = "不合格"


class AppealStatus(str, enum.Enum):
    OPEN = "申诉中"
    UPHELD = "申诉成立"      # 复核承认缺口覆盖 → 追加认定
    REJECTED = "申诉驳回"


# ---------------------------------------------------------------------------
# 能力贡献方式（资格解释不变量：每份证据的实际贡献必须可解释）
# ---------------------------------------------------------------------------


class CoverageKind(str, enum.Enum):
    DIRECT = "直接覆盖"
    SUBSTITUTION = "替代覆盖"
    APPEAL_GRANTED = "申诉认定"
    NOT_COUNTED = "未计入"


# ---------------------------------------------------------------------------
# 错误
# ---------------------------------------------------------------------------


class DomainError(Exception):
    """所有可预期业务错误的基类。"""

    code = "domain_error"

    def __init__(self, message: str, *, details: dict | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict:
        return {"code": self.code, "message": self.message, "details": self.details}


class NotFound(DomainError):
    code = "not_found"


class Conflict(DomainError):
    code = "conflict"


class PermissionDenied(DomainError):
    code = "permission_denied"


class RuleError(DomainError):
    code = "rule_error"


# ---------------------------------------------------------------------------
# 标识与时间
# ---------------------------------------------------------------------------

_SLUG_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")


def require_slug(value: str, field: str) -> str:
    if not isinstance(value, str) or not _SLUG_RE.match(value):
        raise RuleError(f"{field} 只能包含字母、数字、下划线和短横线，且以字母或数字开头")
    return value


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def parse_iso(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# 核心值对象
# ---------------------------------------------------------------------------


@dataclasses.dataclass(frozen=True)
class RuleRef:
    """一条规则的版本引用。"""

    rule_set: str
    version: int

    def __str__(self) -> str:  # pragma: no cover - 调试辅助
        return f"{self.rule_set}@v{self.version}"


@dataclasses.dataclass(frozen=True)
class CoverageContribution:
    """单份证据对单个能力目标的实际贡献（判定解释的最小单元）。"""

    evidence_id: str
    goal_code: str
    kind: CoverageKind
    ratio: float            # 计入学时相对目标要求学时的比例（截顶后）
    hours: float            # 实际计入的学时（截顶后）
    capped: bool            # 是否因替代封顶被截断
    note: str = ""


@dataclasses.dataclass(frozen=True)
class GoalCoverageResult:
    """单个能力目标的覆盖结论。"""

    goal_code: str
    goal_name: str
    required_hours: float
    direct_hours: float
    substitution_hours: float
    appeal_hours: float
    total_hours: float
    ratio: float                 # total / required
    satisfied: bool
    gap_hours: float
    contributions: tuple[CoverageContribution, ...]


@dataclasses.dataclass(frozen=True)
class EvidenceExplanation:
    """单份证据在本次判定中的完整去向（“实际贡献”解释）。"""

    evidence_id: str
    status: str
    unit_code: str
    unit_name: str
    issuer_id: str
    hours: float
    decision_points: tuple[str, ...]     # 参与覆盖的目标
    excluded_reason: str | None          # 未计入原因（撤销/重复/验证不通过/不映射任何目标）
    contributions: tuple[CoverageContribution, ...]


@dataclasses.dataclass(frozen=True)
class EvaluationResult:
    """一次资格判定的完整结果（含缺口与逐证据贡献）。"""

    teacher_id: str
    rule_set: str
    rule_version: int
    decided_at: str
    decision: Decision
    total_required_hours: float
    total_covered_hours: float
    goal_results: tuple[GoalCoverageResult, ...]
    evidence_explanations: tuple[EvidenceExplanation, ...]
    gaps: tuple[str, ...]
    summary: str

    def to_dict(self) -> dict:
        return {
            "teacher_id": self.teacher_id,
            "rule_set": self.rule_set,
            "rule_version": self.rule_version,
            "decided_at": self.decided_at,
            "decision": self.decision.value,
            "total_required_hours": self.total_required_hours,
            "total_covered_hours": self.total_covered_hours,
            "gaps": list(self.gaps),
            "summary": self.summary,
            "goals": [
                {
                    "goal_code": g.goal_code,
                    "goal_name": g.goal_name,
                    "required_hours": g.required_hours,
                    "direct_hours": round(g.direct_hours, 4),
                    "substitution_hours": round(g.substitution_hours, 4),
                    "appeal_hours": round(g.appeal_hours, 4),
                    "total_hours": round(g.total_hours, 4),
                    "ratio": round(g.ratio, 4),
                    "satisfied": g.satisfied,
                    "gap_hours": round(g.gap_hours, 4),
                    "contributions": [
                        {
                            "evidence_id": c.evidence_id,
                            "kind": c.kind.value,
                            "ratio": round(c.ratio, 4),
                            "hours": round(c.hours, 4),
                            "capped": c.capped,
                            "note": c.note,
                        }
                        for c in g.contributions
                    ],
                }
                for g in self.goal_results
            ],
            "evidence": [
                {
                    "evidence_id": e.evidence_id,
                    "status": e.status,
                    "unit_code": e.unit_code,
                    "unit_name": e.unit_name,
                    "issuer_id": e.issuer_id,
                    "hours": e.hours,
                    "decision_points": list(e.decision_points),
                    "excluded_reason": e.excluded_reason,
                    "contributions": [
                        {
                            "goal_code": c.goal_code,
                            "kind": c.kind.value,
                            "ratio": round(c.ratio, 4),
                            "hours": round(c.hours, 4),
                            "capped": c.capped,
                            "note": c.note,
                        }
                        for c in e.contributions
                    ],
                }
                for e in self.evidence_explanations
            ],
        }
