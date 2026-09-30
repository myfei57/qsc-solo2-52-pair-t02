"""What a silo is allowed to do once its stock reaches the safety line.

The safety line itself is fixed **per silo**: it is a physical reserve
belonging to the vessel, never a per grade quantity.  Only the reaction to
reaching that line differs, and that reaction is keyed by ``(silo, grade)``:

* ``BLOCK`` stops the next dosing from starting (hard interlock);
* ``WARN`` lets production continue but raises an early alarm.

A policy row may also carry ``warn_lead_batches``: the forecast starts
warning while this many complete batches still separate the silo from its
safety line.  Pairs without an explicit row fail safe to ``BLOCK``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError

BLOCK = "block"
WARN = "warn"
ACTIONS = (BLOCK, WARN)

DEFAULT_ACTION = BLOCK
DEFAULT_LEAD_BATCHES = 3


@dataclass(frozen=True, slots=True)
class SiloGradePolicy:
    """The reaction of one silo for one concrete grade."""

    silo: str
    grade: str
    action: str
    warn_lead_batches: int = DEFAULT_LEAD_BATCHES

    def __post_init__(self) -> None:
        if not self.silo:
            raise ValidationError("policy needs a silo")
        if not self.grade:
            raise ValidationError("policy needs a grade")
        if self.action not in ACTIONS:
            raise ValidationError("policy action must be block or warn", action=self.action)
        if self.warn_lead_batches < 0:
            raise ValidationError("warn lead must not be negative", lead=self.warn_lead_batches)

    def describe(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "grade": self.grade,
            "action": self.action,
            "warn_lead_batches": self.warn_lead_batches,
        }


def policy_for(
    policies: Mapping[tuple[str, str], SiloGradePolicy],
    *,
    silo: str,
    grade: str,
) -> SiloGradePolicy:
    """Return the configured row or the fail safe default (block)."""

    found = policies.get((silo, grade))
    if found is not None:
        return found
    return SiloGradePolicy(silo=silo, grade=grade, action=DEFAULT_ACTION)
