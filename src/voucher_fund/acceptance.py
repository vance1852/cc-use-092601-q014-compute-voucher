"""贯通券批次登记、规则发布、作业确认分摊、核销与双人复核调整的离线验收。"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .clock import FrozenClock
from .service import VoucherService


def run(workspace: Path) -> dict[str, object]:
    connection = sqlite3.connect(":memory:", isolation_level=None)
    connection.row_factory = sqlite3.Row
    service = VoucherService(connection, FrozenClock(datetime(2026, 9, 26, 8, 0, tzinfo=timezone.utc)))
    service.create_user("tenant-a", "甲企业算力专员", "tenant", "tenant-a")
    service.create_user("city", "市数据局券管理员", "funder", "city-bureau")
    service.create_user("park", "园区管委会补贴专员", "funder", "park-admin")
    service.create_user("op-1", "运营专员一", "operator")
    service.create_user("op-2", "运营专员二", "operator")
    service.create_user("audit", "审计员", "auditor")
    service.register_batch(
        "city",
        {
            "batch_id": "CITY-2026-03",
            "kind": "city_voucher",
            "tenant_scope": ["*"],
            "resource_scope": ["gpu-h100", "gpu-a100"],
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_until": "2026-12-31T23:59:59Z",
            "total_cap_cny": "100000",
            "idempotency_key": "batch-city-03",
        },
    )
    service.register_batch(
        "park",
        {
            "batch_id": "PARK-09",
            "kind": "park_subsidy",
            "tenant_scope": ["tenant-a"],
            "resource_scope": ["gpu-h100"],
            "valid_from": "2026-09-01T00:00:00Z",
            "valid_until": "2026-10-31T23:59:59Z",
            "total_cap_cny": "30000",
            "idempotency_key": "batch-park-09",
        },
    )
    service.publish_rule(
        "op-1",
        "tenant-a",
        [
            {"kind": "city_voucher", "max_share_percent": "50"},
            {"kind": "park_subsidy", "max_share_percent": "20"},
            {"kind": "enterprise_self"},
        ],
        "初版核销顺序：市级券、园区补贴、企业自付",
    )
    jobs = {
        "job-001": "10000",
        "job-002": "6000",
        "job-003": "4000",
        "job-004": "2000",
    }
    for job_id, estimate in jobs.items():
        service.submit_job(
            "tenant-a",
            {
                "job_id": job_id,
                "tenant_id": "tenant-a",
                "resource": "gpu-h100",
                "estimated_cost_cny": estimate,
                "idempotency_key": f"submit-{job_id}",
            },
        )
        service.confirm_job("op-1", job_id, f"confirm-{job_id}")
    settled_completed = service.settle_job("op-1", "job-001", "completed", "8000", "settle-job-001")
    replayed = service.settle_job("op-1", "job-001", "completed", "8000", "settle-job-001")
    settled_partial = service.settle_job("op-1", "job-002", "partial", "3000", "settle-job-002")
    settled_failed = service.settle_job("op-1", "job-003", "failed", "0", "settle-job-003")
    settled_cancelled = service.settle_job("op-1", "job-004", "cancelled", "0", "settle-job-004")
    service.publish_rule(
        "op-1",
        "tenant-a",
        [
            {"kind": "city_voucher", "max_share_percent": "40"},
            {"kind": "park_subsidy", "max_share_percent": "20"},
            {"kind": "enterprise_self"},
        ],
        "修订：市级券单作业封顶下调为40%",
    )
    service.submit_job(
        "tenant-a",
        {
            "job_id": "job-005",
            "tenant_id": "tenant-a",
            "resource": "gpu-h100",
            "estimated_cost_cny": "5000",
            "idempotency_key": "submit-job-005",
        },
    )
    confirmed_v2 = service.confirm_job("op-1", "job-005", "confirm-job-005")
    adjustment = service.propose_adjustment(
        "op-1",
        "job-005",
        [
            {"kind": "city_voucher", "batch_id": "CITY-2026-03", "amount_cny": "1500"},
            {"kind": "park_subsidy", "batch_id": "PARK-09", "amount_cny": "1000"},
            {"kind": "enterprise_self", "amount_cny": "2500"},
        ],
        "市级券额度紧张，与企业协商后下调券承担额",
    )
    reviewed = service.review_adjustment("op-2", adjustment["adjustment_id"], True)
    settled_adjusted = service.settle_job("op-1", "job-005", "completed", "5000", "settle-job-005")
    result = {
        "status": "ok",
        "workspace": workspace.name,
        "completed": settled_completed,
        "completed_replayed": replayed["replayed"],
        "partial": settled_partial,
        "failed": settled_failed,
        "cancelled": settled_cancelled,
        "confirmed_v2_rule_version": confirmed_v2["rule_version"],
        "adjustment": reviewed,
        "adjusted_settlement": settled_adjusted,
        "tenant_statement": service.job_statement("tenant-a", "job-001"),
        "funder_statement": service.job_statement("city", "job-001"),
        "park_ledger": service.batch_ledger("park", "PARK-09"),
        "audit": service.audit_chain("audit"),
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
