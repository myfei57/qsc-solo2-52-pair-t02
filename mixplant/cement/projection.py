"""Estimate how many more batches the current binder levels can cover.

The estimate is a *projection*, never an authoritative balance: only the
measured drawdowns booked by the ledger move the level.  When a silo is
untrusted its remaining count is reported as unknown rather than guessed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class SiloProjection:
    """Remaining batches as seen from one silo."""

    silo: str
    material: str
    level_kg: float
    demand_kg: float
    remaining_batches: int | None
    trusted: bool
    reason: str

    def describe(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "material": self.material,
            "level_kg": self.level_kg,
            "demand_kg": self.demand_kg,
            "remaining_batches": self.remaining_batches,
            "trusted": self.trusted,
            "reason": self.reason,
        }


def _floor_div(level_kg: float, demand_kg: float) -> int:
    # Whole batches only, minus a tiny epsilon so a level equal to the floor
    # reads as "one batch" rather than floating into "zero".
    if demand_kg <= 0:
        return 0
    count = int((level_kg + 1e-6) // demand_kg)
    return max(count, 0)


def project_silo(
    *,
    silo: str,
    material: str,
    level_kg: float,
    demand_kg: float,
    trusted: bool,
) -> SiloProjection:
    """Project one silo for a single batch's binder demand."""

    if demand_kg <= 0:
        return SiloProjection(silo, material, level_kg, demand_kg, None, trusted, "material_not_in_recipe")
    if not trusted:
        return SiloProjection(silo, material, level_kg, demand_kg, None, False, "untrusted_level")
    remaining = _floor_div(level_kg, demand_kg)
    return SiloProjection(silo, material, level_kg, demand_kg, remaining, True, "ok")


def project_plant(
    balances: Mapping[str, Any],
    demand_by_material: Mapping[str, float],
) -> dict[str, Any]:
    """Project every silo and fold them into the count the console headlines.

    ``balances`` maps silo name to a balance-like object; demand maps a binder
    material (cement / fly-ash) to the kg one batch of the current recipe uses.
    The plant-wide number is the smallest trusted silo count.
    """

    projections: list[SiloProjection] = []
    for name in sorted(balances):
        balance = balances[name]
        material = balance.material if hasattr(balance, "material") else str(balance["material"])
        level = float(balance.level_kg if hasattr(balance, "level_kg") else balance["level_kg"])
        trusted = bool(balance.trusted if hasattr(balance, "trusted") else balance["trusted"])
        demand = float(demand_by_material.get(material, 0.0))
        projections.append(
            project_silo(silo=name, material=material, level_kg=level, demand_kg=demand, trusted=trusted)
        )
    usable = [p.remaining_batches for p in projections if p.remaining_batches is not None and p.demand_kg > 0]
    unknown = any(not p.trusted and p.demand_kg > 0 for p in projections)
    # Any untrusted binder silo makes the headline count unknowable: the
    # smallest *trusted* count must not masquerade as a plant-wide promise.
    remaining = None if unknown else (min(usable) if usable else None)
    return {
        "remaining_batches": remaining,
        "trusted": not unknown and remaining is not None,
        "limiting_silo": _limiting_silo(projections, remaining),
        "silos": [p.describe() for p in projections],
    }


def _limiting_silo(projections: list[SiloProjection], remaining: int | None) -> str | None:
    if remaining is None:
        return None
    for projection in sorted(projections, key=lambda p: p.silo):
        if projection.remaining_batches == remaining and projection.demand_kg > 0:
            return projection.silo
    return None
