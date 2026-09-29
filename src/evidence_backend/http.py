"""HTTP 接口：仅依赖标准库的 JSON API（Bearer 令牌认证）。"""
from __future__ import annotations

import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable

from .domain import aggregates as agg
from .errors import AuthenticationError, DomainError
from .event_store import EventStore
from .identity import AuthService, Principal
from .repository import AggregateRepo
from .services import (
    AppealService,
    EvaluationService,
    EvidenceService,
    QueryService,
    RegistryService,
)


class Application:
    """服务依赖容器。"""

    def __init__(self, db_path: str = ":memory:") -> None:
        self.store = EventStore(db_path)
        self.auth = AuthService(self.store.connection)
        self.repo = AggregateRepo(self.store)
        self.registries = RegistryService(self.store, self.repo)
        self.evidences = EvidenceService(self.store, self.repo, self.registries)
        self.evaluations = EvaluationService(self.store, self.repo, self.registries)
        self.appeals = AppealService(self.store, self.repo, self.evaluations)
        self.queries = QueryService(self.store, self.repo)

    def close(self) -> None:
        self.store.close()


Route = tuple[str, re.Pattern[str], Callable[..., Any]]


class ApiHandler(BaseHTTPRequestHandler):
    app: Application

    server_version = "EvidenceBackend/0.1"

    def log_message(self, fmt: str, *args: Any) -> None:  # 安静的测试日志
        if self.server.app_verbose:  # type: ignore[attr-defined]
            super().log_message(fmt, *args)

    # ---- 工具方法 -------------------------------------------------------

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise DomainError("请求体不是合法 JSON") from exc
        if not isinstance(body, dict):
            raise DomainError("请求体必须是 JSON 对象")
        return body

    def _write_json(self, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _principal(self) -> Principal:
        header = self.headers.get("Authorization", "")
        token = header.removeprefix("Bearer ").strip() if header.startswith("Bearer ") else None
        return self.app.auth.authenticate(token)

    # ---- 路由 -----------------------------------------------------------

    def _dispatch(self, method: str) -> None:
        path = self.path.split("?", 1)[0].rstrip("/") or "/"
        query = self.path.split("?", 1)[1] if "?" in self.path else ""
        routes: list[Route] = ROUTES.get(method, [])
        for pattern, handler in routes:
            match = pattern.fullmatch(path)
            if match:
                try:
                    payload = handler(self, query=query, **match.groupdict())
                except DomainError as exc:
                    self._write_json(
                        exc.http_status,
                        {"error": {"code": exc.code, "message": str(exc)}},
                    )
                    return
                if payload is None:
                    payload = {"ok": True}
                self._write_json(200, payload)
                return
        self._write_json(404, {"error": {"code": "NOT_FOUND", "message": f"无此接口：{method} {path}"}})

    def do_GET(self) -> None:
        self._safe_dispatch("GET")

    def do_POST(self) -> None:
        self._safe_dispatch("POST")

    def _safe_dispatch(self, method: str) -> None:
        try:
            self._dispatch(method)
        except AuthenticationError as exc:  # 认证在 handler 内抛出时兜底
            self._write_json(exc.http_status, {"error": {"code": exc.code, "message": str(exc)}})
        except BrokenPipeError:  # pragma: no cover
            pass

    # ---- 认证与账户 -----------------------------------------------------

    def login(self, query: str) -> dict[str, Any]:
        body = self._read_json()
        token = self.app.auth.issue_token(body["actor_id"], body["secret"])
        p = self.app.auth.authenticate(token)
        return {"token": token, "actor_id": p.actor_id, "role": p.role, "org_id": p.org_id}

    def create_account(self, query: str) -> dict[str, Any]:
        p = self._principal()
        p.require_role(agg.ROLE_ADMIN, agg.ROLE_REVIEWER)
        body = self._read_json()
        self.app.auth.create_account(
            body["actor_id"], body["role"], body["secret"], body.get("org_id", "")
        )
        return {"ok": True, "actor_id": body["actor_id"], "role": body["role"]}

    # ---- 登记 -----------------------------------------------------------

    def register_issuer(self, query: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.registries.register_issuer(
            p, name=b["name"], kind=b.get("kind", ""), org_id=b["org_id"],
            contact=b.get("contact", ""),
        )

    def issuer_status(self, query: str, issuer_id: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.registries.change_issuer_status(p, issuer_id, b["status"])

    def register_goal(self, query: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.registries.register_goal(
            p, goal_id=b["goal_id"], title=b["title"], description=b.get("description", "")
        )

    def register_unit(self, query: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.registries.register_training_unit(
            p, code=b["code"], title=b["title"], category=b["category"],
            goal_id=b["goal_id"], default_hours=b["default_hours"],
        )

    def retire_unit(self, query: str, unit_id: str) -> dict[str, Any]:
        return self.app.registries.retire_training_unit(self._principal(), unit_id)

    def register_ruleset(self, query: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.registries.register_ruleset(
            p, name=b["name"], version=b["version"], goals=b["goals"],
            substitutions=b.get("substitutions"),
        )

    def publish_ruleset(self, query: str, ruleset_id: str) -> dict[str, Any]:
        b = self._read_json()
        return self.app.registries.publish_ruleset(
            self._principal(), ruleset_id, b.get("effective_from")
        )

    def deprecate_ruleset(self, query: str, ruleset_id: str) -> dict[str, Any]:
        return self.app.registries.deprecate_ruleset(self._principal(), ruleset_id)

    def applicable_ruleset(self, query: str) -> dict[str, Any]:
        self._principal()
        params = _parse_query(query)
        return self.app.registries.applicable_ruleset(params.get("as_of"))

    def register_teacher(self, query: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.registries.register_teacher(
            p, teacher_id=b["teacher_id"], name=b["name"], org_id=b.get("org_id", "")
        )

    # ---- 证据 -----------------------------------------------------------

    def submit_evidence(self, query: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.evidences.submit(
            p, teacher_id=b["teacher_id"], unit_id=b["unit_id"], issuer_id=b["issuer_id"],
            hours=b["hours"], issued_on=b["issued_on"], external_ref=b.get("external_ref", ""),
        )

    def verify_evidence(self, query: str, evidence_id: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.evidences.verify(
            p, evidence_id, decision=b["decision"], note=b.get("note", ""),
            hours=b.get("hours"),
        )

    def revoke_evidence(self, query: str, evidence_id: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.evidences.revoke(p, evidence_id, reason=b["reason"])

    def reinstate_evidence(self, query: str, evidence_id: str) -> dict[str, Any]:
        return self.app.evidences.reinstate(
            self._principal(), evidence_id, note=self._read_json().get("note", "")
        )

    def list_evidences(self, query: str) -> dict[str, Any]:
        p = self._principal()
        params = _parse_query(query)
        items = self.app.queries.list_evidences(p, params.get("teacher_id"))
        return {"count": len(items), "items": items}

    def get_evidence(self, query: str, evidence_id: str) -> dict[str, Any]:
        return self.app.queries.get_evidence(self._principal(), evidence_id)

    def evidence_history(self, query: str, evidence_id: str) -> dict[str, Any]:
        events = self.app.queries.evidence_history(self._principal(), evidence_id)
        return {"evidence_id": evidence_id, "events": events}

    # ---- 判定与申诉 -----------------------------------------------------

    def create_evaluation(self, query: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.evaluations.evaluate(
            p, b["teacher_id"], ruleset_id=b.get("ruleset_id"),
            reason=b.get("reason", ""),
        )

    def portfolio(self, query: str, teacher_id: str) -> dict[str, Any]:
        return self.app.queries.portfolio(self._principal(), teacher_id)

    def evaluation_history(self, query: str, teacher_id: str) -> dict[str, Any]:
        items = self.app.queries.evaluation_history(self._principal(), teacher_id)
        return {"teacher_id": teacher_id, "count": len(items), "items": items}

    def open_appeal(self, query: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.appeals.open_appeal(p, teacher_id=b["teacher_id"], reason=b["reason"])

    def review_appeal(self, query: str, appeal_id: str) -> dict[str, Any]:
        p = self._principal()
        b = self._read_json()
        return self.app.appeals.review_appeal(
            p, appeal_id, decision=b["decision"], note=b["note"],
            ruleset_id=b.get("ruleset_id"),
        )

    def list_appeals(self, query: str) -> dict[str, Any]:
        p = self._principal()
        params = _parse_query(query)
        items = self.app.queries.list_appeals(p, params.get("teacher_id"))
        return {"count": len(items), "items": items}

    def get_appeal(self, query: str, appeal_id: str) -> dict[str, Any]:
        return self.app.queries.get_appeal(self._principal(), appeal_id)

    # ---- 目录与审计 -----------------------------------------------------

    def catalog(self, query: str) -> dict[str, Any]:
        self._principal()
        return self.app.queries.catalog()

    def raw_history(self, query: str) -> dict[str, Any]:
        """审核员专用：查看原始事件流（只追加日志）。"""
        p = self._principal()
        p.require_reviewer()
        params = _parse_query(query)
        aggregate = params.get("aggregate")
        aggregate_id = params.get("id")
        if aggregate and aggregate_id:
            events = self.app.store.events_for(aggregate, aggregate_id)
        else:
            events = self.app.store.all_events(aggregate)
        return {"count": len(events), "events": events}


def _parse_query(query: str) -> dict[str, str]:
    params: dict[str, str] = {}
    for part in query.split("&"):
        if not part:
            continue
        key, _, value = part.partition("=")
        params[key] = value
    return params


def _routes() -> dict[str, list[Route]]:
    p = lambda path: re.compile(r"^/api" + path + r"$")
    gid = lambda name: rf"/(?P<{name}>[A-Za-z0-9_-]+)"
    return {
        "POST": [
            (p("/auth/token"), ApiHandler.login),
            (p("/admin/accounts"), ApiHandler.create_account),
            (p("/admin/issuers"), ApiHandler.register_issuer),
            (p(f"/admin/issuers{gid('issuer_id')}/status"), ApiHandler.issuer_status),
            (p("/admin/goals"), ApiHandler.register_goal),
            (p("/admin/training-units"), ApiHandler.register_unit),
            (p(f"/admin/training-units{gid('unit_id')}/retire"), ApiHandler.retire_unit),
            (p("/admin/rulesets"), ApiHandler.register_ruleset),
            (p(f"/admin/rulesets{gid('ruleset_id')}/publish"), ApiHandler.publish_ruleset),
            (p(f"/admin/rulesets{gid('ruleset_id')}/deprecate"), ApiHandler.deprecate_ruleset),
            (p("/admin/teachers"), ApiHandler.register_teacher),
            (p("/evidences"), ApiHandler.submit_evidence),
            (p(f"/evidences{gid('evidence_id')}/verify"), ApiHandler.verify_evidence),
            (p(f"/evidences{gid('evidence_id')}/revoke"), ApiHandler.revoke_evidence),
            (p(f"/evidences{gid('evidence_id')}/reinstate"), ApiHandler.reinstate_evidence),
            (p("/evaluations"), ApiHandler.create_evaluation),
            (p("/appeals"), ApiHandler.open_appeal),
            (p(f"/appeals{gid('appeal_id')}/review"), ApiHandler.review_appeal),
        ],
        "GET": [
            (p("/catalog"), ApiHandler.catalog),
            (p("/rulesets/applicable"), ApiHandler.applicable_ruleset),
            (p("/evidences"), ApiHandler.list_evidences),
            (p(f"/evidences{gid('evidence_id')}"), ApiHandler.get_evidence),
            (p(f"/evidences{gid('evidence_id')}/history"), ApiHandler.evidence_history),
            (p(f"/teachers{gid('teacher_id')}/portfolio"), ApiHandler.portfolio),
            (p(f"/teachers{gid('teacher_id')}/evaluations"), ApiHandler.evaluation_history),
            (p("/appeals"), ApiHandler.list_appeals),
            (p(f"/appeals{gid('appeal_id')}"), ApiHandler.get_appeal),
            (p("/history"), ApiHandler.raw_history),
        ],
    }


ROUTES = _routes()


def build_server(db_path: str, host: str = "127.0.0.1", port: int = 8080, verbose: bool = False) -> ThreadingHTTPServer:
    app = Application(db_path)

    class _BoundHandler(ApiHandler):
        pass

    _BoundHandler.app = app
    server = ThreadingHTTPServer((host, port), _BoundHandler)
    server.app = app  # type: ignore[attr-defined]
    server.app_verbose = verbose  # type: ignore[attr-defined]
    return server


def main() -> None:
    import argparse

    parser = argparse.ArgumentParser(description="教师培训证据组合后端")
    parser.add_argument("--db", default="evidence.db", help="SQLite 数据库路径")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    server = build_server(args.db, args.host, args.port, args.verbose)
    print(f"服务已启动：http://{args.host}:{args.port} （数据库 {args.db}）")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.app.close()  # type: ignore[attr-defined]


if __name__ == "__main__":
    main()
