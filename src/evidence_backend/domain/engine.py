"""资格判定引擎（确定性、纯函数）。

引擎不读取数据库、不产生副作用：输入规则集版本、培训单元目录与教师证据清单，
输出可解释的判定结果——每个能力目标的覆盖情况、缺口，以及每份证据
对最终结论的实际贡献（直接证据 / 替代路径 / 替代比例与上限 / 未被计入的原因）。
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any

from .aggregates import (
    ST_ACCEPTED,
    ST_DUPLICATE,
    ST_REJECTED,
    ST_REVOKED,
    ST_SUBMITTED,
)

QUALIFIED = "QUALIFIED"
NOT_QUALIFIED = "NOT_QUALIFIED"

PATH_DIRECT = "DIRECT"
PATH_SUBSTITUTION = "SUBSTITUTION"

ROLE_NEEDED = "NEEDED"      # 该证据的学时是满足目标所必需的
ROLE_SURPLUS = "SURPLUS"    # 目标已满足，该证据的学时属于富余
ROLE_NONE = "NONE"          # 未计入

# 证据非计入状态的解释原因。
EXCLUSION_REASONS = {
    ST_SUBMITTED: "证据尚在验证中，未通过签发方核验",
    ST_DUPLICATE: "重复提交，已关联到先到证据，不重复计学时",
    ST_REJECTED: "验证未通过，证据被拒绝",
    ST_REVOKED: "证明已被签发方撤销，不再作为判定依据",
}


def evaluate(
    *,
    ruleset: dict[str, Any],
    units: dict[str, dict[str, Any]],
    evidences: list[dict[str, Any]],
) -> dict[str, Any]:
    """执行一次完整判定。

    ``ruleset`` 需含 ``goals`` 与可选 ``substitutions``；
    ``units`` 为 unit_id -> 培训单元登记信息；
    ``evidences`` 为教师名下证据的当前快照。
    """
    goals = ruleset["goals"]
    substitutions = ruleset.get("substitutions", [])

    # unit_id -> 指向某能力目标的替代规则（同一单元对同一目标只允许一条规则）。
    sub_index: dict[tuple[str, str], dict[str, Any]] = {}
    for rule in substitutions:
        sub_index[(rule["unit_id"], rule["goal_id"])] = rule

    # 每个替代规则在本次判定内已经计入的学时（用于 max_hours 上限分摊）。
    rule_used: dict[str, float] = defaultdict(float)

    goal_results: list[dict[str, Any]] = []
    # evidence_id -> 跨目标的贡献明细
    per_evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
    evidence_basis: dict[str, dict[str, Any]] = {}

    for ev in evidences:
        evidence_basis[ev["evidence_id"]] = {
            "evidence_id": ev["evidence_id"],
            "unit_id": ev["unit_id"],
            "issuer_id": ev["issuer_id"],
            "hours": ev["hours"],
            "status": ev["status"],
            "counted": False,
            "exclusion_reason": EXCLUSION_REASONS.get(ev["status"], "未知状态"),
        }

    for goal in goals:
        goal_id = goal["goal_id"]
        required_hours = float(goal["required_hours"])
        required_evidence = int(goal.get("required_evidence_count", 1))

        # 收集候选贡献：直接证据优先，替代证据按规则与证据编号排序，保证确定性。
        candidates: list[dict[str, Any]] = []
        for ev in sorted(evidences, key=lambda e: e["evidence_id"]):
            if ev["status"] != ST_ACCEPTED:
                continue
            unit = units.get(ev["unit_id"])
            if unit is None:
                continue
            if unit["goal_id"] == goal_id:
                candidates.append(
                    _candidate(ev, path=PATH_DIRECT, ratio=1.0, rule_id=None)
                )
            elif (ev["unit_id"], goal_id) in sub_index:
                rule = sub_index[(ev["unit_id"], goal_id)]
                candidates.append(
                    _candidate(
                        ev,
                        path=PATH_SUBSTITUTION,
                        ratio=float(rule.get("ratio", 1.0)),
                        rule_id=rule["substitution_id"],
                        max_hours=rule.get("max_hours"),
                    )
                )

        candidates.sort(key=lambda c: (0 if c["path"] == PATH_DIRECT else 1, c["evidence_id"]))

        covered = 0.0
        contributions: list[dict[str, Any]] = []
        for cand in candidates:
            counted = cand["raw_hours"] * cand["ratio"]
            if cand["rule_id"] is not None and cand["max_hours"] is not None:
                cap_left = max(0.0, float(cand["max_hours"]) - rule_used[cand["rule_id"]])
                counted = min(counted, cap_left)
            counted = round(counted, 4)
            rule_used[cand["rule_id"]] += counted  # type: ignore[index]

            # 计入时目标在学时或证据份数上仍未满足，则该证据是“必需”证据，
            # 否则只是锦上添花的富余学时。
            needed_so_far = sum(1 for c in contributions if c["necessity"] == ROLE_NEEDED)
            still_short = covered + 1e-9 < required_hours or needed_so_far < required_evidence
            necessity = ROLE_NEEDED if still_short and counted > 0 else ROLE_SURPLUS
            covered = round(covered + counted, 4)

            detail = {
                "evidence_id": cand["evidence_id"],
                "unit_id": cand["unit_id"],
                "path": cand["path"],
                "substitution_id": cand["rule_id"],
                "ratio": cand["ratio"],
                "raw_hours": cand["raw_hours"],
                "counted_hours": counted,
                "necessity": necessity if counted > 0 else ROLE_NONE,
            }
            contributions.append(detail)
            per_evidence[cand["evidence_id"]].append(
                {"goal_id": goal_id, **detail}
            )
            if counted > 0:
                evidence_basis[cand["evidence_id"]]["counted"] = True
                evidence_basis[cand["evidence_id"]]["exclusion_reason"] = ""

        # 需求判定：以“计入学时的必需证据份数”为准。
        needed_count = sum(1 for c in contributions if c["necessity"] == ROLE_NEEDED)
        gap = round(max(0.0, required_hours - covered), 4)
        hours_ok = covered + 1e-9 >= required_hours
        count_ok = needed_count >= required_evidence
        satisfied = hours_ok and count_ok

        reasons: list[str] = []
        if not hours_ok:
            reasons.append(f"能力目标尚缺 {gap:g} 学时的有效证据")
        if not count_ok:
            reasons.append(f"至少需要 {required_evidence} 份直接或替代证据覆盖该目标")

        goal_results.append(
            {
                "goal_id": goal_id,
                "title": goal.get("title", goal_id),
                "required_hours": required_hours,
                "required_evidence_count": required_evidence,
                "covered_hours": round(covered, 4),
                "gap_hours": gap,
                "satisfied": satisfied,
                "contributions": contributions,
                "unsatisfied_reasons": reasons,
            }
        )

    all_satisfied = bool(goal_results) and all(g["satisfied"] for g in goal_results)
    result = QUALIFIED if all_satisfied else NOT_QUALIFIED
    gaps = [
        {
            "goal_id": g["goal_id"],
            "title": g["title"],
            "gap_hours": g["gap_hours"],
            "reasons": g["unsatisfied_reasons"],
        }
        for g in goal_results
        if not g["satisfied"]
    ]

    contributions_view = [
        {"evidence_id": ev_id, "items": items}
        for ev_id, items in sorted(per_evidence.items())
    ]

    return {
        "result": result,
        "goals": goal_results,
        "gaps": gaps,
        "contributions": contributions_view,
        "evidence_basis": [evidence_basis[k] for k in sorted(evidence_basis)],
        "summary_text": _summarize(result, goal_results),
    }


def _candidate(
    ev: dict[str, Any], *, path: str, ratio: float, rule_id: str | None,
    max_hours: float | None = None,
) -> dict[str, Any]:
    return {
        "evidence_id": ev["evidence_id"],
        "unit_id": ev["unit_id"],
        "path": path,
        "ratio": ratio,
        "rule_id": rule_id,
        "max_hours": max_hours,
        "raw_hours": float(ev["hours"]),
    }


def _summarize(result: str, goals: list[dict[str, Any]]) -> str:
    if result == QUALIFIED:
        covered = "、".join(g["title"] for g in goals)
        return f"全部能力目标（{covered}）均被有效证据覆盖，资格判定通过。"
    parts = []
    for g in goals:
        if not g["satisfied"]:
            parts.append(
                f"{g['title']}已覆盖 {g['covered_hours']:g}/{g['required_hours']:g} 学时"
                + ("；" + "，".join(g["unsatisfied_reasons"]) if g["unsatisfied_reasons"] else "")
            )
    return "资格暂不通过：" + "；".join(parts) + "。"
