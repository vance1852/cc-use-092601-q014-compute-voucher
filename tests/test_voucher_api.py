from __future__ import annotations

import json
import sqlite3
import unittest

from voucher_settlement.api import JsonApplication
from voucher_settlement.service import VoucherService


class VoucherApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.app = JsonApplication(VoucherService(self.connection))
        for user_id, role, extra in (
            ("city-admin", "sponsor_admin", {"sponsor_id": "city"}),
            ("ops", "operator", {}),
            ("reviewer", "reviewer", {}),
            ("tenant-a", "tenant", {"tenant_id": "tenant-a"}),
            ("audit", "auditor", {}),
        ):
            self.post("/users", {
                "user_id": user_id, "display_name": user_id, "role": role, **extra,
            })
        self.post("/voucher-batches", {
            "batch_id": "city-q3", "sponsor_id": "city", "name": "市级券",
            "eligible_tenants": ["tenant-a"], "scope_kind": "any",
            "scope_products": [], "scope_facilities": [],
            "valid_from": "2026-07-01", "valid_until": "2026-12-31",
            "total_cap_cny": "20000.00", "per_job_cap_cny": "3000.00",
            "cover_percent": "60", "priority": 10,
        }, actor="city-admin")

    def tearDown(self) -> None:
        self.connection.close()

    def request(self, method: str, path: str, payload=None, actor: str = "ops"):
        body = json.dumps(payload).encode() if payload is not None else b""
        headers = {"X-Actor-Id": actor} if actor else {}
        return self.app.handle(method, path, headers, body)

    def post(self, path: str, payload, actor: str = "ops"):
        return self.request("POST", path, payload, actor).body

    def test_health(self) -> None:
        response = self.app.handle("GET", "/health")
        self.assertEqual(response.status, 200)
        self.assertEqual(response.body["status"], "ok")

    def test_confirm_settle_review_flow_over_http(self) -> None:
        confirmed = self.app.handle("POST", "/jobs/confirm", {"X-Actor-Id": "ops"}, json.dumps({
            "job_id": "j1", "tenant_id": "tenant-a", "facility_id": "cluster-a",
            "product": "gpu-h100", "service_date": "2026-09-26",
            "estimated_cost_cny": "5000.00", "idempotency_key": "k1",
        }).encode())
        self.assertEqual(confirmed.status, 201)
        self.assertEqual(confirmed.body["allocation"]["total_sponsored_cny"], "3000.00")
        settled = self.request("POST", "/jobs/settle", {
            "job_id": "j1", "actual_cost_cny": "5000.00",
            "result": "completed", "idempotency_key": "s1",
        })
        self.assertEqual(settled.status, 200)

    def test_missing_actor_is_rejected(self) -> None:
        response = self.app.handle("GET", "/sponsor/report")
        self.assertEqual(response.status, 422)
        self.assertEqual(response.body["error"]["code"], "validation_failed")

    def test_tenant_forbidden_on_sponsor_report(self) -> None:
        response = self.request("GET", "/sponsor/report", actor="tenant-a")
        self.assertEqual(response.status, 403)
        self.assertEqual(response.body["error"]["code"], "forbidden")


if __name__ == "__main__":
    unittest.main()
