"""Stock service: binder levels, remaining batches and the safety line.

The rules fixed here (they are intentionally not configurable per call):

* safety lines are judged **per silo** as a kilogram reserve;
* reaching the line blocks or warns per ``(silo, grade)`` policy, defaulting
  to ``block`` when a pair was never configured;
* stock only moves on a confirmed scale readback.  A missing or invalid
  readback is a partial failure: the estimated mass is parked on a pending
  entry, the silo is marked untrusted and a human must confirm before any
  further dosing or refill;
* every mutation is idempotent on an explicit ``request_id`` and on the
  natural ``(batch, material)`` dosing key (plus the delivery ticket for
  refills), so a replayed command never deducts twice;
* the same canonical balance document feeds the console and audit.
"""

from __future__ import annotations

from typing import Any

from ..errors import InterlockBlockedError, NotFoundError, ValidationError
from ..service_base import PlantService
from ..statemachine.machine import SequenceMachine
from .forecast import batch_forecast
from .ledger import (
    BALANCE_KIND,
    DRAW_KIND,
    PENDING_KIND,
    REFILL_KIND,
    REGISTER_KIND,
    RESOLVE_KIND,
    ROUTE_KIND,
    StockLedger,
)
from .policy import BLOCK, SiloGradePolicy, policy_for
from .reconcile import reconcile
from .units import to_grams, to_kg

POLICY_KIND = "stock.policy"

SCALE = "scale"
MANUAL = "manual"


