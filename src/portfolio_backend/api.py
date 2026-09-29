"""基于标准库 http.server 的 JSON HTTP 接口。

鉴权：所有请求需带 ``X-User-Id`` 头，对应用户表中的用户；
具体能做什么由服务层按角色与机构强制（见 services 模块文档）。

启动：

    python -m portfolio_backend.api --db portfolio.sqlite3 --port 8080

加 ``--seed`` 写入演示数据。
"""
from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse

from .database import Database
from .domain import DomainError, Role
from .seed import seed
from .services import Service


def _pattern(method: str, path_re: str):
    return method, re.compile(path_re)


class Api:
    """路由与处理逻辑（与 HTTP 传输解耦，便于直接调用测试）。"""

    def __init__(self, service: Service) -> None:
        self.svc = service
        # (method, compiled_path) -> (handler_name, param_names)
        self.routes = [
            _pattern("POST", r"^/admin/organizations$"),
            _pattern("POST", r"^/admin/users$"),
            _pattern("POST", r"^/admin/units$"),
            _pattern("POST", r"^/admin/issuer-authorizations$"),
            _pattern("POST", r"^/admin/rule-sets$"),
            _pattern("POST", r"^/admin/rule-sets/(?P<rule_set>[^/]+)/versions$"),
            _pattern("POST", r"^/admin/rule-sets/(?P<rule_set>[^/]+)/versions/(?P<version>\d+)/goals$"),
            _pattern("POST", r"^/admin/rule-sets/(?P<rule_set>[^/]+)/versions/(?P<version>\d+)/unit-goals$"),
            _pattern("POST", r"^/admin/rule-sets/(?P<rule_set>[^/]+)/versions/(?P<version>\d+)/substitutions$"),
            _pattern("POST", r"^/admin/rule-sets/(?P<rule_set>[^/]+)/versions/(?P<version>\d+)/publish$"),
            _pattern("GET", r"^/rule-sets/(?P<rule_set>[^/]+)/versions/(?P<version>\d+)$"),
            _pattern("POST", r"^/admin/enrollments$"),
            _pattern("POST", r"^/teachers/(?P<teacher_id>[^/]+)/evidences$"),
            _pattern("GET", r"^/teachers/(?P<teacher_id>[^/]+)/portfolio$"),
            _pattern("POST", r"^/teachers/(?P<teacher_id>[^/]+)/evaluations$"),
            _pattern("GET", r"^/teachers/(?P<teacher_id>[^/]+)/evaluations$"),
            _pattern("POST", r"^/teachers/(?P<teacher_id>[^/]+)/appeals$"),
            _pattern("POST", r"^/evidences/(?P<evidence_id>[^/]+)/(?P<action>verify|reject|revoke)$"),
            _pattern("POST", r"^/appeals/(?P<appeal_id>[^/]+)/review$"),
            _pattern("GET", r"^/reviewer/teachers$"),
            _pattern("GET", r"^/issuer/evidences$"),
            _pattern("GET", r"^/events$"),
            _pattern("GET", r"^/audit/chain$"),
            _pattern("GET", r"^/health$"),
        ]

    def match(self, method: str, path: str):
        for m, rx in self.routes:
            if m != method:
                continue
            mt = rx.match(path)
            if mt:
                return mt.groupdict()
        return None

    def dispatch(self, method: str, path: str, body: dict, principal_id: str | None) -> tuple[int, dict]:
        if path == "/health":
            return 200, {"status": "ok"}
        if not principal_id:
            return 401, {"code": "unauthorized", "message": "缺少 X-User-Id 请求头"}
        principal = self.svc.principal(principal_id)
        params = self.match(method, path)
        if params is None:
            return 404, {"code": "not_found", "message": f"无此接口：{method} {path}"}
        params = {k: (int(v) if v.isdigit() else v) for k, v in params.items()}

        try:
            return 200, self._handle(method, path, principal, body, params)
        except DomainError as exc:
            status = {
                "not_found": 404,
                "conflict": 409,
                "permission_denied": 403,
                "rule_error": 422,
            }.get(exc.code, 400)
            return status, exc.to_dict()

    # -- 处理函数 ---------------------------------------------------------

    def _handle(self, method, path, principal, body, params) -> dict:
        s = self.svc
        if path == "/admin/organizations":
            return s.create_organization(principal, body["org_id"], body["name"], body["kind"])
        if path == "/admin/users":
            return s.create_user(
                principal, body["user_id"], body["name"], Role(body["role"]), body.get("org_id")
            )
        if path == "/admin/units":
            return s.register_unit(principal, body["unit_code"], body["unit_name"], body["category"])
        if path == "/admin/issuer-authorizations":
            return s.authorize_issuer(principal, body["issuer_id"], body["unit_code"])
        if path == "/admin/rule-sets":
            return s.create_rule_set(principal, body["rule_set"], body["name"])
        if method == "POST" and path.endswith("/versions"):
            return s.create_rule_version(principal, params["rule_set"], body.get("note", ""))
        if path.endswith("/goals"):
            return s.add_goal(
                principal, params["rule_set"], params["version"],
                body["goal_code"], body["goal_name"], float(body["required_hours"]),
                int(body.get("sort_order", 0)),
            )
        if path.endswith("/unit-goals"):
            return s.map_unit_goal(
                principal, params["rule_set"], params["version"],
                body["unit_code"], body["goal_code"], float(body.get("weight", 1.0)),
            )
        if path.endswith("/substitutions"):
            return s.add_substitution(
                principal, params["rule_set"], params["version"],
                body["from_unit"], body["to_unit"],
                float(body["ratio"]), float(body["cap_ratio"]),
            )
        if path.endswith("/publish"):
            return s.publish_rule_version(principal, params["rule_set"], params["version"])
        if method == "GET" and path.startswith("/rule-sets/"):
            return s.get_rule_version(params["rule_set"], params["version"])
        if path == "/admin/enrollments":
            return s.enroll_teacher(
                principal, body["teacher_id"], body["org_id"],
                body["rule_set"], body.get("rule_version"),
            )
        if path.startswith("/teachers/"):
            tid = params["teacher_id"]
            if path.endswith("/evidences"):
                return s.submit_evidence(
                    principal, tid, body["unit_code"], body["issuer_id"],
                    float(body["hours"]), body["issued_on"], body.get("external_ref", ""),
                )
            if path.endswith("/portfolio"):
                return s.get_portfolio(principal, tid)
            if path.endswith("/evaluations"):
                if method == "GET":
                    return {"evaluations": s.list_evaluations(principal, tid)}
                return s.evaluate_teacher(principal, tid, persist=bool(body.get("persist", True)))
            if path.endswith("/appeals"):
                return s.file_appeal(
                    principal, tid, body["reason"], body.get("evidence_id")
                )
        if method == "POST" and path.startswith("/evidences/"):
            action = params["action"]
            eid = params["evidence_id"]
            if action == "verify":
                return s.verify_evidence(principal, eid, body.get("note", ""))
            if action == "reject":
                return s.reject_evidence(principal, eid, body["reason"])
            return s.revoke_evidence(principal, eid, body["reason"])
        if path.startswith("/appeals/") and path.endswith("/review"):
            return s.review_appeal(
                principal, params["appeal_id"], bool(body["uphold"]),
                body["decision_note"], body.get("credits"),
            )
        if path == "/reviewer/teachers":
            return {"teachers": s.list_teachers_for_reviewer(principal)}
        if path == "/issuer/evidences":
            return {"evidences": s.list_evidence_for_issuer(principal)}
        if path == "/events":
            return {"events": s.list_events(principal)}
        if path == "/audit/chain":
            if principal.role is not Role.ADMIN:
                from .domain import PermissionDenied

                raise PermissionDenied("仅平台管理员可执行链审计")
            return self.svc.db.verify_chain()
        raise DomainError("未实现的接口")  # pragma: no cover


