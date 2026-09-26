from __future__ import annotations

import json
import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from voucher_fund.api import JsonApplication
from voucher_fund.clock import FrozenClock
from voucher_fund.errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from voucher_fund.ledger import BatchCapacity, LayerSpec, build_plan, compute_settlement
from voucher_fund.service import VoucherService


CITY = "city_voucher"
PARK = "park_subsidy"
SELF = "enterprise_self"


class LedgerTests(unittest.TestCase):
    def test_plan_respects_layer_order_caps_and_fefo(self) -> None:
        batches = [
            BatchCapacity("CITY-B", CITY, "2026-12-31T00:00:00Z", Decimal("300")),
            BatchCapacity("CITY-A", CITY, "2026-10-31T00:00:00Z", Decimal("200")),
            BatchCapacity("PARK-1", PARK, "2026-11-30T00:00:00Z", Decimal("150")),
        ]
        plan = build_plan(
            Decimal("1000"),
            [LayerSpec(CITY, Decimal("50")), LayerSpec(PARK, Decimal("20")), LayerSpec(SELF, None)],
            batches,
        )
        self.assertEqual(
            [(line["batch_id"], line["amount_cny"]) for line in plan["lines"]],
            [("CITY-A", "200.00"), ("CITY-B", "300.00"), ("PARK-1", "150.00"), (None, "350.00")],
        )
        self.assertEqual(plan["voucher_total_cny"], "650.00")
        self.assertEqual(plan["self_pay_cny"], "350.00")
        self.assertTrue(plan["notes"])
        self.assertIn("上限500.00元", plan["lines"][0]["reason"])
        self.assertTrue(all(line["reason"] for line in plan["lines"]))

    def test_plan_shortfall_falls_through_to_self_pay(self) -> None:
        plan = build_plan(
            Decimal("1000"),
            [LayerSpec(CITY, Decimal("50")), LayerSpec(SELF, None)],
            [BatchCapacity("CITY-1", CITY, "2026-10-31T00:00:00Z", Decimal("120"))],
        )
        self.assertEqual(plan["voucher_total_cny"], "120.00")
        self.assertEqual(plan["self_pay_cny"], "880.00")
        self.assertIn("380.00", plan["notes"][0])

    def test_plan_fully_funded_has_no_self_line(self) -> None:
        plan = build_plan(
            Decimal("400"),
            [LayerSpec(CITY, Decimal("100")), LayerSpec(SELF, None)],
            [BatchCapacity("CITY-1", CITY, "2026-10-31T00:00:00Z", Decimal("500"))],
        )
        self.assertEqual([line["kind"] for line in plan["lines"]], [CITY])
        self.assertEqual(plan["self_pay_cny"], "0.00")

    def test_settlement_completed_redeems_in_order_and_carries_forward(self) -> None:
        lines = [
            {"kind": CITY, "batch_id": "CITY-1", "amount_cny": "500.00"},
            {"kind": PARK, "batch_id": "PARK-1", "amount_cny": "200.00"},
            {"kind": SELF, "batch_id": None, "amount_cny": "300.00"},
        ]
        result = compute_settlement(lines, Decimal("550"), "completed")
        self.assertEqual(result["redeemed_total_cny"], "550.00")
        self.assertEqual(result["self_pay_cny"], "0.00")
        self.assertEqual(result["resolutions"][0]["state"], "redeemed")
        self.assertEqual(result["resolutions"][1]["state"], "carried_forward")
        self.assertEqual(result["resolutions"][1]["redeemed_cny"], "50.00")
        self.assertEqual(result["resolutions"][1]["returned_cny"], "150.00")

    def test_settlement_actual_above_estimate_bills_self_pay(self) -> None:
        lines = [{"kind": CITY, "batch_id": "CITY-1", "amount_cny": "500.00"}]
        result = compute_settlement(lines, Decimal("1200"), "completed")
        self.assertEqual(result["redeemed_total_cny"], "500.00")
        self.assertEqual(result["self_pay_cny"], "700.00")

    def test_settlement_failed_and_cancelled_release_everything(self) -> None:
        lines = [{"kind": CITY, "batch_id": "CITY-1", "amount_cny": "500.00"}]
        for outcome in ("failed", "cancelled"):
            result = compute_settlement(lines, Decimal("0"), outcome)
            self.assertEqual(result["redeemed_total_cny"], "0.00")
            self.assertEqual(result["resolutions"][0]["state"], "released")
            self.assertEqual(result["resolutions"][0]["returned_cny"], "500.00")

    def test_settlement_rejects_unknown_outcome(self) -> None:
        with self.assertRaises(ValueError):
            compute_settlement([], Decimal("1"), "unknown")


class ServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = VoucherService(self.connection, self.clock)
        self.service.create_user("tenant-a", "甲企业", "tenant", "tenant-a")
        self.service.create_user("tenant-b", "乙企业", "tenant", "tenant-b")
        self.service.create_user("city", "市数据局", "funder", "city-bureau")
        self.service.create_user("park", "园区管委会", "funder", "park-admin")
        self.service.create_user("op-1", "运营一", "operator")
        self.service.create_user("op-2", "运营二", "operator")
        self.service.create_user("audit", "审计", "auditor")
        self.service.register_batch(
            "city",
            {
                "batch_id": "CITY-1",
                "kind": CITY,
                "tenant_scope": ["*"],
                "resource_scope": ["gpu-h100"],
                "valid_from": "2026-09-01T00:00:00Z",
                "valid_until": "2026-12-31T23:59:59Z",
                "total_cap_cny": "100000",
                "idempotency_key": "batch-city-1",
            },
        )
        self.service.register_batch(
            "park",
            {
                "batch_id": "PARK-1",
                "kind": PARK,
                "tenant_scope": ["tenant-a"],
                "resource_scope": ["gpu-h100"],
                "valid_from": "2026-09-01T00:00:00Z",
                "valid_until": "2026-10-31T23:59:59Z",
                "total_cap_cny": "30000",
                "idempotency_key": "batch-park-1",
            },
        )
        self.service.publish_rule(
            "op-1",
            "tenant-a",
            [
                {"kind": CITY, "max_share_percent": "50"},
                {"kind": PARK, "max_share_percent": "20"},
                {"kind": SELF},
            ],
            "初版",
        )

    def tearDown(self) -> None:
        self.connection.close()

    def submit(self, job_id: str, estimate: str, tenant: str = "tenant-a", resource: str = "gpu-h100") -> None:
        self.service.submit_job(
            tenant,
            {
                "job_id": job_id,
                "tenant_id": tenant,
                "resource": resource,
                "estimated_cost_cny": estimate,
                "idempotency_key": f"submit-{job_id}",
            },
        )

    def confirm(self, job_id: str, estimate: str = "10000") -> dict[str, object]:
        self.submit(job_id, estimate)
        return self.service.confirm_job("op-1", job_id, f"confirm-{job_id}")

    def batch(self, batch_id: str) -> dict[str, object]:
        return self.service.batch_ledger("audit", batch_id)["batch"]

    # -------------------------------------------------------------- 登记与规则

    def test_register_batch_replay_and_conflict(self) -> None:
        payload = {
            "batch_id": "CITY-2",
            "kind": CITY,
            "tenant_scope": ["tenant-a"],
            "resource_scope": ["gpu-h100"],
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_until": "2026-11-30T23:59:59Z",
            "total_cap_cny": "5000",
            "idempotency_key": "batch-city-2",
        }
        first = self.service.register_batch("city", payload)
        second = self.service.register_batch("city", payload)
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["batch_id"], second["batch_id"])
        with self.assertRaises(Conflict):
            self.service.register_batch("city", dict(payload, total_cap_cny="6000"))

    def test_register_batch_validates_scope_and_validity(self) -> None:
        base = {
            "batch_id": "BAD-1",
            "kind": CITY,
            "tenant_scope": ["tenant-a"],
            "resource_scope": ["gpu-h100"],
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_until": "2026-12-31T23:59:59Z",
            "total_cap_cny": "100",
            "idempotency_key": "batch-bad-1",
        }
        with self.assertRaises(ValidationFailed):
            self.service.register_batch("city", dict(base, kind="province_voucher"))
        with self.assertRaises(ValidationFailed):
            self.service.register_batch("city", dict(base, valid_until="2026-08-01T00:00:00Z"))
        with self.assertRaises(ValidationFailed):
            self.service.register_batch("city", dict(base, resource_scope=["gpu-h100", "*"]))
        with self.assertRaises(Forbidden):
            self.service.register_batch("tenant-a", base)

    def test_rule_requires_self_pay_fallback_layer(self) -> None:
        with self.assertRaises(ValidationFailed):
            self.service.publish_rule("op-1", "tenant-b", [{"kind": CITY, "max_share_percent": "50"}])
        with self.assertRaises(ValidationFailed):
            self.service.publish_rule(
                "op-1", "tenant-b", [{"kind": SELF, "max_share_percent": "10"}]
            )

    # -------------------------------------------------------------- 确认与冻结

    def test_confirm_builds_explainable_plan_and_freezes_quota(self) -> None:
        result = self.confirm("job-1", "10000")
        self.assertEqual(result["state"], "confirmed")
        self.assertEqual(result["rule_version"], 1)
        self.assertEqual(
            [(line["kind"], line["amount_cny"]) for line in result["lines"]],
            [(CITY, "5000.00"), (PARK, "2000.00"), (SELF, "3000.00")],
        )
        self.assertTrue(all(line["reason"] for line in result["lines"]))
        self.assertEqual(self.batch("CITY-1")["frozen_cny"], "5000.00")
        self.assertEqual(self.batch("CITY-1")["remaining_cny"], "95000.00")
        self.assertEqual(self.batch("PARK-1")["frozen_cny"], "2000.00")

    def test_confirm_is_idempotent_and_key_conflict_on_different_job(self) -> None:
        self.submit("job-1", "10000")
        first = self.service.confirm_job("op-1", "job-1", "confirm-key")
        second = self.service.confirm_job("op-1", "job-1", "confirm-key")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["lines"], second["lines"])
        self.submit("job-2", "10000")
        with self.assertRaises(Conflict):
            self.service.confirm_job("op-1", "job-2", "confirm-key")

    def test_confirm_skips_ineligible_batches(self) -> None:
        self.service.register_batch(
            "city",
            {
                "batch_id": "CITY-EXPIRED",
                "kind": CITY,
                "tenant_scope": ["*"],
                "resource_scope": ["gpu-h100"],
                "valid_from": "2026-06-01T00:00:00Z",
                "valid_until": "2026-08-31T23:59:59Z",
                "total_cap_cny": "999999",
                "idempotency_key": "batch-expired",
            },
        )
        result = self.confirm("job-1", "10000")
        batches = [line.get("batch_id") for line in result["lines"]]
        self.assertNotIn("CITY-EXPIRED", batches)
        # PARK-1 只适用于 tenant-a；tenant-b 的作业只能自付
        self.service.publish_rule(
            "op-1", "tenant-b", [{"kind": PARK, "max_share_percent": "80"}, {"kind": SELF}]
        )
        self.submit("job-b", "1000", tenant="tenant-b")
        result_b = self.service.confirm_job("op-1", "job-b", "confirm-job-b")
        self.assertEqual(result_b["self_pay_cny"], "1000.00")

    def test_confirm_without_rule_fails(self) -> None:
        self.submit("job-b", "1000", tenant="tenant-b")
        with self.assertRaises(InvalidState):
            self.service.confirm_job("op-1", "job-b", "confirm-job-b")

    def test_tenant_can_only_submit_own_jobs(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.submit_job(
                "tenant-a",
                {
                    "job_id": "job-x",
                    "tenant_id": "tenant-b",
                    "resource": "gpu-h100",
                    "estimated_cost_cny": "100",
                    "idempotency_key": "submit-job-x",
                },
            )

    # -------------------------------------------------------------- 核销

    def test_settle_completed_redeems_actual_and_updates_batches(self) -> None:
        self.confirm("job-1", "10000")
        result = self.service.settle_job("op-1", "job-1", "completed", "8000", "settle-job-1")
        self.assertEqual(result["redeemed_total_cny"], "7000.00")
        self.assertEqual(result["self_pay_cny"], "1000.00")
        city = self.batch("CITY-1")
        self.assertEqual(city["frozen_cny"], "0.00")
        self.assertEqual(city["redeemed_cny"], "5000.00")
        park = self.batch("PARK-1")
        self.assertEqual(park["redeemed_cny"], "2000.00")
        statement = self.service.job_statement("audit", "job-1")
        self.assertEqual(statement["state"], "settled")
        self.assertEqual(len(statement["redemptions"]), 2)

    def test_settle_partial_carries_forward_unused_quota(self) -> None:
        self.confirm("job-1", "10000")
        result = self.service.settle_job("op-1", "job-1", "partial", "4000", "settle-job-1")
        self.assertEqual(result["redeemed_total_cny"], "4000.00")
        resolutions = {item["batch_id"]: item for item in result["resolutions"]}
        self.assertEqual(resolutions["CITY-1"]["state"], "carried_forward")
        self.assertEqual(resolutions["CITY-1"]["returned_cny"], "1000.00")
        self.assertEqual(resolutions["PARK-1"]["state"], "carried_forward")
        self.assertEqual(resolutions["PARK-1"]["returned_cny"], "2000.00")
        self.assertEqual(self.batch("CITY-1")["remaining_cny"], "96000.00")
        self.assertEqual(self.batch("PARK-1")["remaining_cny"], "30000.00")

    def test_settle_failed_and_cancelled_release_frozen_quota(self) -> None:
        self.confirm("job-1", "10000")
        self.confirm("job-2", "10000")
        failed = self.service.settle_job("op-1", "job-1", "failed", "0", "settle-job-1")
        cancelled = self.service.settle_job("op-1", "job-2", "cancelled", "0", "settle-job-2")
        for result in (failed, cancelled):
            self.assertEqual(result["redeemed_total_cny"], "0.00")
            self.assertTrue(all(item["state"] == "released" for item in result["resolutions"]))
        self.assertEqual(self.batch("CITY-1")["frozen_cny"], "0.00")
        self.assertEqual(self.batch("CITY-1")["redeemed_cny"], "0.00")
        self.assertEqual(self.batch("PARK-1")["remaining_cny"], "30000.00")

    def test_settle_validates_outcome_amounts(self) -> None:
        self.confirm("job-1", "10000")
        with self.assertRaises(ValidationFailed):
            self.service.settle_job("op-1", "job-1", "failed", "10", "settle-bad-1")
        with self.assertRaises(ValidationFailed):
            self.service.settle_job("op-1", "job-1", "partial", "10000", "settle-bad-2")
        with self.assertRaises(ValidationFailed):
            self.service.settle_job("op-1", "job-1", "completed", "0", "settle-bad-3")
        with self.assertRaises(ValidationFailed):
            self.service.settle_job("op-1", "job-1", "paused", "10", "settle-bad-4")

    def test_settle_replay_and_payload_conflict(self) -> None:
        self.confirm("job-1", "10000")
        first = self.service.settle_job("op-1", "job-1", "completed", "8000", "settle-job-1")
        second = self.service.settle_job("op-1", "job-1", "completed", "8000", "settle-job-1")
        self.assertFalse(first["replayed"])
        self.assertTrue(second["replayed"])
        self.assertEqual(first["resolutions"], second["resolutions"])
        with self.assertRaises(Conflict):
            self.service.settle_job("op-1", "job-1", "completed", "8100", "settle-job-1")
        with self.assertRaises(InvalidState):
            self.service.settle_job("op-1", "job-1", "completed", "8000", "settle-other-key")

    def test_settle_requires_confirmed_job(self) -> None:
        self.submit("job-1", "10000")
        with self.assertRaises(InvalidState):
            self.service.settle_job("op-1", "job-1", "completed", "100", "settle-job-1")

    # -------------------------------------------------------------- 规则版本

    def test_rule_revision_does_not_rewrite_confirmed_jobs(self) -> None:
        confirmed = self.confirm("job-1", "10000")
        self.assertEqual(confirmed["voucher_total_cny"], "7000.00")
        self.service.publish_rule(
            "op-1",
            "tenant-a",
            [{"kind": CITY, "max_share_percent": "10"}, {"kind": SELF}],
            "修订：市级券封顶降至10%",
        )
        statement = self.service.job_statement("audit", "job-1")
        self.assertEqual(statement["rule_version"], 1)
        self.assertEqual(
            [(line["kind"], line["amount_cny"]) for line in statement["lines"]],
            [(CITY, "5000.00"), (PARK, "2000.00"), (SELF, "3000.00")],
        )
        new_confirm = self.confirm("job-2", "10000")
        self.assertEqual(new_confirm["rule_version"], 2)
        self.assertEqual(new_confirm["voucher_total_cny"], "1000.00")
        history = self.service.rule_history("audit", "tenant-a")
        self.assertEqual([item["version"] for item in history["versions"]], [1, 2])

    # -------------------------------------------------------------- 人工调整

    def adjust_lines(self, city_amount: str = "1500") -> list[dict[str, object]]:
        return [
            {"kind": CITY, "batch_id": "CITY-1", "amount_cny": city_amount},
            {"kind": PARK, "batch_id": "PARK-1", "amount_cny": "2000"},
            {"kind": SELF, "amount_cny": "6500"},
        ]

    def test_adjustment_requires_dual_review_and_forms_new_version(self) -> None:
        self.confirm("job-1", "10000")
        proposal = self.service.propose_adjustment("op-1", "job-1", self.adjust_lines(), "市级券额度紧张")
        with self.assertRaises(Forbidden):
            self.service.review_adjustment("op-1", proposal["adjustment_id"], True)
        reviewed = self.service.review_adjustment("op-2", proposal["adjustment_id"], True)
        self.assertEqual(reviewed["status"], "approved")
        self.assertEqual(reviewed["plan_version"], 2)
        statement = self.service.job_statement("audit", "job-1")
        self.assertEqual(statement["plan_version"], 2)
        self.assertEqual(statement["plan_source"], "adjustment")
        self.assertEqual(
            [(line["kind"], line["amount_cny"]) for line in statement["lines"]],
            [(CITY, "1500.00"), (PARK, "2000.00"), (SELF, "6500.00")],
        )
        self.assertEqual(self.batch("CITY-1")["frozen_cny"], "1500.00")
        holds = statement["holds"]
        self.assertEqual([hold["state"] for hold in holds], ["frozen", "frozen"])
        result = self.service.settle_job("op-1", "job-1", "completed", "10000", "settle-job-1")
        self.assertEqual(result["redeemed_total_cny"], "3500.00")
        self.assertEqual(result["self_pay_cny"], "6500.00")

    def test_adjustment_rejection_keeps_original_plan(self) -> None:
        self.confirm("job-1", "10000")
        proposal = self.service.propose_adjustment("op-1", "job-1", self.adjust_lines(), "尝试调整")
        reviewed = self.service.review_adjustment("op-2", proposal["adjustment_id"], False)
        self.assertEqual(reviewed["status"], "rejected")
        statement = self.service.job_statement("audit", "job-1")
        self.assertEqual(statement["plan_version"], 1)
        self.assertEqual(self.batch("CITY-1")["frozen_cny"], "5000.00")

    def test_adjustment_validates_total_and_batch_eligibility(self) -> None:
        self.confirm("job-1", "10000")
        with self.assertRaises(ValidationFailed):
            self.service.propose_adjustment(
                "op-1",
                "job-1",
                [
                    {"kind": CITY, "batch_id": "CITY-1", "amount_cny": "1000"},
                    {"kind": SELF, "amount_cny": "1000"},
                ],
                "合计不等于预估费用",
            )
        with self.assertRaises(ValidationFailed):
            self.service.propose_adjustment(
                "op-1",
                "job-1",
                [
                    {"kind": PARK, "batch_id": "CITY-1", "amount_cny": "5000"},
                    {"kind": SELF, "amount_cny": "5000"},
                ],
                "券种类与批次不一致",
            )
        with self.assertRaises(NotFound):
            self.service.propose_adjustment(
                "op-1",
                "job-1",
                [
                    {"kind": CITY, "batch_id": "CITY-404", "amount_cny": "5000"},
                    {"kind": SELF, "amount_cny": "5000"},
                ],
                "批次不存在",
            )

    def test_adjustment_blocked_when_pending_or_settled(self) -> None:
        self.confirm("job-1", "10000")
        self.service.propose_adjustment("op-1", "job-1", self.adjust_lines(), "第一版调整")
        with self.assertRaises(Conflict):
            self.service.propose_adjustment("op-1", "job-1", self.adjust_lines(), "重复申请")
        self.confirm("job-2", "10000")
        self.service.settle_job("op-1", "job-2", "completed", "10000", "settle-job-2")
        with self.assertRaises(InvalidState):
            self.service.propose_adjustment("op-1", "job-2", self.adjust_lines(), "已结算作业")

    # -------------------------------------------------------------- 权限视图

    def test_tenant_sees_only_own_jobs(self) -> None:
        self.confirm("job-1", "10000")
        statement = self.service.job_statement("tenant-a", "job-1")
        self.assertEqual(len(statement["lines"]), 3)
        self.assertIn("notes", statement)
        with self.assertRaises(NotFound):
            self.service.job_statement("tenant-b", "job-1")

    def test_funder_sees_only_own_funded_lines(self) -> None:
        self.confirm("job-1", "10000")
        city_view = self.service.job_statement("city", "job-1")
        self.assertEqual([(line["kind"]) for line in city_view["lines"]], [CITY])
        self.assertNotIn("notes", city_view)
        self.assertEqual([hold["batch_id"] for hold in city_view["holds"]], ["CITY-1"])
        park_view = self.service.job_statement("park", "job-1")
        self.assertEqual([line["batch_id"] for line in park_view["lines"]], ["PARK-1"])
        self.service.register_batch(
            "city",
            {
                "batch_id": "CITY-OTHER",
                "kind": CITY,
                "tenant_scope": ["tenant-b"],
                "resource_scope": ["gpu-h100"],
                "valid_from": "2026-09-01T00:00:00Z",
                "valid_until": "2026-12-31T23:59:59Z",
                "total_cap_cny": "100",
                "idempotency_key": "batch-city-other",
            },
        )
        self.service.create_user("city2", "另一资助方", "funder", "city-other-bureau")
        with self.assertRaises(NotFound):
            self.service.job_statement("city2", "job-1")

    def test_batch_ledger_scoped_to_own_funder(self) -> None:
        self.confirm("job-1", "10000")
        ledger = self.service.batch_ledger("park", "PARK-1")
        self.assertEqual(ledger["batch"]["batch_id"], "PARK-1")
        self.assertEqual(len(ledger["holds"]), 1)
        with self.assertRaises(NotFound):
            self.service.batch_ledger("park", "CITY-1")
        with self.assertRaises(Forbidden):
            self.service.list_batches("tenant-a")
        self.assertEqual(len(self.service.list_batches("city")["batches"]), 1)
        self.assertEqual(len(self.service.list_batches("audit")["batches"]), 2)

    def test_audit_chain_detects_tampering(self) -> None:
        self.confirm("job-1", "10000")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE voucher_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])
        with self.assertRaises(Forbidden):
            self.service.audit_chain("tenant-a")


class ApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = VoucherService(self.connection, clock)
        self.app = JsonApplication(self.service)
        self.service.create_user("op", "运营", "operator")

    def tearDown(self) -> None:
        self.connection.close()

    def test_health_and_error_shape(self) -> None:
        self.assertEqual(self.app.handle("GET", "/health").status, 200)
        missing_actor = self.app.handle("GET", "/batches")
        self.assertEqual(missing_actor.status, 422)
        unknown = self.app.handle("GET", "/nope", {"X-Actor-Id": "op"})
        self.assertEqual(unknown.status, 404)
        self.assertEqual(unknown.body["error"]["code"], "route_not_found")

    def test_job_flow_over_http(self) -> None:
        headers = {"X-Actor-Id": "op"}
        rule = self.app.handle(
            "POST",
            "/rules",
            headers,
            json.dumps(
                {
                    "tenant_id": "tenant-a",
                    "layers": [{"kind": "enterprise_self"}],
                    "note": "仅自付",
                }
            ).encode(),
        )
        self.assertEqual(rule.status, 201)
        self.service.create_user("tenant-a", "甲企业", "tenant", "tenant-a")
        submitted = self.app.handle(
            "POST",
            "/jobs",
            {"X-Actor-Id": "tenant-a"},
            json.dumps(
                {
                    "job_id": "job-http",
                    "tenant_id": "tenant-a",
                    "resource": "gpu-h100",
                    "estimated_cost_cny": "100",
                    "idempotency_key": "submit-job-http",
                }
            ).encode(),
        )
        self.assertEqual(submitted.status, 201)
        confirmed = self.app.handle(
            "POST", "/jobs/job-http/confirm", headers, json.dumps({"idempotency_key": "confirm-job-http"}).encode()
        )
        self.assertEqual(confirmed.status, 200)
        self.assertEqual(confirmed.body["self_pay_cny"], "100.00")
        settled = self.app.handle(
            "POST",
            "/jobs/job-http/settle",
            headers,
            json.dumps(
                {"outcome": "completed", "actual_cost_cny": "100", "idempotency_key": "settle-job-http"}
            ).encode(),
        )
        self.assertEqual(settled.status, 200)
        statement = self.app.handle("GET", "/jobs/job-http/statement", headers)
        self.assertEqual(statement.body["state"], "settled")


if __name__ == "__main__":
    unittest.main()
