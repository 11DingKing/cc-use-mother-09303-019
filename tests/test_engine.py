"""判定引擎的纯函数测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from portfolio_backend.domain import CoverageKind, Decision
from portfolio_backend.engine import evaluate


def base_inputs(**overrides):
    goals = [
        {"goal_code": "G1", "goal_name": "理论", "required_hours": 40, "sort_order": 1},
        {"goal_code": "G2", "goal_name": "实践", "required_hours": 20, "sort_order": 2},
    ]
    direct_maps = [
        {"unit_code": "theory", "goal_code": "G1", "weight": 1.0},
        {"unit_code": "practice", "goal_code": "G2", "weight": 1.0},
    ]
    substitutions = [
        {"from_unit": "lecture", "to_unit": "practice", "ratio": 0.5, "cap_ratio": 0.5},
    ]
    values = dict(
        teacher_id="t1",
        rule_set="std",
        rule_version=1,
        goals=goals,
        direct_maps=direct_maps,
        substitutions=substitutions,
        evidences=[],
        appeal_credits=[],
    )
    values.update(overrides)
    return values


def ev(evidence_id, unit, hours, status="验证", **kw):
    row = {
        "evidence_id": evidence_id,
        "unit_code": unit,
        "unit_name": unit,
        "issuer_id": "iss",
        "hours": hours,
        "status": status,
    }
    row.update(kw)
    return row


class EngineTest(unittest.TestCase):
    def test_direct_coverage_qualifies(self):
        r = evaluate(**base_inputs(evidences=[
            ev("e1", "theory", 40),
            ev("e2", "practice", 20),
        ]))
        self.assertIs(r.decision, Decision.QUALIFIED)
        self.assertEqual(r.gaps, ())
        by_goal = {g.goal_code: g for g in r.goal_results}
        self.assertEqual(by_goal["G1"].direct_hours, 40)
        self.assertEqual(by_goal["G2"].direct_hours, 20)

    def test_total_hours_enough_but_goal_uncovered_is_not_qualified(self):
        # 60 学时全堆在理论上，实践目标零覆盖——核心反例。
        r = evaluate(**base_inputs(evidences=[ev("e1", "theory", 60)]))
        self.assertIs(r.decision, Decision.NOT_QUALIFIED)
        self.assertEqual(len(r.gaps), 1)
        self.assertIn("G2", r.gaps[0])
        self.assertIn("尚缺 20", r.gaps[0])
        # 总覆盖按达标目标封顶计 40，而不是 60。
        self.assertEqual(r.total_covered_hours, 40)

    def test_unmapped_unit_hours_excluded_with_reason(self):
        r = evaluate(**base_inputs(evidences=[
            ev("e1", "theory", 40),
            ev("e2", "practice", 20),
            ev("e3", "unrelated", 100),
        ]))
        explanation = {e.evidence_id: e for e in r.evidence_explanations}["e3"]
        self.assertIn("未映射到任何能力目标", explanation.excluded_reason)
        self.assertEqual(explanation.decision_points, ())

    def test_invalid_statuses_are_excluded(self):
        cases = [
            ("撤销", "撤销"),
            ("验证不通过", "核验否认"),
            ("提交", "尚待签发方验证"),
            ("重复提交", "重复提交"),
        ]
        for status, fragment in cases:
            with self.subTest(status=status):
                r = evaluate(**base_inputs(evidences=[
                    ev("e1", "theory", 40),
                    ev("e2", "practice", 20, status=status, duplicate_of="e0"),
                ]))
                explanation = {e.evidence_id: e for e in r.evidence_explanations}["e2"]
                self.assertIsNotNone(explanation.excluded_reason)
                self.assertIn(fragment, explanation.excluded_reason)
                self.assertEqual(explanation.decision_points, ())

    def test_partial_substitution_is_ratioed_and_capped(self):
        # 封顶 = G2 要求 20 * 0.5 = 10 学时。
        # 第一份讲座 8h * 0.5 = 4（不截顶）；第二份 24h * 0.5 = 12，只剩 6 预算 → 截顶。
        r = evaluate(**base_inputs(evidences=[
            ev("e1", "theory", 40),
            ev("e2", "lecture", 8),
            ev("e3", "lecture", 24),
        ]))
        g2 = {g.goal_code: g for g in r.goal_results}["G2"]
        self.assertAlmostEqual(g2.substitution_hours, 10.0)
        self.assertFalse(g2.satisfied)  # 10 < 20
        capped = [c for c in g2.contributions if c.capped]
        self.assertEqual(len(capped), 1)
        self.assertEqual(capped[0].evidence_id, "e3")
        self.assertAlmostEqual(capped[0].hours, 6.0)
        # 证据解释：e3 的 G2 贡献明确标注截顶。
        e3 = {e.evidence_id: e for e in r.evidence_explanations}["e3"]
        self.assertEqual(e3.decision_points, ("G2",))
        self.assertIn("封顶", e3.contributions[0].note)

    def test_appeal_credit_is_additive_and_qualifies(self):
        r = evaluate(**base_inputs(
            evidences=[ev("e1", "theory", 40), ev("e2", "practice", 12)],
            appeal_credits=[
                {"evidence_id": "e2", "goal_code": "G2", "hours": 8, "reason": "补认带教"},
            ],
        ))
        self.assertIs(r.decision, Decision.QUALIFIED)
        g2 = {g.goal_code: g for g in r.goal_results}["G2"]
        self.assertEqual(g2.appeal_hours, 8)
        kinds = {c.kind for c in g2.contributions}
        self.assertIn(CoverageKind.APPEAL_GRANTED, kinds)
        e2 = {e.evidence_id: e for e in r.evidence_explanations}["e2"]
        self.assertIn("G2", e2.decision_points)

    def test_weighted_direct_mapping(self):
        r = evaluate(**base_inputs(
            direct_maps=[{"unit_code": "theory", "goal_code": "G1", "weight": 0.5},
                         {"unit_code": "practice", "goal_code": "G2", "weight": 1.0}],
            evidences=[ev("e1", "theory", 60), ev("e2", "practice", 20)],
        ))
        g1 = {g.goal_code: g for g in r.goal_results}["G1"]
        self.assertEqual(g1.direct_hours, 30)
        self.assertFalse(g1.satisfied)

    def test_result_is_deterministic(self):
        kwargs = base_inputs(evidences=[
            ev("e2", "lecture", 24), ev("e1", "theory", 40), ev("e0", "lecture", 8),
        ])
        r1 = evaluate(**kwargs).to_dict()
        r2 = evaluate(**kwargs).to_dict()
        # 时间戳外其余完全一致；贡献顺序由证据编号决定。
        for payload in (r1, r2):
            payload.pop("decided_at")
        self.assertEqual(r1, r2)

    def test_empty_goals_rejected(self):
        with self.assertRaises(ValueError):
            evaluate(**base_inputs(goals=[]))


if __name__ == "__main__":
    unittest.main()
