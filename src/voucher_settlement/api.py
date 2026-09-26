"""无第三方依赖的算力券核销 HTTP JSON 接口。"""

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
    body: Mapping[str, Any] | list[Any]


class JsonApplication:
    def __init__(self, service: VoucherService) -> None:
        self.service = service
        # SQLite 连接跨线程共享，串行化请求处理以保证事务边界不被交错。
        self._lock = threading.Lock()

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

    def handle(
        self,
        method: str,
        target: str,
        headers: Mapping[str, str] | None = None,
        body: bytes = b"",
    ) -> Response:
        normalized = {key.lower(): value for key, value in (headers or {}).items()}
        parsed = urlparse(target)
        path = parsed.path.rstrip("/") or "/"
        parts = [part for part in path.split("/") if part]
        try:
            if method == "GET" and path == "/health":
                return Response(200, {"status": "ok"})
            payload = self._json(body) if method in {"POST", "PUT", "PATCH"} else {}
            # 用户登记是引导操作（首个用户尚不存在），无需调用方身份。
            if method == "POST" and path == "/users":
                return Response(
                    201,
                    self.service.create_user(
                        payload["user_id"],
                        payload["display_name"],
                        payload["role"],
                        sponsor_id=payload.get("sponsor_id"),
                        tenant_id=payload.get("tenant_id"),
                    ),
                )
            actor = self._actor(normalized)
            service = self.service
            if method == "POST" and path == "/voucher-batches":
                return Response(201, service.register_batch(actor, payload))
            if method == "GET" and len(parts) == 2 and parts[0] == "voucher-batches":
                return Response(200, service.get_batch_view(actor, parts[1]))
            if method == "GET" and len(parts) == 3 and parts[0] == "voucher-batches" and parts[2] == "versions":
                return Response(200, {"versions": service.list_rule_versions(actor, parts[1])})
            if method == "POST" and len(parts) == 3 and parts[0] == "voucher-batches" and parts[2] == "revisions":
                body_with_id = dict(payload, batch_id=parts[1])
                return Response(201, service.revise_rule(actor, body_with_id, payload.get("change_note", "")))
            if method == "POST" and path == "/jobs/confirm":
                return Response(201, service.confirm_job(actor, payload))
            if method == "POST" and path == "/jobs/settle":
                return Response(200, service.settle_job(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "jobs" and parts[2] == "cancel":
                return Response(200, service.cancel_job(actor, parts[1], payload["idempotency_key"]))
            if method == "GET" and len(parts) == 2 and parts[0] == "jobs":
                return Response(200, service.job_detail(actor, parts[1]))
            if method == "GET" and path == "/tenant/carry-balances":
                return Response(200, {"balances": service.tenant_carry_balances(actor)})
            if method == "GET" and path == "/sponsor/report":
                return Response(200, service.sponsor_report(actor))
            if method == "POST" and path == "/adjustments":
                return Response(201, service.request_adjustment(actor, payload))
            if method == "POST" and len(parts) == 3 and parts[0] == "adjustments" and parts[2] == "review":
                return Response(
                    200,
                    service.review_adjustment(
                        actor, int(parts[1]), bool(payload["approve"]), payload.get("note", "")
                    ),
                )
            if method == "GET" and path == "/audit/chain":
                return Response(200, service.audit_chain(actor))
            return Response(404, {"error": {"code": "route_not_found", "message": "接口不存在"}})
        except VoucherError as exc:
            return Response(exc.status, {"error": {"code": exc.code, "message": str(exc)}})
        except (KeyError, TypeError, ValueError) as exc:
            return Response(422, {"error": {"code": "invalid_request", "message": str(exc)}})


def make_handler(application: JsonApplication):
    class Handler(BaseHTTPRequestHandler):
        server_version = "VoucherSettlement/1"

        def do_GET(self) -> None:  # noqa: N802
            self._dispatch()

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch()

        def _dispatch(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            body = self.rfile.read(length) if length else b""
            with application._lock:
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
    parser.add_argument("--database", type=Path, default=Path("voucher_settlement.sqlite3"))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8083)
    args = parser.parse_args(argv)
    connection = connect(args.database)
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