class StockService(PlantService):
    """Owns the binder stock ledger on top of the record stream."""

    origin = "stock"
    line = "mix"
    subject = "stock"

    def __init__(self, context, recipes, machine: SequenceMachine | None = None) -> None:
        super().__init__(context, machine)
        self.recipes = recipes
        self.ledger = StockLedger()
        self.policies: dict[tuple[str, str], SiloGradePolicy] = {}
        # request_id -> stored response, the in memory half of idempotency
        self._responses: dict[str, dict[str, Any]] = {}

    # ------------------------------------------------------------ configuration

    def register_silo(
        self,
        *,
        request_id: str,
        silo: str,
        material: str,
        opening_kg: float,
        safety_kg: float,
    ) -> dict[str, Any]:
        """Enter the opening level of a silo (the manual replenishment entry)."""

        self._require_id(request_id)
        payload = {
            "active": True,
            "request_id": request_id,
            "silo": silo,
            "material": material,
            "opening_g": to_grams(opening_kg, field="opening_kg"),
            "safety_g": to_grams(safety_kg, field="safety_kg"),
        }
        self.publish(REGISTER_KIND, payload)
        self.ledger.apply_record({"kind": REGISTER_KIND, "payload": payload})
        self.emit("stock.silo_registered", {"silo": silo, "material": material})
        return self._remember(request_id, self._balance())

    def set_policy(
        self,
        *,
        request_id: str,
        silo: str,
        grade: str,
        action: str,
        warn_lead_batches: int | None = None,
    ) -> dict[str, Any]:
        """Fix whether one silo blocks or warns at the line for one grade."""

        self._require_id(request_id)
        if action not in ("block", "warn"):
            raise ValidationError("policy action must be block or warn", action=action)
        policy = SiloGradePolicy(
            silo=silo,
            grade=grade,
            action=action,
            warn_lead_batches=3 if warn_lead_batches is None else int(warn_lead_batches),
        )
        self.publish(
            POLICY_KIND,
            {
                "active": True,
                "request_id": request_id,
                **policy.describe(),
            },
        )
        self.policies[(silo, grade)] = policy
        return self._remember(request_id, {"policy": policy.describe()})

    # ------------------------------------------------------------------ queries

    def forecast(
        self,
        *,
        grade: str | None = None,
        quantity: float = 1.0,
        recipe_name: str | None = None,
    ) -> dict[str, Any]:
        """Estimate how many batches the current binder stock still supports."""

        recipe = self._recipe(grade, recipe_name)
        verdict = batch_forecast(
            ledger=self.ledger,
            grade=recipe.grade.code,
            quantity=quantity,
            factors=recipe.factors,
            policies=self.policies,
        )
        return verdict.describe()

    def precheck(
        self,
        *,
        batch_id: str,
        grade: str | None = None,
        quantity: float = 1.0,
        recipe_name: str | None = None,
    ) -> dict[str, Any]:
        """Decide whether the next dosing of ``batch_id`` may start."""

        recipe = self._recipe(grade, recipe_name)
        verdict = batch_forecast(
            ledger=self.ledger,
            grade=recipe.grade.code,
            quantity=quantity,
            factors=recipe.factors,
            policies=self.policies,
        )
        return {"allowed": not verdict.blocked_by, "forecast": verdict.describe()}

    # ------------------------------------------------------------------ dosing

    def dose(
        self,
        *,
        request_id: str,
        batch_id: str,
        silo: str,
        material: str,
        estimated_kg: float,
        grade: str,
        actual_kg: float | None = None,
        readback: str = SCALE,
        reason: str = "scale_readback_missing",
    ) -> dict[str, Any]:
        """Book one dosing from ``silo``.

        ``actual_kg`` with a ``scale`` readback is a hard deduction.  A
        missing/error readback parks ``estimated_kg`` as pending and leaves
        the balance untouched.
        """

        self._require_id(request_id)
        existing = self._responses.get(request_id)
        if existing is not None:
            return existing
        # A retry that carries a new request id but the same natural key is
        # still the same dosing: return the already booked outcome.
        natural = self._booked_outcome(batch_id, material)
        if natural is not None:
            return {"idempotent": True, **natural}

        stock = self.ledger.require_silo(silo)
        policy = policy_for(self.policies, silo=silo, grade=grade)
        estimated_g = to_grams(estimated_kg, field="estimated_kg")
        if estimated_g <= 0:
            raise ValidationError("estimated dosing mass must be positive", silo=silo)

        missing = readback in ("missing", "error") or actual_kg is None
        if not missing:
            grams = to_grams(float(actual_kg), field="actual_kg")
            if grams <= 0:
                raise ValidationError("actual dosing mass must be positive", silo=silo)
            # Hard interlock, judged on this exact dosing: untrusted silo,
            # a block policy crossed at the line, or the hard zero floor.
            if not stock.trusted:
                raise InterlockBlockedError(
                    "the silo is untrusted and awaits confirmation",
                    silo=silo,
                    pending=sorted(stock.pending),
                )
            projected = stock.balance_g - grams
            if projected < 0:
                raise InterlockBlockedError(
                    "the dosing would empty the silo",
                    silo=silo,
                    dosing_g=grams,
                    balance_g=stock.balance_g,
                )
            if projected < stock.safety_g and policy.action == BLOCK:
                raise InterlockBlockedError(
                    "the safety line interlock stops this dosing",
                    silo=silo,
                    grade=grade,
                    level_g=stock.balance_g,
                    safety_g=stock.safety_g,
                )
            payload = {
                "active": True,
                "request_id": request_id,
                "batch_id": batch_id,
                "silo": silo,
                "material": material,
                "grams_g": grams,
                "kg": to_kg(grams),
                "estimated_g": estimated_g,
                "grade": grade,
                "source": SCALE,
                "tick": self.context.clock.now(),
            }
            self.publish(DRAW_KIND, payload)
            self.ledger.apply_record({"kind": DRAW_KIND, "payload": payload})
            self._check_level_alarms(silo, grade)
            result = {
                "deducted": True,
                "trusted": True,
                "silo": self.ledger.require_silo(silo).describe(),
                "balance": self._balance(),
            }
            self.emit("stock.dosed", {"silo": silo, "batch_id": batch_id, "kg": to_kg(grams)})
            return self._remember(request_id, result)

        # Partial failure: never deduct the estimate, mark untrusted, wait.
        if not stock.trusted:
            raise InterlockBlockedError(
                "the silo is already untrusted and awaits confirmation",
                silo=silo,
                pending=sorted(stock.pending),
            )
        pending_id = self.context.ids.next("pend")
        payload = {
            "active": True,
            "request_id": request_id,
            "pending_id": pending_id,
            "batch_id": batch_id,
            "silo": silo,
            "material": material,
            "estimated_g": estimated_g,
            "estimated_kg": to_kg(estimated_g),
            "grade": grade,
            "reason": reason if readback == "error" else "scale_readback_missing",
            "tick": self.context.clock.now(),
        }
        self.publish(PENDING_KIND, payload)
        self.ledger.apply_record({"kind": PENDING_KIND, "payload": payload})
        self.raise_alarm("stock.readback_missing", {"silo": silo, "batch_id": batch_id, "pending_id": pending_id})
        result = {
            "deducted": False,
            "trusted": False,
            "pending_id": pending_id,
            "silo": self.ledger.require_silo(silo).describe(),
            "balance": self._balance(),
        }
        self.emit("stock.readback_missing", {"silo": silo, "batch_id": batch_id, "pending_id": pending_id})
        return self._remember(request_id, result)

    def report_readback(
        self,
        *,
        request_id: str,
        pending_id: str,
        silo: str,
        actual_kg: float,
    ) -> dict[str, Any]:
        """Resolve a pending dosing when the scale reading arrives later."""

        return self._resolve(
            request_id=request_id,
            pending_id=pending_id,
            silo=silo,
            actual_kg=actual_kg,
            source=SCALE,
        )

    def confirm_pending(
        self,
        *,
        request_id: str,
        pending_id: str,
        silo: str,
        actual_kg: float | None = None,
        accept_estimate: bool = False,
        operator: str = "operator",
    ) -> dict[str, Any]:
        """Human resolution of a pending dosing.

        The human either enters the real mass, or explicitly accepts the
        estimate (``accept_estimate``) — the estimate is never applied on
        the machine's own say so.
        """

        return self._resolve(
            request_id=request_id,
            pending_id=pending_id,
            silo=silo,
            actual_kg=actual_kg,
            source=MANUAL,
            accept_estimate=accept_estimate,
            operator=operator,
        )

    def _resolve(
        self,
        *,
        request_id: str,
        pending_id: str,
        silo: str,
        actual_kg: float | None,
        source: str,
        accept_estimate: bool = False,
        operator: str = "scale",
    ) -> dict[str, Any]:
        self._require_id(request_id)
        if request_id in self._responses:
            return self._responses[request_id]
        stock = self.ledger.require_silo(silo)
        entry = stock.pending.get(pending_id)
        if entry is None:
            # Already resolved: the natural key (batch, material) is booked,
            # so report the standing outcome instead of a stale lookup error.
            outcome = None
            for candidate in self.store.history(RESOLVE_KIND):
                if candidate.payload.get("pending_id") == pending_id:
                    batch_id = str(candidate.payload["batch_id"])
                    material = str(candidate.payload["material"])
                    outcome = self._booked_outcome(batch_id, material)
                    break
            if outcome is not None:
                return {"idempotent": True, **outcome}
            raise NotFoundError("pending dosing does not exist", pending_id=pending_id, silo=silo)
        if self.ledger.is_dosed(entry.batch_id, entry.material):
            return {
                "idempotent": True,
                "deducted": True,
                "trusted": stock.trusted,
                "silo": stock.describe(),
                "balance": self.ledger.balance(),
            }
        if actual_kg is None:
            if not accept_estimate:
                raise ValidationError(
                    "confirm a real mass or explicitly accept the estimate",
                    pending_id=pending_id,
                )
            grams = entry.estimated_g
        else:
            grams = to_grams(float(actual_kg), field="actual_kg")
            if grams <= 0:
                raise ValidationError("confirmed mass must be positive", pending_id=pending_id)
        payload = {
            "active": True,
            "request_id": request_id,
            "pending_id": pending_id,
            "batch_id": entry.batch_id,
            "silo": silo,
            "material": entry.material,
            "grams_g": grams,
            "kg": to_kg(grams),
            "source": source,
            "operator": operator,
            "accepted_estimate": accept_estimate,
            "grade": "",
            "tick": self.context.clock.now(),
        }
        self.publish(RESOLVE_KIND, payload)
        self.ledger.apply_record({"kind": RESOLVE_KIND, "payload": payload})
        self.emit(
            "stock.pending_resolved",
            {"silo": silo, "pending_id": pending_id, "kg": to_kg(grams)},
        )
        return self._remember(
            request_id,
            {
                "deducted": True,
                "trusted": self.ledger.require_silo(silo).trusted,
                "silo": self.ledger.require_silo(silo).describe(),
                "balance": self._balance(),
            },
        )

    # ------------------------------------------------------------------ refill

    def refill(
        self,
        *,
        request_id: str,
        silo: str,
        ticket: str,
        mode: str = "additive",
        added_kg: float | None = None,
        measured_kg: float | None = None,
    ) -> dict[str, Any]:
        """Record a delivery (additive) or a measured level reset.

        A silo with an unresolved dosing cannot be replenished until a human
        closes the gap, so replenishment books can never hide a partial
        failure.
        """

        self._require_id(request_id)
        if request_id in self._responses:
            return self._responses[request_id]
        if not ticket:
            raise ValidationError("a refill needs a delivery ticket", silo=silo)
        stock = self.ledger.require_silo(silo)
        if self.ledger.ticket_used(silo, ticket):
            return {"idempotent": True, "silo": stock.describe(), "balance": self.ledger.balance()}
        if not stock.trusted:
            raise InterlockBlockedError(
                "unresolved dosing must be confirmed before a refill",
                silo=silo,
                pending=sorted(stock.pending),
            )
        before_g = stock.balance_g
        if mode == "additive":
            if added_kg is None:
                raise ValidationError("an additive refill needs added_kg", silo=silo, ticket=ticket)
            added_g = to_grams(added_kg, field="added_kg")
            payload = {
                "active": True,
                "request_id": request_id,
                "silo": silo,
                "ticket": ticket,
                "mode": "additive",
                "delta_g": added_g,
                "delta_kg": to_kg(added_g),
                "level_g": before_g + added_g,
                "tick": self.context.clock.now(),
            }
        elif mode == "measured":
            if measured_kg is None:
                raise ValidationError("a measured refill needs measured_kg", silo=silo, ticket=ticket)
            level_g = to_grams(measured_kg, field="measured_kg")
            payload = {
                "active": True,
                "request_id": request_id,
                "silo": silo,
                "ticket": ticket,
                "mode": "measured",
                "delta_g": level_g - before_g,
                "delta_kg": to_kg(level_g - before_g),
                "level_g": level_g,
                "tick": self.context.clock.now(),
            }
        else:
            raise ValidationError("unknown refill mode", mode=mode)
        self.publish(REFILL_KIND, payload)
        self.ledger.apply_record({"kind": REFILL_KIND, "payload": payload})
        self.emit("stock.refilled", {"silo": silo, "ticket": ticket, "delta_kg": payload["delta_kg"]})
        return self._remember(
            request_id,
            {"silo": self.ledger.require_silo(silo).describe(), "balance": self._balance()},
        )

    # ------------------------------------------------------------------ routing

    def switch_route(
        self,
        *,
        request_id: str,
        material: str,
        silo: str,
        transfer_kg: float = 0.0,
    ) -> dict[str, Any]:
        """Point a material at a different silo (a silo change).

        The books before and after the change carry the same total mass; any
        residual physically carried between silos is moved explicitly.
        """

        self._require_id(request_id)
        if request_id in self._responses:
            return self._responses[request_id]
        old = self.ledger.route_for_material(material)
        transfer_g = to_grams(transfer_kg, field="transfer_kg")
        payload = {
            "active": True,
            "request_id": request_id,
            "material": material,
            "silo": silo,
            "old_silo": old,
            "transfer_g": transfer_g,
            "transfer_kg": to_kg(transfer_g),
            "tick": self.context.clock.now(),
        }
        self.publish(ROUTE_KIND, payload)
        self.ledger.apply_record({"kind": ROUTE_KIND, "payload": payload})
        self.emit("stock.route_switched", {"material": material, "silo": silo, "old_silo": old})
        return self._remember(
            request_id,
            {"silo": self.ledger.require_silo(silo).describe(), "balance": self._balance()},
        )

    # ------------------------------------------------------------- reconciliation

    def reconcile(self) -> dict[str, Any]:
        """Check the live books, the console snapshot and the journal agree."""

        report = reconcile(repository=self.store, ledger=self.ledger)
        result = report.describe()
        if result["consistent"]:
            self.emit("stock.reconciled", {"digest": result["live_digest"]})
        else:
            self.raise_alarm("stock.reconcile_failed", result)
        return result

    # ------------------------------------------------------------------ restart

    def adopt(self) -> int:
        """Fold every committed stock record back after a restart."""

        count = 0
        for record in self.store.visible():
            if record.kind == POLICY_KIND:
                policy = SiloGradePolicy(
                    silo=str(record.payload["silo"]),
                    grade=str(record.payload["grade"]),
                    action=str(record.payload["action"]),
                    warn_lead_batches=int(record.payload.get("warn_lead_batches", 3)),
                )
                self.policies[(policy.silo, policy.grade)] = policy
                continue
            self.ledger.apply_record({"kind": record.kind, "payload": record.payload})
            count += 1
        pend_records = [
            record
            for record in self.store.visible()
            if record.kind == PENDING_KIND
        ]
        if pend_records:
            highest = max(int(str(record.payload["pending_id"]).split("-")[-1]) for record in pend_records)
            self.context.ids.restore("pend", highest)
        return count

    def status(self) -> dict[str, Any]:
        balance = self._balance()
        selected = self.recipes.book.selected_name()
        forecast = None
        if selected is not None:
            recipe = self.recipes.book.selected()
            forecast = batch_forecast(
                ledger=self.ledger,
                grade=recipe.grade.code,
                quantity=1.0,
                factors=recipe.factors,
                policies=self.policies,
            ).describe()
        return {
            "selected_recipe": selected,
            "policies": [policy.describe() for policy in sorted(self.policies.values(), key=lambda p: (p.silo, p.grade))],
            "forecast": forecast,
            "balance": balance,
        }

    # ------------------------------------------------------------------ helpers

    def _booked_outcome(self, batch_id: str, material: str) -> dict[str, Any] | None:
        """Return the outcome already booked for a batch dosing, if any."""

        silo = self.ledger.dosed_silo(batch_id, material)
        if silo is not None:
            return {
                "deducted": True,
                "trusted": self.ledger.require_silo(silo).trusted,
                "silo": self.ledger.require_silo(silo).describe(),
                "balance": self.ledger.balance(),
            }
        pending = self.ledger.pending_for_batch(batch_id, material)
        if pending is not None:
            return {
                "deducted": False,
                "trusted": False,
                "pending_id": pending.pending_id,
                "silo": self.ledger.require_silo(pending.silo).describe(),
                "balance": self.ledger.balance(),
            }
        return None

    def _recipe(self, grade: str | None, recipe_name: str | None):
        if recipe_name is not None:
            return self.recipes.book.current(recipe_name)
        if grade is not None:
            return self.recipes.book.current_for_grade(grade)
        return self.recipes.book.selected()

    def _factor_kg_per_batch(self, material: str, grade: str) -> float:
        recipe = self.recipes.book.current_for_grade(grade)
        factor = float(recipe.factors.get(material, 0.0))
        if factor <= 0:
            raise ValidationError("the grade recipe has no factor for this material", grade=grade, material=material)
        return factor

    def _check_level_alarms(self, silo: str, grade: str) -> None:
        stock = self.ledger.require_silo(silo)
        policy = policy_for(self.policies, silo=silo, grade=grade)
        try:
            per_batch = to_grams(self._factor_kg_per_batch(stock.material, grade))
        except NotFoundError:
            return
        available = stock.balance_g - stock.safety_g
        if stock.balance_g <= stock.safety_g:
            name = "stock.below_safety" if policy.action == "warn" else "stock.safety_block_pending"
            self.raise_alarm(name, {"silo": silo, "grade": grade, "level_kg": to_kg(stock.balance_g)})
        elif available < per_batch * max(policy.warn_lead_batches, 1):
            self.raise_alarm(
                "stock.safety_approaching",
                {
                    "silo": silo,
                    "grade": grade,
                    "level_kg": to_kg(stock.balance_g),
                    "safe_batches": max(available, 0) // per_batch,
                },
            )

    def _balance(self) -> dict[str, Any]:
        document = self.ledger.balance()
        self.publish(BALANCE_KIND, document)
        return document

    @staticmethod
    def _require_id(request_id: str) -> None:
        if not request_id:
            raise ValidationError("stock mutations need an idempotent request_id")

    def _remember(self, request_id: str, result: dict[str, Any]) -> dict[str, Any]:
        stored = dict(result)
        self._responses[request_id] = stored
        return dict(stored)
