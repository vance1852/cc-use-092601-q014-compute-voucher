"""无第三方依赖的算力券核销与联合资助分摊 HTTP JSON 接口。"""

from __future__ import annotations

import argparse
import json
import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlparse

from .errors import VoucherError, ValidationFailed
from .service import VoucherService
from .storage import connect


@dataclass(frozen=True, slots=True)
class Response:
    status: int
    body: Mapping[str, Any]


class JsonApplication:
    def __init__(self, service: VoucherService) -> None:
        self.service = service

    @staticmethod
    def _actor(headers: Mapping[str, str]) -> str:
        actor = headers.get("x-actor-id", "").strip()
        if not actor:
            raise ValidationFailed("缺少 X-Actor-Id")
        return actor

    @staticmethod
    def _json(body: bytes) -> dict[str, Any]:
        if not body:
            return {}
        try:
            value = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValidationFailed("请求体必须是 UTF-8 JSON 对象") from exc
        if not isinstance(value, dict):
            raise ValidationFailed("请求体必须是 JSON 对象")
        return value

    def handle(self, method: str, target: str, headers: Mapping[str, str] | None = None, body: bytes = b"") -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            actor = self._actor(normalized)
            if method == "POST" and path == "/users":
                return Response(
                    201,
                    self.service.create_user(
                        payload["user_id"], payload["display_name"], payload["role"], payload.get("org_id", "platform")
                    ),
                )
            if method == "POST" and path == "/batches":
                return Response(201, self.service.register_batch(actor, payload))
            if method == "GET" and path == "/batches":
                return Response(200, self.service.list_batches(actor))
            if method == "GET" and len(parts) == 3 and parts[0] == "batches" and parts[2] == "ledger":
                return Response(200, self.service.batch_ledger(actor, parts[1]))
            if method == "POST" and path == "/rules":
                return Response(
                    201, self.service.publish_rule(actor, payload["tenant_id"], payload["layers"], payload.get("note", ""))
                )
            if method == "GET" and len(parts) == 2 and parts[0] == "rules":
                return Response(200, self.service.rule_history(actor, parts[1]))
            if method == "POST" and path == "/jobs":
                return Response(201, self.service.submit_job(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "confirm":
                return Response(200, self.service.confirm_job(actor, parts[1], payload["idempotency_key"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "settle":
                return Response(
                    200,
                    self.service.settle_job(
                        actor, parts[1], payload["outcome"], payload["actual_cost_cny"], payload["idempotency_key"]
                    ),
                )
            if method == "GET" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "statement":
                return Response(200, self.service.job_statement(actor, parts[1]))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "adjustments":
                return Response(201, self.service.propose_adjustment(actor, parts[1], payload["lines"], payload["reason"]))
            if method == "POST" and len(parts) == 3 and parts[0] == "adjustments" and parts[2] == "review":
                return Response(200, self.service.review_adjustment(actor, int(parts[1]), bool(payload["approve"])))
            if method == "GET" and path == "/audit/chain":
                return Response(200, self.service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except VoucherError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    # 单连接嵌入式部署：请求串行处理，避免多线程并发使用同一 SQLite 连接。
    dispatch_lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        server_version = "VoucherFund/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with dispatch_lock:
                response = application.handle(self.command, self.path, dict(self.headers.items()), body)
            encoded = json.dumps(response.body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(response.status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:
            return

    return Handler


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="启动算力券核销与联合资助分摊服务")
    parser.add_argument("--database", type=Path, default=Path("voucher_fund.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database, check_same_thread=False)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(JsonApplication(VoucherService(connection))))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        connection.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
