"""贯通券批次登记、联合资助分摊、冻结、核销、结转、规则修订、
双人人工调整与权限视图的离线验收。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from decimal import Decimal
from pathlib import Path

from .clock import FrozenClock
from .errors import Conflict, Forbidden
from .service import VoucherService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = VoucherService(
        connection, FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc))
    )
    # 用户：市级资助方、园区资助方、运营、复核、租户、审计。
    service.create_user("city-admin", "市级管理员", "sponsor_admin", sponsor_id="city")
    service.create_user("park-admin", "园区管理员", "sponsor_admin", sponsor_id="park")
    service.create_user("ops", "平台运营", "operator")
    service.create_user("reviewer", "调整复核员", "reviewer")
    service.create_user("tenant-lambda", "Lambda 科技", "tenant", tenant_id="tenant-lambda")
    service.create_user("audit", "审计员", "auditor")

    # 市级券：覆盖 60%，单作业上限 3000，总池 20000，高优先级。
    service.register_batch("city-admin", {
        "batch_id": "city-2026-q3",
        "sponsor_id": "city",
        "name": "2026 年三季度市级算力券",
        "eligible_tenants": ["tenant-lambda"],
        "scope_kind": "product",
        "scope_products": ["gpu-h100", "gpu-a100"],
        "scope_facilities": [],
        "valid_from": "2026-07-01",
        "valid_until": "2026-12-31",
        "total_cap_cny": "20000.00",
        "per_job_cap_cny": "3000.00",
        "cover_percent": "60",
        "priority": 10,
    })
    # 园区补贴：覆盖 30%，单作业上限 1500，总池 8000。
    service.register_batch("park-admin", {
        "batch_id": "park-2026-q3",
        "sponsor_id": "park",
        "name": "园区联合资助补贴",
        "eligible_tenants": ["tenant-lambda"],
        "scope_kind": "facility",
        "scope_products": [],
        "scope_facilities": ["cluster-a"],
        "valid_from": "2026-07-01",
        "valid_until": "2026-12-31",
        "total_cap_cny": "8000.00",
        "per_job_cap_cny": "1500.00",
        "cover_percent": "30",
        "priority": 20,
    })

    # 作业一：费用 5000。市级 3000（60%=3000 且触顶），园区 1500（30%=1500 且触顶），自付 500。
    confirm_one = {
        "job_id": "job-001",
        "tenant_id": "tenant-lambda",
        "facility_id": "cluster-a",
        "product": "gpu-h100",
        "service_date": "2026-09-26",
        "estimated_cost_cny": "5000.00",
        "idempotency_key": "confirm-001",
    }
    plan_one = service.confirm_job("ops", confirm_one)
    assert plan_one["allocation"]["total_sponsored_cny"] == "4500.00"
    assert plan_one["allocation"]["self_paid_cny"] == "500.00"
    assert [share["batch_id"] for share in plan_one["allocation"]["shares"]] == [
        "city-2026-q3", "park-2026-q3",
    ]
    # 相同请求安全重放；改动内容复用编号必须冲突。
    assert service.confirm_job("ops", confirm_one)["replayed"] is True
    try:
        service.confirm_job("ops", dict(confirm_one, estimated_cost_cny="5001.00"))
    except Conflict:
        pass
    else:  # pragma: no cover
        raise AssertionError("不同内容复用幂等键必须冲突")

    # 部分完成：实际消耗 2000。核销市级 1333.33、园区 666.67（按冻结比例），
    # 未用的 2500 冻结额结转给该租户。
    settle_one = service.settle_job("ops", {
        "job_id": "job-001",
        "actual_cost_cny": "2000.00",
        "result": "partial",
        "idempotency_key": "settle-001",
    })
    assert settle_one["totals"]["consumed_cny"] == "2000.00"
    assert settle_one["totals"]["carried_cny"] == "2500.00"
    carry_ids = {item["batch_id"]: item["carry_id"] for item in settle_one["carry_forward"]}

    # 作业二优先消耗结转余额（同批次结转优先于批次预算）。
    plan_two = service.confirm_job("ops", {
        "job_id": "job-002",
        "tenant_id": "tenant-lambda",
        "facility_id": "cluster-a",
        "product": "gpu-h100",
        "service_date": "2026-09-27",
        "estimated_cost_cny": "3000.00",
        "idempotency_key": "confirm-002",
    })
    sources = {(share["batch_id"], share["source"]) for share in plan_two["allocation"]["shares"]}
    assert ("city-2026-q3", "carry") in sources
    service.settle_job("ops", {
        "job_id": "job-002", "actual_cost_cny": "3000.00",
        "result": "completed", "idempotency_key": "settle-002",
    })

    # 作业三失败：冻结全部释放回资助批次与结转余额。
    service.confirm_job("ops", {
        "job_id": "job-003", "tenant_id": "tenant-lambda", "facility_id": "cluster-a",
        "product": "gpu-h100", "service_date": "2026-09-28",
        "estimated_cost_cny": "1000.00", "idempotency_key": "confirm-003",
    })
    cancelled = service.cancel_job("ops", "job-003", "cancel-003")
    assert service.cancel_job("ops", "job-003", "cancel-003")["replayed"] is True
    assert Decimal(cancelled["released_cny"]) > 0

    # 规则修订：市级提高单作业上限，但已确认作业保留旧版本快照。
    # 作业四位于园区范围外的设施 cluster-b，只有市级券覆盖 3000，企业自付 2000。
    service.confirm_job("ops", {
        "job_id": "job-004", "tenant_id": "tenant-lambda", "facility_id": "cluster-b",
        "product": "gpu-h100", "service_date": "2026-09-29",
        "estimated_cost_cny": "5000.00", "idempotency_key": "confirm-004",
    })
    service.revise_rule("city-admin", {
        "batch_id": "city-2026-q3", "expected_version": 1,
        "sponsor_id": "city", "name": "2026 年三季度市级算力券（修订）",
        "eligible_tenants": ["tenant-lambda"], "scope_kind": "product",
        "scope_products": ["gpu-h100", "gpu-a100"], "scope_facilities": [],
        "valid_from": "2026-07-01", "valid_until": "2026-12-31",
        "total_cap_cny": "20000.00", "per_job_cap_cny": "5000.00",
        "cover_percent": "60", "priority": 10,
    }, "提高单作业资助上限")
    job_four = service.job_detail("audit", "job-004")
    city_share = next(share for share in job_four["shares"] if share["batch_id"] == "city-2026-q3")
    assert city_share["rule_version"] == 1, "规则修订不能改写已确认作业"
    assert city_share["frozen_cny"] == "3000.00"
    service.settle_job("ops", {
        "job_id": "job-004", "actual_cost_cny": "5000.00",
        "result": "completed", "idempotency_key": "settle-004",
    })

    # 人工调整：运营申请，不能自审；复核员双人复核后形成结算新版本。
    adjustment = service.request_adjustment("ops", {
        "job_id": "job-004", "batch_id": "city-2026-q3",
        "delta_cny": "100.00", "reason": "市级券口径补登",
        "idempotency_key": "adj-001",
    })
    try:
        service.review_adjustment("ops", adjustment["adjustment_id"], True, "自审")
    except Forbidden:
        pass
    else:  # pragma: no cover
        raise AssertionError("申请人不能复核自己的调整")
    reviewed = service.review_adjustment("reviewer", adjustment["adjustment_id"], True, "凭证齐全")
    assert reviewed["settlement_version"] == 2
    assert service.job_detail("audit", "job-004")["settlement_version"] == 2

    # 权限视图：租户、资助方、审计看到的范围各不相同。
    tenant_view = service.job_detail("tenant-lambda", "job-004")
    sponsor_view = service.job_detail("city-admin", "job-004")
    assert all(share["sponsor_id"] == "city" for share in sponsor_view["shares"])
    assert sponsor_view["redacted_shares"] == 0
    assert sponsor_view["self_paid_cny"] is None
    # 园区未参与作业四，不可见。
    try:
        service.job_detail("park-admin", "job-004")
    except Forbidden:
        pass
    else:  # pragma: no cover
        raise AssertionError("园区不应看到未参与的作业")
    # 园区只看到作业一中属于本资助方的份额。
    park_job_one = service.job_detail("park-admin", "job-001")
    assert [share["batch_id"] for share in park_job_one["shares"]] == ["park-2026-q3"]
    assert park_job_one["redacted_shares"] == 1

    result = {
        "status": "ok",
        "plan_job_001": plan_one["allocation"],
        "settle_job_001": settle_one["totals"],
        "carry_ids": carry_ids,
        "cancelled_job_003": cancelled,
        "tenant_carry_balances": service.tenant_carry_balances("tenant-lambda"),
        "city_report": service.sponsor_report("city-admin"),
        "park_report": service.sponsor_report("park-admin"),
        "tenant_view_job_004": {"self_paid_cny": tenant_view["self_paid_cny"], "shares": tenant_view["shares"]},
        "sponsor_view_job_004": sponsor_view,
        "park_view_job_001": park_job_one,
        "rule_versions": service.list_rule_versions("audit", "city-2026-q3"),
        "audit": service.audit_chain("audit"),
        "workspace": workspace.name,
    }
    connection.close()
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="运行算力券核销与联合资助分摊离线验收")
    parser.add_argument("--workspace", type=Path, default=Path.cwd())
    args = parser.parse_args(argv)
    print(json.dumps(run(args.workspace), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
