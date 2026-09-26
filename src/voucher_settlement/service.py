"""算力券核销与联合资助分摊的事务用例。"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import date, timedelta
from decimal import Decimal
from typing import Any, Mapping

from .clock import SystemClock, utc_text
from .errors import Conflict, Forbidden, InvalidState, NotFound, ValidationFailed
from .models import AdjustmentInput, JobCompleteInput, JobConfirmInput, VoucherBatchInput
from .settlement import (
    BatchCandidate,
    ZERO,
    allocate_outcome,
    canonical_json,
    digest,
    money,
    self_paid,
    split_plan,
    text,
)
from .storage import initialize, transaction


CARRY_DAYS = 90

ROLE_PERMISSIONS = {
    "sponsor_admin": {"batch.write", "rule.revise", "adjustment.request", "sponsor.read"},
    "operator": {"job.write", "job.settle", "adjustment.request", "report.read"},
    "reviewer": {"adjustment.review", "report.read"},
    "tenant": {"job.write", "tenant.read"},
    "auditor": {"audit.read", "report.read"},
}
ROLES = set(ROLE_PERMISSIONS)


class VoucherService:
    def __init__(self, connection: sqlite3.Connection, clock=None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        initialize(connection)

    def _now(self) -> str:
        return utc_text(self.clock.now())

    @staticmethod
    def _parse(factory):
        try:
            return factory()
        except ValueError as exc:
            raise ValidationFailed(str(exc)) from exc

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
            "INSERT INTO voucher_audit_events(entity_type,entity_id,event_type,actor_id,"
            "payload_json,previous_hash,event_hash,created_at) VALUES(?,?,?,?,?,?,?,?)",
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

    def _replay(self, scope: str, key: str, request_digest: str) -> dict[str, Any] | None:
        stored = self.connection.execute(
            "SELECT request_sha256,response_json FROM voucher_idempotency "
            "WHERE scope=? AND idempotency_key=?",
            (scope, key),
        ).fetchone()
        if stored is None:
            return None
        if stored["request_sha256"] != request_digest:
            raise Conflict("幂等键对应不同的请求内容")
        return json.loads(stored["response_json"])

    def _store_replay(
        self, scope: str, key: str, request_digest: str, response: Mapping[str, Any]
    ) -> None:
        self.connection.execute(
            "INSERT INTO voucher_idempotency(scope,idempotency_key,request_sha256,"
            "response_json,created_at) VALUES(?,?,?,?,?)",
            (scope, key, request_digest, canonical_json(response), self._now()),
        )

    # -- 用户与目录 -------------------------------------------------------

    def create_user(
        self,
        user_id: str,
        display_name: str,
        role: str,
        *,
        sponsor_id: str | None = None,
        tenant_id: str | None = None,
    ) -> dict[str, Any]:
        if role not in ROLES:
            raise ValidationFailed("未知角色")
        if not user_id.strip() or not display_name.strip():
            raise ValidationFailed("用户编号和名称不能为空")
        if role == "sponsor_admin" and not sponsor_id:
            raise ValidationFailed("资助方管理员必须绑定 sponsor_id")
        if role == "tenant" and not tenant_id:
            raise ValidationFailed("租户用户必须绑定 tenant_id")
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO voucher_users(user_id,display_name,role,sponsor_id,tenant_id,"
                    "created_at) VALUES(?,?,?,?,?,?)",
                    (
                        user_id.strip(),
                        display_name.strip(),
                        role,
                        sponsor_id,
                        tenant_id,
                        self._now(),
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("用户已经存在") from exc
        return {"user_id": user_id.strip(), "role": role, "sponsor_id": sponsor_id, "tenant_id": tenant_id}

    # -- 券批次与规则版本 -------------------------------------------------

    def register_batch(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "batch.write")
        actor = self._user(actor_id)
        batch = self._parse(lambda: VoucherBatchInput.from_dict(raw))
        if actor["role"] == "sponsor_admin" and actor["sponsor_id"] != batch.sponsor_id:
            raise Forbidden("资助方管理员只能登记本资助方的券批次")
        content = digest(batch.to_dict())
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                self.connection.execute(
                    "INSERT INTO voucher_batches(batch_id,sponsor_id,name,current_version,"
                    "total_cap_cny,content_sha256,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,1,?,?,?,?,?)",
                    (
                        batch.batch_id,
                        batch.sponsor_id,
                        batch.name,
                        text(batch.total_cap_cny),
                        content,
                        actor_id,
                        now,
                        now,
                    ),
                )
                self._insert_version(batch, 1, actor_id, now, "初始登记")
                self._audit(
                    "voucher_batch",
                    batch.batch_id,
                    "batch.registered",
                    actor_id,
                    {"sponsor_id": batch.sponsor_id, "total_cap_cny": text(batch.total_cap_cny), "version": 1},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("券批次编号已经存在") from exc
        return self.batch_detail(batch.batch_id)

    def _insert_version(
        self,
        batch: VoucherBatchInput,
        version: int,
        actor_id: str,
        now: str,
        change_note: str,
    ) -> None:
        content = digest(batch.to_dict() | {"version": version})
        self.connection.execute(
            "INSERT INTO voucher_batch_versions(batch_id,version,eligible_tenants_json,scope_kind,"
            "scope_products_json,scope_facilities_json,valid_from,valid_until,per_job_cap_cny,"
            "cover_percent,priority,change_note,content_sha256,created_by,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                batch.batch_id,
                version,
                canonical_json(list(batch.eligible_tenants)),
                batch.scope_kind,
                canonical_json(list(batch.scope_products)),
                canonical_json(list(batch.scope_facilities)),
                batch.valid_from,
                batch.valid_until,
                text(batch.per_job_cap_cny),
                text(batch.cover_percent),
                batch.priority,
                change_note,
                content,
                actor_id,
                now,
            ),
        )

    def revise_rule(self, actor_id: str, raw: Mapping[str, Any], change_note: str) -> dict[str, Any]:
        """登记资助规则新版本；历史版本与已确认作业的快照均不可变。"""
        self._require(actor_id, "rule.revise")
        actor = self._user(actor_id)
        batch = self._parse(lambda: VoucherBatchInput.from_dict(raw))
        if actor["role"] == "sponsor_admin" and actor["sponsor_id"] != batch.sponsor_id:
            raise Forbidden("资助方管理员只能修订本资助方的规则")
        expected_version = int(raw["expected_version"])
        current = self.connection.execute(
            "SELECT * FROM voucher_batches WHERE batch_id=?", (batch.batch_id,)
        ).fetchone()
        if current is None:
            raise NotFound("券批次不存在")
        if current["sponsor_id"] != batch.sponsor_id:
            raise ValidationFailed("不能改变券批次的资助方")
        if current["current_version"] != expected_version:
            raise Conflict("规则版本已变化，请基于最新版本修订")
        frozen_and_consumed = money(Decimal(current["frozen_cny"]) + Decimal(current["consumed_cny"]))
        if batch.total_cap_cny < frozen_and_consumed:
            raise ValidationFailed(
                f"修订后资助上限不能低于已冻结与已核销之和 {text(frozen_and_consumed)}"
            )
        new_version = expected_version + 1
        now = self._now()
        content = digest(batch.to_dict() | {"version": new_version})
        with transaction(self.connection, immediate=True):
            self._insert_version(batch, new_version, actor_id, now, change_note[:512])
            self.connection.execute(
                "UPDATE voucher_batches SET name=?,current_version=?,total_cap_cny=?,"
                "content_sha256=?,updated_at=? WHERE batch_id=?",
                (batch.name, new_version, text(batch.total_cap_cny), content, now, batch.batch_id),
            )
            self._audit(
                "voucher_batch",
                batch.batch_id,
                "rule.revised",
                actor_id,
                {"from_version": expected_version, "to_version": new_version, "change_note": change_note[:512]},
            )
        return self.batch_detail(batch.batch_id)

    def batch_detail(self, batch_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM voucher_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        if row is None:
            raise NotFound("券批次不存在")
        version = self.connection.execute(
            "SELECT * FROM voucher_batch_versions WHERE batch_id=? AND version=?",
            (batch_id, row["current_version"]),
        ).fetchone()
        return {
            "batch_id": row["batch_id"],
            "sponsor_id": row["sponsor_id"],
            "name": row["name"],
            "state": row["state"],
            "version": row["current_version"],
            "total_cap_cny": row["total_cap_cny"],
            "frozen_cny": row["frozen_cny"],
            "consumed_cny": row["consumed_cny"],
            "available_cny": text(
                money(Decimal(row["total_cap_cny"]) - Decimal(row["frozen_cny"]) - Decimal(row["consumed_cny"]))
            ),
            "rule": {
                "eligible_tenants": json.loads(version["eligible_tenants_json"]),
                "scope_kind": version["scope_kind"],
                "scope_products": json.loads(version["scope_products_json"]),
                "scope_facilities": json.loads(version["scope_facilities_json"]),
                "valid_from": version["valid_from"],
                "valid_until": version["valid_until"],
                "per_job_cap_cny": version["per_job_cap_cny"],
                "cover_percent": version["cover_percent"],
                "priority": version["priority"],
                "change_note": version["change_note"],
                "content_sha256": version["content_sha256"],
            },
        }

    def get_batch_view(self, actor_id: str, batch_id: str) -> dict[str, Any]:
        """按权限返回券批次：运营/复核/审计与本资助方可见预算池；
        租户只可见当前适用规则，预算池与资助方内部信息脱敏。"""
        user = self._user(actor_id)
        detail = self.batch_detail(batch_id)
        role = user["role"]
        if role == "sponsor_admin":
            if user["sponsor_id"] != detail["sponsor_id"]:
                raise Forbidden("资助方只能查看本资助方的券批次")
            return detail
        if role in {"operator", "reviewer", "auditor"}:
            return detail
        if role == "tenant":
            rule = detail["rule"]
            if user["tenant_id"] not in rule["eligible_tenants"]:
                raise Forbidden("该券批次不适用于当前租户")
            return {
                "batch_id": detail["batch_id"],
                "name": detail["name"],
                "state": detail["state"],
                "version": detail["version"],
                "rule": {
                    "scope_kind": rule["scope_kind"],
                    "scope_products": rule["scope_products"],
                    "scope_facilities": rule["scope_facilities"],
                    "valid_from": rule["valid_from"],
                    "valid_until": rule["valid_until"],
                    "per_job_cap_cny": rule["per_job_cap_cny"],
                    "cover_percent": rule["cover_percent"],
                },
            }
        raise Forbidden("当前角色无权查看券批次")

    def list_rule_versions(self, actor_id: str, batch_id: str) -> list[dict[str, Any]]:
        user = self._user(actor_id)
        if user["role"] == "sponsor_admin":
            detail = self.batch_detail(batch_id)
            if user["sponsor_id"] != detail["sponsor_id"]:
                raise Forbidden("资助方只能查看本资助方的规则历史")
        elif user["role"] not in {"operator", "reviewer", "auditor"}:
            raise Forbidden("当前角色无权查看规则修订历史")
        rows = self.connection.execute(
            "SELECT version,scope_kind,valid_from,valid_until,per_job_cap_cny,cover_percent,"
            "priority,change_note,content_sha256,created_by,created_at,eligible_tenants_json "
            "FROM voucher_batch_versions WHERE batch_id=? ORDER BY version",
            (batch_id,),
        ).fetchall()
        if not rows:
            raise NotFound("券批次不存在")
        return [
            {
                "version": row["version"],
                "eligible_tenants": json.loads(row["eligible_tenants_json"]),
                "scope_kind": row["scope_kind"],
                "valid_from": row["valid_from"],
                "valid_until": row["valid_until"],
                "per_job_cap_cny": row["per_job_cap_cny"],
                "cover_percent": row["cover_percent"],
                "priority": row["priority"],
                "change_note": row["change_note"],
                "content_sha256": row["content_sha256"],
                "created_by": row["created_by"],
                "created_at": row["created_at"],
            }
            for row in rows
        ]

    # -- 作业确认与冻结 ---------------------------------------------------

    def _release_expired_carries(self, as_of: str) -> list[tuple[int, str, Decimal]]:
        """把截至作业服务日已过期的结转余额释放回资助批次可用池。"""
        now = self._now()
        rows = self.connection.execute(
            "SELECT carry_id,batch_id,remaining_cny FROM voucher_carry_balances "
            "WHERE state='open' AND remaining_cny>'0.00' AND expires_on<?",
            (as_of,),
        ).fetchall()
        expired: list[tuple[int, str, Decimal]] = []
        for row in rows:
            amount = Decimal(row["remaining_cny"])
            self.connection.execute(
                "UPDATE voucher_carry_balances SET state='released',updated_at=? WHERE carry_id=?",
                (now, row["carry_id"]),
            )
            self.connection.execute(
                "UPDATE voucher_batches SET frozen_cny=? WHERE batch_id=?",
                (text(self._batch_frozen(row["batch_id"]) - amount), row["batch_id"]),
            )
            expired.append((int(row["carry_id"]), row["batch_id"], amount))
            self._audit(
                "voucher_carry",
                str(row["carry_id"]),
                "carry.expired",
                "system",
                {"batch_id": row["batch_id"], "released_cny": text(amount), "as_of": as_of},
            )
        return expired

    def _gather_candidates(self, job: JobConfirmInput) -> list[BatchCandidate]:
        batch_rows = self.connection.execute(
            "SELECT b.*,v.* FROM voucher_batches b "
            "JOIN voucher_batch_versions v ON v.batch_id=b.batch_id AND v.version=b.current_version "
            "WHERE b.state='active' ORDER BY v.priority,b.batch_id"
        ).fetchall()
        candidates: list[BatchCandidate] = []
        for row in batch_rows:
            tenants = json.loads(row["eligible_tenants_json"])
            if job.tenant_id not in tenants:
                continue
            if not (row["valid_from"] <= job.service_date <= row["valid_until"]):
                continue
            if row["scope_kind"] == "product" and job.product not in json.loads(row["scope_products_json"]):
                continue
            if row["scope_kind"] == "facility" and job.facility_id not in json.loads(row["scope_facilities_json"]):
                continue
            available = money(
                Decimal(row["total_cap_cny"]) - Decimal(row["frozen_cny"]) - Decimal(row["consumed_cny"])
            )
            if available > ZERO:
                candidates.append(
                    BatchCandidate(
                        batch_id=row["batch_id"],
                        sponsor_id=row["sponsor_id"],
                        cover_percent=Decimal(row["cover_percent"]),
                        per_job_cap=Decimal(row["per_job_cap_cny"]),
                        available_cny=available,
                        priority=int(row["priority"]),
                        source="batch",
                        rule_version=int(row["current_version"]),
                    )
                )
            carry_rows = self.connection.execute(
                "SELECT * FROM voucher_carry_balances WHERE batch_id=? AND tenant_id=? AND state='open' "
                "AND remaining_cny>'0.00' AND expires_on>=? ORDER BY expires_on,carry_id",
                (row["batch_id"], job.tenant_id, job.service_date),
            ).fetchall()
            for carry in carry_rows:
                candidates.append(
                    BatchCandidate(
                        batch_id=row["batch_id"],
                        sponsor_id=row["sponsor_id"],
                        cover_percent=Decimal("100"),
                        per_job_cap=Decimal(carry["remaining_cny"]),
                        available_cny=Decimal(carry["remaining_cny"]),
                        priority=int(row["priority"]),
                        source="carry",
                        carry_id=int(carry["carry_id"]),
                        rule_version=int(row["current_version"]),
                    )
                )
        return candidates

    def confirm_job(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "job.write")
        actor = self._user(actor_id)
        job = self._parse(lambda: JobConfirmInput.from_dict(raw))
        if actor["role"] == "tenant" and actor["tenant_id"] != job.tenant_id:
            raise Forbidden("租户只能为自己登记训练作业")
        request_digest = digest(raw)
        now = self._now()
        try:
            with transaction(self.connection, immediate=True):
                # 候选收集、分摊计算与冻结写入必须在同一写事务内，避免并发超卖。
                replayed = self._replay("job.confirm", job.idempotency_key, request_digest)
                if replayed is not None:
                    return replayed | {"replayed": True}
                expired_carries = self._release_expired_carries(job.service_date)
                candidates = self._gather_candidates(job)
                shares = split_plan(job.estimated_cost_cny, candidates)
                sponsored = money(sum((share.amount_cny for share in shares), ZERO))
                allocation = {
                    "estimated_cost_cny": text(money(job.estimated_cost_cny)),
                    "total_sponsored_cny": text(sponsored),
                    "self_paid_cny": text(self_paid(job.estimated_cost_cny, sponsored)),
                    "shares": [share.as_dict() for share in shares],
                    "explanation": [
                        {"batch_id": share.batch_id, "source": share.source, "basis": share.basis}
                        for share in shares
                    ],
                }
                response: dict[str, Any] = {
                    "job_id": job.job_id,
                    "state": "confirmed",
                    "settlement_version": 1,
                    "allocation": allocation,
                    "replayed": False,
                }
                self.connection.execute(
                    "INSERT INTO voucher_jobs(job_id,tenant_id,facility_id,product,service_date,"
                    "estimated_cost_cny,state,plan_json,idempotency_key,created_by,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?, 'confirmed',?,?,?,?,?)",
                    (
                        job.job_id,
                        job.tenant_id,
                        job.facility_id,
                        job.product,
                        job.service_date,
                        text(money(job.estimated_cost_cny)),
                        canonical_json(allocation),
                        job.idempotency_key,
                        actor_id,
                        now,
                        now,
                    ),
                )
                for share in shares:
                    self.connection.execute(
                        "INSERT INTO voucher_job_shares(job_id,batch_id,sponsor_id,rule_version,"
                        "source,carry_id,frozen_cny,basis,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            job.job_id,
                            share.batch_id,
                            share.sponsor_id,
                            share.rule_version,
                            share.source,
                            share.carry_id or 0,
                            text(share.amount_cny),
                            share.basis,
                            now,
                        ),
                    )
                    if share.source == "batch":
                        self.connection.execute(
                            "UPDATE voucher_batches SET frozen_cny=? WHERE batch_id=?",
                            (
                                text(self._batch_frozen(share.batch_id) + share.amount_cny),
                                share.batch_id,
                            ),
                        )
                    else:
                        new_remaining = money(
                            self._carry_remaining(share.carry_id) - share.amount_cny
                        )
                        self.connection.execute(
                            "UPDATE voucher_carry_balances SET remaining_cny=?,"
                            "state=CASE WHEN ?<='0.00' THEN 'consumed' ELSE 'open' END,updated_at=? "
                            "WHERE carry_id=?",
                            (text(new_remaining), text(new_remaining), now, share.carry_id),
                        )
                self.connection.execute(
                    "INSERT INTO voucher_settlement_versions(job_id,version,kind,allocation_json,"
                    "created_by,created_at) VALUES(?,1,'confirmed',?,?,?)",
                    (job.job_id, canonical_json(allocation), actor_id, now),
                )
                self._store_replay("job.confirm", job.idempotency_key, request_digest, response)
                self._audit(
                    "voucher_job",
                    job.job_id,
                    "job.confirmed",
                    actor_id,
                    {
                        "estimated_cost_cny": allocation["estimated_cost_cny"],
                        "total_sponsored_cny": allocation["total_sponsored_cny"],
                        "share_count": len(shares),
                        "expired_carries_released": len(expired_carries),
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("作业编号或幂等键冲突") from exc
        return response

    def _batch_frozen(self, batch_id: str) -> Decimal:
        row = self.connection.execute(
            "SELECT frozen_cny FROM voucher_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        return Decimal(row["frozen_cny"])

    def _carry_remaining(self, carry_id: int | None) -> Decimal:
        row = self.connection.execute(
            "SELECT remaining_cny FROM voucher_carry_balances WHERE carry_id=?", (carry_id,)
        ).fetchone()
        return Decimal(row["remaining_cny"])

    def _job(self, job_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM voucher_jobs WHERE job_id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise NotFound("作业不存在")
        return row

    def _shares(self, job_id: str) -> list[sqlite3.Row]:
        return list(
            self.connection.execute(
                "SELECT * FROM voucher_job_shares WHERE job_id=? ORDER BY share_id", (job_id,)
            ).fetchall()
        )

    # -- 作业结算：完成/失败/部分完成 ------------------------------------

    def settle_job(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "job.settle")
        payload = self._parse(lambda: JobCompleteInput.from_dict(raw))
        request_digest = digest(raw)
        now = self._now()
        with transaction(self.connection, immediate=True):
            replayed = self._replay("job.settle", payload.idempotency_key, request_digest)
            if replayed is not None:
                return replayed | {"replayed": True}
            job = self.connection.execute(
                "SELECT * FROM voucher_jobs WHERE job_id=?", (payload.job_id,)
            ).fetchone()
            if job is None:
                raise NotFound("作业不存在")
            if job["state"] != "confirmed":
                raise InvalidState("只有已确认尚未结算的作业可以上报结果")
            estimated = Decimal(job["estimated_cost_cny"])
            share_rows = self._shares(payload.job_id)
            frozen_shares = [
                {
                    "batch_id": row["batch_id"],
                    "sponsor_id": row["sponsor_id"],
                    "source": row["source"],
                    "carry_id": row["carry_id"] if row["carry_id"] else None,
                    "frozen_cny": row["frozen_cny"],
                }
                for row in share_rows
            ]
            try:
                outcomes = allocate_outcome(
                    payload.result, estimated, frozen_shares, payload.actual_cost_cny
                )
            except ValueError as exc:
                raise ValidationFailed(str(exc)) from exc
            totals = {
                "consumed_cny": text(money(sum((item.consumed_cny for item in outcomes), ZERO))),
                "released_cny": text(money(sum((item.released_cny for item in outcomes), ZERO))),
                "carried_cny": text(money(sum((item.carried_cny for item in outcomes), ZERO))),
            }
            final_self_paid = money(payload.actual_cost_cny - Decimal(totals["consumed_cny"]))
            new_carries: list[dict[str, Any]] = []
            settlement_date = date.fromisoformat(job["service_date"])
            batch_delta: dict[str, dict[str, Decimal]] = {}
            for outcome, row in zip(outcomes, share_rows):
                self.connection.execute(
                    "UPDATE voucher_job_shares SET consumed_cny=?,released_cny=?,"
                    "carried_forward_cny=? WHERE share_id=?",
                    (
                        text(outcome.consumed_cny),
                        text(outcome.released_cny),
                        text(outcome.carried_cny),
                        row["share_id"],
                    ),
                )
                delta = batch_delta.setdefault(outcome.batch_id, {"frozen_leave": ZERO, "consumed_add": ZERO})
                # 核销离开冻结池并计入已核销；结转继续冻结在新余额中。
                delta["frozen_leave"] += outcome.consumed_cny
                delta["consumed_add"] += outcome.consumed_cny
                if outcome.source == "batch":
                    # 批次预算份额释放直接回到批次可用池。
                    delta["frozen_leave"] += outcome.released_cny
                elif outcome.released_cny > ZERO:
                    # 结转份额释放回补原结转余额，冻结池不变。
                    restored = money(self._carry_remaining(outcome.carry_id) + outcome.released_cny)
                    self.connection.execute(
                        "UPDATE voucher_carry_balances SET remaining_cny=?,state='open',updated_at=? "
                        "WHERE carry_id=?",
                        (text(restored), now, outcome.carry_id),
                    )
                if outcome.carried_cny > ZERO:
                    if outcome.source == "carry":
                        source_carry = self.connection.execute(
                            "SELECT expires_on FROM voucher_carry_balances WHERE carry_id=?",
                            (outcome.carry_id,),
                        ).fetchone()
                        expires_on = min(
                            date.fromisoformat(source_carry["expires_on"]),
                            settlement_date + timedelta(days=CARRY_DAYS),
                        ).isoformat()
                    else:
                        expires_on = (settlement_date + timedelta(days=CARRY_DAYS)).isoformat()
                    cursor = self.connection.execute(
                        "INSERT INTO voucher_carry_balances(batch_id,tenant_id,source_job_id,"
                        "remaining_cny,expires_on,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                        (
                            outcome.batch_id,
                            job["tenant_id"],
                            job["job_id"],
                            text(outcome.carried_cny),
                            expires_on,
                            now,
                            now,
                        ),
                    )
                    new_carries.append(
                        {
                            "carry_id": int(cursor.lastrowid),
                            "batch_id": outcome.batch_id,
                            "remaining_cny": text(outcome.carried_cny),
                            "expires_on": expires_on,
                        }
                    )
            for batch_id, delta in batch_delta.items():
                self.connection.execute(
                    "UPDATE voucher_batches SET frozen_cny=?,consumed_cny=? WHERE batch_id=?",
                    (
                        text(self._batch_frozen(batch_id) - delta["frozen_leave"]),
                        text(self._batch_consumed(batch_id) + delta["consumed_add"]),
                        batch_id,
                    ),
                )
            settle_cursor = self.connection.execute(
                "UPDATE voucher_jobs SET state='settled',settled_result=?,actual_cost_cny=?,"
                "self_paid_cny=?,settled_at=?,updated_at=? WHERE job_id=? AND state='confirmed'",
                (
                    payload.result,
                    text(money(payload.actual_cost_cny)),
                    text(final_self_paid),
                    now,
                    now,
                    job["job_id"],
                ),
            )
            if settle_cursor.rowcount != 1:
                raise InvalidState("作业状态已变化，结算失败")
            response = {
                "job_id": job["job_id"],
                "state": "settled",
                "result": payload.result,
                "actual_cost_cny": text(money(payload.actual_cost_cny)),
                "self_paid_cny": text(final_self_paid),
                "totals": totals,
                "sponsors": [outcome.as_dict() for outcome in outcomes],
                "carry_forward": new_carries,
                "replayed": False,
            }
            self._store_replay("job.settle", payload.idempotency_key, request_digest, response)
            self._audit(
                "voucher_job",
                job["job_id"],
                "job.settled",
                actor_id,
                {"result": payload.result, **totals, "self_paid_cny": text(final_self_paid)},
            )
        return response

    def _batch_consumed(self, batch_id: str) -> Decimal:
        row = self.connection.execute(
            "SELECT consumed_cny FROM voucher_batches WHERE batch_id=?", (batch_id,)
        ).fetchone()
        return Decimal(row["consumed_cny"])

    # -- 取消：释放全部冻结额度 -------------------------------------------

    def cancel_job(self, actor_id: str, job_id: str, idempotency_key: str) -> dict[str, Any]:
        self._require(actor_id, "job.write")
        actor = self._user(actor_id)
        key_digest = digest({"job_id": job_id, "idempotency_key": idempotency_key})
        now = self._now()
        with transaction(self.connection, immediate=True):
            replayed = self._replay("job.cancel", idempotency_key, key_digest)
            if replayed is not None:
                return replayed | {"replayed": True}
            job = self.connection.execute(
                "SELECT * FROM voucher_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise NotFound("作业不存在")
            if actor["role"] == "tenant" and actor["tenant_id"] != job["tenant_id"]:
                raise Forbidden("租户只能取消自己的作业")
            if job["state"] != "confirmed":
                raise InvalidState("只有已确认尚未结算的作业可以取消")
            released_total = ZERO
            for row in self._shares(job_id):
                amount = Decimal(row["frozen_cny"])
                released_total += amount
                if row["source"] == "batch":
                    # 批次预算份额释放回可用池。
                    self.connection.execute(
                        "UPDATE voucher_batches SET frozen_cny=? WHERE batch_id=?",
                        (text(self._batch_frozen(row["batch_id"]) - amount), row["batch_id"]),
                    )
                else:
                    # 结转份额释放回补原结转余额，冻结池总额不变。
                    self.connection.execute(
                        "UPDATE voucher_carry_balances SET remaining_cny=?,state='open',updated_at=? "
                        "WHERE carry_id=?",
                        (text(self._carry_remaining(row["carry_id"]) + amount), now, row["carry_id"]),
                    )
                self.connection.execute(
                    "UPDATE voucher_job_shares SET released_cny=? WHERE share_id=?",
                    (text(amount), row["share_id"]),
                )
            cancel_cursor = self.connection.execute(
                "UPDATE voucher_jobs SET state='cancelled',cancelled_at=?,updated_at=? "
                "WHERE job_id=? AND state='confirmed'",
                (now, now, job_id),
            )
            if cancel_cursor.rowcount != 1:
                raise InvalidState("作业状态已变化，取消失败")
            response = {
                "job_id": job_id,
                "state": "cancelled",
                "released_cny": text(money(released_total)),
                "replayed": False,
            }
            self._store_replay("job.cancel", idempotency_key, key_digest, response)
            self._audit(
                "voucher_job",
                job_id,
                "job.cancelled",
                actor_id,
                {"released_cny": response["released_cny"]},
            )
        return response

    # -- 人工调整：双人复核与新版本 ---------------------------------------

    def request_adjustment(self, actor_id: str, raw: Mapping[str, Any]) -> dict[str, Any]:
        self._require(actor_id, "adjustment.request")
        actor = self._user(actor_id)
        adjustment = self._parse(lambda: AdjustmentInput.from_dict(raw))
        if adjustment.delta_cny == ZERO:
            raise ValidationFailed("调整金额不能为零")
        request_digest = digest(raw)
        replayed = self._replay("adjustment.request", adjustment.idempotency_key, request_digest)
        if replayed is not None:
            return replayed | {"replayed": True}
        job = self._job(adjustment.job_id)
        if job["state"] != "settled":
            raise InvalidState("只能调整已结算作业")
        if job["settled_result"] == "failed":
            raise InvalidState("失败作业没有可调整的核销金额")
        matching = [
            row for row in self._shares(adjustment.job_id) if row["batch_id"] == adjustment.batch_id
        ]
        if not matching:
            raise ValidationFailed("该作业没有来自此券批次的分摊份额")
        if actor["role"] == "sponsor_admin":
            sponsor_id = matching[0]["sponsor_id"]
            if actor["sponsor_id"] != sponsor_id:
                raise Forbidden("资助方管理员只能申请调整本资助方的核销金额")
        current_consumed = money(sum((Decimal(row["consumed_cny"]) + Decimal(row["adjusted_cny"])) for row in matching))
        sponsor_total = money(
            sum(
                (Decimal(row["consumed_cny"]) + Decimal(row["adjusted_cny"]))
                for row in self._shares(adjustment.job_id)
            )
        )
        actual = Decimal(job["actual_cost_cny"])
        new_batch_consumed = money(current_consumed + adjustment.delta_cny)
        new_sponsor_total = money(sponsor_total + adjustment.delta_cny)
        if new_batch_consumed < ZERO:
            raise ValidationFailed("调整后该资助方核销金额不能为负")
        if new_sponsor_total > actual:
            raise ValidationFailed("调整后资助总额不能超过作业实际费用（企业自付不能为负）")
        if adjustment.delta_cny > ZERO:
            batch = self.connection.execute(
                "SELECT * FROM voucher_batches WHERE batch_id=?", (adjustment.batch_id,)
            ).fetchone()
            available = money(
                Decimal(batch["total_cap_cny"]) - Decimal(batch["frozen_cny"]) - Decimal(batch["consumed_cny"])
            )
            if available < adjustment.delta_cny:
                raise ValidationFailed(
                    f"券批次可用额度不足，当前可追加核销 {text(available)} 元"
                )
        try:
            with transaction(self.connection, immediate=True):
                cursor = self.connection.execute(
                    "INSERT INTO voucher_adjustments(job_id,batch_id,delta_cny,reason,state,"
                    "requested_by,requested_at,idempotency_key) VALUES(?,?,?,?,'pending',?,?,?)",
                    (
                        adjustment.job_id,
                        adjustment.batch_id,
                        text(adjustment.delta_cny),
                        adjustment.reason,
                        actor_id,
                        self._now(),
                        adjustment.idempotency_key,
                    ),
                )
                adjustment_id = int(cursor.lastrowid)
                response = {
                    "adjustment_id": adjustment_id,
                    "job_id": adjustment.job_id,
                    "batch_id": adjustment.batch_id,
                    "delta_cny": text(adjustment.delta_cny),
                    "state": "pending",
                    "replayed": False,
                }
                self._store_replay(
                    "adjustment.request", adjustment.idempotency_key, request_digest, response
                )
                self._audit(
                    "voucher_adjustment",
                    str(adjustment_id),
                    "adjustment.requested",
                    actor_id,
                    {"job_id": adjustment.job_id, "batch_id": adjustment.batch_id, "delta_cny": text(adjustment.delta_cny)},
                )
        except sqlite3.IntegrityError as exc:
            raise Conflict("调整申请幂等键冲突") from exc
        return response

    def review_adjustment(
        self, actor_id: str, adjustment_id: int, approve: bool, note: str
    ) -> dict[str, Any]:
        self._require(actor_id, "adjustment.review")
        row = self.connection.execute(
            "SELECT * FROM voucher_adjustments WHERE adjustment_id=?", (adjustment_id,)
        ).fetchone()
        if row is None:
            raise NotFound("调整申请不存在")
        if row["state"] != "pending":
            raise InvalidState("调整申请已经复核")
        if row["requested_by"] == actor_id:
            raise Forbidden("申请人不能复核自己的调整，必须由第二人双人复核")
        now = self._now()
        with transaction(self.connection, immediate=True):
            if not approve:
                self.connection.execute(
                    "UPDATE voucher_adjustments SET state='rejected',reviewed_by=?,reviewed_at=?,"
                    "review_note=? WHERE adjustment_id=? AND state='pending'",
                    (actor_id, now, note[:512], adjustment_id),
                )
                self._audit(
                    "voucher_adjustment",
                    str(adjustment_id),
                    "adjustment.rejected",
                    actor_id,
                    {"job_id": row["job_id"], "note": note[:512]},
                )
                return {"adjustment_id": adjustment_id, "state": "rejected"}
            # 复核时重新校验预算与作业状态，防止申请挂起期间额度变化。
            job = self._job(row["job_id"])
            if job["state"] != "settled":
                raise InvalidState("作业状态已变化，不能批准调整")
            delta = Decimal(row["delta_cny"])
            target_rows = [
                item for item in self._shares(row["job_id"]) if item["batch_id"] == row["batch_id"]
            ]
            current_consumed = money(
                sum((Decimal(item["consumed_cny"]) + Decimal(item["adjusted_cny"])) for item in target_rows)
            )
            sponsor_total = money(
                sum(
                    (Decimal(item["consumed_cny"]) + Decimal(item["adjusted_cny"]))
                    for item in self._shares(row["job_id"])
                )
            )
            new_batch_consumed = money(current_consumed + delta)
            new_sponsor_total = money(sponsor_total + delta)
            if new_batch_consumed < ZERO or new_sponsor_total > Decimal(job["actual_cost_cny"]):
                raise InvalidState("调整金额已不再满足核销边界")
            batch = self.connection.execute(
                "SELECT * FROM voucher_batches WHERE batch_id=?", (row["batch_id"],)
            ).fetchone()
            available = money(
                Decimal(batch["total_cap_cny"]) - Decimal(batch["frozen_cny"]) - Decimal(batch["consumed_cny"])
            )
            if delta > ZERO and available < delta:
                raise InvalidState("券批次可用额度已不足，不能批准追加核销")
            target = next((item for item in target_rows if item["source"] == "batch"), target_rows[0])
            # 份额的实际核销数不变，差额累计在 adjusted_cny；展示口径为两者之和。
            self.connection.execute(
                "UPDATE voucher_job_shares SET adjusted_cny=? WHERE share_id=?",
                (text(money(Decimal(target["adjusted_cny"]) + delta)), target["share_id"]),
            )
            self.connection.execute(
                "UPDATE voucher_batches SET consumed_cny=? WHERE batch_id=?",
                (text(self._batch_consumed(row["batch_id"]) + delta), row["batch_id"]),
            )
            new_self_paid = money(Decimal(job["self_paid_cny"]) - delta)
            new_version = int(job["settlement_version"]) + 1
            self.connection.execute(
                "UPDATE voucher_jobs SET self_paid_cny=?,settlement_version=?,updated_at=? WHERE job_id=?",
                (text(new_self_paid), new_version, now, row["job_id"]),
            )
            self.connection.execute(
                "UPDATE voucher_adjustments SET state='approved',reviewed_by=?,reviewed_at=?,"
                "review_note=? WHERE adjustment_id=? AND state='pending'",
                (actor_id, now, note[:512], adjustment_id),
            )
            allocation = self._allocation_snapshot(row["job_id"])
            self.connection.execute(
                "INSERT INTO voucher_settlement_versions(job_id,version,kind,adjustment_id,"
                "allocation_json,created_by,created_at) VALUES(?,?, 'adjusted',?,?,?,?)",
                (row["job_id"], new_version, adjustment_id, canonical_json(allocation), actor_id, now),
            )
            self._audit(
                "voucher_adjustment",
                str(adjustment_id),
                "adjustment.approved",
                actor_id,
                {
                    "job_id": row["job_id"],
                    "batch_id": row["batch_id"],
                    "delta_cny": text(delta),
                    "new_version": new_version,
                },
            )
        return {
            "adjustment_id": adjustment_id,
            "state": "approved",
            "job_id": row["job_id"],
            "settlement_version": new_version,
        }

    def _allocation_snapshot(self, job_id: str) -> dict[str, Any]:
        job = self._job(job_id)
        shares = []
        for row in self._shares(job_id):
            shares.append(
                {
                    "batch_id": row["batch_id"],
                    "sponsor_id": row["sponsor_id"],
                    "source": row["source"],
                    "carry_id": row["carry_id"] or None,
                    "rule_version": row["rule_version"],
                    "frozen_cny": row["frozen_cny"],
                    "consumed_cny": text(money(Decimal(row["consumed_cny"]) + Decimal(row["adjusted_cny"]))),
                    "released_cny": row["released_cny"],
                    "carried_forward_cny": row["carried_forward_cny"],
                    "adjusted_cny": row["adjusted_cny"],
                    "basis": row["basis"],
                }
            )
        return {
            "estimated_cost_cny": job["estimated_cost_cny"],
            "actual_cost_cny": job["actual_cost_cny"],
            "self_paid_cny": job["self_paid_cny"],
            "shares": shares,
        }

    # -- 按权限可见的明细查询 ---------------------------------------------

    def job_detail(self, actor_id: str, job_id: str) -> dict[str, Any]:
        user = self._user(actor_id)
        job = self._job(job_id)
        share_rows = self._shares(job_id)
        role = user["role"]
        if role == "tenant":
            if user["tenant_id"] != job["tenant_id"]:
                raise Forbidden("租户只能查看本租户作业明细")
            visible_share_rows = share_rows
        elif role == "sponsor_admin":
            visible_share_rows = [row for row in share_rows if row["sponsor_id"] == user["sponsor_id"]]
            if not visible_share_rows:
                raise Forbidden("资助方只能查看本资助方参与的作业明细")
        else:
            if role not in {"operator", "reviewer", "auditor"}:
                raise Forbidden("当前角色无权查看作业明细")
            visible_share_rows = share_rows
        versions = self.connection.execute(
            "SELECT version,kind,adjustment_id,created_by,created_at,allocation_json "
            "FROM voucher_settlement_versions WHERE job_id=? ORDER BY version",
            (job_id,),
        ).fetchall()
        confirmed_plan = json.loads(job["plan_json"])
        if role == "sponsor_admin":
            # 资助方只能看到本资助方的份额，企业自付与其他资助方信息脱敏。
            sponsor_batch_ids = {
                share["batch_id"]
                for share in confirmed_plan["shares"]
                if share["sponsor_id"] == user["sponsor_id"]
            }
            confirmed_plan = {
                **confirmed_plan,
                "shares": [
                    share
                    for share in confirmed_plan["shares"]
                    if share["batch_id"] in sponsor_batch_ids
                ],
                "explanation": [
                    item
                    for item in confirmed_plan["explanation"]
                    if item["batch_id"] in sponsor_batch_ids
                ],
                "total_sponsored_cny": None,
                "self_paid_cny": None,
            }
        detail = {
            "job_id": job["job_id"],
            "tenant_id": job["tenant_id"],
            "facility_id": job["facility_id"],
            "product": job["product"],
            "service_date": job["service_date"],
            "state": job["state"],
            "settled_result": job["settled_result"],
            "settlement_version": job["settlement_version"],
            "estimated_cost_cny": job["estimated_cost_cny"],
            "actual_cost_cny": job["actual_cost_cny"],
            "self_paid_cny": job["self_paid_cny"],
            "confirmed_plan": confirmed_plan,
            "shares": [
                {
                    "batch_id": row["batch_id"],
                    "sponsor_id": row["sponsor_id"],
                    "source": row["source"],
                    "rule_version": row["rule_version"],
                    "frozen_cny": row["frozen_cny"],
                    "consumed_cny": text(money(Decimal(row["consumed_cny"]) + Decimal(row["adjusted_cny"]))),
                    "released_cny": row["released_cny"],
                    "carried_forward_cny": row["carried_forward_cny"],
                    "basis": row["basis"],
                }
                for row in visible_share_rows
            ],
            "redacted_shares": len(share_rows) - len(visible_share_rows),
            "versions": [
                {
                    "version": row["version"],
                    "kind": row["kind"],
                    "adjustment_id": row["adjustment_id"],
                    "created_by": row["created_by"],
                    "created_at": row["created_at"],
                }
                for row in versions
            ],
        }
        if role == "sponsor_admin":
            # 资助方不需要看到企业自付与其他租户线索之外的内部版本全文。
            detail["self_paid_cny"] = None
        return detail

    def tenant_carry_balances(self, actor_id: str) -> list[dict[str, Any]]:
        user = self._require(actor_id, "tenant.read")
        rows = self.connection.execute(
            "SELECT c.carry_id,c.batch_id,b.sponsor_id,c.source_job_id,c.remaining_cny,"
            "c.expires_on,c.state,c.updated_at FROM voucher_carry_balances c "
            "JOIN voucher_batches b ON b.batch_id=c.batch_id "
            "WHERE c.tenant_id=? ORDER BY c.expires_on,c.carry_id",
            (user["tenant_id"],),
        ).fetchall()
        return [dict(row) for row in rows]

    def sponsor_report(self, actor_id: str) -> dict[str, Any]:
        user = self._require(actor_id, "sponsor.read")
        rows = self.connection.execute(
            "SELECT batch_id,name,state,current_version,total_cap_cny,frozen_cny,consumed_cny "
            "FROM voucher_batches WHERE sponsor_id=? ORDER BY batch_id",
            (user["sponsor_id"],),
        ).fetchall()
        batches = []
        for row in rows:
            batches.append(
                {
                    "batch_id": row["batch_id"],
                    "name": row["name"],
                    "state": row["state"],
                    "version": row["current_version"],
                    "total_cap_cny": row["total_cap_cny"],
                    "frozen_cny": row["frozen_cny"],
                    "consumed_cny": row["consumed_cny"],
                    "available_cny": text(
                        money(
                            Decimal(row["total_cap_cny"])
                            - Decimal(row["frozen_cny"])
                            - Decimal(row["consumed_cny"])
                        )
                    ),
                }
            )
        return {"sponsor_id": user["sponsor_id"], "batches": batches}

    def audit_chain(self, actor_id: str) -> dict[str, Any]:
        self._require(actor_id, "audit.read")
        rows = self.connection.execute(
            "SELECT * FROM voucher_audit_events ORDER BY event_id"
        ).fetchall()
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
