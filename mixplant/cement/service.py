"""Cement service: the gate, the flow and the binder silo inventory.

The service covers both binder silos (cement and fly ash).  Every inventory
mutation is written to the committed stream and is idempotent:

* refills and level declarations carry a client reference;
* drawdowns carry the batch id they fed.

A missing scale readback is a partial failure: no estimated mass is deducted.
The silo is marked untrusted and feeding is refused until an operator confirms
or rejects the parked consumption.
"""

from __future__ import annotations

from typing import Any

from ..errors import InterlockBlockedError, NotFoundError, OrderViolationError
from ..service_base import PlantService
from ..statemachine.phases import MixPhase
from .flow import flow_reading
from .gate import gate_state
from .inventory import (
    DRAW_KIND,
    OPEN_KIND,
    REFILL_KIND,
    RESOLVE_APPLY,
    RESOLVE_KIND,
    RESOLVE_REJECT,
    SAFETY_KIND,
    UNTRUSTED_KIND,
    InventoryLedger,
)
from .projection import project_plant
from .safety import ALARM, BLOCK, SafetyTable
from .silo import silo_state

GATE_KIND = "cement.gate"
FLOW_KIND = "cement.flow"
SILO_KIND = "cement.silo"

BINDER_MATERIALS = ("cement", "fly-ash")

DEFAULT_OPENING = {"silo-1": ("cement", 60000.0), "silo-2": ("fly-ash", 25000.0)}


