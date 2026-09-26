"""确定性的联合资助分摊与核销计算。

金额一律使用 ``Decimal``，存储与传输使用定点文本（两位小数）。
分摊顺序按券批次 priority、batch_id 稳定排序；同一批次优先使用结转
余额（早到期者优先），再使用批次预算。每一层受覆盖比例、单作业上限、
可用额度三项约束，余数结转给企业自付。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from typing import Mapping, Sequence


ZERO = Decimal("0")
HUNDRED = Decimal("100")
MONEY_QUANTUM = Decimal("0.01")


def money(value: Decimal) -> Decimal:
    return value.quantize(MONEY_QUANTUM, rounding=ROUND_HALF_UP)


def text(value: Decimal) -> str:
    return format(value, "f")


def canonical_json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def digest(value: object) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class BatchCandidate:
    batch_id: str
    sponsor_id: str
    cover_percent: Decimal
    per_job_cap: Decimal
    available_cny: Decimal
    priority: int
    source: str = "batch"
    carry_id: int | None = None
    rule_version: int = 0


@dataclass(frozen=True, slots=True)
class Share:
    batch_id: str
    sponsor_id: str
    amount_cny: Decimal
    basis: str
    source: str = "batch"
    carry_id: int | None = None
    rule_version: int = 0

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "sponsor_id": self.sponsor_id,
            "source": self.source,
            "carry_id": self.carry_id,
            "rule_version": self.rule_version,
            "amount_cny": text(self.amount_cny),
            "basis": self.basis,
        }


def _candidate_order(item: BatchCandidate) -> tuple[int, str, int, int]:
    # 同一批次内结转余额优先（按结转编号，即早到期先入者优先）。
    source_order = 0 if item.source == "carry" else 1
    return (item.priority, item.batch_id, source_order, item.carry_id or 0)


def split_plan(
    estimated_cost: Decimal,
    candidates: Sequence[BatchCandidate],
) -> list[Share]:
    """按核销顺序生成可解释的分摊方案（不含企业自付）。

    每个候选来源可承担的金额依次受：
    1. ``cover_percent`` 覆盖比例（作用于原始费用；结转余额按 100%）；
    2. ``per_job_cap`` 单作业资助上限（结转余额以剩余额为限）；
    3. ``available_cny`` 可用额度。
    """
    if estimated_cost < ZERO:
        raise ValueError("预计费用不能为负数")
    remaining = money(estimated_cost)
    shares: list[Share] = []
    for candidate in sorted(candidates, key=_candidate_order):
        if remaining <= ZERO:
            break
        cover = money(estimated_cost * candidate.cover_percent / HUNDRED)
        amount = money(min(remaining, cover, candidate.per_job_cap, candidate.available_cny))
        if amount <= ZERO:
            continue
        if candidate.source == "carry":
            basis = (
                f"使用部分完成作业结转余额（结转编号 {candidate.carry_id}），"
                f"剩余可结转 {text(candidate.available_cny)} 元"
            )
        else:
            basis = (
                f"按覆盖比例 {text(candidate.cover_percent)}% 分摊，"
                f"受单作业上限 {text(candidate.per_job_cap)} 元与批次剩余额度 "
                f"{text(candidate.available_cny)} 元约束"
            )
        shares.append(
            Share(
                candidate.batch_id,
                candidate.sponsor_id,
                amount,
                basis,
                source=candidate.source,
                carry_id=candidate.carry_id,
                rule_version=candidate.rule_version,
            )
        )
        remaining = money(remaining - amount)
    return shares


def self_paid(estimated_cost: Decimal, sponsored: Decimal) -> Decimal:
    """企业自付兜底，恒等于费用减去已分摊资助。"""
    return money(money(estimated_cost) - money(sponsored))


def settle_totals(
    result: str,
    estimated_cost: Decimal,
    frozen_total: Decimal,
    actual_cost: Decimal,
) -> dict[str, Decimal]:
    """按作业结果计算应核销与应释放/结转总额。

    - ``completed``：按实际费用核销，冻结余额释放回资助批次；
    - ``failed``：已冻结额度全部释放；
    - ``partial``：按实际费用核销，余额结转供该租户后续作业使用。
    """
    estimated_cost = money(estimated_cost)
    frozen_total = money(frozen_total)
    actual_cost = money(actual_cost)
    if result == "failed":
        if actual_cost != ZERO:
            raise ValueError("失败作业的实际费用必须为零")
        consumed = ZERO
    elif result in {"completed", "partial"}:
        if actual_cost <= ZERO:
            raise ValueError("完成或部分完成作业的实际费用必须大于零")
        if actual_cost > estimated_cost:
            raise ValueError("实际费用不能超过确认时的预计费用")
        consumed = min(actual_cost, frozen_total)
    else:
        raise ValueError("未知作业结果")
    consumed = money(consumed)
    remainder = money(frozen_total - consumed)
    return {
        "sponsor_consumed": consumed,
        "released": remainder if result != "partial" else ZERO,
        "carried": remainder if result == "partial" else ZERO,
        "self_paid": money(actual_cost - consumed),
    }


@dataclass(frozen=True, slots=True)
class ShareOutcome:
    batch_id: str
    sponsor_id: str
    source: str
    carry_id: int | None
    frozen_cny: Decimal
    consumed_cny: Decimal
    released_cny: Decimal
    carried_cny: Decimal

    def as_dict(self) -> dict[str, object]:
        return {
            "batch_id": self.batch_id,
            "sponsor_id": self.sponsor_id,
            "source": self.source,
            "carry_id": self.carry_id,
            "frozen_cny": text(self.frozen_cny),
            "consumed_cny": text(self.consumed_cny),
            "released_cny": text(self.released_cny),
            "carried_cny": text(self.carried_cny),
        }


def allocate_outcome(
    result: str,
    estimated_cost: Decimal,
    frozen_shares: Sequence[Mapping[str, object]],
    actual_cost: Decimal,
) -> list[ShareOutcome]:
    """把核销/释放/结转总额按冻结比例摊回每个份额，末位吸收舍入差。"""
    ordered = list(frozen_shares)
    frozen_total = money(sum((Decimal(str(item["frozen_cny"])) for item in ordered), ZERO))
    totals = settle_totals(result, estimated_cost, frozen_total, actual_cost)
    consumed_left = totals["sponsor_consumed"]
    remainder_left = money(totals["released"] + totals["carried"])
    outcomes: list[ShareOutcome] = []
    for position, item in enumerate(ordered):
        frozen_amount = money(Decimal(str(item["frozen_cny"])))
        last = position == len(ordered) - 1
        if last:
            consumed_amount = money(consumed_left)
            remainder_amount = money(remainder_left)
        else:
            consumed_amount = (
                money(totals["sponsor_consumed"] * frozen_amount / frozen_total)
                if frozen_total > ZERO else ZERO
            )
            remainder_amount = money(frozen_amount - consumed_amount)
            consumed_left = money(consumed_left - consumed_amount)
            remainder_left = money(remainder_left - remainder_amount)
        if result == "partial":
            released_amount, carried_amount = ZERO, remainder_amount
        else:
            released_amount, carried_amount = remainder_amount, ZERO
        outcomes.append(
            ShareOutcome(
                batch_id=str(item["batch_id"]),
                sponsor_id=str(item["sponsor_id"]),
                source=str(item["source"]),
                carry_id=item.get("carry_id"),  # type: ignore[arg-type]
                frozen_cny=frozen_amount,
                consumed_cny=consumed_amount,
                released_cny=released_amount,
                carried_cny=carried_amount,
            )
        )
    return outcomes
