"""Safety line policy for the binder silos.

The safety line is judged **per silo**, never per grade: a silo physically
holds one material and a level that is low for one grade is low for every
grade.  What grade changes is the *reaction* — whether crossing the line only
raises an alarm or also blocks the next batching charge.  That reaction is a
``(silo, grade)`` matrix with an alarm-only default, so adding a grade never
silently starts blocking production.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import ValidationError

ALARM = "alarm"
BLOCK = "block"
ACTIONS = (ALARM, BLOCK)


@dataclass(frozen=True, slots=True)
class SafetyPolicy:
    """The level floor of one silo and its per grade reaction."""

    silo: str
    safety_kg: float
    default_action: str
    grade_actions: Mapping[str, str]

    def __post_init__(self) -> None:
        if not self.silo:
            raise ValidationError("safety policy needs a silo")
        if self.safety_kg < 0:
            raise ValidationError("safety line must not be negative", silo=self.silo, safety_kg=self.safety_kg)
        if self.default_action not in ACTIONS:
            raise ValidationError("safety action must be alarm or block", action=self.default_action)
        for grade, action in self.grade_actions.items():
            if action not in ACTIONS:
                raise ValidationError("safety action must be alarm or block", silo=self.silo, grade=grade, action=action)

    def action_for(self, grade: str) -> str:
        """Resolve the reaction for one grade (grade overrides default)."""

        return self.grade_actions.get(grade, self.default_action)

    def describe(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "safety_kg": self.safety_kg,
            "default_action": self.default_action,
            "grade_actions": dict(sorted(self.grade_actions.items())),
        }


class SafetyTable:
    """The policy of every silo, rebuilt from the configuration stream."""

    __slots__ = ("_policies", "_fallback")

    def __init__(self, fallback_kg: float = 5000.0) -> None:
        self._policies: dict[str, SafetyPolicy] = {}
        self._fallback = fallback_kg

    def set(
        self,
        silo: str,
        safety_kg: float,
        *,
        default_action: str = ALARM,
        grade_actions: Mapping[str, str] | None = None,
    ) -> SafetyPolicy:
        policy = SafetyPolicy(
            silo=silo,
            safety_kg=float(safety_kg),
            default_action=default_action,
            grade_actions=dict(grade_actions or {}),
        )
        self._policies[silo] = policy
        return policy

    def adopt(self, silo: str, payload: Mapping[str, Any]) -> SafetyPolicy:
        raw_grades = payload.get("grade_actions", {})
        grade_actions = (
            {str(grade): str(action) for grade, action in dict(raw_grades).items()}
            if isinstance(raw_grades, Mapping)
            else {}
        )
        return self.set(
            silo,
            float(payload.get("safety_kg", self._fallback)),
            default_action=str(payload.get("default_action", ALARM)),
            grade_actions=grade_actions,
        )

    def policy(self, silo: str) -> SafetyPolicy:
        existing = self._policies.get(silo)
        if existing is not None:
            return existing
        # A silo without an explicit policy is alarm-only at the fallback floor.
        return self.set(silo, self._fallback)

    def evaluate(self, silo: str, level_kg: float, *, trusted: bool, grade: str) -> "SafetyVerdict":
        """Judge a silo level for the grade that is about to be batched."""

        policy = self.policy(silo)
        if not trusted:
            # An untrusted level can never be proved above the line: block.
            return SafetyVerdict(
                silo=silo,
                level_kg=level_kg,
                safety_kg=policy.safety_kg,
                grade=grade,
                below=True,
                action=BLOCK,
                reason="untrusted_level",
            )
        below = level_kg < policy.safety_kg
        action = policy.action_for(grade) if below else ALARM
        return SafetyVerdict(
            silo=silo,
            level_kg=level_kg,
            safety_kg=policy.safety_kg,
            grade=grade,
            below=below,
            action=action,
            reason="below_safety" if below else "ok",
        )

    def describe(self) -> dict[str, Any]:
        return {silo: self._policies[silo].describe() for silo in sorted(self._policies)}


@dataclass(frozen=True, slots=True)
class SafetyVerdict:
    """The judgement for one silo against the grade being batched."""

    silo: str
    level_kg: float
    safety_kg: float
    grade: str
    below: bool
    action: str
    reason: str

    @property
    def blocks(self) -> bool:
        return self.action == BLOCK

    @property
    def alarms(self) -> bool:
        return self.below

    def describe(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "level_kg": self.level_kg,
            "safety_kg": self.safety_kg,
            "grade": self.grade,
            "below": self.below,
            "action": self.action,
            "reason": self.reason,
        }
