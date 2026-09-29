"""HTTP API 端到端测试（真实套接字上的 JSON 请求）。"""
from __future__ import annotations

import http.client
import json
import sys
import threading
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from portfolio_backend.api import make_server
from portfolio_backend.database import Database
from portfolio_backend.seed import seed


class ApiClient:
    def __init__(self, host: str, port: int) -> None:
        self.host, self.port = host, port

    def call(self, method: str, path: str, body: dict | None = None, user: str | None = None):
        conn = http.client.HTTPConnection(self.host, self.port, timeout=5)
        headers = {"Content-Type": "application/json"}
        if user:
            headers["X-User-Id"] = user
        raw = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        conn.request(method, path, body=raw, headers=headers)
        resp = conn.getresponse()
        data = json.loads(resp.read().decode("utf-8"))
        conn.close()
        return resp.status, data


class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.db = Database(":memory:")
        seed(cls.db)
        cls.server = make_server("127.0.0.1", 0, cls.db)
        cls.port = cls.server.server_address[1]
        cls.thread = threading.Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()
        cls.api = ApiClient("127.0.0.1", cls.port)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()
        cls.db.close()

    def test_health_and_auth(self):
        status, data = self.api.call("GET", "/health")
        self.assertEqual(status, 200)
        status, data = self.api.call("GET", "/reviewer/teachers")
        self.assertEqual(status, 401)

    def test_wang_evaluation_explains_gap_and_contributions(self):
        status, data = self.api.call(
            "POST", "/teachers/t_wang/evaluations", {"persist": False}, user="r_chen"
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["decision"], "不合格")
        self.assertTrue(data["gaps"])
        g3 = {g["goal_code"]: g for g in data["goals"]}["G3"]
        self.assertEqual(g3["total_hours"], 0)
        # 每份有效证据都有去向解释
        for e in data["evidence"]:
            if e["status"] == "验证":
                self.assertTrue(
                    e["decision_points"] or e["excluded_reason"],
                    f"证据 {e['evidence_id']} 缺少贡献或排除原因",
                )

    def test_org_isolation_enforced_over_http(self):
        # 第二中心的审核员不能看王老师
        status, data = self.api.call("GET", "/teachers/t_wang/portfolio", user="r_zhao")
        self.assertEqual(status, 403)
        self.assertEqual(data["code"], "permission_denied")
        # 签发方不能看组合
        status, _ = self.api.call("GET", "/teachers/t_wang/portfolio", user="s_univ")
        self.assertEqual(status, 403)

    def test_teacher_submits_duplicate_then_issuer_flow(self):
        status, first = self.api.call(
            "POST", "/teachers/t_wang/evidences",
            {"unit_code": "online_study", "issuer_id": "iss_univ",
             "hours": 4, "issued_on": "2026-06-01", "external_ref": "DUP-HTTP-1"},
            user="t_wang",
        )
        self.assertEqual(status, 200, first)
        status, second = self.api.call(
            "POST", "/teachers/t_wang/evidences",
            {"unit_code": "online_study", "issuer_id": "iss_univ",
             "hours": 4, "issued_on": "2026-06-01", "external_ref": "DUP-HTTP-1"},
            user="t_wang",
        )
        self.assertEqual(status, 200)
        self.assertEqual(second["status"], "重复提交")
        self.assertEqual(second["duplicate_of"], first["evidence_id"])

        # 其他签发方不能验证
        status, _ = self.api.call(
            "POST", f"/evidences/{first['evidence_id']}/verify", {}, user="s_ent"
        )
        self.assertEqual(status, 403)
        # 正确签发方验证
        status, data = self.api.call(
            "POST", f"/evidences/{first['evidence_id']}/verify", {"note": "ok"}, user="s_univ"
        )
        self.assertEqual(status, 200, data)
        self.assertEqual(data["status"], "验证")

    def test_teacher_cannot_submit_for_others(self):
        status, data = self.api.call(
            "POST", "/teachers/t_li/evidences",
            {"unit_code": "online_study", "issuer_id": "iss_univ",
             "hours": 1, "issued_on": "2026-06-02"},
            user="t_wang",
        )
        self.assertEqual(status, 403)

    def test_appeal_and_review_http(self):
        # 李老师的实践证据可再发起一次申诉并追加认定（历史追加，不影响既有结论记录）
        status, portfolio = self.api.call("GET", "/teachers/t_li/portfolio", user="t_li")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(portfolio["history"]), 10)
        status, appeal = self.api.call(
            "POST", "/teachers/t_li/appeals",
            {"reason": "HTTP 端到端申诉", "evidence_id": None}, user="t_li",
        )
        self.assertEqual(status, 200, appeal)
        status, review = self.api.call(
            "POST", f"/appeals/{appeal['appeal_id']}/review",
            {"uphold": False, "decision_note": "不予追加"}, user="r_chen",
        )
        self.assertEqual(status, 200)
        self.assertEqual(review["status"], "申诉驳回")

    def test_evaluation_history_http(self):
        status, data = self.api.call("GET", "/teachers/t_li/evaluations", user="r_chen")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(len(data["evaluations"]), 2)
        decisions = {e["decision"] for e in data["evaluations"]}
        self.assertEqual(decisions, {"不合格", "合格"})

    def test_admin_audit_chain(self):
        status, data = self.api.call("GET", "/audit/chain", user="u_admin")
        self.assertEqual(status, 200)
        self.assertTrue(data["ok"])
        status, _ = self.api.call("GET", "/audit/chain", user="r_chen")
        self.assertEqual(status, 403)

    def test_frozen_rules_reject_edits(self):
        status, data = self.api.call(
            "POST", "/admin/rule-sets/standard/versions/1/goals",
            {"goal_code": "GX", "goal_name": "越权新增", "required_hours": 1},
            user="u_admin",
        )
        self.assertEqual(status, 409)

    def test_bad_json(self):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        conn.request(
            "POST", "/admin/units", body="{not-json",
            headers={"Content-Type": "application/json", "X-User-Id": "u_admin"},
        )
        resp = conn.getresponse()
        self.assertEqual(resp.status, 400)
        resp.read()
        conn.close()


if __name__ == "__main__":
    unittest.main()
