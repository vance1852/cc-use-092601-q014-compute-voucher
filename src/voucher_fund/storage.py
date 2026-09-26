"""算力券核销与联合资助分摊的 SQLite 模式和事务辅助。"""

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
    role TEXT NOT NULL CHECK(role IN ('tenant','funder','operator','auditor')),
    org_id TEXT NOT NULL,
    active INTEGER NOT NULL DEFAULT 1 CHECK(active IN (0,1)),
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS voucher_batches (
    batch_id TEXT PRIMARY KEY,
    funder_id TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('city_voucher','park_subsidy')),
    tenant_scope_json TEXT NOT NULL,
    resource_scope_json TEXT NOT NULL,
    valid_from TEXT NOT NULL,
    valid_until TEXT NOT NULL,
    total_cap_cny TEXT NOT NULL,
    frozen_cny TEXT NOT NULL DEFAULT '0.00',
    redeemed_cny TEXT NOT NULL DEFAULT '0.00',
    state TEXT NOT NULL DEFAULT 'active' CHECK(state IN ('active','closed')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    created_by TEXT NOT NULL REFERENCES voucher_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_funder
ON voucher_batches(funder_id, kind, valid_until);

CREATE TABLE IF NOT EXISTS funding_rules (
    tenant_id TEXT NOT NULL,
    version INTEGER NOT NULL CHECK(version > 0),
    layers_json TEXT NOT NULL,
    note TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES voucher_users(user_id),
    created_at TEXT NOT NULL,
    PRIMARY KEY(tenant_id, version)
);

CREATE TABLE IF NOT EXISTS jobs (
    job_id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL,
    resource TEXT NOT NULL,
    estimated_cost_cny TEXT NOT NULL,
    actual_cost_cny TEXT,
    rule_version INTEGER,
    outcome TEXT CHECK(outcome IN ('completed','partial','failed','cancelled')),
    state TEXT NOT NULL DEFAULT 'submitted' CHECK(state IN ('submitted','confirmed','settled')),
    revision INTEGER NOT NULL DEFAULT 1,
    idempotency_key TEXT NOT NULL UNIQUE,
    submitted_by TEXT NOT NULL REFERENCES voucher_users(user_id),
    submitted_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_tenant
ON jobs(tenant_id, state, submitted_at);

CREATE TABLE IF NOT EXISTS allocation_plans (
    plan_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    version INTEGER NOT NULL CHECK(version > 0),
    rule_version INTEGER NOT NULL,
    lines_json TEXT NOT NULL,
    notes_json TEXT NOT NULL,
    total_cost_cny TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'active' CHECK(status IN ('active','superseded','settled')),
    source TEXT NOT NULL DEFAULT 'confirmation' CHECK(source IN ('confirmation','adjustment')),
    adjustment_id INTEGER,
    created_by TEXT NOT NULL REFERENCES voucher_users(user_id),
    created_at TEXT NOT NULL,
    UNIQUE(job_id, version)
);

CREATE TABLE IF NOT EXISTS holds (
    hold_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    plan_id INTEGER NOT NULL REFERENCES allocation_plans(plan_id),
    batch_id TEXT NOT NULL REFERENCES voucher_batches(batch_id),
    amount_cny TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'frozen' CHECK(state IN ('frozen','redeemed','released','carried_forward')),
    redeemed_cny TEXT NOT NULL DEFAULT '0.00',
    created_at TEXT NOT NULL,
    resolved_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_holds_batch
ON holds(batch_id, state, hold_id);

CREATE INDEX IF NOT EXISTS idx_holds_job
ON holds(job_id, hold_id);

CREATE TABLE IF NOT EXISTS redemptions (
    redemption_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    hold_id INTEGER NOT NULL REFERENCES holds(hold_id),
    batch_id TEXT NOT NULL REFERENCES voucher_batches(batch_id),
    amount_cny TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    created_by TEXT NOT NULL REFERENCES voucher_users(user_id),
    created_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_redemptions_batch
ON redemptions(batch_id, redemption_id);

CREATE TABLE IF NOT EXISTS adjustments (
    adjustment_id INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id TEXT NOT NULL REFERENCES jobs(job_id),
    lines_json TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
    proposed_by TEXT NOT NULL REFERENCES voucher_users(user_id),
    proposed_at TEXT NOT NULL,
    reviewed_by TEXT REFERENCES voucher_users(user_id),
    reviewed_at TEXT
);

CREATE INDEX IF NOT EXISTS idx_adjustments_job
ON adjustments(job_id, adjustment_id);

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


def connect(path: str | Path, *, check_same_thread: bool = True) -> sqlite3.Connection:
    connection = sqlite3.connect(str(path), isolation_level=None, timeout=10, check_same_thread=check_same_thread)
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


def row_dict(row: sqlite3.Row | None) -> dict[str, object] | None:
    return None if row is None else dict(row)
