"""应用服务层端到端测试：登记、证据流转、撤销、申诉与隔离。"""
from __future__ import annotations

import sqlite3
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evidence_backend.domain import aggregates as agg
from evidence_backend.errors import Conflict, NotFound, PermissionDenied, ValidationError
from evidence_backend.event_store import EventStore
from evidence_backend.identity import AuthService, Principal
from evidence_backend.repository import AggregateRepo
from evidence_backend.services import (
    AppealService,
    EvaluationService,
    EvidenceService,
    QueryService,
    RegistryService,
)


def principal(actor_id, role, org_id="") -> Principal:
    return Principal(actor_id=actor_id, role=role, org_id=org_id)


class World:
    """搭建一套完整目录与账户的测试夹具。"""

    def __init__(self) -> None:
        self.store = EventStore(":memory:")
        self.auth = AuthService(self.store.connection)
        self.repo = AggregateRepo(self.store)
        self.reg = RegistryService(self.store, self.repo)
        self.evd = EvidenceService(self.store, self.repo, self.reg)
        self.eva = EvaluationService(self.store, self.repo, self.reg)
        self.apl = AppealService(self.store, self.repo, self.eva)
        self.q = QueryService(self.store, self.repo)

        self.reviewer = principal("rv1", agg.ROLE_REVIEWER)
        self.issuer_a = principal("iss_a_user", agg.ROLE_ISSUER, "ORG_A")
        self.issuer_b = principal("iss_b_user", agg.ROLE_ISSUER, "ORG_B")
        self.t1 = principal("T001", agg.ROLE_TEACHER)
        self.t2 = principal("T002", agg.ROLE_TEACHER)
        self._seed()

    def _seed(self) -> None:
        self.iss_a = self.reg.register_issuer(
            self.reviewer, name="A 研修平台", kind="线上平台", org_id="ORG_A")["issuer_id"]
        self.iss_b = self.reg.register_issuer(
            self.reviewer, name="B 企业", kind="企业", org_id="ORG_B")["issuer_id"]

        self.reg.register_goal(self.reviewer, goal_id="G1", title="数字化教学设计")
        self.reg.register_goal(self.reviewer, goal_id="G2", title="企业实践转化")
        self.reg.register_goal(self.reviewer, goal_id="G3", title="协同教研反思")

        self.u_online = self.reg.register_training_unit(
            self.reviewer, code="ONLINE-1", title="线上研修课", category="线上研修",
            goal_id="G1", default_hours=10)["unit_id"]
        self.u_ent = self.reg.register_training_unit(
            self.reviewer, code="ENT-1", title="企业跟岗", category="企业实践",
            goal_id="G2", default_hours=16)["unit_id"]
        self.u_jy = self.reg.register_training_unit(
            self.reviewer, code="JY-1", title="联合教研", category="联合教研",
            goal_id="G3", default_hours=12)["unit_id"]

        rs = self.reg.register_ruleset(
            self.reviewer, name="教师资格规则", version="2026.1",
            goals=[
                {"goal_id": "G1", "title": "数字化教学设计", "required_hours": 20, "required_evidence_count": 2},
                {"goal_id": "G2", "title": "企业实践转化", "required_hours": 16, "required_evidence_count": 1},
                {"goal_id": "G3", "title": "协同教研反思", "required_hours": 12, "required_evidence_count": 1},
            ],
            substitutions=[
                {"unit_id": self.u_jy, "goal_id": "G1", "ratio": 0.5, "max_hours": 6},
            ],
        )
        self.rs_id = rs["ruleset_id"]
        self.reg.publish_ruleset(self.reviewer, self.rs_id, "2026-01-01")

        self.reg.register_teacher(self.reviewer, teacher_id="T001", name="张老师")
        self.reg.register_teacher(self.reviewer, teacher_id="T002", name="李老师")


class RegistrationTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World()

    def test_only_reviewer_can_register(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.w.reg.register_goal(self.w.t1, goal_id="GX", title="越权目标")

    def test_unknown_goal_or_bad_category_rejected(self) -> None:
        with self.assertRaises(ValidationError):
            self.w.reg.register_training_unit(
                self.w.reviewer, code="X", title="X", category="其他",
                goal_id="G1", default_hours=1)
        with self.assertRaises(ValidationError):
            self.w.reg.register_training_unit(
                self.w.reviewer, code="X", title="X", category="线上研修",
                goal_id="NOPE", default_hours=1)

    def test_ruleset_versioning(self) -> None:
        with self.assertRaises(Conflict):
            self.w.reg.register_ruleset(
                self.w.reviewer, name="教师资格规则", version="2026.1",
                goals=[{"goal_id": "G1", "required_hours": 1}])
        rs2 = self.w.reg.register_ruleset(
            self.w.reviewer, name="教师资格规则", version="2027.1",
            goals=[{"goal_id": "G1", "title": "数字化教学设计", "required_hours": 30}])
        # 未发布版本不能用于判定
        with self.assertRaises(Conflict):
            self.w.eva.evaluate(self.w.reviewer, "T001", ruleset_id=rs2["ruleset_id"])
        self.w.reg.publish_ruleset(self.w.reviewer, rs2["ruleset_id"], "2027-01-01")
        self.w.reg.deprecate_ruleset(self.w.reviewer, rs2["ruleset_id"])
        with self.assertRaises(Conflict):
            self.w.reg.deprecate_ruleset(self.w.reviewer, rs2["ruleset_id"])

    def test_applicable_ruleset_picks_latest_effective(self) -> None:
        rs2 = self.w.reg.register_ruleset(
            self.w.reviewer, name="教师资格规则", version="2027.1",
            goals=[{"goal_id": "G1", "title": "数字化教学设计", "required_hours": 30}])
        self.w.reg.publish_ruleset(self.w.reviewer, rs2["ruleset_id"], "2027-06-01")
        self.assertEqual(
            self.w.reg.applicable_ruleset("2027-01-01")["ruleset_id"], self.w.rs_id)
        self.assertEqual(
            self.w.reg.applicable_ruleset("2027-06-01")["ruleset_id"], rs2["ruleset_id"])


class EvidenceLifecycleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.w = World()

    def _submit_all_t1(self):
        e1 = self.w.evd.submit(
            self.w.t1, teacher_id="T001", unit_id=self.w.u_online,
            issuer_id=self.w.iss_a, hours=12, issued_on="2026-03-01")
        e2 = self.w.evd.submit(
            self.w.t1, teacher_id="T001", unit_id=self.w.u_online,
            issuer_id=self.w.iss_a, hours=10, issued_on="2026-04-01", external_ref="REF-2")
        e3 = self.w.evd.submit(
            self.w.t1, teacher_id="T001", unit_id=self.w.u_ent,
            issuer_id=self.w.iss_b, hours=16, issued_on="2026-05-01")
        e4 = self.w.evd.submit(
            self.w.t1, teacher_id="T001", unit_id=self.w.u_jy,
            issuer_id=self.w.iss_a, hours=12, issued_on="2026-06-01")
        return e1, e2, e3, e4

    def test_teacher_cannot_submit_for_others(self) -> None:
        with self.assertRaises(PermissionDenied):
            self.w.evd.submit(
                self.w.t1, teacher_id="T002", unit_id=self.w.u_online,
                issuer_id=self.w.iss_a, hours=5, issued_on="2026-03-01")

    def test_duplicate_submission_is_linked_append_only(self) -> None:
        first = self.w.evd.submit(
            self.w.t1, teacher_id="T001", unit_id=self.w.u_online,
            issuer_id=self.w.iss_a, hours=12, issued_on="2026-03-01", external_ref="DUP-1")
        dup = self.w.evd.submit(
            self.w.t1, teacher_id="T001", unit_id=self.w.u_online,
            issuer_id=self.w.iss_a, hours=12, issued_on="2026-09-01", external_ref="DUP-1")
        self.assertEqual(dup["status"], agg.ST_DUPLICATE)
        self.assertEqual(dup["duplicate_of"], first["evidence_id"])
        history = self.w.q.evidence_history(self.w.t1, dup["evidence_id"])
        self.assertEqual([e["event_type"] for e in history],
                         [agg.EV_SUBMITTED, agg.EV_DUPLICATE_LINKED])
        # 重复件不可被验证
        with self.assertRaises(Conflict):
            self.w.evd.verify(self.w.reviewer, dup["evidence_id"], decision="ACCEPT")

    def test_issuer_org_can_verify_only_own_documents(self) -> None:
        e1, _, e3, _ = self._submit_all_t1()
        # B 机构不能验证 A 签发的证明
        with self.assertRaises(PermissionDenied):
            self.w.evd.verify(self.w.issuer_b, e1["evidence_id"], decision="ACCEPT")
        # A 机构可以验证自己的；审核员全域可验证
        self.w.evd.verify(self.w.issuer_a, e1["evidence_id"], decision="ACCEPT")
        self.w.evd.verify(self.w.reviewer, e3["evidence_id"], decision="ACCEPT")
        with self.assertRaises(Conflict):
            self.w.evd.verify(self.w.reviewer, e3["evidence_id"], decision="REJECT")

    def test_org_isolation_in_queries(self) -> None:
        self._submit_all_t1()
        a_docs = self.w.q.list_evidences(self.w.issuer_a)
        b_docs = self.w.q.list_evidences(self.w.issuer_b)
        self.assertEqual({d["issuer_id"] for d in a_docs}, {self.w.iss_a})
        self.assertEqual({d["issuer_id"] for d in b_docs}, {self.w.iss_b})
        # 教师只能看到自己的组合
        t2_docs = self.w.q.list_evidences(self.w.t2)
        self.assertEqual(t2_docs, [])
        with self.assertRaises(PermissionDenied):
            self.w.q.portfolio(self.w.t2, "T001")

    def test_revocation_re_evaluation_and_appeal(self) -> None:
        e1, e2, e3, e4 = self._submit_all_t1()
        for e in (e1, e2, e3, e4):
            self.w.evd.verify(self.w.reviewer, e["evidence_id"], decision="ACCEPT")

        first_eval = self.w.eva.evaluate(self.w.reviewer, "T001")
        self.assertEqual(first_eval["result"], "QUALIFIED")

        # 撤销关键的企业实践证据 -> 缺口重新出现，旧判定被替代但历史保留。
        self.w.evd.revoke(self.w.issuer_b, e3["evidence_id"], reason="发现代签")
        second_eval = self.w.eva.evaluate(
            self.w.reviewer, "T001", trigger=agg.TRIGGER_REVOCATION, reason="证据撤销后重判")
        self.assertEqual(second_eval["result"], "NOT_QUALIFIED")
        self.assertEqual({g["goal_id"] for g in second_eval["gaps"]}, {"G2"})
        hist = self.w.q.evaluation_history(self.w.reviewer, "T001")
        self.assertEqual([h["status"] for h in hist], ["SUPERSEDED", "CURRENT"])
        # 被撤销证据的贡献必须显式标注，不再计入。
        basis = {b["evidence_id"]: b for b in second_eval["evidence_basis"]}
        self.assertFalse(basis[e3["evidence_id"]]["counted"])
        self.assertIn("撤销", basis[e3["evidence_id"]]["exclusion_reason"])

        # 教师申诉；审核员复核认定撤销有误（恢复证据）并撤销原判定。
        appeal = self.w.apl.open_appeal(
            self.w.t1, teacher_id="T001", reason="企业实践真实有效，代签系误认")
        with self.assertRaises(PermissionDenied):
            self.w.apl.review_appeal(
                self.w.t1, appeal["appeal_id"], decision=agg.APPEAL_UPHELD, note="x")
        self.w.evd.reinstate(self.w.reviewer, e3["evidence_id"], note="复核确认真实")
        reviewed = self.w.apl.review_appeal(
            self.w.reviewer, appeal["appeal_id"],
            decision=agg.APPEAL_OVERTURNED, note="撤销依据不成立，重新判定")
        self.assertEqual(reviewed["status"], agg.APPEAL_OVERTURNED)
        new_eval = self.w.q.current_evaluation(self.w.t1, "T001")
        self.assertEqual(new_eval["result"], "QUALIFIED")
        self.assertEqual(new_eval["trigger"], agg.TRIGGER_APPEAL)
        # 申诉与证据历史均只追加。
        self.assertEqual(len(self.w.q.get_appeal(self.w.t1, appeal["appeal_id"])["reviews"]), 1)
        ev_hist = self.w.q.evidence_history(self.w.reviewer, e3["evidence_id"])
        self.assertEqual(
            [x["event_type"] for x in ev_hist],
            [agg.EV_SUBMITTED, agg.EV_ACCEPTED, agg.EV_REVOKED, agg.EV_REINSTATED],
        )

    def test_gap_report_explains_each_evidences_contribution(self) -> None:
        # 只有一份线上研修 + 一份联合教研：G1 可由直接 12 学时 + 替代折算 6 达到 18，仍缺 2。
        e1 = self.w.evd.submit(
            self.w.t1, teacher_id="T001", unit_id=self.w.u_online,
            issuer_id=self.w.iss_a, hours=12, issued_on="2026-03-01")
        e2 = self.w.evd.submit(
            self.w.t1, teacher_id="T001", unit_id=self.w.u_jy,
            issuer_id=self.w.iss_a, hours=12, issued_on="2026-04-01")
        self.w.evd.verify(self.w.reviewer, e1["evidence_id"], decision="ACCEPT")
        self.w.evd.verify(self.w.reviewer, e2["evidence_id"], decision="ACCEPT")
        report = self.w.eva.evaluate(self.w.reviewer, "T001")
        self.assertEqual(report["result"], "NOT_QUALIFIED")
        g1 = next(g for g in report["goals"] if g["goal_id"] == "G1")
        self.assertEqual(g1["gap_hours"], 2)
        paths = {c["evidence_id"]: c["path"] for c in g1["contributions"]}
        self.assertEqual(paths[e1["evidence_id"]], "DIRECT")
        self.assertEqual(paths[e2["evidence_id"]], "SUBSTITUTION")
        self.assertIn("G2", {g["goal_id"] for g in report["gaps"]})
        self.assertTrue(report["explanation"]["summary"].startswith("资格暂不通过"))

    def test_event_log_is_physically_append_only(self) -> None:
        self._submit_all_t1()
        with self.assertRaises(sqlite3.IntegrityError):
            self.w.store.connection.execute("DELETE FROM event_log")
        with self.assertRaises(sqlite3.IntegrityError):
            self.w.store.connection.execute("UPDATE event_log SET event_type = 'x'")

    def test_appeal_requires_existing_evaluation(self) -> None:
        with self.assertRaises(NotFound):
            self.w.apl.open_appeal(self.w.t2, teacher_id="T002", reason="还没有判定")


if __name__ == "__main__":
    unittest.main()
