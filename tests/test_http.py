"""HTTP API 端到端测试：真实启动服务，通过 HTTP 调用完整业务链路。"""
from __future__ import annotations

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from evidence_backend.http import build_server


class ApiClient:
    def __init__(self, base: str, token: str | None = None) -> None:
        self.base = base
        self.token = token

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, dict]:
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", "Bearer " + self.token)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))


class HttpFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = build_server(":memory:", "127.0.0.1", 0)
        cls.port = cls.server.server_address[1]
        cls.base = f"http://127.0.0.1:{cls.port}/api"
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        # 引导管理员账户
        cls.server.app.auth.create_account("admin", "admin", "admin-secret")
        # 各角色账户
        admin = ApiClient(cls.base)
        st, resp = admin.request("POST", "/auth/token", {"actor_id": "admin", "secret": "admin-secret"})
        admin.token = resp["token"]
        for account in [
            {"actor_id": "rv1", "role": "reviewer", "secret": "rv"},
            {"actor_id": "iss_a", "role": "issuer", "secret": "ia", "org_id": "ORG_A"},
            {"actor_id": "T001", "role": "teacher", "secret": "t1"},
        ]:
            st, resp = admin.request("POST", "/admin/accounts", account)
            assert st == 200, resp

        def login(actor_id, secret):
            c = ApiClient(cls.base)
            st, resp = c.request("POST", "/auth/token", {"actor_id": actor_id, "secret": secret})
            assert st == 200, resp
            c.token = resp["token"]
            return c

        cls.rv = login("rv1", "rv")
        cls.iss = login("iss_a", "ia")
        cls.t1 = login("T001", "t1")
        cls.anon = ApiClient(cls.base)

        cls._seed_catalog()

    @classmethod
    def _seed_catalog(cls) -> None:
        _, issuer = cls.rv.request("POST", "/admin/issuers",
                                   {"name": "A 研修平台", "kind": "线上平台", "org_id": "ORG_A"})
        cls.issuer_id = issuer["issuer_id"]
        for gid, title in [("G1", "数字化教学设计"), ("G2", "企业实践转化")]:
            assert cls.rv.request("POST", "/admin/goals", {"goal_id": gid, "title": title})[0] == 200
        _, unit = cls.rv.request("POST", "/admin/training-units", {
            "code": "ONLINE-1", "title": "线上研修课", "category": "线上研修",
            "goal_id": "G1", "default_hours": 10})
        cls.unit_id = unit["unit_id"]
        _, rs = cls.rv.request("POST", "/admin/rulesets", {
            "name": "规则", "version": "2026.1",
            "goals": [
                {"goal_id": "G1", "title": "数字化教学设计", "required_hours": 10, "required_evidence_count": 1},
                {"goal_id": "G2", "title": "企业实践转化", "required_hours": 8, "required_evidence_count": 1},
            ]})
        cls.rs_id = rs["ruleset_id"]
        assert cls.rv.request("POST", f"/admin/rulesets/{cls.rs_id}/publish",
                              {"effective_from": "2026-01-01"})[0] == 200
        assert cls.rv.request("POST", "/admin/teachers",
                              {"teacher_id": "T001", "name": "张老师"})[0] == 200

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.app.close()

    def test_01_auth_required(self) -> None:
        st, resp = self.anon.request("GET", "/catalog")
        self.assertEqual(st, 401)
        self.assertEqual(resp["error"]["code"], "UNAUTHENTICATED")

    def test_02_full_flow_gap_then_qualified(self) -> None:
        # 教师提交证明
        st, ev = self.t1.request("POST", "/evidences", {
            "teacher_id": "T001", "unit_id": self.unit_id, "issuer_id": self.issuer_id,
            "hours": 12, "issued_on": "2026-03-01", "external_ref": "CERT-1"})
        self.assertEqual(st, 200)
        evidence_id = ev["evidence_id"]
        self.assertEqual(ev["status"], "SUBMITTED")

        # 教师不能自己验证
        st, resp = self.t1.request("POST", f"/evidences/{evidence_id}/verify", {"decision": "ACCEPT"})
        self.assertEqual(st, 403)
        # 签发机构验证通过
        st, resp = self.iss.request("POST", f"/evidences/{evidence_id}/verify", {"decision": "ACCEPT"})
        self.assertEqual(st, 200, resp)
        self.assertEqual(resp["status"], "ACCEPTED")

        # 重复提交同一外部凭证号 -> 自动标记重复
        st, dup = self.t1.request("POST", "/evidences", {
            "teacher_id": "T001", "unit_id": self.unit_id, "issuer_id": self.issuer_id,
            "hours": 12, "issued_on": "2026-03-01", "external_ref": "CERT-1"})
        self.assertEqual(dup["status"], "DUPLICATE")

        # 首次判定：G2 缺证据，不合格并给出解释
        st, evaluation = self.rv.request("POST", "/evaluations", {"teacher_id": "T001"})
        self.assertEqual(st, 200)
        self.assertEqual(evaluation["result"], "NOT_QUALIFIED")
        self.assertEqual([g["goal_id"] for g in evaluation["gaps"]], ["G2"])
        self.assertIn("企业实践转化", evaluation["explanation"]["summary"])
        # 每份证据都有贡献说明
        self.assertTrue(any(c["items"] for c in evaluation["contributions"]))

        # 教师可读本人判定与组合
        st, portfolio = self.t1.request("GET", "/teachers/T001/portfolio")
        self.assertEqual(st, 200)
        self.assertEqual(portfolio["current_evaluation"]["result"], "NOT_QUALIFIED")

        # 申诉：缺少 G2 证据的情况下只能维持原判定
        st, appeal = self.t1.request("POST", "/appeals",
                                     {"teacher_id": "T001", "reason": "学时已够为何不合格"})
        self.assertEqual(st, 200)
        st, reviewed = self.rv.request("POST", f"/appeals/{appeal['appeal_id']}/review",
                                       {"decision": "UPHELD", "note": "G2 无证据，缺口解释无误"})
        self.assertEqual(st, 200)
        self.assertEqual(reviewed["status"], "UPHELD")

        # 历史接口对教师开放，原始审计日志对教师关闭
        st, hist = self.t1.request("GET", f"/evidences/{evidence_id}/history")
        self.assertEqual(st, 200)
        self.assertEqual(len(hist["events"]), 2)
        st, resp = self.t1.request("GET", "/history")
        self.assertEqual(st, 403)
        st, audit = self.rv.request("GET", "/history?aggregate=evidence")
        self.assertEqual(st, 200)
        self.assertGreaterEqual(audit["count"], 2)

    def test_03_org_isolation_issuer_only_sees_own(self) -> None:
        st, items = self.iss.request("GET", "/evidences")
        self.assertEqual(st, 200)
        self.assertTrue(items["items"])
        self.assertTrue(all(i["issuer"]["org_id"] == "ORG_A" for i in items["items"]))


if __name__ == "__main__":
    unittest.main()
