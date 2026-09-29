"""服务层集成测试：权限隔离、只追加历史、规则冻结、端到端流程。"""
from __future__ import annotations

import json
import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from portfolio_backend.database import Database
from portfolio_backend.domain import (
    AppealStatus,
    Conflict,
    EvidenceStatus,
    NotFound,
    PermissionDenied,
    Role,
    RuleError,
)
from portfolio_backend.seed import seed
from portfolio_backend.services import Principal, Service


class ServiceCase(unittest.TestCase):
    def setUp(self) -> None:
        self.db = Database(":memory:")
        self.svc = Service(self.db)
        self.admin = Principal("u_admin", Role.ADMIN, name="管理员")
        self.svc.create_organization(self.admin, "org_a", "甲培训中心", "培训机构")
        self.svc.create_organization(self.admin, "org_b", "乙培训中心", "培训机构")
        self.svc.create_organization(self.admin, "iss_x", "X 签发方", "签发方")
        self.svc.create_organization(self.admin, "iss_y", "Y 签发方", "签发方")
        self.svc.create_user(self.admin, "t1", "教师一", Role.TEACHER)
        self.svc.create_user(self.admin, "t2", "教师二", Role.TEACHER)
        self.svc.create_user(self.admin, "ra", "甲机构审核员", Role.ORG_REVIEWER, org_id="org_a")
        self.svc.create_user(self.admin, "rb", "乙机构审核员", Role.ORG_REVIEWER, org_id="org_b")
        self.svc.create_user(self.admin, "sx", "X 经办人", Role.ISSUER_STAFF, org_id="iss_x")
        self.svc.create_user(self.admin, "sy", "Y 经办人", Role.ISSUER_STAFF, org_id="iss_y")
        self.svc.register_unit(self.admin, "u_theory", "理论课程", "线上研修")
        self.svc.register_unit(self.admin, "u_practice", "实践课程", "企业实践")
        self.svc.authorize_issuer(self.admin, "iss_x", "u_theory")
        self.svc.authorize_issuer(self.admin, "iss_y", "u_practice")
        self.svc.create_rule_set(self.admin, "std", "标准")
        self.svc.create_rule_version(self.admin, "std")
        self.svc.add_goal(self.admin, "std", 1, "G1", "理论", 10)
        self.svc.add_goal(self.admin, "std", 1, "G2", "实践", 10)
        self.svc.map_unit_goal(self.admin, "std", 1, "u_theory", "G1")
        self.svc.map_unit_goal(self.admin, "std", 1, "u_practice", "G2")
        self.svc.publish_rule_version(self.admin, "std", 1)
        self.svc.enroll_teacher(self.admin, "t1", "org_a", "std", 1)
        self.svc.enroll_teacher(self.admin, "t2", "org_b", "std", 1)
        self.t1 = self.svc.principal("t1")
        self.t2 = self.svc.principal("t2")
        self.ra = self.svc.principal("ra")
        self.rb = self.svc.principal("rb")
        self.sx = self.svc.principal("sx")
        self.sy = self.svc.principal("sy")

    def tearDown(self) -> None:
        self.db.close()

    # -- 权限隔离 ---------------------------------------------------------

    def test_teacher_can_only_access_own_portfolio(self):
        with self.assertRaises(PermissionDenied):
            self.svc.get_portfolio(self.t1, "t2")
        ok = self.svc.get_portfolio(self.t1, "t1")
        self.assertEqual(ok["teacher_id"], "t1")

    def test_reviewer_org_isolation(self):
        with self.assertRaises(PermissionDenied):
            self.svc.evaluate_teacher(self.ra, "t2")
        with self.assertRaises(PermissionDenied):
            self.svc.list_events(self.ra, teacher_id="t2")
        # 本机构教师可以
        self.svc.evaluate_teacher(self.ra, "t1", persist=False)

    def test_issuer_cannot_touch_other_issuers_evidence(self):
        eid = self.svc.submit_evidence(
            self.t1, "t1", "u_theory", "iss_x", 10, "2026-01-01", "X-1"
        )["evidence_id"]
        with self.assertRaises(PermissionDenied):
            self.svc.verify_evidence(self.sy, eid)
        self.svc.verify_evidence(self.sx, eid)

    def test_issuer_must_be_authorized_for_unit(self):
        # iss_y 未被授权 u_theory
        eid = self.svc.submit_evidence(
            self.t1, "t1", "u_theory", "iss_x", 10, "2026-01-02", "X-2"
        )["evidence_id"]
        # 授权正常路径已覆盖；直接构造一个未授权组合：
        self.svc.authorize_issuer  # 存在性
        self.svc.revoke_evidence(self.sx, eid, reason="清理")
        eid2 = self.svc.submit_evidence(
            self.t1, "t1", "u_theory", "iss_x", 10, "2026-01-03", "X-3"
        )["evidence_id"]
        # 撤销授权后验证应被拒
        with self.db.tx() as conn:
            conn.execute(
                "DELETE FROM issuer_authorizations WHERE issuer_id = 'iss_x' AND unit_code = 'u_theory'"
            )
        with self.assertRaises(PermissionDenied):
            self.svc.verify_evidence(self.sx, eid2)

    def test_issuer_evidence_list_scoped_to_own_org(self):
        self.svc.submit_evidence(self.t1, "t1", "u_theory", "iss_x", 10, "2026-01-04", "X-4")
        self.svc.submit_evidence(self.t2, "t2", "u_practice", "iss_y", 10, "2026-01-05", "Y-1")
        x_items = self.svc.list_evidence_for_issuer(self.sx)
        self.assertEqual({e["issuer_id"] if "issuer_id" in e else "iss_x" for e in x_items}, {"iss_x"})
        self.assertEqual(len(x_items), 1)

    # -- 只追加历史 -------------------------------------------------------

    def test_event_log_is_append_only(self):
        self.svc.submit_evidence(self.t1, "t1", "u_theory", "iss_x", 10, "2026-02-01", "X-10")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute("UPDATE events SET payload = '{}' WHERE seq = 1")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.conn.execute("DELETE FROM events WHERE seq = 1")

    def test_hash_chain_links_all_events(self):
        self.svc.submit_evidence(self.t1, "t1", "u_theory", "iss_x", 10, "2026-02-02", "X-11")
        eid = self.svc.submit_evidence(self.t1, "t1", "u_theory", "iss_x", 10, "2026-02-02", "X-11")
        self.assertEqual(eid["status"], EvidenceStatus.DUPLICATE.value)
        report = self.db.verify_chain()
        self.assertTrue(report["ok"], report)
        self.assertGreaterEqual(report["event_count"], 2)

    # -- 证据生命周期 -----------------------------------------------------

    def test_duplicate_submission_is_kept_and_excluded(self):
        first = self.svc.submit_evidence(
            self.t1, "t1", "u_theory", "iss_x", 10, "2026-03-01", "DUP-1"
        )["evidence_id"]
        dup = self.svc.submit_evidence(
            self.t1, "t1", "u_theory", "iss_x", 10, "2026-03-01", "DUP-1"
        )
        self.assertEqual(dup["status"], EvidenceStatus.DUPLICATE.value)
        self.assertEqual(dup["duplicate_of"], first)
        # 记录仍在（可追溯）
        portfolio = self.svc.get_portfolio(self.t1, "t1")
        statuses = {e["evidence_id"]: e["status"] for e in portfolio["evidences"]}
        self.assertEqual(statuses[dup["evidence_id"]], "重复提交")

    def test_revocation_appends_history_and_excludes_evidence(self):
        eid = self.svc.submit_evidence(
            self.t1, "t1", "u_theory", "iss_x", 10, "2026-03-10", "R-1"
        )["evidence_id"]
        self.svc.verify_evidence(self.sx, eid)
        self.svc.revoke_evidence(self.sx, eid, reason="发现证明不实")
        result = self.svc.evaluate_teacher(self.ra, "t1", persist=False)
        self.assertEqual(result["decision"], "不合格")
        explanation = {e["evidence_id"]: e for e in result["evidence"]}[eid]
        self.assertIn("撤销", explanation["excluded_reason"])
        # 撤销事件可在历史中查到且不可改
        types = [e["event_type"] for e in self.svc.list_events(self.t1)]
        self.assertIn("evidence.revoked", types)
        with self.assertRaises(Conflict):
            self.svc.revoke_evidence(self.sx, eid, reason="再次撤销")

    def test_unverified_evidence_not_counted(self):
        self.svc.submit_evidence(self.t1, "t1", "u_theory", "iss_x", 10, "2026-03-11", "P-1")
        result = self.svc.evaluate_teacher(self.ra, "t1", persist=False)
        self.assertEqual(result["decision"], "不合格")

    # -- 规则版本冻结 -----------------------------------------------------

    def test_published_rule_version_is_frozen(self):
        with self.assertRaises(Conflict):
            self.svc.add_goal(self.admin, "std", 1, "G3", "新增", 5)
        with self.assertRaises(Conflict):
            self.svc.map_unit_goal(self.admin, "std", 1, "u_theory", "G2")
        with self.assertRaises(Conflict):
            self.svc.add_substitution(self.admin, "std", 1, "u_theory", "u_practice", 0.5, 0.5)
        # 新版本可以正常演进
        self.svc.create_rule_version(self.admin, "std", note="v2")
        self.svc.add_goal(self.admin, "std", 2, "G3", "新增", 5)

    def test_only_admin_manages_rules(self):
        with self.assertRaises(PermissionDenied):
            self.svc.create_rule_set(self.ra, "rogue", "越权规则集")

    def test_version_without_goal_cannot_publish(self):
        self.svc.create_rule_version(self.admin, "std", note="空版本")
        with self.assertRaises(RuleError):
            self.svc.publish_rule_version(self.admin, "std", 2)

    # -- 申诉与复核 -------------------------------------------------------

    def test_appeal_review_and_credit_flow(self):
        e1 = self.svc.submit_evidence(
            self.t1, "t1", "u_theory", "iss_x", 10, "2026-04-01", "A-0"
        )["evidence_id"]
        self.svc.verify_evidence(self.sx, e1)
        eid = self.svc.submit_evidence(
            self.t1, "t1", "u_practice", "iss_y", 6, "2026-04-01", "A-1"
        )["evidence_id"]
        self.svc.verify_evidence(self.sy, eid)
        before = self.svc.evaluate_teacher(self.ra, "t1", persist=False)
        self.assertEqual(before["decision"], "不合格")  # G2 缺 4

        appeal_id = self.svc.file_appeal(self.t1, "t1", "另有 4 学时", evidence_id=eid)["appeal_id"]
        # 外机构审核员不能复核
        with self.assertRaises(PermissionDenied):
            self.svc.review_appeal(self.rb, appeal_id, True, "越权", credits=[])
        self.svc.review_appeal(
            self.ra, appeal_id, uphold=True,
            decision_note="补认 4 学时",
            credits=[{"evidence_id": eid, "goal_code": "G2", "hours": 4}],
        )
        after = self.svc.evaluate_teacher(self.ra, "t1", persist=False)
        self.assertEqual(after["decision"], "合格")
        g2 = {g["goal_code"]: g for g in after["goals"]}["G2"]
        self.assertEqual(g2["appeal_hours"], 4)
        # 复核结论不可改
        with self.assertRaises(Conflict):
            self.svc.review_appeal(self.ra, appeal_id, False, "翻案", credits=[])
        # 驳回的申诉不产生认定
        a2 = self.svc.file_appeal(self.t1, "t1", "再申诉")["appeal_id"]
        out = self.svc.review_appeal(self.ra, a2, uphold=False, decision_note="证据不足")
        self.assertEqual(out["status"], AppealStatus.REJECTED.value)

    # -- 判定历史与可追溯 -------------------------------------------------

    def test_evaluations_are_persisted_as_history(self):
        self.svc.submit_evidence(self.t1, "t1", "u_theory", "iss_x", 10, "2026-05-01", "H-1")
        self.svc.evaluate_teacher(self.ra, "t1")
        self.svc.evaluate_teacher(self.ra, "t1")
        history = self.svc.list_evaluations(self.ra, "t1")
        self.assertEqual(len(history), 2)
        self.assertTrue(all(h["result"]["rule_version"] == 1 for h in history))


class SeedScenarioTest(unittest.TestCase):
    """用完整演示数据复核需求中的每一项承诺。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.db = Database(":memory:")
        cls.result = seed(cls.db)
        cls.svc = Service(cls.db)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.db.close()

    def test_wang_hours_exceed_threshold_but_g3_gap_makes_unqualified(self):
        w = self.result["wang"]["evaluation"]
        self.assertEqual(w["decision"], "不合格")
        g3 = {g["goal_code"]: g for g in w["goals"]}["G3"]
        self.assertEqual(g3["total_hours"], 0)
        self.assertEqual(g3["gap_hours"], 32)
        self.assertEqual(len(w["gaps"]), 1)
        self.assertIn("G3", w["gaps"][0])

    def test_wang_duplicate_evidence_excluded(self):
        w = self.result["wang"]["evaluation"]
        dup_ids = {self.result["wang"]["evidences"]["duplicate"]}
        explained = {e["evidence_id"] for e in w["evidence"]}
        self.assertTrue(dup_ids & explained)
        dup = [e for e in w["evidence"] if e["evidence_id"] in dup_ids][0]
        self.assertEqual(dup["status"], "重复提交")
        self.assertIn("重复提交", dup["excluded_reason"])

    def test_li_partial_substitution_caps_second_evidence(self):
        first = self.result["li"]["first_evaluation"]
        g2 = {g["goal_code"]: g for g in first["goals"]}["G2"]
        self.assertEqual(g2["substitution_hours"], 6)  # 4 + 2（截顶）
        capped = [c for c in g2["contributions"] if c["capped"]]
        self.assertEqual(len(capped), 1)

    def test_li_revoked_evidence_excluded_but_history_kept(self):
        first = self.result["li"]["first_evaluation"]
        revoked_id = self.result["li"]["evidences"]["revoked"]
        entry = {e["evidence_id"]: e for e in first["evidence"]}[revoked_id]
        self.assertEqual(entry["status"], "撤销")
        self.assertIsNotNone(entry["excluded_reason"])

    def test_li_qualifies_only_after_appeal_credit(self):
        self.assertEqual(self.result["li"]["first_evaluation"]["decision"], "不合格")
        self.assertEqual(self.result["li"]["second_evaluation"]["decision"], "合格")

    def test_full_chain_intact(self):
        report = self.db.verify_chain()
        self.assertTrue(report["ok"], report)
        self.assertGreater(report["event_count"], 50)

    def test_cross_org_isolation_in_seed(self):
        reviewer_b = self.svc.principal("r_zhao")  # org_school2
        with self.assertRaises(PermissionDenied):
            self.svc.get_portfolio(reviewer_b, "t_wang")


if __name__ == "__main__":
    unittest.main()
