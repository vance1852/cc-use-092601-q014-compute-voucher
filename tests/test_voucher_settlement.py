from __future__ import annotations

import sqlite3
import unittest
from datetime import datetime, timezone
from decimal import Decimal

from voucher_settlement.clock import FrozenClock
from voucher_settlement.errors import Conflict, Forbidden, InvalidState, ValidationFailed
from voucher_settlement.settlement import (
    BatchCandidate,
    allocate_outcome,
    money,
    self_paid,
    settle_totals,
    split_plan,
)
from voucher_settlement.service import VoucherService


CITY = {
    "batch_id": "city-q3", "sponsor_id": "city", "name": "市级券",
    "eligible_tenants": ["tenant-a"], "scope_kind": "product",
    "scope_products": ["gpu-h100"], "scope_facilities": [],
    "valid_from": "2026-07-01", "valid_until": "2026-12-31",
    "total_cap_cny": "20000.00", "per_job_cap_cny": "3000.00",
    "cover_percent": "60", "priority": 10,
}
PARK = {
    "batch_id": "park-q3", "sponsor_id": "park", "name": "园区补贴",
    "eligible_tenants": ["tenant-a"], "scope_kind": "any",
    "scope_products": [], "scope_facilities": [],
    "valid_from": "2026-07-01", "valid_until": "2026-12-31",
    "total_cap_cny": "8000.00", "per_job_cap_cny": "1500.00",
    "cover_percent": "30", "priority": 20,
}


class SettlementFunctionTests(unittest.TestCase):
    def test_split_plan_order_caps_and_self_paid(self) -> None:
        shares = split_plan(Decimal("5000"), [
            BatchCandidate("park-q3", "park", Decimal("30"), Decimal("1500"), Decimal("8000"), 20),
            BatchCandidate("city-q3", "city", Decimal("60"), Decimal("3000"), Decimal("20000"), 10),
        ])
        self.assertEqual([share.batch_id for share in shares], ["city-q3", "park-q3"])
        self.assertEqual([share.amount_cny for share in shares], [Decimal("3000.00"), Decimal("1500.00")])
        sponsored = sum((share.amount_cny for share in shares), Decimal("0"))
        self.assertEqual(self_paid(Decimal("5000"), sponsored), Decimal("500.00"))

    def test_insufficient_budget_cascades_to_self_paid(self) -> None:
        shares = split_plan(Decimal("5000"), [
            BatchCandidate("city-q3", "city", Decimal("60"), Decimal("3000"), Decimal("100.00"), 10),
        ])
        self.assertEqual(shares[0].amount_cny, Decimal("100.00"))
        self.assertEqual(self_paid(Decimal("5000"), Decimal("100.00")), Decimal("4900.00"))

    def test_settle_outcomes(self) -> None:
        completed = settle_totals("completed", Decimal("5000"), Decimal("4500"), Decimal("4000"))
        self.assertEqual(completed["sponsor_consumed"], Decimal("4000.00"))
        self.assertEqual(completed["released"], Decimal("500.00"))
        self.assertEqual(completed["self_paid"], Decimal("0.00"))
        failed = settle_totals("failed", Decimal("5000"), Decimal("4500"), Decimal("0"))
        self.assertEqual(failed["released"], Decimal("4500.00"))
        partial = settle_totals("partial", Decimal("5000"), Decimal("4500"), Decimal("2000"))
        self.assertEqual(partial["carried"], Decimal("2500.00"))
        with self.assertRaises(ValueError):
            settle_totals("failed", Decimal("5000"), Decimal("4500"), Decimal("10"))
        with self.assertRaises(ValueError):
            settle_totals("completed", Decimal("5000"), Decimal("4500"), Decimal("6000"))

    def test_allocate_outcome_last_share_absorbs_rounding(self) -> None:
        outcomes = allocate_outcome("partial", Decimal("5000"), [
            {"batch_id": "city-q3", "sponsor_id": "city", "source": "batch", "carry_id": None, "frozen_cny": "3000"},
            {"batch_id": "park-q3", "sponsor_id": "park", "source": "batch", "carry_id": None, "frozen_cny": "1500"},
        ], Decimal("2000"))
        consumed = sum((item.consumed_cny for item in outcomes), Decimal("0"))
        carried = sum((item.carried_cny for item in outcomes), Decimal("0"))
        self.assertEqual(money(consumed), Decimal("2000.00"))
        self.assertEqual(money(carried), Decimal("2500.00"))
        for item in outcomes:
            self.assertEqual(item.consumed_cny + item.carried_cny, item.frozen_cny)


class VoucherServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.connection = sqlite3.connect(":memory:", isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.clock = FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
        self.service = VoucherService(self.connection, self.clock)
        self.service.create_user("city-admin", "市级", "sponsor_admin", sponsor_id="city")
        self.service.create_user("park-admin", "园区", "sponsor_admin", sponsor_id="park")
        self.service.create_user("ops", "运营", "operator")
        self.service.create_user("reviewer", "复核", "reviewer")
        self.service.create_user("tenant-a", "租户A", "tenant", tenant_id="tenant-a")
        self.service.create_user("tenant-b", "租户B", "tenant", tenant_id="tenant-b")
        self.service.create_user("audit", "审计", "auditor")
        self.service.register_batch("city-admin", CITY)
        self.service.register_batch("park-admin", PARK)

    def tearDown(self) -> None:
        self.connection.close()

    def confirm(self, job_id: str, cost: str, key: str, **overrides):
        payload = {
            "job_id": job_id, "tenant_id": "tenant-a", "facility_id": "cluster-a",
            "product": "gpu-h100", "service_date": "2026-09-26",
            "estimated_cost_cny": cost, "idempotency_key": key,
        }
        payload.update(overrides)
        return self.service.confirm_job("ops", payload)

    def test_batch_validation(self) -> None:
        bad = dict(CITY, batch_id="bad", valid_until="2026-01-01")
        with self.assertRaises(ValidationFailed):
            self.service.register_batch("city-admin", bad)
        with self.assertRaises(Conflict):
            self.service.register_batch("city-admin", CITY)
        with self.assertRaises(Forbidden):
            self.service.register_batch("park-admin", dict(CITY, batch_id="city-other"))

    def test_eligibility_scope_and_validity(self) -> None:
        # 不适用的租户：无任何资助，全部企业自付。
        other = self.confirm("j1", "1000", "k1", tenant_id="tenant-b")
        self.assertEqual(other["allocation"]["shares"], [])
        self.assertEqual(other["allocation"]["self_paid_cny"], "1000.00")
        # 超出市级券资源范围：只有 scope=any 的园区券。
        scoped = self.confirm("j2", "1000", "k2", product="cpu-highmem")
        self.assertEqual([s["batch_id"] for s in scoped["allocation"]["shares"]], ["park-q3"])
        # 超出有效期。
        expired = self.confirm("j3", "1000", "k3", service_date="2027-01-01")
        self.assertEqual(expired["allocation"]["shares"], [])

    def test_confirm_replay_and_content_conflict(self) -> None:
        payload = {
            "job_id": "j1", "tenant_id": "tenant-a", "facility_id": "cluster-a",
            "product": "gpu-h100", "service_date": "2026-09-26",
            "estimated_cost_cny": "5000.00", "idempotency_key": "idem-1",
        }
        first = self.service.confirm_job("ops", payload)
        second = self.service.confirm_job("ops", payload)
        self.assertTrue(second["replayed"])
        self.assertEqual(second["allocation"], first["allocation"])
        with self.assertRaises(Conflict):
            self.service.confirm_job("ops", dict(payload, estimated_cost_cny="5001.00"))

    def test_failed_job_releases_full_freeze(self) -> None:
        self.confirm("j1", "5000", "k1")
        before = self.service.batch_detail("city-q3")
        result = self.service.settle_job("ops", {
            "job_id": "j1", "actual_cost_cny": "0", "result": "failed", "idempotency_key": "s1",
        })
        self.assertEqual(result["totals"]["released_cny"], "4500.00")
        after = self.service.batch_detail("city-q3")
        self.assertEqual(after["frozen_cny"], "0.00")
        self.assertEqual(after["available_cny"], before["total_cap_cny"])
        with self.assertRaises(InvalidState):
            self.service.settle_job("ops", {
                "job_id": "j1", "actual_cost_cny": "0", "result": "failed", "idempotency_key": "s2",
            })

    def test_cancel_releases_freeze_and_is_idempotent(self) -> None:
        self.confirm("j1", "5000", "k1")
        first = self.service.cancel_job("ops", "j1", "cancel-1")
        second = self.service.cancel_job("ops", "j1", "cancel-1")
        self.assertTrue(second["replayed"])
        self.assertEqual(first["released_cny"], second["released_cny"])
        self.assertEqual(self.service.batch_detail("city-q3")["frozen_cny"], "0.00")
        with self.assertRaises(InvalidState):
            self.service.confirm("x", "1", "x") if False else self.service.cancel_job("ops", "j1", "cancel-2")

    def test_partial_completion_carries_balance_forward(self) -> None:
        self.confirm("j1", "5000", "k1")
        settled = self.service.settle_job("ops", {
            "job_id": "j1", "actual_cost_cny": "2000", "result": "partial", "idempotency_key": "s1",
        })
        self.assertEqual(settled["totals"]["carried_cny"], "2500.00")
        balances = {b["batch_id"]: b for b in self.service.tenant_carry_balances("tenant-a")}
        self.assertEqual(balances["city-q3"]["remaining_cny"], "1666.67")
        # 结转余额优先于批次预算使用。
        second = self.confirm("j2", "2000", "k2")
        first_share = second["allocation"]["shares"][0]
        self.assertEqual(first_share["source"], "carry")
        self.assertEqual(first_share["amount_cny"], "1666.67")

    def test_rule_revision_does_not_rewrite_confirmed_job(self) -> None:
        confirmed = self.confirm("j1", "5000", "k1")
        city_share = next(s for s in confirmed["allocation"]["shares"] if s["batch_id"] == "city-q3")
        self.assertEqual(city_share["amount_cny"], "3000.00")
        self.service.revise_rule("city-admin", dict(CITY, expected_version=1, per_job_cap_cny="5000.00"), "放宽上限")
        self.assertEqual(self.service.batch_detail("city-q3")["version"], 2)
        stored = self.service.job_detail("audit", "j1")
        self.assertEqual(stored["shares"][0]["rule_version"], 1)
        # 新作业按新版本分摊。
        later = self.confirm("j2", "5000", "k2", service_date="2026-09-27")
        new_city = next(s for s in later["allocation"]["shares"] if s["batch_id"] == "city-q3")
        self.assertEqual(new_city["rule_version"], 2)
        self.assertEqual(new_city["amount_cny"], "3000.00")  # 60% 覆盖比例仍是 3000
        with self.assertRaises(Conflict):
            self.service.revise_rule("city-admin", dict(CITY, expected_version=1), "过期版本")

    def test_cap_cannot_shrink_below_frozen_plus_consumed(self) -> None:
        self.confirm("j1", "5000", "k1")
        with self.assertRaises(ValidationFailed):
            self.service.revise_rule(
                "city-admin",
                dict(CITY, expected_version=1, total_cap_cny="1000.00", per_job_cap_cny="1000.00"),
                "缩减预算",
            )

    def test_adjustment_requires_two_person_review_and_new_version(self) -> None:
        self.confirm("j1", "5000", "k1", facility_id="cluster-b")
        self.service.settle_job("ops", {
            "job_id": "j1", "actual_cost_cny": "5000", "result": "completed", "idempotency_key": "s1",
        })
        requested = self.service.request_adjustment("ops", {
            "job_id": "j1", "batch_id": "city-q3", "delta_cny": "100.00",
            "reason": "补登", "idempotency_key": "adj-1",
        })
        with self.assertRaises(Forbidden):
            self.service.review_adjustment("ops", requested["adjustment_id"], True, "自审")
        approved = self.service.review_adjustment("reviewer", requested["adjustment_id"], True, "凭证齐全")
        self.assertEqual(approved["settlement_version"], 2)
        detail = self.service.job_detail("audit", "j1")
        self.assertEqual(detail["settlement_version"], 2)
        self.assertEqual(detail["self_paid_cny"], "400.00")
        city = next(s for s in detail["shares"] if s["batch_id"] == "city-q3")
        self.assertEqual(city["consumed_cny"], "3100.00")
        self.assertEqual([v["kind"] for v in detail["versions"]], ["confirmed", "adjusted"])

    def test_adjustment_rejection_leaves_settlement_unchanged(self) -> None:
        self.confirm("j1", "5000", "k1", facility_id="cluster-b")
        self.service.settle_job("ops", {
            "job_id": "j1", "actual_cost_cny": "5000", "result": "completed", "idempotency_key": "s1",
        })
        requested = self.service.request_adjustment("ops", {
            "job_id": "j1", "batch_id": "city-q3", "delta_cny": "100.00",
            "reason": "补登", "idempotency_key": "adj-1",
        })
        rejected = self.service.review_adjustment("reviewer", requested["adjustment_id"], False, "凭证不足")
        self.assertEqual(rejected["state"], "rejected")
        self.assertEqual(self.service.job_detail("audit", "j1")["settlement_version"], 1)

    def test_role_based_visibility(self) -> None:
        self.confirm("j1", "5000", "k1")
        # 租户 B 不能查看租户 A 的作业。
        with self.assertRaises(Forbidden):
            self.service.job_detail("tenant-b", "j1")
        # 园区只看到自己的份额，且看不到企业自付。
        park_view = self.service.job_detail("park-admin", "j1")
        self.assertEqual([s["batch_id"] for s in park_view["shares"]], ["park-q3"])
        self.assertIsNone(park_view["self_paid_cny"])
        self.assertIsNone(park_view["confirmed_plan"]["self_paid_cny"])
        # 市级看不到园区专属设施范围之外...这里用没有市级份额的作业。
        other = self.confirm("j2", "1000", "k2", product="cpu-highmem")
        self.assertEqual([s["batch_id"] for s in other["allocation"]["shares"]], ["park-q3"])
        with self.assertRaises(Forbidden):
            self.service.job_detail("city-admin", "j2")

    def test_tenant_can_view_own_job(self) -> None:
        self.confirm("j1", "5000", "k1")
        view = self.service.job_detail("tenant-a", "j1")
        self.assertEqual(view["tenant_id"], "tenant-a")
        self.assertEqual(len(view["shares"]), 2)

    def test_tenant_cannot_confirm_other_tenant_job(self) -> None:
        with self.assertRaises(Forbidden):
            self.service.confirm_job("tenant-a", {
                "job_id": "j9", "tenant_id": "tenant-b", "facility_id": "cluster-a",
                "product": "gpu-h100", "service_date": "2026-09-26",
                "estimated_cost_cny": "100", "idempotency_key": "k9",
            })

    def test_expired_carry_is_released_back_to_batch(self) -> None:
        self.confirm("j1", "5000", "k1")
        self.service.settle_job("ops", {
            "job_id": "j1", "actual_cost_cny": "2000", "result": "partial", "idempotency_key": "s1",
        })
        carrying = self.service.batch_detail("city-q3")
        self.assertEqual(carrying["frozen_cny"], "1666.67")
        # 结转有效期 90 天；12 月 25 日到期，次年 1 月的新作业触发释放回批次池。
        later = self.confirm("j2", "1000", "k2", service_date="2027-01-02")
        # 市级券本身已过有效期，无资助；但过期结转已被释放，冻结归零。
        self.assertEqual(later["allocation"]["shares"], [])
        self.assertEqual(self.service.batch_detail("city-q3")["frozen_cny"], "0.00")
        balances = self.service.tenant_carry_balances("tenant-a")
        self.assertEqual({b["state"] for b in balances if b["batch_id"] == "city-q3"}, {"released"})

    def test_audit_chain_detects_tampering(self) -> None:
        self.confirm("j1", "5000", "k1")
        self.assertTrue(self.service.audit_chain("audit")["valid"])
        self.connection.execute("UPDATE voucher_audit_events SET payload_json='{}' WHERE event_id=1")
        self.assertFalse(self.service.audit_chain("audit")["valid"])


if __name__ == "__main__":
    unittest.main()
