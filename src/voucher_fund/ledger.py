"""确定性的联合资助分摊与核销计算，全部使用 Decimal 并可直接序列化。"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Mapping, Sequence


ZERO = Decimal("0")
HUNDRED = Decimal("100")
KIND_LABELS = {"city_voucher": "市级算力券", "park_subsidy": "园区补贴", "enterprise_self": "企业自付"}


def quantize_money(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def decimal_text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class LayerSpec:
    kind: str
    max_share_percent: Decimal | None


@dataclass(frozen=True, slots=True)
class BatchCapacity:
    """确认作业时某券批次的可用额度快照。"""

    batch_id: str
    kind: str
    valid_until: str
    remaining_cny: Decimal


def _label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


def build_plan(
    estimated_cost: Decimal,
    layers: Sequence[LayerSpec],
    batches: Iterable[BatchCapacity],
) -> dict[str, object]:
    """按核销顺序把预估费用分摊到各资助层，返回可解释的方案。

    券层按 max_share_percent 占预估费用的比例封顶，同层多个批次按有效期
    先到先核销（FEFO，并列时按批次号）依次承担；券层承担不足的部分顺次
    下落，最终由企业自付兜底。
    """
    if estimated_cost <= ZERO:
        raise ValueError("预估费用必须大于零")
    estimate = quantize_money(estimated_cost)
    by_kind: dict[str, list[BatchCapacity]] = {}
    for batch in batches:
        if batch.remaining_cny < ZERO:
            raise ValueError("批次剩余额度不能为负数")
        by_kind.setdefault(batch.kind, []).append(batch)
    for kind_batches in by_kind.values():
        kind_batches.sort(key=lambda item: (item.valid_until, item.batch_id))

    lines: list[dict[str, object]] = []
    notes: list[str] = []
    covered = ZERO
    for layer_index, layer in enumerate(layers, start=1):
        if layer.kind == "enterprise_self":
            continue
        assert layer.max_share_percent is not None
        target = quantize_money(estimate * layer.max_share_percent / HUNDRED)
        layer_target = min(target, estimate - covered)
        allocated = ZERO
        for batch in by_kind.get(layer.kind, []):
            if allocated >= layer_target:
                break
            take = quantize_money(min(batch.remaining_cny, layer_target - allocated))
            if take <= ZERO:
                continue
            allocated += take
            lines.append(
                {
                    "layer": layer_index,
                    "kind": layer.kind,
                    "batch_id": batch.batch_id,
                    "amount_cny": decimal_text(take),
                    "reason": (
                        f"规则第{layer_index}层{_label(layer.kind)}按{decimal_text(layer.max_share_percent)}%封顶"
                        f"（上限{decimal_text(target)}元），批次{batch.batch_id}剩余额度"
                        f"{decimal_text(batch.remaining_cny)}元，承担{decimal_text(take)}元"
                    ),
                }
            )
        covered += allocated
        shortfall = quantize_money(layer_target - allocated)
        if shortfall > ZERO:
            notes.append(
                f"规则第{layer_index}层{_label(layer.kind)}目标{decimal_text(layer_target)}元，"
                f"可用批次额度不足，{decimal_text(shortfall)}元转入后续层承担"
            )
    self_pay = quantize_money(estimate - covered)
    if self_pay > ZERO:
        lines.append(
            {
                "layer": len(layers),
                "kind": "enterprise_self",
                "batch_id": None,
                "amount_cny": decimal_text(self_pay),
                "reason": f"各资助层承担后剩余{decimal_text(self_pay)}元由企业自付",
            }
        )
    return {
        "estimated_cost_cny": decimal_text(estimate),
        "voucher_total_cny": decimal_text(quantize_money(covered)),
        "self_pay_cny": decimal_text(self_pay),
        "lines": lines,
        "notes": notes,
    }


def compute_settlement(
    lines: Sequence[Mapping[str, object]],
    actual_cost: Decimal,
    outcome: str,
) -> dict[str, object]:
    """按实际消耗核销已冻结额度。

    completed/partial：按方案行顺序核销，每行核销不超过冻结额，未用部分结转；
    failed/cancelled：不核销，全部冻结额释放。企业自付行为信息行，不参与冻结。
    """
    if outcome not in {"completed", "partial", "failed", "cancelled"}:
        raise ValueError("未知的核销结果类型")
    if actual_cost < ZERO:
        raise ValueError("实际消耗不能为负数")
    actual = quantize_money(actual_cost)
    resolutions: list[dict[str, object]] = []
    if outcome in {"failed", "cancelled"}:
        for line in lines:
            if line["kind"] == "enterprise_self":
                continue
            amount = Decimal(str(line["amount_cny"]))
            resolutions.append(
                {
                    "batch_id": line["batch_id"],
                    "state": "released",
                    "redeemed_cny": "0.00",
                    "returned_cny": decimal_text(amount),
                }
            )
        return {
            "outcome": outcome,
            "actual_cost_cny": "0.00",
            "redeemed_total_cny": "0.00",
            "self_pay_cny": "0.00",
            "resolutions": resolutions,
        }
    remaining = actual
    redeemed_total = ZERO
    for line in lines:
        if line["kind"] == "enterprise_self":
            continue
        amount = Decimal(str(line["amount_cny"]))
        redeem = quantize_money(min(amount, remaining))
        remaining = quantize_money(remaining - redeem)
        returned = quantize_money(amount - redeem)
        redeemed_total += redeem
        resolutions.append(
            {
                "batch_id": line["batch_id"],
                "state": "redeemed" if returned == ZERO else "carried_forward",
                "redeemed_cny": decimal_text(redeem),
                "returned_cny": decimal_text(returned),
            }
        )
    return {
        "outcome": outcome,
        "actual_cost_cny": decimal_text(actual),
        "redeemed_total_cny": decimal_text(quantize_money(redeemed_total)),
        "self_pay_cny": decimal_text(remaining),
        "resolutions": resolutions,
    }
