"""资格判定引擎（纯函数）。

引擎不读写数据库，输入全部由服务层从“已发布且冻结”的规则版本与
只追加事件日志中重建，输出每个能力目标的缺口以及每份证据的实际贡献。

判定不使用“累计学时达标即合格”：必须逐目标核算直接覆盖、替代覆盖
（支持比例折算与封顶截断，即部分替代）和申诉认定。
"""
from __future__ import annotations

from collections import defaultdict
from dataclasses import replace as dc_replace

from .domain import (
    CoverageContribution,
    CoverageKind,
    Decision,
    EvaluationResult,
    EvidenceExplanation,
    GoalCoverageResult,
    iso,
    utcnow,
)

_EPS = 1e-9


class _GoalAccum:
    def __init__(self, goal_code: str, goal_name: str, required_hours: float) -> None:
        self.goal_code = goal_code
        self.goal_name = goal_name
        self.required_hours = required_hours
        self.direct_hours = 0.0
        self.substitution_hours = 0.0
        self.appeal_hours = 0.0
        self.contributions: list[CoverageContribution] = []

    @property
    def total(self) -> float:
        return self.direct_hours + self.substitution_hours + self.appeal_hours


def evaluate(
    *,
    teacher_id: str,
    rule_set: str,
    rule_version: int,
    goals: list[dict],
    direct_maps: list[dict],
    substitutions: list[dict],
    evidences: list[dict],
    appeal_credits: list[dict],
) -> EvaluationResult:
    """执行一次资格判定。

    参数约定
    --------
    goals: [{goal_code, goal_name, required_hours, sort_order?}]
    direct_maps: [{unit_code, goal_code, weight?}]，培训单元直连能力目标
    substitutions: [{from_unit, to_unit, ratio, cap_ratio}]，
        from_unit 的学时按 ratio 折算替代 to_unit，每个目标的替代总量
        不超过 required_hours * cap_ratio（部分替代由此自然产生）。
    evidences: [{evidence_id, unit_code, unit_name, issuer_id, hours,
        status, duplicate_of?}]，status 取 EvidenceStatus 的值。
    appeal_credits: [{evidence_id, goal_code, hours, reason?}]，
        申诉复核成立后追加的目标学时认定。
    """
    if not goals:
        raise ValueError("规则版本未登记任何能力目标，无法判定")

    goals = sorted(goals, key=lambda g: (g.get("sort_order", 0), g["goal_code"]))
    accs = {
        g["goal_code"]: _GoalAccum(g["goal_code"], g["goal_name"], float(g["required_hours"]))
        for g in goals
    }

    # unit_code -> 该单元直连的目标 [(goal_code, weight)]
    direct_index: dict[str, list[tuple[str, float]]] = defaultdict(list)
    for m in direct_maps:
        if m["goal_code"] in accs:
            direct_index[m["unit_code"]].append(
                (m["goal_code"], float(m.get("weight", 1.0)))
            )

    # 替代单元 -> [(目标单元, ratio, cap_ratio)]
    sub_rules: dict[str, list[tuple[str, float, float]]] = defaultdict(list)
    for s in substitutions:
        sub_rules[s["from_unit"]].append(
            (s["to_unit"], float(s["ratio"]), float(s["cap_ratio"]))
        )

    # 每条替代关系对每个目标有独立封顶预算 required_hours * cap_ratio。
    # 按证据编号顺序确定性地先到先得，后到证据被截顶——部分替代由此产生，
    # 且在逐证据贡献明细中可解释。
    RuleKey = tuple[str, str, str]  # (from_unit, to_unit, goal_code)
    sub_budget: dict[RuleKey, float] = {}
    for s in substitutions:
        for goal_code, _ in direct_index.get(s["to_unit"], []):
            sub_budget[(s["from_unit"], s["to_unit"], goal_code)] = (
                accs[goal_code].required_hours * float(s["cap_ratio"])
            )

    explanations: dict[str, EvidenceExplanation] = {}
    decision_points: dict[str, set[str]] = defaultdict(set)

    def record(ev_id: str, goal_code: str, contrib: CoverageContribution) -> None:
        accs[goal_code].contributions.append(contrib)
        if contrib.kind is not CoverageKind.NOT_COUNTED and contrib.hours > 0:
            decision_points[ev_id].add(goal_code)

    # 封顶按提交顺序先到先得（submitted_seq 为库内行序，其次时间与编号），
    # 保证同一组输入结论稳定可复现。
    def _ev_sort_key(e: dict) -> tuple:
        return (
            e.get("submitted_seq") if e.get("submitted_seq") is not None else 1 << 30,
            e.get("submitted_at") or "",
            e["evidence_id"],
        )

    for ev in sorted(evidences, key=_ev_sort_key):
        ev_id = ev["evidence_id"]
        hours = float(ev["hours"])
        excluded = _excluded_reason(ev)
        contribs_before = sum(len(a.contributions) for a in accs.values())

        if excluded is None:
            # 1) 直连覆盖
            for goal_code, weight in sorted(direct_index.get(ev["unit_code"], [])):
                gained = hours * weight
                acc = accs[goal_code]
                acc.direct_hours += gained
                record(
                    ev_id,
                    goal_code,
                    CoverageContribution(
                        evidence_id=ev_id,
                        goal_code=goal_code,
                        kind=CoverageKind.DIRECT,
                        ratio=gained / acc.required_hours if acc.required_hours > 0 else 0.0,
                        hours=gained,
                        capped=False,
                        note="培训单元直连能力目标",
                    ),
                )

            # 2) 替代覆盖（折算 + 每条关系独立封顶，可能截顶 → 部分替代）
            for target_unit, ratio, _cap in sorted(sub_rules.get(ev["unit_code"], [])):
                for goal_code, _w in sorted(direct_index.get(target_unit, [])):
                    acc = accs[goal_code]
                    wanted = hours * ratio
                    key = (ev["unit_code"], target_unit, goal_code)
                    remaining = max(0.0, sub_budget.get(key, 0.0))
                    gained = min(wanted, remaining)
                    capped = wanted > gained + _EPS
                    sub_budget[key] = max(0.0, remaining - gained)
                    if wanted > _EPS:
                        acc.substitution_hours += gained
                        note = f"按 {ratio:g} 折算替代单元 {target_unit}"
                        if capped:
                            note += "；超出该替代关系封顶，超出部分未计入"
                        record(
                            ev_id,
                            goal_code,
                            CoverageContribution(
                                evidence_id=ev_id,
                                goal_code=goal_code,
                                kind=CoverageKind.SUBSTITUTION,
                                ratio=gained / acc.required_hours if acc.required_hours > 0 else 0.0,
                                hours=gained,
                                capped=capped,
                                note=note,
                            ),
                        )

        if (
            excluded is None
            and sum(len(a.contributions) for a in accs.values()) == contribs_before
        ):
            # 验证通过、但在本规则版本下不覆盖任何目标（单元无映射，
            # 或替代目标单元未挂目标）：正是“只累计学时便判定合格”
            # 会漏掉的关键缺口。
            excluded = (
                f"培训单元 {ev['unit_code']} 在规则 {rule_set}@v{rule_version} "
                "中未映射到任何能力目标，学时不计入资格判定"
            )

        per_evidence_contribs = [
            c
            for code_goal in accs.values()
            for c in code_goal.contributions
            if c.evidence_id == ev_id
        ]
        explanations[ev_id] = EvidenceExplanation(
            evidence_id=ev_id,
            status=ev["status"],
            unit_code=ev["unit_code"],
            unit_name=ev.get("unit_name", ev["unit_code"]),
            issuer_id=ev["issuer_id"],
            hours=hours,
            decision_points=tuple(sorted(decision_points.get(ev_id, ()))),
            excluded_reason=excluded,
            contributions=tuple(per_evidence_contribs),
        )

    # 3) 申诉复核成立 → 追加认定（只追加，不改原始证据状态）
    for credit in sorted(appeal_credits, key=lambda c: (c["evidence_id"], c["goal_code"])):
        goal_code = credit["goal_code"]
        if goal_code not in accs:
            continue
        acc = accs[goal_code]
        gained = float(credit["hours"])
        acc.appeal_hours += gained
        contrib = CoverageContribution(
            evidence_id=credit["evidence_id"],
            goal_code=goal_code,
            kind=CoverageKind.APPEAL_GRANTED,
            ratio=gained / acc.required_hours if acc.required_hours > 0 else 0.0,
            hours=gained,
            capped=False,
            note="申诉复核成立，追加认定：" + credit.get("reason", ""),
        )
        acc.contributions.append(contrib)
        decision_points[credit["evidence_id"]].add(goal_code)
        existing = explanations.get(credit["evidence_id"])
        if existing is not None:
            explanations[credit["evidence_id"]] = dc_replace(
                existing,
                decision_points=tuple(sorted(decision_points[credit["evidence_id"]])),
                contributions=tuple(list(existing.contributions) + [contrib]),
            )

    goal_results: list[GoalCoverageResult] = []
    gaps: list[str] = []
    total_required = 0.0
    total_covered = 0.0
    for acc in accs.values():
        total = acc.total
        satisfied = total + _EPS >= acc.required_hours
        gap = max(0.0, acc.required_hours - total)
        total_required += acc.required_hours
        total_covered += min(total, acc.required_hours)
        goal_results.append(
            GoalCoverageResult(
                goal_code=acc.goal_code,
                goal_name=acc.goal_name,
                required_hours=acc.required_hours,
                direct_hours=acc.direct_hours,
                substitution_hours=acc.substitution_hours,
                appeal_hours=acc.appeal_hours,
                total_hours=total,
                ratio=total / acc.required_hours if acc.required_hours > 0 else 0.0,
                satisfied=satisfied,
                gap_hours=gap,
                contributions=tuple(acc.contributions),
            )
        )
        if not satisfied:
            gaps.append(
                f"能力目标 {acc.goal_code}（{acc.goal_name}）尚缺 {gap:g} 学时："
                f"直接 {acc.direct_hours:g}、替代 {acc.substitution_hours:g}、"
                f"申诉认定 {acc.appeal_hours:g}，要求 {acc.required_hours:g}"
            )

    decision = Decision.QUALIFIED if not gaps else Decision.NOT_QUALIFIED
    if decision is Decision.QUALIFIED:
        summary = (
            f"全部 {len(goal_results)} 项能力目标均有证据覆盖，资格合格；"
            f"共认定 {total_covered:g}/{total_required:g} 学时。"
        )
    else:
        summary = (
            f"资格不合格：{len(gaps)}/{len(goal_results)} 项能力目标缺少有效证据覆盖。"
            "注意：总学时达标不代表能力目标被覆盖，请查看缺口明细。"
        )

    return EvaluationResult(
        teacher_id=teacher_id,
        rule_set=rule_set,
        rule_version=rule_version,
        decided_at=iso(utcnow()),
        decision=decision,
        total_required_hours=total_required,
        total_covered_hours=total_covered,
        goal_results=tuple(goal_results),
        evidence_explanations=tuple(
            explanations[e["evidence_id"]]
            for e in sorted(evidences, key=_ev_sort_key)
        ),
        gaps=tuple(gaps),
        summary=summary,
    )


def _excluded_reason(ev: dict) -> str | None:
    """返回证据未参与判定的原因；None 表示可参与覆盖计算。"""
    status = ev["status"]
    if status == "撤销":
        return "证明已被签发方撤销，历史保留但不计入判定"
    if status == "验证不通过":
        return "签发方核验否认，不计入判定"
    if status == "重复提交":
        dup = ev.get("duplicate_of")
        return f"重复提交（与证据 {dup} 去重指纹一致），原件保留、本次不重复计入"
    if status == "提交":
        return "尚待签发方验证，暂不计入判定"
    if status != "验证":
        return f"证据状态为 {status}，不计入判定"
    return None
