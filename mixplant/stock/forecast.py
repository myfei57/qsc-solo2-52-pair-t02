"""Estimate how many batches the binder stock still supports.

The estimate always counts the number of *complete* batches a silo can feed
while staying on or above its safety line.  The safety line is compared per
silo (a physical number); the block versus warn verdict is looked up per
``(silo, grade)`` through the policy table.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .ledger import SiloStock
from .policy import BLOCK, WARN, SiloGradePolicy, policy_for


@dataclass(frozen=True, slots=True)
class SiloForecast:
    """The stock verdict for one silo against one recipe per batch."""

    silo: str
    material: str
    trusted: bool
    level_g: int
    safety_g: int
    per_batch_g: int
    safe_batches: int | None
    below_safety: bool
    action: str
    blocks: bool
    alarm: str | None

    def describe(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "material": self.material,
            "trusted": self.trusted,
            "level_g": self.level_g,
            "safety_g": self.safety_g,
            "per_batch_g": self.per_batch_g,
            "safe_batches": self.safe_batches,
            "below_safety": self.below_safety,
            "action": self.action,
            "blocks": self.blocks,
            "alarm": self.alarm,
        }


def safe_batches(available_g: int, per_batch_g: int) -> int:
    """Whole batches that fit in the mass above the safety line."""

    if per_batch_g <= 0:
        raise ValueError("per batch mass must be positive")
    if available_g < 0:
        return 0
    return available_g // per_batch_g


def evaluate_silo(
    *,
    stock: SiloStock,
    grade: str,
    per_batch_g: int,
    policy: SiloGradePolicy,
) -> SiloForecast:
    """Judge one silo for the next batch.

    ``blocks`` answers "may the next dosing start?".  The test is the level
    *after* one more batch: a ``block`` policy stops the dosing that would
    cross the safety line, while a ``warn`` policy lets it run up to the
    physical zero floor (which always blocks).  An untrusted silo is always
    blocked: a missing scale readback must never be followed by another
    estimated draw.
    """

    projected = stock.balance_g - per_batch_g
    below_safety = stock.balance_g <= stock.safety_g
    crosses_line = projected < stock.safety_g
    empties = projected < 0
    if not stock.trusted:
        return SiloForecast(
            silo=stock.name,
            material=stock.material,
            trusted=False,
            level_g=stock.balance_g,
            safety_g=stock.safety_g,
            per_batch_g=per_batch_g,
            safe_batches=None,
            below_safety=below_safety,
            action=policy.action,
            blocks=True,
            alarm="stock.silo_untrusted",
        )
    batches = safe_batches(stock.balance_g - stock.safety_g, per_batch_g)
    blocks = empties or (crosses_line and policy.action == BLOCK)
    alarm: str | None = None
    if crosses_line and policy.action == WARN:
        alarm = "stock.below_safety"
    elif not crosses_line and batches <= max(policy.warn_lead_batches - 1, 0):
        alarm = "stock.safety_approaching"
    return SiloForecast(
        silo=stock.name,
        material=stock.material,
        trusted=True,
        level_g=stock.balance_g,
        safety_g=stock.safety_g,
        per_batch_g=per_batch_g,
        safe_batches=batches,
        below_safety=below_safety,
        action=policy.action,
        blocks=blocks,
        alarm=alarm,
    )


@dataclass(frozen=True, slots=True)
class BatchForecast:
    """The plant wide verdict for one recipe and batch size."""

    grade: str
    quantity: float
    trusted: bool
    safe_batches: int | None
    blocked_by: tuple[str, ...]
    alarms: tuple[tuple[str, str], ...]
    silos: tuple[SiloForecast, ...]

    def describe(self) -> dict[str, Any]:
        return {
            "grade": self.grade,
            "quantity": self.quantity,
            "trusted": self.trusted,
            "safe_batches": self.safe_batches,
            "blocked_by": list(self.blocked_by),
            "alarms": [{"silo": silo, "alarm": alarm} for silo, alarm in self.alarms],
            "silos": [item.describe() for item in self.silos],
        }


def batch_forecast(
    *,
    ledger: Any,
    grade: str,
    quantity: float,
    factors: Mapping[str, float],
    policies: Mapping[tuple[str, str], SiloGradePolicy],
) -> BatchForecast:
    """Fold every binder silo feeding ``factors`` into one verdict."""

    from .units import to_grams

    if quantity <= 0:
        raise ValueError("batch quantity must be positive")
    verdicts: list[SiloForecast] = []
    for material in sorted(factors):
        silo_name = ledger.route_for_material(material)
        if silo_name is None:
            continue
        stock = ledger.require_silo(silo_name)
        per_batch = to_grams(factors[material] * quantity)
        verdicts.append(
            evaluate_silo(
                stock=stock,
                grade=grade,
                per_batch_g=per_batch,
                policy=policy_for(policies, silo=silo_name, grade=grade),
            )
        )
    trusted = all(item.trusted for item in verdicts)
    safe_values = [item.safe_batches for item in verdicts if item.safe_batches is not None]
    safe = min(safe_values) if trusted and safe_values else None
    blocked = tuple(item.silo for item in verdicts if item.blocks)
    alarms = tuple((item.silo, item.alarm) for item in verdicts if item.alarm)
    return BatchForecast(
        grade=grade,
        quantity=quantity,
        trusted=trusted,
        safe_batches=safe,
        blocked_by=blocked,
        alarms=alarms,
        silos=tuple(verdicts),
    )
