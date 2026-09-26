"""算力券与联合资助分摊的输入契约。"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping, Sequence

from .clock import parse_utc, utc_text
from .errors import ValidationFailed


IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{1,63}$")
BATCH_KINDS = {"city_voucher", "park_subsidy"}
FUNDER_KIND_LABELS = {"city_voucher": "市级算力券", "park_subsidy": "园区补贴", "enterprise_self": "企业自付"}
RESOURCES = {"gpu-h100", "gpu-a100", "gpu-l40s", "accelerator-npu", "cpu-highmem", "storage-io"}
SETTLE_OUTCOMES = {"completed", "partial", "failed", "cancelled"}


def required_text(value: object, field: str, maximum: int = 256) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationFailed(f"{field} 不能为空")
    result = value.strip()
    if len(result) > maximum:
        raise ValidationFailed(f"{field} 不能超过 {maximum} 个字符")
    return result


def identifier(value: object, field: str) -> str:
    result = required_text(value, field, 64)
    if not IDENTIFIER.fullmatch(result):
        raise ValidationFailed(f"{field} 格式不正确")
    return result


def decimal_value(
    value: object,
    field: str,
    *,
    minimum: Decimal | None = None,
    maximum: Decimal | None = None,
) -> Decimal:
    if isinstance(value, bool):
        raise ValidationFailed(f"{field} 必须是数值")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise ValidationFailed(f"{field} 必须是十进制数值") from exc
    if not result.is_finite():
        raise ValidationFailed(f"{field} 必须是有限数值")
    if minimum is not None and result < minimum:
        raise ValidationFailed(f"{field} 不能小于 {minimum}")
    if maximum is not None and result > maximum:
        raise ValidationFailed(f"{field} 不能大于 {maximum}")
    return result


def scope_list(value: object, field: str, allowed: set[str]) -> tuple[str, ...]:
    """适用租户或资源范围：["*"] 表示全部，否则为受支持的标识列表。"""
    if not isinstance(value, Sequence) or isinstance(value, str) or not value:
        raise ValidationFailed(f"{field} 必须是非空列表")
    result: list[str] = []
    for item in value:
        text = required_text(item, field, 64)
        if text == "*":
            if len(value) > 1:
                raise ValidationFailed(f"{field} 使用 * 时不能再列其他项")
            return ("*",)
        text = identifier(text, field)
        if text not in allowed:
            raise ValidationFailed(f"{field} 包含不受支持的取值 {text}")
        if text in result:
            raise ValidationFailed(f"{field} 存在重复取值 {text}")
        result.append(text)
    return tuple(result)


def tenant_scope(value: object, field: str = "tenant_scope") -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str) or not value:
        raise ValidationFailed(f"{field} 必须是非空列表")
    result: list[str] = []
    for item in value:
        text = required_text(item, field, 64)
        if text == "*":
            if len(value) > 1:
                raise ValidationFailed(f"{field} 使用 * 时不能再列其他项")
            return ("*",)
        text = identifier(text, field)
        if text in result:
            raise ValidationFailed(f"{field} 存在重复取值 {text}")
        result.append(text)
    return tuple(result)


def resource_scope(value: object, field: str = "resource_scope") -> tuple[str, ...]:
    return scope_list(value, field, RESOURCES)


def utc_field(value: object, field: str) -> str:
    text = required_text(value, field, 40)
    try:
        parsed = parse_utc(text, field)
    except ValueError as exc:
        raise ValidationFailed(str(exc)) from exc
    return utc_text(parsed)


@dataclass(frozen=True, slots=True)
class VoucherBatch:
    batch_id: str
    kind: str
    tenant_scope: tuple[str, ...]
    resource_scope: tuple[str, ...]
    valid_from: str
    valid_until: str
    total_cap_cny: Decimal
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "VoucherBatch":
        kind = required_text(raw.get("kind"), "kind", 32)
        if kind not in BATCH_KINDS:
            raise ValidationFailed("kind 必须是 city_voucher 或 park_subsidy")
        valid_from = utc_field(raw.get("valid_from"), "valid_from")
        valid_until = utc_field(raw.get("valid_until"), "valid_until")
        if valid_until <= valid_from:
            raise ValidationFailed("valid_until 必须晚于 valid_from")
        return cls(
            batch_id=identifier(raw.get("batch_id"), "batch_id"),
            kind=kind,
            tenant_scope=tenant_scope(raw.get("tenant_scope")),
            resource_scope=resource_scope(raw.get("resource_scope")),
            valid_from=valid_from,
            valid_until=valid_until,
            total_cap_cny=decimal_value(raw.get("total_cap_cny"), "total_cap_cny", minimum=Decimal("0.01")),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class RuleLayer:
    """资助规则中的一层核销顺序：券种类加单作业分摊比例上限，或企业自付兜底。"""

    kind: str
    max_share_percent: Decimal | None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind}
        if self.max_share_percent is not None:
            result["max_share_percent"] = format(self.max_share_percent, "f")
        return result


def parse_layers(value: object) -> tuple[RuleLayer, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str) or not value:
        raise ValidationFailed("layers 必须是非空列表")
    layers: list[RuleLayer] = []
    seen: set[str] = set()
    for index, item in enumerate(value):
        field = f"layers[{index}]"
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"{field} 必须是对象")
        kind = required_text(item.get("kind"), f"{field}.kind", 32)
        if kind not in BATCH_KINDS and kind != "enterprise_self":
            raise ValidationFailed(f"{field}.kind 必须是 city_voucher、park_subsidy 或 enterprise_self")
        if kind in seen:
            raise ValidationFailed(f"{field}.kind 重复 {kind}")
        seen.add(kind)
        share = item.get("max_share_percent")
        if kind == "enterprise_self":
            if share is not None:
                raise ValidationFailed("enterprise_self 层不能设置 max_share_percent")
            layers.append(RuleLayer(kind, None))
        else:
            layers.append(
                RuleLayer(
                    kind,
                    decimal_value(
                        share, f"{field}.max_share_percent", minimum=Decimal("0.01"), maximum=Decimal("100")
                    ),
                )
            )
    if layers[-1].kind != "enterprise_self":
        raise ValidationFailed("layers 最后一层必须是 enterprise_self 作为兜底")
    return tuple(layers)


@dataclass(frozen=True, slots=True)
class JobRequest:
    job_id: str
    tenant_id: str
    resource: str
    estimated_cost_cny: Decimal
    idempotency_key: str

    @classmethod
    def from_dict(cls, raw: Mapping[str, Any]) -> "JobRequest":
        resource = required_text(raw.get("resource"), "resource", 32)
        if resource not in RESOURCES:
            raise ValidationFailed("resource 不是受支持的资源类型")
        return cls(
            job_id=identifier(raw.get("job_id"), "job_id"),
            tenant_id=identifier(raw.get("tenant_id"), "tenant_id"),
            resource=resource,
            estimated_cost_cny=decimal_value(
                raw.get("estimated_cost_cny"), "estimated_cost_cny", minimum=Decimal("0.01")
            ),
            idempotency_key=identifier(raw.get("idempotency_key"), "idempotency_key"),
        )


@dataclass(frozen=True, slots=True)
class AdjustmentLine:
    """人工调整后的单行分摊：券批次或企业自付。"""

    kind: str
    amount_cny: Decimal
    batch_id: str | None

    def as_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {"kind": self.kind, "amount_cny": format(self.amount_cny, "f")}
        if self.batch_id is not None:
            result["batch_id"] = self.batch_id
        return result


def parse_adjustment_lines(value: object) -> tuple[AdjustmentLine, ...]:
    if not isinstance(value, Sequence) or isinstance(value, str) or not value:
        raise ValidationFailed("lines 必须是非空列表")
    lines: list[AdjustmentLine] = []
    seen_batches: set[str] = set()
    self_seen = False
    for index, item in enumerate(value):
        field = f"lines[{index}]"
        if not isinstance(item, Mapping):
            raise ValidationFailed(f"{field} 必须是对象")
        kind = required_text(item.get("kind"), f"{field}.kind", 32)
        if kind == "enterprise_self":
            if self_seen:
                raise ValidationFailed("enterprise_self 行只能出现一次")
            self_seen = True
            amount = decimal_value(item.get("amount_cny"), f"{field}.amount_cny", minimum=Decimal("0"))
            lines.append(AdjustmentLine(kind, amount, None))
            continue
        amount = decimal_value(item.get("amount_cny"), f"{field}.amount_cny", minimum=Decimal("0.01"))
        if kind not in BATCH_KINDS:
            raise ValidationFailed(f"{field}.kind 必须是 city_voucher、park_subsidy 或 enterprise_self")
        batch_id = identifier(item.get("batch_id"), f"{field}.batch_id")
        if batch_id in seen_batches:
            raise ValidationFailed(f"批次 {batch_id} 在调整中重复出现")
        seen_batches.add(batch_id)
        lines.append(AdjustmentLine(kind, amount, batch_id))
    if not self_seen:
        raise ValidationFailed("lines 必须包含一行 enterprise_self")
    return tuple(lines)
