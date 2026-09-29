"""判定引擎的确定性规则测试。"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evidence_backend.domain.aggregates import (
    ST_ACCEPTED,
    ST_DUPLICATE,
    ST_REJECTED,
    ST_REVOKED,
    ST_SUBMITTED,
)
from evidence_backend.domain.engine import (
    NOT_QUALIFIED,
    PATH_DIRECT,
    PATH_SUBSTITUTION,
    QUALIFIED,
    ROLE_NEEDED,
    ROLE_SURPLUS,
    evaluate,
)


def ruleset_v1():
    return {
        "ruleset_id": "rs_demo",
        "name": "教师资格规则",
        "version": "2026.1",
        "goals": [
            {"goal_id": "G1", "title": "数字化教学设计", "required_hours": 20, "required_evidence_count": 2},
            {"goal_id": "G2", "title": "企业实践转化", "required_hours": 16, "required_evidence_count": 1},
            {"goal_id": "G3", "title": "协同教研反思", "required_hours": 12, "required_evidence_count": 1},
        ],
        "substitutions": [
            # 联合教研可折算替代 G1，折算比例 0.5，最多计入 6 学时
            {"substitution_id": "sub_jy_g1", "unit_id": "U_JY", "goal_id": "G1", "ratio": 0.5, "max_hours": 6},
            # 线上研修可部分替代 G3，每学时计 0.5，最多 4
            {"substitution_id": "sub_online_g3", "unit_id": "U_ONLINE", "goal_id": "G3", "ratio": 0.5, "max_hours": 4},
        ],
    }


UNITS = {
    "U_ONLINE": {"unit_id": "U_ONLINE", "code": "ONLINE", "title": "线上研修", "category": "线上研修", "goal_id": "G1"},
    "U_ENT": {"unit_id": "U_ENT", "code": "ENT", "title": "企业跟岗", "category": "企业实践", "goal_id": "G2"},
    "U_JY": {"unit_id": "U_JY", "code": "JY", "title": "联合教研", "category": "联合教研", "goal_id": "G3"},
}


def ev(eid, unit, hours, status=ST_ACCEPTED):
    return {
        "evidence_id": eid, "teacher_id": "T1", "unit_id": unit,
        "issuer_id": "iss1", "hours": hours, "issued_on": "2026-03-01", "status": status,
    }


class EngineTest(unittest.TestCase):
    def test_hours_accumulated_but_goal_uncovered_is_not_qualified(self):
        # 只有 G1 的线上研修：学时再多也不能掩盖 G2/G3 缺口。
        evidences = [ev("ev1", "U_ONLINE", 30), ev("ev2", "U_ONLINE", 24)]
        report = evaluate(ruleset=ruleset_v1(), units=UNITS, evidences=evidences)
        self.assertEqual(report["result"], NOT_QUALIFIED)
        gap_goals = {g["goal_id"] for g in report["gaps"]}
        self.assertEqual(gap_goals, {"G2", "G3"})
        g2 = next(g for g in report["goals"] if g["goal_id"] == "G2")
        self.assertEqual(g2["covered_hours"], 0)
        self.assertIn("尚缺 16 学时", "".join(g2["unsatisfied_reasons"]))

    def test_full_coverage_qualified_and_explains_contribution(self):
        evidences = [
            ev("ev1", "U_ONLINE", 12),
            ev("ev2", "U_ONLINE", 10),
            ev("ev3", "U_ENT", 16),
            ev("ev4", "U_JY", 12),
        ]
        report = evaluate(ruleset=ruleset_v1(), units=UNITS, evidences=evidences)
        self.assertEqual(report["result"], QUALIFIED)
        g1 = next(g for g in report["goals"] if g["goal_id"] == "G1")
        direct = [c for c in g1["contributions"] if c["path"] == PATH_DIRECT]
        self.assertEqual([c["evidence_id"] for c in direct], ["ev1", "ev2"])
        self.assertTrue(all(c["necessity"] == ROLE_NEEDED for c in direct))

        # 富余学时必须被标记为 SURPLUS，而不是冒充足额贡献。
        evidences.append(ev("ev5", "U_ONLINE", 8))
        report = evaluate(ruleset=ruleset_v1(), units=UNITS, evidences=evidences)
        g1 = next(g for g in report["goals"] if g["goal_id"] == "G1")
        surplus = [c for c in g1["contributions"] if c["necessity"] == ROLE_SURPLUS]
        self.assertTrue(surplus)

    def test_partial_substitution_ratio_and_cap(self):
        # G3 无直接证据：12 学时线上研修按 0.5 折算 = 6，受 4 学时上限约束，仍缺 8。
        evidences = [
            ev("ev1", "U_ONLINE", 12),
            ev("ev2", "U_ONLINE", 10),
            ev("ev3", "U_ENT", 16),
        ]
        report = evaluate(ruleset=ruleset_v1(), units=UNITS, evidences=evidences)
        self.assertEqual(report["result"], NOT_QUALIFIED)
        g3 = next(g for g in report["goals"] if g["goal_id"] == "G3")
        self.assertEqual(len(g3["contributions"]), 2)
        self.assertEqual(g3["contributions"][0]["counted_hours"], 4)  # 触顶
        self.assertEqual(g3["contributions"][1]["counted_hours"], 0)
        self.assertEqual(g3["gap_hours"], 8)
        self.assertEqual(g3["contributions"][0]["path"], PATH_SUBSTITUTION)
        self.assertEqual(g3["contributions"][0]["substitution_id"], "sub_online_g3")

        # 补足直接的联合教研证据后通过。
        evidences.append(ev("ev4", "U_JY", 12))
        report = evaluate(ruleset=ruleset_v1(), units=UNITS, evidences=evidences)
        self.assertEqual(report["result"], QUALIFIED)
        g3 = next(g for g in report["goals"] if g["goal_id"] == "G3")
        self.assertEqual(g3["contributions"][0]["path"], PATH_DIRECT)  # 直接证据优先

    def test_non_accepted_evidence_never_counts_with_reason(self):
        evidences = [
            ev("ev1", "U_ONLINE", 12, ST_SUBMITTED),
            ev("ev2", "U_ONLINE", 10, ST_DUPLICATE),
            ev("ev3", "U_ENT", 16, ST_REJECTED),
            ev("ev4", "U_JY", 12, ST_REVOKED),
        ]
        report = evaluate(ruleset=ruleset_v1(), units=UNITS, evidences=evidences)
        self.assertEqual(report["result"], NOT_QUALIFIED)
        reasons = {b["evidence_id"]: b["exclusion_reason"] for b in report["evidence_basis"]}
        self.assertFalse(any(b["counted"] for b in report["evidence_basis"]))
        self.assertIn("验证中", reasons["ev1"])
        self.assertIn("重复", reasons["ev2"])
        self.assertIn("拒绝", reasons["ev3"])
        self.assertIn("撤销", reasons["ev4"])

    def test_evaluation_is_deterministic_regardless_of_input_order(self):
        a = [
            ev("ev4", "U_JY", 12), ev("ev2", "U_ONLINE", 10),
            ev("ev3", "U_ENT", 16), ev("ev1", "U_ONLINE", 12),
        ]
        r1 = evaluate(ruleset=ruleset_v1(), units=UNITS, evidences=list(reversed(a)))
        r2 = evaluate(ruleset=ruleset_v1(), units=UNITS, evidences=a)
        self.assertEqual(r1["result"], r2["result"])
        for g1, g2 in zip(r1["goals"], r2["goals"]):
            self.assertEqual(
                [c["evidence_id"] for c in g1["contributions"]],
                [c["evidence_id"] for c in g2["contributions"]],
            )

    def test_required_evidence_count_blocks_single_certificate(self):
        # 一份 30 学时的线上研修满足 G1 学时，却不满足“至少 2 份证据”。
        report = evaluate(
            ruleset=ruleset_v1(), units=UNITS, evidences=[ev("ev1", "U_ONLINE", 30)]
        )
        g1 = next(g for g in report["goals"] if g["goal_id"] == "G1")
        self.assertFalse(g1["satisfied"])
        self.assertTrue(any("2 份" in r for r in g1["unsatisfied_reasons"]))


if __name__ == "__main__":
    unittest.main()
