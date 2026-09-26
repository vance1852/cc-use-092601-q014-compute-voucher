"""算力券核销服务的 SQLite 模式与事务辅助。"""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator


SCHEMA = """
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS voucher_users (
    user_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN
        ('sponsor_admin','operator','reviewer','tenant','auditor')),
    sponsor_id TEXT,
    tenant_id TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

-- 资助方与预算池（额度口径跨版本共享：consumed + frozen <= total_cap）。
CREATE TABLE IF NOT EXISTS voucher_batches (
    batch_id TEXT PRIMARY KEY,
    sponsor_id TEXT NOT NULL,
    name TEXT NOT NULL,
    current_version INTEGER NOT NULL DEFAULT 1,
    total_cap_cny TEXT NOT NULL,
    frozen_cny TEXT NOT NULL DEFAULT '0.00',
    consumed_cny TEXT NOT NULL DEFAULT '0.00',
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','retired')),
    content_sha256 TEXT NOT NULL,
    revision INTEGER NOT NULL DEFAULT 1,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

-- 资助规则版本：确认作业时快照版本号，规则修订不影响已确认作业。
CREATE TABLE IF NOT EXISTS voucher_batch_versions (
    batch_id TEXT NOT NULL REFERENCES voucher_batches(batch_id),
    version INTEGER NOT NULL,
    eligible_tenants_json TEXT NOT NULL,
    scope_kind TEXT NOT NULL CHECK(scope_kind IN ('product','facility','any')),
    scope_products_json TEXT NOT NULL,
    scope_facilities_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    per_job_cap_cny TEXT NOT NULL,
    cover_percent TEXT NOT NULL,
    priority INTEGER NOT NULL,
    change_note TEXT NOT NULL DEFAULT '',
    content_sha256 TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(batch_id, version)
);

CREATE TABLE IF NOT EXISTS voucher_jobs (
    job_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    facility_id TEXT NOT NULL,
    product TEXT NOT NULL,
    service_date TEXT NOT NULL,
    estimated_cost_cny TEXT NOT NULL,
    actual_cost_cny TEXT,
    self_paid_cny TEXT,
    state TEXT NOT NULL DEFAULT 'confirmed'
        CHECK(state IN ('confirmed','settled','cancelled')),
    settled_result TEXT CHECK(settled_result IN ('completed','failed','partial')),
    settlement_version INTEGER NOT NULL DEFAULT 1,
    plan_json TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    settled_at TEXT,
    cancelled_at TEXT,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_voucher_jobs_tenant
ON voucher_jobs(tenant_id, created_at);

-- 冻结与核销台账（每个资助方一行；source 区分批次额度与结转额度）。
-- carry_id 为 0 表示批次预算，否则引用 voucher_carry_balances。
CREATE TABLE IF NOT EXISTS voucher_job_shares (
    share_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES voucher_jobs(job_id),
    batch_id TEXT NOT NULL,
    sponsor_id TEXT NOT NULL,
    rule_version INTEGER NOT NULL,
    source TEXT NOT NULL CHECK(source IN ('batch','carry')),
    carry_id INTEGER NOT NULL DEFAULT 0,
    frozen_cny TEXT NOT NULL,
    consumed_cny TEXT NOT NULL DEFAULT '0.00',
    released_cny TEXT NOT NULL DEFAULT '0.00',
    carried_forward_cny TEXT NOT NULL DEFAULT '0.00',
    adjusted_cny TEXT NOT NULL DEFAULT '0.00',
    basis TEXT NOT NULL,
    created_at TEXT NOT NULL,
    UNIQUE(job_id, batch_id, source, carry_id)
);

CREATE INDEX IF NOT EXISTS idx_voucher_shares_batch
ON voucher_job_shares(batch_id, job_id);

-- 部分完成结转余额：仍计入 voucher_batches.frozen_cny。
CREATE TABLE IF NOT EXISTS voucher_carry_balances (
    carry_id INTEGER PRIMARY KEY AUTOINCREMENT,
    batch_id TEXT NOT NULL REFERENCES voucher_batches(batch_id),
    tenant_id TEXT NOT NULL,
    source_job_id TEXT NOT NULL REFERENCES voucher_jobs(job_id),
    remaining_cny TEXT NOT NULL,
    expires_on TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'open' CHECK(state IN ('open','consumed','released')),
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_voucher_carry_use
ON voucher_carry_balances(tenant_id, batch_id, state, expires_on);

-- 人工调整申请：申请人与复核人必须不同，批准后形成作业结算新版本。
CREATE TABLE IF NOT EXISTS voucher_adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES voucher_jobs(job_id),
    batch_id TEXT NOT NULL,
    delta_cny TEXT NOT NULL,
    reason TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','approved','rejected')),
    requested_by TEXT NOT NULL,
    requested_at TEXT NOT NULL,
    reviewed_by TEXT,
    reviewed_at TEXT,
    review_note TEXT,
    idempotency_key TEXT NOT NULL UNIQUE
);

CREATE INDEX IF NOT EXISTS idx_voucher_adjustments_job
ON voucher_adjustments(job_id, adjustment_id);

-- 作业分摊方案的版本留存（确认时 v1，每次批准的人工调整追加新版本）。
CREATE TABLE IF NOT EXISTS voucher_settlement_versions (
    job_id TEXT NOT NULL REFERENCES voucher_jobs(job_id),
    version INTEGER NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('confirmed','adjusted')),
    adjustment_id INTEGER,
    allocation_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(job_id, version)
);

CREATE TABLE IF NOT EXISTS voucher_idempotency (
    scope TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    request_sha256 TEXT NOT NULL,
    response_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY(scope, idempotency_key)
);

CREATE TABLE IF NOT EXISTS voucher_audit_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_type TEXT NOT NULL,
    entity_id TEXT NOT NULL,
    event_type TEXT NOT NULL,
    actor_id TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    previous_hash TEXT NOT NULL,
    event_hash TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_voucher_audit_entity
ON voucher_audit_events(entity_type, entity_id, event_id);
"""


def connect(path: str | Path) -> sqlite3.Connection:
    # check_same_thread=False：ThreadingHTTPServer 下各请求线程共享连接，
    # 写事务统一 BEGIN IMMEDIATE 并由 busy_timeout 串行化。
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=False)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA busy_timeout=5000")
    initialize(connection)
    return connection


def initialize(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA)


@contextmanager
def transaction(connection: sqlite3.Connection, *, immediate: bool = False) -> Iterator[None]:
    connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
    try:
        yield
    except BaseException:
        connection.rollback()
        raise
    else:
        connection.commit()