class CementService(PlantService):
    """Feeds dry binder through a gate that the mixing line must open first."""

    origin = "cement"
    line = "mix"
    subject = "cement"

    def __init__(self, context, machine=None, opening: dict[str, tuple[str, float]] | None = None) -> None:
        super().__init__(context, machine)
        # The opening values are only a fallback until the stream proves what
        # is really in the silos; they are not counted as bookable movements.
        self.ledger = InventoryLedger(DEFAULT_OPENING if opening is None else opening, movements=False)
        self.safety = SafetyTable()
        # The configured lines are fixed at boot; a later cement.set_safety
        # (itself persisted) overrides them for the running station.
        for silo, policy in getattr(context.config, "silo_safety", {}).items():
            self.safety.set(
                silo,
                policy.safety_kg,
                default_action=policy.default_action,
                grade_actions=dict(policy.grade_actions),
            )

    # ------------------------------------------------------------- silo setup

    def load_silo(
        self,
        *,
        silo: str,
        material: str,
        level_kg: float,
        client_ref: str | None = None,
        reason: str = "manual fill",
    ) -> dict[str, Any]:
        """Book a manual binder fill.

        If the silo is unknown this establishes its opening level; otherwise
        the reported amount is treated as a refill on top of the current
        trusted balance.  Idempotent on ``client_ref``.  A declared absolute
        level (stock take / silo change) goes through :meth:`declare_level`.
        """

        ref = client_ref or self.context.ids.next("silo_decl")
        existing = self.ledger.ref(ref)
        if existing is not None and existing.seq is not None:
            return {
                "silo": self._silo_payload(existing.silo),
                "movement": existing.describe(),
                "reconciliation": self.ledger.reconciliation(),
                "idempotent": True,
            }
        if self.ledger.has_declared(silo):
            movement = self.ledger.refill(
                silo, level_kg, tick=self.context.clock.now(), client_ref=ref,
                material=material, reason=reason,
            )
            kind = REFILL_KIND
        else:
            movement = self.ledger.declare(
                silo, material, level_kg, tick=self.context.clock.now(), client_ref=ref,
                reason="opening level declared",
            )
            kind = OPEN_KIND
        record = self.publish(kind, self._movement_payload(movement))
        self.ledger.stamp_last(record.seq)
        self._publish_level(silo)
        self.emit("cement.level_declared", {"silo": silo, "level_kg": level_kg, "client_ref": ref})
        return {
            "silo": self._silo_payload(silo),
            "movement": movement.describe(),
            "reconciliation": self.ledger.reconciliation(),
            "idempotent": False,
        }

    def declare_level(
        self,
        *,
        silo: str,
        material: str,
        level_kg: float,
        client_ref: str | None = None,
        reason: str = "level declared",
    ) -> dict[str, Any]:
        """Declare an absolute gauged level (stock take / silo change)."""

        ref = client_ref or self.context.ids.next("silo_decl")
        existing = self.ledger.ref(ref)
        if existing is not None and existing.seq is not None:
            return {"silo": self._silo_payload(existing.silo), "movement": existing.describe(), "idempotent": True}
        movement = self.ledger.declare(
            silo, material, level_kg, tick=self.context.clock.now(), client_ref=ref, reason=reason
        )
        record = self.publish(OPEN_KIND, self._movement_payload(movement))
        self.ledger.stamp_last(record.seq)
        self._publish_level(silo)
        self.emit("cement.level_declared", {"silo": silo, "level_kg": level_kg, "client_ref": ref})
        return {"silo": self._silo_payload(silo), "movement": movement.describe(), "idempotent": False}

    def refill(
        self,
        *,
        silo: str,
        kg: float,
        client_ref: str | None = None,
        material: str | None = None,
        reason: str = "delivery",
    ) -> dict[str, Any]:
        """Add a delivery to a silo and return the before/after reconciliation."""

        ref = client_ref or self.context.ids.next("silo_refill")
        existing = self.ledger.ref(ref)
        if existing is not None and existing.seq is not None:
            return {
                "silo": self._silo_payload(existing.silo),
                "movement": existing.describe(),
                "reconciliation": self.ledger.reconciliation(),
                "idempotent": True,
            }
        movement = self.ledger.refill(
            silo, kg, tick=self.context.clock.now(), client_ref=ref, material=material, reason=reason
        )
        record = self.publish(REFILL_KIND, self._movement_payload(movement))
        self.ledger.stamp_last(record.seq)
        self._publish_level(silo)
        self.emit("cement.refilled", {"silo": silo, "kg": kg, "client_ref": ref, "level_kg": movement.after_kg})
        return {
            "silo": self._silo_payload(silo),
            "movement": movement.describe(),
            "reconciliation": self.ledger.reconciliation(),
            "idempotent": False,
        }

    def set_safety(
        self,
        *,
        silo: str,
        safety_kg: float,
        default_action: str = ALARM,
        grade_actions: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Fix the per silo safety line and its per grade reaction matrix."""

        policy = self.safety.set(
            silo, safety_kg, default_action=default_action, grade_actions=grade_actions or {}
        )
        self.publish(SAFETY_KIND, {"active": True, **policy.describe()})
        self.emit("cement.safety_set", policy.describe())
        return {"policy": policy.describe()}

    # ------------------------------------------------------------------ gate

    def open_gate(self, *, silo: str, target_kg: float) -> dict[str, Any]:
        """Open the binder gate, refusing when the safety policy blocks."""

        self.require_order(target_kg > 0, "the cement gate needs a positive target", silo=silo)
        balance = self.ledger.balance(silo)
        grade = self._current_grade()
        verdict = self.safety.evaluate(silo, balance.level_kg, trusted=balance.trusted, grade=grade)
        if verdict.blocks:
            self.raise_alarm(
                "cement.safety_block",
                {**verdict.describe(), "target_kg": target_kg},
            )
            raise InterlockBlockedError(
                "the silo safety line blocks this charge",
                silo=silo,
                grade=grade,
                level_kg=balance.level_kg,
                safety_kg=verdict.safety_kg,
                trusted=balance.trusted,
            )
        state = gate_state(open=True, silo=silo, target_kg=target_kg)
        self.publish(GATE_KIND, state.describe())
        self.emit("cement.gate_opened", {"silo": silo, "target_kg": state.target_kg})
        if verdict.alarms:
            self.raise_alarm("cement.below_safety", verdict.describe())
        return {"gate": state.describe(), "safety": verdict.describe(), "inventory": self.inventory()}

    def close_gate(
        self,
        *,
        silo: str,
        mass_kg: float | None = None,
        rate_kgs: float | None = None,
        batch_id: str | None = None,
        readback: bool = True,
    ) -> dict[str, Any]:
        """Close the gate after a measured discharge.

        ``readback=False`` (or a missing ``mass_kg``) models a lost scale
        readback: a partial failure.  Nothing is deducted; the silo becomes
        untrusted and feeds are blocked pending a manual resolution.
        """

        self.require("cement.feed")
        bid = batch_id or self._current_batch_id()
        gate = gate_state(open=False, silo=silo, target_kg=(mass_kg or 0.0))
        self.publish(GATE_KIND, gate.describe())

        if not readback or mass_kg is None or rate_kgs is None:
            return self._partial_failure(silo, bid=bid)
        reading = flow_reading(
            thresholds=self.context.thresholds,
            silo=silo,
            rate_kgs=rate_kgs,
            mass_kg=mass_kg,
        )
        self.publish(FLOW_KIND, reading.describe())

        existing = self.ledger.draw_for(silo, bid)
        if existing is not None and existing.seq is not None:
            # Retried completion: the stream already booked this batch once.
            level = existing.after_kg
            idempotent = True
            movement = existing
        else:
            movement = self.ledger.draw(
                silo, mass_kg, tick=self.context.clock.now(), batch_id=bid
            )
            record = self.publish(DRAW_KIND, self._movement_payload(movement))
            self.ledger.stamp_last(record.seq)
            level = movement.after_kg
            idempotent = False
        self._publish_level(silo)

        if not reading.ok:
            self.raise_alarm("cement.flow_over_limit", reading.describe())
        self.emit("cement.fed", {"silo": silo, "mass_kg": reading.mass_kg, "batch_id": bid})
        if self.machine.is_at(MixPhase.WATER_IN.value):
            self.advance(MixPhase.CEMENT_IN.value, "binder washed in after the water")
        return {
            "flow": reading.describe(),
            "level_kg": level,
            "movement": movement.describe(),
            "idempotent": idempotent,
            "inventory": self.inventory(),
            "reconciliation": self.ledger.reconciliation(),
        }

    def _partial_failure(self, silo: str, *, bid: str) -> dict[str, Any]:
        balance = self.ledger.balance(silo)
        estimate = self._estimated_demand(silo)
        prior = self.ledger.pending_for(silo, bid)
        if prior is not None:
            # A retried completion with the readback still missing must not
            # raise a second alarm or write a second untrusted record.
            return {
                "partial_failure": True,
                "trusted": False,
                "silo": self._silo_payload(silo),
                "estimated_kg": estimate,
                "idempotent": True,
                "message": "scale readback missing; level not deducted, awaiting manual confirmation",
            }
        movement = self.ledger.mark_pending(
            silo,
            estimate,
            tick=self.context.clock.now(),
            batch_id=bid,
        )
        self.publish(
            UNTRUSTED_KIND,
            {
                "active": True,
                "silo": silo,
                "material": balance.material,
                "delta_kg": 0.0,
                "before_kg": balance.level_kg,
                "after_kg": balance.level_kg,
                "pending_kg": estimate,
                "reason": movement.reason,
                "batch_id": bid,
                "trusted": False,
                "estimated_kg": estimate,
            },
        )
        self._publish_level(silo)
        self.raise_alarm(
            "cement.readback_missing",
            {"silo": silo, "batch_id": bid, "estimated_kg": estimate, "level_kg": balance.level_kg},
        )
        self.emit("cement.readback_missing", {"silo": silo, "batch_id": bid, "estimated_kg": estimate})
        if self.machine.is_at(MixPhase.WATER_IN.value):
            self.advance(MixPhase.CEMENT_IN.value, "binder fed with missing readback; level untrusted")
        return {
            "partial_failure": True,
            "trusted": False,
            "silo": self._silo_payload(silo),
            "estimated_kg": estimate,
            "idempotent": False,
            "message": "scale readback missing; level not deducted, awaiting manual confirmation",
        }

    def resolve_draw(
        self,
        *,
        silo: str,
        batch_id: str,
        mode: str,
        measured_kg: float | None = None,
        reason: str = "manual confirmation",
    ) -> dict[str, Any]:
        """Manually clear a pending partial failure (apply the mass or reject)."""

        if mode not in (RESOLVE_APPLY, RESOLVE_REJECT):
            raise OrderViolationError("resolution mode must be apply or reject", mode=mode)
        already_resolved = self.ledger.resolution_for(silo, batch_id)
        movement = self.ledger.resolve(
            silo,
            tick=self.context.clock.now(),
            batch_id=batch_id,
            mode=mode,
            measured_kg=measured_kg,
            reason=reason,
        )
        if already_resolved is not None and already_resolved.seq is not None:
            # A resolution for this batch was already committed: idempotent reply.
            return {
                "silo": self._silo_payload(silo),
                "movement": already_resolved.describe(),
                "reconciliation": self.ledger.reconciliation(),
                "idempotent": True,
            }
        self.publish(RESOLVE_KIND, self._movement_payload(movement))
        self.ledger.stamp_last(self.store.last_seq())
        self._publish_level(silo)
        self.emit(
            "cement.draw_resolved",
            {"silo": silo, "batch_id": batch_id, "mode": mode, "level_kg": movement.after_kg},
        )
        return {
            "silo": self._silo_payload(silo),
            "movement": movement.describe(),
            "reconciliation": self.ledger.reconciliation(),
            "idempotent": False,
        }

    # ----------------------------------------------------------------- reads

    def inventory(self) -> dict[str, Any]:
        return {
            "trusted": all(b.trusted for b in self.ledger.balances()),
            "levels": {b.silo: b.describe() for b in self.ledger.balances()},
        }

    def projection(self) -> dict[str, Any]:
        """Estimate the batches left under the current recipe for every silo."""

        grade = self._current_grade()
        demand = self._binder_demand()
        result = project_plant({b.silo: b for b in self.ledger.balances()}, demand)
        result["grade"] = grade
        result["demand_kg"] = {material: demand.get(material, 0.0) for material in BINDER_MATERIALS}
        return result

    def reconciliation(self) -> dict[str, Any]:
        """Recompute balances from movements for console/audit cross checking."""

        return self.ledger.reconciliation()

    def movements(self, silo: str | None = None) -> dict[str, Any]:
        return {"movements": [m.describe() for m in self.ledger.movements(silo=silo)]}

    def adopt_inventory(self, records) -> None:
        """Rebuild the ledger and safety table from committed stream records.

        Inventory is rebuilt from the latest absolute declaration per silo as
        the opening balance, then replaying only the movements after it, so a
        restart reaches the same totals regardless of stream interleaving.
        """

        stream = tuple(records)
        last_open: dict[str, tuple[int, Any]] = {}
        for index, record in enumerate(stream):
            if record.kind == OPEN_KIND:
                last_open[str(record.payload.get("silo", ""))] = (index, record)
        opening = {
            silo: (str(record.payload.get("material", "")), float(record.payload["after_kg"]))
            for silo, (_, record) in last_open.items()
        }
        # Only rebuild from the stream when it actually carries a declaration;
        # otherwise keep the service opening balances untouched.
        if opening:
            self.ledger = InventoryLedger(opening, movements=True)
            # The seeded opening markers describe the declaration that
            # established the baseline; stamp them with the stream record so
            # reconciliation can anchor on it.
            for silo, (_, record) in last_open.items():
                self.ledger.stamp_opening(silo, record.seq, record.tick)
        base_index = {silo: index for silo, (index, _) in last_open.items()}
        for index, record in enumerate(stream):
            if record.kind == SAFETY_KIND:
                self.safety.adopt(str(record.payload["silo"]), record.payload)
                continue
            if record.kind not in (OPEN_KIND, REFILL_KIND, DRAW_KIND, UNTRUSTED_KIND, RESOLVE_KIND):
                continue
            silo = str(record.payload.get("silo", ""))
            if record.kind == OPEN_KIND or (silo in base_index and index <= base_index[silo]):
                continue
            self.ledger.adopt(record)
        # Keep a config-only policy (one never persisted via set_safety)
        # effective after a restart.
        for silo, policy in getattr(self.context.config, "silo_safety", {}).items():
            if silo not in self.safety.describe():
                self.safety.set(
                    silo,
                    policy.safety_kg,
                    default_action=policy.default_action,
                    grade_actions=dict(policy.grade_actions),
                )

    def status(self) -> dict[str, Any]:
        gate = self.payload_entry("cement", "gate")
        flow = self.payload_entry("cement", "flow")
        return {
            "gate_open": bool(gate.get("active", False)) if gate else False,
            "silo": gate.get("silo") if gate else None,
            "target_kg": gate.get("target_kg") if gate else None,
            "flow": None if flow is None else dict(flow),
            "inventory": self.inventory(),
            "projection": self.projection(),
            "safety": self.safety.describe(),
            "reconciliation": self.ledger.reconciliation(),
        }

    # ---------------------------------------------------------------- helpers

    def _publish_level(self, silo: str) -> None:
        balance = self.ledger.balance(silo)
        state = silo_state(silo, balance.material, balance.level_kg)
        payload = state.describe()
        payload.update({"trusted": balance.trusted, "pending_kg": balance.pending_kg})
        self.publish(SILO_KIND, payload)

    def _silo_payload(self, silo: str) -> dict[str, Any]:
        return self.ledger.balance(silo).describe()

    def _movement_payload(self, movement) -> dict[str, Any]:
        payload = movement.describe()
        payload["active"] = True
        payload.pop("seq", None)
        return payload

    def _current_batch_id(self) -> str:
        batch = self.context.batches.latest()
        if batch is None:
            raise NotFoundError("declare a batch before closing the binder gate")
        return batch.batch_id

    def _recipe(self):
        """Return the currently selected recipe from the runtime services."""

        recipes = getattr(self.context, "recipes", None)
        if recipes is None:
            return None
        try:
            return recipes.current()
        except Exception:
            return None

    def _current_grade(self) -> str:
        batch = self.context.batches.latest()
        if batch is not None:
            return batch.grade
        recipe = self._recipe()
        return "" if recipe is None else recipe.grade.code

    def _current_quantity(self) -> float:
        batch = self.context.batches.latest()
        if batch is not None:
            return batch.quantity
        return 1.0

    def _binder_demand(self) -> dict[str, float]:
        """kg of each binder that one batch of the current recipe consumes."""

        recipe = self._recipe()
        if recipe is None:
            return {}
        quantity = self._current_quantity()
        return {
            material: round(recipe.factor(material) * quantity, 4)
            for material in recipe.materials
            if material in BINDER_MATERIALS
        }

    def _estimated_demand(self, silo: str) -> float:
        balance = self.ledger.balance(silo)
        return self._binder_demand().get(balance.material, 0.0) or 1.0
