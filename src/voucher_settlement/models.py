"""算力券领域的输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
SCOPE_KINDS = {"product", "facility", "any"}
JOB_RESULTS = {"completed", "failed", "partial"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValueError(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValueError(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValueError(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValueError(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValueError(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValueError(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValueError(f"{field} 不能大于 {maximum}")
    return result


def date_text(value: object, field: str) -> str:
    result = required_text(value, field, 10)
    try:
        return date.fromisoformat(result).isoformat()
    except ValueError as exc:
        raise ValueError(f"{field} 必须是 YYYY-MM-DD 日期") from exc


@dataclass(frozen=True, slots=True)
class VoucherBatchInput:
    """券批次登记内容：资助方、适用租户、资源范围、有效期与资助上限。"""

    batch_id: str
    sponsor_id: str
    name: str
    eligible_tenants: tuple[str, ...]
    scope_kind: str
    scope_products: tuple[str, ...]
    scope_facilities: tuple[str, ...]
    valid_from: str
    valid_until: str
    total_cap_cny: Decimal
    per_job_cap_cny: Decimal
    cover_percent: Decimal
    priority: int

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "VoucherBatchInput":
        scope_kind = required_text(raw.get("scope_kind"), "scope_kind", 16).lower()
        if scope_kind not in SCOPE_KINDS:
            raise ValueError("scope_kind 必须是 product、facility 或 any")
        eligible_tenants = raw.get("eligible_tenants")
        if not isinstance(eligible_tenants, list) or not eligible_tenants:
            raise ValueError("eligible_tenants 必须是非空数组")
        tenants = tuple(identifier(item, "eligible_tenants") for item in eligible_tenants)
        if len(set(tenants)) != len(tenants):
            raise ValueError("eligible_tenants 不能重复")
        products_raw = raw.get("scope_products", [])
        facilities_raw = raw.get("scope_facilities", [])
        if not isinstance(products_raw, list) or not isinstance(facilities_raw, list):
            raise ValueError("资源范围必须是数组")
        products = tuple(
            required_text(item, "scope_products", 32) for item in products_raw
        )
        facilities = tuple(
            identifier(item, "scope_facilities") for item in facilities_raw
        )
        if scope_kind == "product" and not products:
            raise ValueError("scope_kind=product 时 scope_products 不能为空")
        if scope_kind == "facility" and not facilities:
            raise ValueError("scope_kind=facility 时 scope_facilities 不能为空")
        priority = raw.get("priority", 100)
        if isinstance(priority, bool) or not isinstance(priority, int) or not 1 <= priority <= 999:
            raise ValueError("priority 必须是 1 到 999 的整数")
        valid_from = date_text(raw.get("valid_from"), "valid_from")
        valid_until = date_text(raw.get("valid_until"), "valid_until")
        if valid_until < valid_from:
            raise ValueError("valid_until 不能早于 valid_from")
        total_cap = decimal_value(raw.get("total_cap_cny"), "total_cap_cny", minimum=Decimal("0.01"))
        per_job_cap = decimal_value(
            raw.get("per_job_cap_cny", total_cap), "per_job_cap_cny", minimum=Decimal("0.01")
        )
        if per_job_cap > total_cap:
            raise ValueError("per_job_cap_cny 不能大于 total_cap_cny")
        return cls(
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            sponsor_id=identifier(raw.get("sponsor_id"), "sponsor_id"),
            name=required_text(raw.get("name"), "name"),
            eligible_tenants=tenants,
            scope_kind=scope_kind,
            scope_products=products,
            scope_facilities=facilities,
            valid_from=valid_from,
            valid_until=valid_until,
            total_cap_cny=total_cap,
            per_job_cap_cny=per_job_cap,
            cover_percent=decimal_value(
                raw.get("cover_percent", 100), "cover_percent",
                minimum=Decimal("0.01"), maximum=Decimal("100"),
            ),
            priority=priority,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "batch_id": self.batch_id,
            "sponsor_id": self.sponsor_id,
            "name": self.name,
            "eligible_tenants": list(self.eligible_tenants),
            "scope_kind": self.scope_kind,
            "scope_products": list(self.scope_products),
            "scope_facilities": list(self.scope_facilities),
            "valid_from": self.valid_from,
            "valid_until": self.valid_until,
            "total_cap_cny": format(self.total_cap_cny, "f"),
            "per_job_cap_cny": format(self.per_job_cap_cny, "f"),
            "cover_percent": format(self.cover_percent, "f"),
            "priority": self.priority,
        }


@dataclass(frozen=True, slots=True)
class JobConfirmInput:
    """作业确认：租户、资源范围、预计费用与业务幂等键。"""

    job_id: str
    tenant_id: str
    facility_id: str
    product: str
    service_date: str
    estimated_cost_cny: Decimal
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "JobConfirmInput":
        cost = decimal_value(
            raw.get("estimated_cost_cny"), "estimated_cost_cny", minimum=Decimal("0.01")
        )
        return cls(
            job_id=identifier(raw.get("job_id"), "job_id"),
            tenant_id=identifier(raw.get("tenant_id"), "tenant_id"),
            facility_id=identifier(raw.get("facility_id"), "facility_id"),
            product=required_text(raw.get("product"), "product", 32),
            service_date=date_text(raw.get("service_date"), "service_date"),
            estimated_cost_cny=cost,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class JobCompleteInput:
    """作业结算：实际消耗与结果（完成/失败/部分完成）。"""

    job_id: str
    actual_cost_cny: Decimal
    result: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "JobCompleteInput":
        result = required_text(raw.get("result"), "result", 16).lower()
        if result not in JOB_RESULTS:
            raise ValueError("result 必须是 completed、failed 或 partial")
        return cls(
            job_id=identifier(raw.get("job_id"), "job_id"),
            actual_cost_cny=decimal_value(
                raw.get("actual_cost_cny"), "actual_cost_cny", minimum=Decimal("0")
            ),
            result=result,
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class AdjustmentInput:
    """人工调整申请：只允许针对已结算作业的资助份额进行差额调整。"""

    job_id: str
    batch_id: str
    delta_cny: Decimal
    reason: str
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "AdjustmentInput":
        return cls(
            job_id=identifier(raw.get("job_id"), "job_id"),
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            delta_cny=decimal_value(raw.get("delta_cny"), "delta_cny"),
            reason=required_text(raw.get("reason"), "reason", 512),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )
