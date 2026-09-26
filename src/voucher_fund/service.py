"""算力券登记、联合资助分摊、冻结核销与双人复核调整的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .ledger import (
    BatchCapacity,
    LayerSpec,
    build_plan,
    canonical_json,
    compute_settlement,
    decimal_text,
    digest,
    quantize_money,
)
from .models import (
    JobRequest,
    VoucherBatch,
    parse_adjustment_lines,
    parse_layers,
    identifier,
    required_text,
    decimal_value,
)
from .storage import initialize, transaction


ZERO = Decimal("0")

ROLE_PERMISSIONS = {
    "tenant": {"job.submit", "job.read", "rule.read"},
    "funder": {"batch.register", "batch.read", "job.read"},
    "operator": {
        "rule.publish",
        "rule.read",
        "job.confirm",
        "job.settle",
        "job.read",
        "batch.read",
        "adjustment.propose",
        "adjustment.review",
    },
    "auditor": {"audit.read", "batch.read", "job.read", "rule.read"},
}


class VoucherService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    def _user(self, user_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM voucher_users WHERE user_id=?", (user_id,)
        ).fetchone()
        if row is None:
            raise NotFound("用户不存在")
        if not row["active"]:
            raise Forbidden("用户已停用")
        return row

    def _require(self, user_id: str, permission: str) -> sqlite3.Row:
        user = self._user(user_id)
        if permission not in ROLE_PERMISSIONS[user["role"]]:
            raise Forbidden(f"角色 {user['role']} 无权执行 {permission}")
        return user

    def _audit(
        self,
        entity_type: str,
        entity_id: str,
        event_type: str,
        actor_id: str,
        payload: Mapping[str, Any],
    ) -> None:
        previous = self.connection.execute(
            "SELECT event_hash FROM voucher_audit_events ORDER BY event_id DESC LIMIT 1"
        ).fetchone()
        previous_hash = "0" * 64 if previous is None else previous["event_hash"]
        body = {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor_id": actor_id,
            "payload": payload,
            "created_at": self._now(),
            "previous_hash": previous_hash,
        }
        event_hash = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
        self.connection.execute(
            "INSERT INTO voucher_audit_events(entity_type,entity_id,event_type,actor_id,payload_json,"
            "previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (
                entity_type,
                entity_id,
                event_type,
                actor_id,
                canonical_json(payload),
                previous_hash,
                event_hash,
                body["created_at"],
            ),
        )

    def _idempotency_lookup(
        self, scope: str, key: str, request_value: Mapping[str, Any]
    ) -> dict[str, Any] | None:
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM voucher_idempotency WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != digest(request_value):
            raise Conflict("幂等键对应不同的请求内容")
        return json.loads(stored["response_json"])

    def _idempotency_store(
        self, scope: str, key: str, request_value: Mapping[str, Any], response: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO voucher_idempotency(scope,idempotency_key,request_sha256,response_json,created_at) "
            "VALUES(?,?,?,?,?)",
            (scope, key, digest(request_value), canonical_json(response), self._now()),
        )

    # ------------------------------------------------------------------ 用户

    def create_user(self, user_id: str, display_name: str, role: str, org_id: str = "platform") -> dict[str, Any]:
        if role not in ROLE_PERMISSIONS:
            raise ValidationFailed("未知角色")
        user_id = identifier(user_id, "user_id")
        display_name = required_text(display_name, "display_name")
        org_id = identifier(org_id, "org_id")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO voucher_users(user_id,display_name,role,org_id,created_at) VALUES(?,?,?,?,?)",
                    (user_id, display_name, role, org_id, self._now()),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id, "role": role, "org_id": org_id}

    # ------------------------------------------------------------------ 券批次

    def register_batch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "batch.register")
        batch = VoucherBatch.from_dict(raw)
        request_value = {"funder_id": user["org_id"], **dict(raw)}
        stored = self._idempotency_lookup("batch.register", batch.idempotency_key, request_value)
        if stored is not None:
            return {**stored, "replayed": True}
        response = {
            "batch_id": batch.batch_id,
            "funder_id": user["org_id"],
            "kind": batch.kind,
            "total_cap_cny": decimal_text(quantize_money(batch.total_cap_cny)),
            "state": "active",
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO voucher_batches(batch_id,funder_id,kind,tenant_scope_json,resource_scope_json,"
                    "valid_from,valid_until,total_cap_cny,idempotency_key,created_by,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        batch.batch_id,
                        user["org_id"],
                        batch.kind,
                        canonical_json(list(batch.tenant_scope)),
                        canonical_json(list(batch.resource_scope)),
                        batch.valid_from,
                        batch.valid_until,
                        decimal_text(quantize_money(batch.total_cap_cny)),
                        batch.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._idempotency_store("batch.register", batch.idempotency_key, request_value, response)
                self._audit(
                    "batch",
                    batch.batch_id,
                    "batch.registered",
                    actor_id,
                    {"kind": batch.kind, "total_cap_cny": response["total_cap_cny"]},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("券批次编号或幂等键冲突") from exc
        return {**response, "replayed": False}

    def _batch_row(self, batch_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM voucher_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFound("券批次不存在")
        return row

    @staticmethod
    def _batch_view(row: sqlite3.Row) -> dict[str, Any]:
        remaining = quantize_money(
            Decimal(row["total_cap_cny"]) - Decimal(row["frozen_cny"]) - Decimal(row["redeemed_cny"])
        )
        return {
            "batch_id": row["batch_id"],
            "funder_id": row["funder_id"],
            "kind": row["kind"],
            "tenant_scope": json.loads(row["tenant_scope_json"]),
            "resource_scope": json.loads(row["resource_scope_json"]),
            "valid_from": row["valid_from"],
            "valid_until": row["valid_until"],
            "total_cap_cny": row["total_cap_cny"],
            "frozen_cny": row["frozen_cny"],
            "redeemed_cny": row["redeemed_cny"],
            "remaining_cny": decimal_text(remaining),
            "state": row["state"],
            "revision": row["revision"],
        }

    def list_batches(self, actor_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "batch.read")
        if user["role"] == "funder":
            rows = self.connection.execute(
                "SELECT * FROM voucher_batches WHERE funder_id=? ORDER BY batch_id", (user["org_id"],)
            ).fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM voucher_batches ORDER BY batch_id").fetchall()
        return {"batches": [self._batch_view(row) for row in rows]}

    # ------------------------------------------------------------------ 资助规则

    def publish_rule(
        self, actor_id: str, tenant_id: str, layers: object, note: str = ""
    ) -> dict[str, Any]:
        self._require(actor_id, "rule.publish")
        tenant_id = identifier(tenant_id, "tenant_id")
        parsed = parse_layers(layers)
        note = required_text(note, "note", 512) if note else ""
        with transaction(self.connection, immediate=True):
            row = self.connection.execute(
                "SELECT max(version) AS max_version FROM funding_rules WHERE tenant_id=?", (tenant_id,)
            ).fetchone()
            version = 1 if row["max_version"] is None else int(row["max_version"]) + 1
            layers_json = canonical_json([layer.as_dict() for layer in parsed])
            self.connection.execute(
                "INSERT INTO funding_rules(tenant_id,version,layers_json,note,created_by,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (tenant_id, version, layers_json, note, actor_id, self._now()),
            )
            self._audit(
                "rule",
                f"{tenant_id}#{version}",
                "rule.published",
                actor_id,
                {"tenant_id": tenant_id, "version": version, "layers": json.loads(layers_json)},
            )
        return {"tenant_id": tenant_id, "version": version, "layers": json.loads(layers_json)}

    def rule_history(self, actor_id: str, tenant_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "rule.read")
        tenant_id = identifier(tenant_id, "tenant_id")
        if user["role"] == "tenant" and user["org_id"] != tenant_id:
            raise NotFound("资助规则不存在")
        rows = self.connection.execute(
            "SELECT * FROM funding_rules WHERE tenant_id=? ORDER BY version", (tenant_id,)
        ).fetchall()
        if not rows:
            raise NotFound("资助规则不存在")
        return {
            "tenant_id": tenant_id,
            "versions": [
                {
                    "version": row["version"],
                    "layers": json.loads(row["layers_json"]),
                    "note": row["note"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                }
                for row in rows
            ],
        }

    # ------------------------------------------------------------------ 作业

    def submit_job(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        user = self._require(actor_id, "job.submit")
        job = JobRequest.from_dict(raw)
        if user["role"] == "tenant" and user["org_id"] != job.tenant_id:
            raise Forbidden("租户只能提交本企业的作业")
        request_value = dict(raw)
        stored = self._idempotency_lookup("job.submit", job.idempotency_key, request_value)
        if stored is not None:
            return {**stored, "replayed": True}
        response = {
            "job_id": job.job_id,
            "tenant_id": job.tenant_id,
            "resource": job.resource,
            "estimated_cost_cny": decimal_text(quantize_money(job.estimated_cost_cny)),
            "state": "submitted",
        }
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO jobs(job_id,tenant_id,resource,estimated_cost_cny,idempotency_key,"
                    "submitted_by,submitted_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        job.job_id,
                        job.tenant_id,
                        job.resource,
                        decimal_text(quantize_money(job.estimated_cost_cny)),
                        job.idempotency_key,
                        actor_id,
                        self._now(),
                    ),
                )
                self._idempotency_store("job.submit", job.idempotency_key, request_value, response)
                self._audit("job", job.job_id, "job.submitted", actor_id, {"tenant_id": job.tenant_id})
        except sqlite3.IntegrityError as exc:
            raise Conflict("作业编号或幂等键冲突") from exc
        return {**response, "replayed": False}

    def _job_row(self, job_id: str) -> sqlite3.Row:
        row = self.connection.execute("SELECT * FROM jobs WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise NotFound("作业不存在")
        return row

    @staticmethod
    def _scope_matches(scope_json: str, value: str) -> bool:
        scope = json.loads(scope_json)
        return "*" in scope or value in scope

    def _eligible_batches(self, job: sqlite3.Row, kinds: set[str], now: str) -> list[sqlite3.Row]:
        rows = self.connection.execute(
            "SELECT * FROM voucher_batches WHERE state='active' ORDER BY valid_until,batch_id"
        ).fetchall()
        eligible = []
        for row in rows:
            if row["kind"] not in kinds:
                continue
            if not (row["valid_from"] <= now <= row["valid_until"]):
                continue
            if not self._scope_matches(row["tenant_scope_json"], job["tenant_id"]):
                continue
            if not self._scope_matches(row["resource_scope_json"], job["resource"]):
                continue
            remaining = Decimal(row["total_cap_cny"]) - Decimal(row["frozen_cny"]) - Decimal(row["redeemed_cny"])
            if remaining <= ZERO:
                continue
            eligible.append(row)
        return eligible

    def _freeze(self, batch_id: str, amount: Decimal) -> None:
        row = self._batch_row(batch_id)
        if row["state"] != "active":
            raise Conflict(f"券批次 {batch_id} 已关闭")
        remaining = Decimal(row["total_cap_cny"]) - Decimal(row["frozen_cny"]) - Decimal(row["redeemed_cny"])
        if remaining < amount:
            raise Conflict(f"券批次 {batch_id} 剩余额度不足")
        cursor = self.connection.execute(
            "UPDATE voucher_batches SET frozen_cny=?,revision=revision+1 WHERE batch_id=? AND revision=?",
            (decimal_text(quantize_money(Decimal(row["frozen_cny"]) + amount)), batch_id, row["revision"]),
        )
        if cursor.rowcount != 1:
            raise Conflict(f"券批次 {batch_id} 额度发生变化，请重试")

    def _unfreeze(self, batch_id: str, amount: Decimal, redeemed: Decimal = ZERO) -> None:
        row = self._batch_row(batch_id)
        frozen = Decimal(row["frozen_cny"]) - amount
        redeemed_total = Decimal(row["redeemed_cny"]) + redeemed
        if frozen < ZERO:
            raise Conflict(f"券批次 {batch_id} 冻结额度不足")
        cursor = self.connection.execute(
            "UPDATE voucher_batches SET frozen_cny=?,redeemed_cny=?,revision=revision+1 "
            "WHERE batch_id=? AND revision=?",
            (
                decimal_text(quantize_money(frozen)),
                decimal_text(quantize_money(redeemed_total)),
                batch_id,
                row["revision"],
            ),
        )
        if cursor.rowcount != 1:
            raise Conflict(f"券批次 {batch_id} 额度发生变化，请重试")

    def confirm_job(self, actor_id: str, job_id: str, idempotency_key: str) -> dict[str, Any]:
        self._require(actor_id, "job.confirm")
        idempotency_key = identifier(idempotency_key, "idempotency_key")
        request_value = {"job_id": job_id}
        with transaction(self.connection, immediate=True):
            stored = self._idempotency_lookup("job.confirm", idempotency_key, request_value)
            if stored is not None:
                return {**stored, "replayed": True}
            job = self._job_row(job_id)
            if job["state"] != "submitted":
                raise InvalidState("作业已确认或已结算")
            rule = self.connection.execute(
                "SELECT * FROM funding_rules WHERE tenant_id=? ORDER BY version DESC LIMIT 1",
                (job["tenant_id"],),
            ).fetchone()
            if rule is None:
                raise InvalidState("租户没有已发布的资助规则")
            layers = [
                LayerSpec(
                    layer["kind"],
                    None if layer["kind"] == "enterprise_self" else Decimal(str(layer["max_share_percent"])),
                )
                for layer in json.loads(rule["layers_json"])
            ]
            now = self._now()
            voucher_kinds = {layer.kind for layer in layers if layer.kind != "enterprise_self"}
            candidates = [
                BatchCapacity(
                    row["batch_id"],
                    row["kind"],
                    row["valid_until"],
                    quantize_money(
                        Decimal(row["total_cap_cny"]) - Decimal(row["frozen_cny"]) - Decimal(row["redeemed_cny"])
                    ),
                )
                for row in self._eligible_batches(job, voucher_kinds, now)
            ]
            plan = build_plan(Decimal(job["estimated_cost_cny"]), layers, candidates)
            cursor = self.connection.execute(
                "INSERT INTO allocation_plans(job_id,version,rule_version,lines_json,notes_json,total_cost_cny,"
                "created_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    job_id,
                    1,
                    rule["version"],
                    canonical_json(plan["lines"]),
                    canonical_json(plan["notes"]),
                    plan["estimated_cost_cny"],
                    actor_id,
                    now,
                ),
            )
            plan_id = int(cursor.lastrowid)
            holds: list[dict[str, Any]] = []
            for line in plan["lines"]:
                if line["kind"] == "enterprise_self":
                    continue
                amount = Decimal(str(line["amount_cny"]))
                self._freeze(str(line["batch_id"]), amount)
                hold_cursor = self.connection.execute(
                    "INSERT INTO holds(job_id,plan_id,batch_id,amount_cny,created_at) VALUES(?,?,?,?,?)",
                    (job_id, plan_id, line["batch_id"], line["amount_cny"], now),
                )
                holds.append(
                    {"hold_id": int(hold_cursor.lastrowid), "batch_id": line["batch_id"], "amount_cny": line["amount_cny"]}
                )
            updated = self.connection.execute(
                "UPDATE jobs SET state='confirmed',rule_version=?,revision=revision+1 "
                "WHERE job_id=? AND state='submitted'",
                (rule["version"], job_id),
            )
            if updated.rowcount != 1:
                raise InvalidState("作业状态已变化")
            response = {
                "job_id": job_id,
                "state": "confirmed",
                "plan_version": 1,
                "rule_version": rule["version"],
                "estimated_cost_cny": plan["estimated_cost_cny"],
                "voucher_total_cny": plan["voucher_total_cny"],
                "self_pay_cny": plan["self_pay_cny"],
                "lines": plan["lines"],
                "notes": plan["notes"],
                "holds": holds,
            }
            self._idempotency_store("job.confirm", idempotency_key, request_value, response)
            self._audit(
                "job",
                job_id,
                "job.confirmed",
                actor_id,
                {
                    "plan_version": 1,
                    "rule_version": rule["version"],
                    "voucher_total_cny": plan["voucher_total_cny"],
                    "self_pay_cny": plan["self_pay_cny"],
                },
            )
        return {**response, "replayed": False}

    def settle_job(
        self,
        actor_id: str,
        job_id: str,
        outcome: str,
        actual_cost_cny: object,
        idempotency_key: str,
    ) -> dict[str, Any]:
        self._require(actor_id, "job.settle")
        outcome = required_text(outcome, "outcome", 32)
        if outcome not in {"completed", "partial", "failed", "cancelled"}:
            raise ValidationFailed("outcome 必须是 completed、partial、failed 或 cancelled")
        actual = quantize_money(decimal_value(actual_cost_cny, "actual_cost_cny", minimum=ZERO))
        idempotency_key = identifier(idempotency_key, "idempotency_key")
        request_value = {"job_id": job_id, "outcome": outcome, "actual_cost_cny": decimal_text(actual)}
        with transaction(self.connection, immediate=True):
            stored = self._idempotency_lookup("job.settle", idempotency_key, request_value)
            if stored is not None:
                return {**stored, "replayed": True}
            job = self._job_row(job_id)
            if job["state"] != "confirmed":
                raise InvalidState("只有已确认的作业可以结算")
            estimated = Decimal(job["estimated_cost_cny"])
            if outcome in {"failed", "cancelled"} and actual != ZERO:
                raise ValidationFailed("取消或失败的作业实际消耗必须为零")
            if outcome == "completed" and actual <= ZERO:
                raise ValidationFailed("完成作业的实际消耗必须大于零")
            if outcome == "partial" and not (ZERO < actual < estimated):
                raise ValidationFailed("部分完成的实际消耗必须大于零且低于预估费用")
            plan = self.connection.execute(
                "SELECT * FROM allocation_plans WHERE job_id=? AND status='active'", (job_id,)
            ).fetchone()
            if plan is None:
                raise InvalidState("作业没有生效中的分摊方案")
            holds = self.connection.execute(
                "SELECT * FROM holds WHERE job_id=? AND plan_id=? ORDER BY hold_id", (job_id, plan["plan_id"])
            ).fetchall()
            lines = [
                {"kind": "voucher", "batch_id": hold["batch_id"], "amount_cny": hold["amount_cny"]}
                for hold in holds
            ]
            settlement = compute_settlement(lines, actual, outcome)
            now = self._now()
            for hold, resolution in zip(holds, settlement["resolutions"]):
                redeemed = Decimal(resolution["redeemed_cny"])
                updated = self.connection.execute(
                    "UPDATE holds SET state=?,redeemed_cny=?,resolved_at=? WHERE hold_id=? AND state='frozen'",
                    (resolution["state"], resolution["redeemed_cny"], now, hold["hold_id"]),
                )
                if updated.rowcount != 1:
                    raise Conflict("冻结记录状态已变化")
                self._unfreeze(hold["batch_id"], Decimal(hold["amount_cny"]), redeemed)
                if redeemed > ZERO:
                    self.connection.execute(
                        "INSERT INTO redemptions(job_id,hold_id,batch_id,amount_cny,idempotency_key,"
                        "created_by,created_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            job_id,
                            hold["hold_id"],
                            hold["batch_id"],
                            resolution["redeemed_cny"],
                            idempotency_key,
                            actor_id,
                            now,
                        ),
                    )
            self.connection.execute(
                "UPDATE allocation_plans SET status='settled' WHERE plan_id=?", (plan["plan_id"],)
            )
            updated = self.connection.execute(
                "UPDATE jobs SET state='settled',outcome=?,actual_cost_cny=?,revision=revision+1 "
                "WHERE job_id=? AND state='confirmed'",
                (outcome, settlement["actual_cost_cny"], job_id),
            )
            if updated.rowcount != 1:
                raise InvalidState("作业状态已变化")
            response = {
                "job_id": job_id,
                "state": "settled",
                "outcome": outcome,
                "actual_cost_cny": settlement["actual_cost_cny"],
                "redeemed_total_cny": settlement["redeemed_total_cny"],
                "self_pay_cny": settlement["self_pay_cny"],
                "resolutions": settlement["resolutions"],
            }
            self._idempotency_store("job.settle", idempotency_key, request_value, response)
            self._audit(
                "job",
                job_id,
                "job.settled",
                actor_id,
                {
                    "outcome": outcome,
                    "actual_cost_cny": settlement["actual_cost_cny"],
                    "redeemed_total_cny": settlement["redeemed_total_cny"],
                },
            )
        return {**response, "replayed": False}

    # ------------------------------------------------------------------ 人工调整

    def propose_adjustment(
        self, actor_id: str, job_id: str, lines: object, reason: str
    ) -> dict[str, Any]:
        self._require(actor_id, "adjustment.propose")
        reason = required_text(reason, "reason", 512)
        parsed = parse_adjustment_lines(lines)
        job = self._job_row(job_id)
        if job["state"] != "confirmed":
            raise InvalidState("只有已确认未结算的作业可以调整")
        total = sum((line.amount_cny for line in parsed), ZERO)
        if quantize_money(total) != Decimal(job["estimated_cost_cny"]):
            raise ValidationFailed("调整行合计必须等于作业预估费用")
        now = self._now()
        for line in parsed:
            if line.kind == "enterprise_self":
                continue
            batch = self._batch_row(str(line.batch_id))
            if batch["kind"] != line.kind:
                raise ValidationFailed(f"调整行券种类与批次 {line.batch_id} 不一致")
            if batch["state"] != "active":
                raise ValidationFailed(f"券批次 {line.batch_id} 已关闭")
            if not (batch["valid_from"] <= now <= batch["valid_until"]):
                raise ValidationFailed(f"券批次 {line.batch_id} 不在有效期内")
            if not self._scope_matches(batch["tenant_scope_json"], job["tenant_id"]):
                raise ValidationFailed(f"券批次 {line.batch_id} 不适用于该租户")
            if not self._scope_matches(batch["resource_scope_json"], job["resource"]):
                raise ValidationFailed(f"券批次 {line.batch_id} 不适用于该资源")
        pending = self.connection.execute(
            "SELECT count(*) AS count FROM adjustments WHERE job_id=? AND status='pending'", (job_id,)
        ).fetchone()
        if pending["count"]:
            raise Conflict("该作业已有待复核的调整申请")
        with transaction(self.connection, immediate=True):
            cursor = self.connection.execute(
                "INSERT INTO adjustments(job_id,lines_json,reason,proposed_by,proposed_at) VALUES(?,?,?,?,?)",
                (job_id, canonical_json([line.as_dict() for line in parsed]), reason, actor_id, now),
            )
            adjustment_id = int(cursor.lastrowid)
            self._audit(
                "adjustment",
                str(adjustment_id),
                "adjustment.proposed",
                actor_id,
                {"job_id": job_id, "reason": reason},
            )
        return {"adjustment_id": adjustment_id, "job_id": job_id, "status": "pending"}

    def review_adjustment(self, actor_id: str, adjustment_id: int, approve: bool) -> dict[str, Any]:
        self._require(actor_id, "adjustment.review")
        row = self.connection.execute(
            "SELECT * FROM adjustments WHERE adjustment_id=?", (adjustment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("调整申请不存在")
        if row["status"] != "pending":
            raise InvalidState("调整申请已经处理")
        if row["proposed_by"] == actor_id:
            raise Forbidden("申请人不能复核自己的调整申请")
        now = self._now()
        if not approve:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "UPDATE adjustments SET status='rejected',reviewed_by=?,reviewed_at=? "
                    "WHERE adjustment_id=? AND status='pending'",
                    (actor_id, now, adjustment_id),
                )
                self._audit("adjustment", str(adjustment_id), "adjustment.rejected", actor_id, {})
            return {"adjustment_id": adjustment_id, "status": "rejected"}
        lines = parse_adjustment_lines(json.loads(row["lines_json"]))
        with transaction(self.connection, immediate=True):
            job = self._job_row(row["job_id"])
            if job["state"] != "confirmed":
                raise InvalidState("作业已结算，不能调整")
            plan = self.connection.execute(
                "SELECT * FROM allocation_plans WHERE job_id=? AND status='active'", (job["job_id"],)
            ).fetchone()
            if plan is None:
                raise InvalidState("作业没有生效中的分摊方案")
            old_holds = self.connection.execute(
                "SELECT * FROM holds WHERE plan_id=? AND state='frozen'", (plan["plan_id"],)
            ).fetchall()
            for hold in old_holds:
                self.connection.execute(
                    "UPDATE holds SET state='released',resolved_at=? WHERE hold_id=?",
                    (now, hold["hold_id"]),
                )
                self._unfreeze(hold["batch_id"], Decimal(hold["amount_cny"]))
            self.connection.execute(
                "UPDATE allocation_plans SET status='superseded' WHERE plan_id=?", (plan["plan_id"],)
            )
            version_row = self.connection.execute(
                "SELECT max(version) AS max_version FROM allocation_plans WHERE job_id=?", (job["job_id"],)
            ).fetchone()
            new_version = int(version_row["max_version"]) + 1
            new_lines: list[dict[str, Any]] = []
            for index, line in enumerate(lines, start=1):
                entry: dict[str, Any] = {
                    "layer": index,
                    "kind": line.kind,
                    "batch_id": line.batch_id,
                    "amount_cny": decimal_text(quantize_money(line.amount_cny)),
                    "reason": f"人工调整（申请{adjustment_id}双人复核通过）：{row['reason']}",
                }
                new_lines.append(entry)
            cursor = self.connection.execute(
                "INSERT INTO allocation_plans(job_id,version,rule_version,lines_json,notes_json,total_cost_cny,"
                "source,adjustment_id,created_by,created_at) VALUES(?,?,?,?,?,?,'adjustment',?,?,?)",
                (
                    job["job_id"],
                    new_version,
                    plan["rule_version"],
                    canonical_json(new_lines),
                    canonical_json([f"人工调整版本，替代第{plan['version']}版方案"]),
                    job["estimated_cost_cny"],
                    adjustment_id,
                    actor_id,
                    now,
                ),
            )
            new_plan_id = int(cursor.lastrowid)
            for line in new_lines:
                if line["kind"] == "enterprise_self":
                    continue
                amount = Decimal(str(line["amount_cny"]))
                batch = self._batch_row(str(line["batch_id"]))
                if batch["state"] != "active" or not (batch["valid_from"] <= now <= batch["valid_until"]):
                    raise Conflict(f"券批次 {line['batch_id']} 当前不可用")
                if not self._scope_matches(batch["tenant_scope_json"], job["tenant_id"]) or not self._scope_matches(
                    batch["resource_scope_json"], job["resource"]
                ):
                    raise Conflict(f"券批次 {line['batch_id']} 不再适用于该作业")
                self._freeze(str(line["batch_id"]), amount)
                self.connection.execute(
                    "INSERT INTO holds(job_id,plan_id,batch_id,amount_cny,created_at) VALUES(?,?,?,?,?)",
                    (job["job_id"], new_plan_id, line["batch_id"], line["amount_cny"], now),
                )
            self.connection.execute(
                "UPDATE adjustments SET status='approved',reviewed_by=?,reviewed_at=? "
                "WHERE adjustment_id=? AND status='pending'",
                (actor_id, now, adjustment_id),
            )
            self._audit(
                "adjustment",
                str(adjustment_id),
                "adjustment.approved",
                actor_id,
                {"job_id": job["job_id"], "plan_version": new_version},
            )
        return {"adjustment_id": adjustment_id, "status": "approved", "plan_version": new_version}

    # ------------------------------------------------------------------ 权限视图

    def _plan_bundle(self, job_id: str) -> tuple[sqlite3.Row, list[sqlite3.Row], list[sqlite3.Row]]:
        plan = self.connection.execute(
            "SELECT * FROM allocation_plans WHERE job_id=? ORDER BY version DESC LIMIT 1", (job_id,)
        ).fetchone()
        if plan is None:
            raise NotFound("作业还没有分摊方案")
        holds = self.connection.execute(
            "SELECT * FROM holds WHERE plan_id=? ORDER BY hold_id", (plan["plan_id"],)
        ).fetchall()
        redemptions = self.connection.execute(
            "SELECT * FROM redemptions WHERE job_id=? ORDER BY redemption_id", (job_id,)
        ).fetchall()
        return plan, holds, redemptions

    def job_statement(self, actor_id: str, job_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "job.read")
        job = self._job_row(job_id)
        if user["role"] == "tenant" and job["tenant_id"] != user["org_id"]:
            raise NotFound("作业不存在")
        plan, holds, redemptions = self._plan_bundle(job_id)
        lines = json.loads(plan["lines_json"])
        base = {
            "job_id": job["job_id"],
            "tenant_id": job["tenant_id"],
            "resource": job["resource"],
            "state": job["state"],
            "outcome": job["outcome"],
            "estimated_cost_cny": job["estimated_cost_cny"],
            "actual_cost_cny": job["actual_cost_cny"],
            "rule_version": job["rule_version"],
            "plan_version": plan["version"],
            "plan_status": plan["status"],
            "plan_source": plan["source"],
        }
        if user["role"] == "funder":
            own = {
                row["batch_id"]
                for row in self.connection.execute(
                    "SELECT batch_id FROM voucher_batches WHERE funder_id=?", (user["org_id"],)
                ).fetchall()
            }
            own_lines = [line for line in lines if line.get("batch_id") in own]
            if not own_lines:
                raise NotFound("作业不存在或不涉及当前资助方")
            own_holds = [hold for hold in holds if hold["batch_id"] in own]
            own_redemptions = [item for item in redemptions if item["batch_id"] in own]
            return {
                **base,
                "lines": own_lines,
                "holds": [self._hold_view(hold) for hold in own_holds],
                "redemptions": [self._redemption_view(item) for item in own_redemptions],
            }
        return {
            **base,
            "lines": lines,
            "notes": json.loads(plan["notes_json"]),
            "holds": [self._hold_view(hold) for hold in holds],
            "redemptions": [self._redemption_view(item) for item in redemptions],
        }

    @staticmethod
    def _hold_view(hold: sqlite3.Row) -> dict[str, Any]:
        return {
            "hold_id": hold["hold_id"],
            "batch_id": hold["batch_id"],
            "amount_cny": hold["amount_cny"],
            "state": hold["state"],
            "redeemed_cny": hold["redeemed_cny"],
            "created_at": hold["created_at"],
            "resolved_at": hold["resolved_at"],
        }

    @staticmethod
    def _redemption_view(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "redemption_id": row["redemption_id"],
            "job_id": row["job_id"],
            "hold_id": row["hold_id"],
            "batch_id": row["batch_id"],
            "amount_cny": row["amount_cny"],
            "created_at": row["created_at"],
        }

    def batch_ledger(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "batch.read")
        batch = self._batch_row(batch_id)
        if user["role"] == "funder" and batch["funder_id"] != user["org_id"]:
            raise NotFound("券批次不存在")
        holds = self.connection.execute(
            "SELECT h.*,j.tenant_id,j.resource FROM holds h JOIN jobs j ON j.job_id=h.job_id "
            "WHERE h.batch_id=? ORDER BY h.hold_id",
            (batch_id,),
        ).fetchall()
        redemptions = self.connection.execute(
            "SELECT * FROM redemptions WHERE batch_id=? ORDER BY redemption_id", (batch_id,)
        ).fetchall()
        return {
            "batch": self._batch_view(batch),
            "holds": [
                {
                    **self._hold_view(hold),
                    "job_id": hold["job_id"],
                    "tenant_id": hold["tenant_id"],
                    "resource": hold["resource"],
                }
                for hold in holds
            ],
            "redemptions": [self._redemption_view(item) for item in redemptions],
        }

    # ------------------------------------------------------------------ 审计

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute("SELECT * FROM voucher_audit_events ORDER BY event_id").fetchall()
        previous_hash = "0" * 64
        valid = True
        for row in rows:
            body = {
                "entity_type": row["entity_type"],
                "entity_id": row["entity_id"],
                "event_type": row["event_type"],
                "actor_id": row["actor_id"],
                "payload": json.loads(row["payload_json"]),
                "created_at": row["created_at"],
                "previous_hash": row["previous_hash"],
            }
            calculated = hashlib.sha256(canonical_json(body).encode("utf-8")).hexdigest()
            if row["previous_hash"] != previous_hash or row["event_hash"] != calculated:
                valid = False
                break
            previous_hash = row["event_hash"]
        return {"valid": valid, "events": len(rows), "head_hash": previous_hash}