class _Handler(BaseHTTPRequestHandler):
    api: Api = None  # 由 make_server 注入

    def _send(self, status: int, payload: dict) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _do(self) -> None:
        parsed = urlparse(self.path)
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b""
        body: dict = {}
        if raw:
            try:
                body = json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError:
                self._send(400, {"code": "bad_json", "message": "请求体不是合法 JSON"})
                return
        try:
            # 与写事务共用同一把可重入锁，串行化所有数据库访问。
            with self.api.svc.db.lock:
                status, payload = self.api.dispatch(
                    self.command, parsed.path, body, self.headers.get("X-User-Id")
                )
        except Exception as exc:  # pragma: no cover - 兜底
            self._send(500, {"code": "internal_error", "message": str(exc)})
            return
        self._send(status, payload)

    do_GET = _do
    do_POST = _do

    def log_message(self, fmt, *args):  # noqa: A003 - 安静一点的访问日志
        pass


def make_server(host: str, port: int, db: Database) -> ThreadingHTTPServer:
    api = Api(Service(db))
    _Handler.api = api
    httpd = ThreadingHTTPServer((host, port), _Handler)
    httpd.api = api
    return httpd


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="教师培训证据组合后端")
    parser.add_argument("--db", default="portfolio.sqlite3", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--seed", action="store_true", help="写入演示数据后启动")
    args = parser.parse_args(argv)

    db = Database(args.db)
    if args.seed:
        seed(db)
        print(f"演示数据已写入 {args.db}")
    server = make_server(args.host, args.port, db)
    print(f"服务监听 http://{args.host}:{args.port}（X-User-Id 头标识操作者）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        db.close()


if __name__ == "__main__":
    main()
