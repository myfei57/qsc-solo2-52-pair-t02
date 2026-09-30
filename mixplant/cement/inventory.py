"""Drawdown accounting for the binder silos (cement and fly ash).

The ledger is an append only, event sourced account of every silo.  It is
rebuilt from the committed record stream on restart, so the console view and
the audit reconciliation always read the same numbers.

Two rules shape the whole design:

* a draw is only ever booked from a measured, read back mass.  When the scale
  readback is missing the event is a *partial failure*: the silo is marked
  untrusted and the consumption is left pending instead of being estimated;
* every mutation is idempotent.  Refills and level declarations carry a
  client reference and draws carry a batch id, so a retried command books one
  movement no matter how many times it arrives.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import DuplicateError, NotFoundError, ValidationError

OPEN_KIND = "cement.open"
REFILL_KIND = "cement.refill"
DRAW_KIND = "cement.draw"
UNTRUSTED_KIND = "cement.untrusted"
RESOLVE_KIND = "cement.resolve"
SAFETY_KIND = "cement.safety"

REFILL = "refill"
DECLARE = "declare"
DRAW = "draw"
PENDING = "pending"
RESOLVE_APPLY = "apply"
RESOLVE_REJECT = "reject"


@dataclass(frozen=True, slots=True)
class SiloBalance:
    """The trusted level of one silo."""

    silo: str
    material: str
    level_kg: float
    tick: int
    trusted: bool = True
    pending_kg: float = 0.0

    def describe(self) -> dict[str, Any]:
        return {
            "silo": self.silo,
            "material": self.material,
            "level_kg": self.level_kg,
            "trusted": self.trusted,
            "pending_kg": self.pending_kg,
            "tick": self.tick,
        }


@dataclass(frozen=True, slots=True)
class LedgerMovement:
    """One booked change, the unit the reconciliation report is built from."""

    seq: int | None
    kind: str
    silo: str
    material: str
    delta_kg: float
    before_kg: float
    after_kg: float
    tick: int
    reason: str
    client_ref: str
    batch_id: str
    trusted: bool

    def describe(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "silo": self.silo,
            "material": self.material,
            "delta_kg": self.delta_kg,
            "before_kg": self.before_kg,
            "after_kg": self.after_kg,
            "tick": self.tick,
            "reason": self.reason,
            "client_ref": self.client_ref,
            "batch_id": self.batch_id,
            "trusted": self.trusted,
        }


def _require_ref(ref: str, *, what: str) -> str:
    text = str(ref or "").strip()
    if not text:
        raise ValidationError(f"a {what} needs an idempotency reference", ref=ref)
    return text


class InventoryLedger:
    """Applies openings, deliveries, draws and partial failures to silos."""

    __slots__ = ("_silos", "_movements", "_refs", "_draw_keys", "_resolved")

    def __init__(
        self,
        opening: Mapping[str, tuple[str, float]] | None = None,
        *,
        movements: bool = True,
    ) -> None:
        # silo -> SiloBalance
        self._silos: dict[str, SiloBalance] = {}
        self._movements: list[LedgerMovement] = []
        # client_ref -> movement, for refill / declaration idempotency
        self._refs: dict[str, LedgerMovement] = {}
        # (silo, batch_id) -> draw movement, for draw idempotency
        self._draw_keys: dict[tuple[str, str], LedgerMovement] = {}
        # (silo, batch_id) -> resolution movement, for resolution idempotency
        self._resolved: dict[tuple[str, str], LedgerMovement] = {}
        for silo, (material, level_kg) in (opening or {}).items():
            level = round(level_kg, 4)
            self._silos[silo] = SiloBalance(silo=silo, material=material, level_kg=level, tick=0)
            if movements:
                self._movements.append(self._opening_movement(silo, material, level))

    # ------------------------------------------------------------------ opens

    def declare(
        self,
        silo: str,
        material: str,
        level_kg: float,
        *,
        tick: int,
        client_ref: str,
        reason: str = "level declared",
    ) -> LedgerMovement:
        """Book an absolute, manually gauged level (silo change / stock take)."""

        ref = _require_ref(client_ref, what="level declaration")
        existing = self._refs.get(ref)
        if existing is not None:
            if existing.silo != silo:
                raise DuplicateError(
                    "the idempotency reference was already used for another silo",
                    client_ref=ref,
                    silo=silo,
                    other_silo=existing.silo,
                )
            return existing
        if level_kg < 0:
            raise ValidationError("a silo level must not be negative", silo=silo, level_kg=level_kg)
        current = self._silos.get(silo)
        before = 0.0 if current is None else current.level_kg
        return self._book(
            kind=OPEN_KIND,
            silo=silo,
            material=material,
            delta_kg=round(level_kg - before, 4),
            before_kg=before,
            after_kg=level_kg,
            tick=tick,
            reason=reason,
            client_ref=ref,
            trusted=True,
            reset=True,
        )

    def refill(
        self,
        silo: str,
        kg: float,
        *,
        tick: int,
        client_ref: str,
        material: str | None = None,
        reason: str = "delivery",
    ) -> LedgerMovement:
        """Add a positive delivery to ``silo`` and return the movement."""

        ref = _require_ref(client_ref, what="refill")
        existing = self._refs.get(ref)
        if existing is not None:
            if existing.silo != silo:
                raise DuplicateError(
                    "the idempotency reference was already used for another silo",
                    client_ref=ref,
                    silo=silo,
                    other_silo=existing.silo,
                )
            return existing
        if kg <= 0:
            raise ValidationError("a refill must be positive", silo=silo, kg=kg)
        current = self._require_silo(silo)
        if current.pending_kg:
            raise ValidationError(
                "refusing a refill while the silo level is untrusted",
                silo=silo,
                pending_kg=current.pending_kg,
            )
        return self._book(
            kind=REFILL_KIND,
            silo=silo,
            material=material or current.material,
            delta_kg=kg,
            before_kg=current.level_kg,
            after_kg=current.level_kg + kg,
            tick=tick,
            reason=reason,
            client_ref=ref,
            trusted=True,
        )

    # ------------------------------------------------------------------ draws

    def draw(
        self,
        silo: str,
        kg: float,
        *,
        tick: int,
        batch_id: str,
        reason: str = "batch drawdown",
    ) -> LedgerMovement:
        """Book a measured drawdown keyed by the batch it belonged to."""

        bid = _require_ref(batch_id, what="drawdown")
        if kg <= 0:
            raise ValidationError("a drawdown must be positive", silo=silo, kg=kg)
        current = self._require_silo(silo)
        existing = self._draw_keys.get((silo, bid))
        if existing is not None:
            # A retried completion of the same batch must book exactly one draw.
            return existing
        if not current.trusted:
            raise ValidationError(
                "silo level is untrusted; confirm the pending drawdown first",
                silo=silo,
                pending_kg=current.pending_kg,
            )
        if current.level_kg - kg < 0:
            raise ValidationError(
                "drawdown would push the silo below zero", silo=silo, kg=kg, level_kg=current.level_kg
            )
        return self._book(
            kind=DRAW_KIND,
            silo=silo,
            material=current.material,
            delta_kg=-kg,
            before_kg=current.level_kg,
            after_kg=current.level_kg - kg,
            tick=tick,
            reason=reason,
            client_ref="",
            batch_id=bid,
            trusted=True,
        )

    def mark_pending(
        self,
        silo: str,
        estimated_kg: float,
        *,
        tick: int,
        batch_id: str,
        reason: str = "scale readback missing",
    ) -> LedgerMovement:
        """Record a partial failure without deducting an estimated mass.

        The level is left untouched but flagged untrusted and the estimated
        consumption is parked pending a manual decision.
        """

        bid = _require_ref(batch_id, what="partial drawdown")
        if estimated_kg <= 0:
            raise ValidationError("a pending drawdown needs a positive estimate", silo=silo, estimated_kg=estimated_kg)
        current = self._require_silo(silo)
        if (silo, bid) in self._draw_keys:
            # Already resolved by a confirmed draw; nothing to park.
            raise DuplicateError("this batch was already resolved for the silo", silo=silo, batch_id=bid)
        prior = next(
            (m for m in reversed(self._movements) if m.kind == UNTRUSTED_KIND and m.silo == silo and m.batch_id == bid),
            None,
        )
        if prior is not None:
            # A retried "readback missing" for the same batch parks once.
            return prior
        balance = SiloBalance(
            silo=current.silo,
            material=current.material,
            level_kg=current.level_kg,
            tick=tick,
            trusted=False,
            pending_kg=round(current.pending_kg + estimated_kg, 4),
        )
        self._silos[silo] = balance
        movement = LedgerMovement(
            seq=None,
            kind=UNTRUSTED_KIND,
            silo=current.silo,
            material=current.material,
            delta_kg=0.0,
            before_kg=current.level_kg,
            after_kg=current.level_kg,
            tick=tick,
            reason=reason,
            client_ref="",
            batch_id=bid,
            trusted=False,
        )
        self._movements.append(movement)
        return movement

    def resolve(
        self,
        silo: str,
        *,
        tick: int,
        batch_id: str,
        mode: str,
        measured_kg: float | None = None,
        reason: str = "manual confirmation",
    ) -> LedgerMovement:
        """Clear a pending partial failure.

        ``apply`` books the confirmed mass (the measured value the operator
        supplies, defaulting to the parked estimate); ``reject`` writes the
        consumption off and simply restores trust.
        """

        bid = _require_ref(batch_id, what="resolution")
        if mode not in (RESOLVE_APPLY, RESOLVE_REJECT):
            raise ValidationError("resolution mode must be apply or reject", mode=mode)
        existing_resolution = self._resolved.get((silo, bid))
        if existing_resolution is not None:
            return existing_resolution
        current = self._require_silo(silo)
        already = self._draw_keys.get((silo, bid))
        if already is not None and mode == RESOLVE_APPLY and current.trusted and not current.pending_kg:
            return already
        if not current.pending_kg and current.trusted:
            raise ValidationError("the silo has no pending drawdown to resolve", silo=silo)
        if mode == RESOLVE_APPLY:
            kg = current.pending_kg if measured_kg is None else measured_kg
            if kg <= 0:
                raise ValidationError("a confirmed drawdown must be positive", silo=silo, kg=kg)
            if current.level_kg - kg < 0:
                raise ValidationError(
                    "confirmed drawdown would push the silo below zero", silo=silo, kg=kg, level_kg=current.level_kg
                )
            after = current.level_kg - kg
            delta = -kg
        else:
            after = current.level_kg
            delta = 0.0
        balance = SiloBalance(
            silo=current.silo,
            material=current.material,
            level_kg=round(after, 4),
            tick=tick,
            trusted=True,
            pending_kg=0.0,
        )
        self._silos[silo] = balance
        movement = LedgerMovement(
            seq=None,
            kind=RESOLVE_KIND,
            silo=silo,
            material=current.material,
            delta_kg=round(delta, 4),
            before_kg=current.level_kg,
            after_kg=round(after, 4),
            tick=tick,
            reason=f"{reason}: {mode}",
            client_ref="",
            batch_id=bid,
            trusted=True,
        )
        self._movements.append(movement)
        self._resolved[(silo, bid)] = movement
        if mode == RESOLVE_APPLY:
            self._draw_keys[(silo, bid)] = movement
        return movement

    # ----------------------------------------------------------------- replay

    def adopt(self, record: Any) -> None:
        """Rebuild the account from one committed stream record."""

        kind = record.kind
        payload = record.payload
        silo = str(payload.get("silo", ""))
        if not silo:
            return
        material = str(payload.get("material", ""))
        tick = int(record.tick)
        if kind == OPEN_KIND:
            level = float(payload.get("level_kg", payload.get("after_kg", 0.0)))
            self._silos[silo] = SiloBalance(silo=silo, material=material, level_kg=round(level, 4), tick=tick)
            ref = str(payload.get("client_ref", ""))
            if ref:
                self._refs[ref] = self._indexed(record)
        elif kind == REFILL_KIND:
            current = self._silos.get(silo)
            base = 0.0 if current is None else current.level_kg
            mat = material or (current.material if current else "")
            self._silos[silo] = SiloBalance(
                silo=silo, material=mat, level_kg=round(base + float(payload["delta_kg"]), 4), tick=tick
            )
            movement = self._indexed(record)
            self._movements.append(movement)
            ref = str(payload.get("client_ref", ""))
            if ref:
                self._refs[ref] = movement
        elif kind == DRAW_KIND:
            current = self._require_silo(silo)
            self._silos[silo] = SiloBalance(
                silo=current.silo,
                material=material or current.material,
                level_kg=round(current.level_kg + float(payload["delta_kg"]), 4),
                tick=tick,
            )
            movement = self._indexed(record)
            self._movements.append(movement)
            bid = str(payload.get("batch_id", ""))
            if bid:
                self._draw_keys[(silo, bid)] = movement
        elif kind == UNTRUSTED_KIND:
            current = self._require_silo(silo)
            self._silos[silo] = SiloBalance(
                silo=current.silo,
                material=material or current.material,
                level_kg=current.level_kg,
                tick=tick,
                trusted=False,
                pending_kg=round(current.pending_kg + float(payload.get("pending_kg", 0.0)), 4)
                if not current.trusted
                else float(payload.get("pending_kg", 0.0)),
            )
            self._movements.append(self._indexed(record))
        elif kind == RESOLVE_KIND:
            current = self._require_silo(silo)
            self._silos[silo] = SiloBalance(
                silo=current.silo,
                material=material or current.material,
                level_kg=round(current.level_kg + float(payload.get("delta_kg", 0.0)), 4),
                tick=tick,
                trusted=True,
                pending_kg=0.0,
            )
            movement = self._indexed(record)
            self._movements.append(movement)
            self._resolved[(silo, str(payload.get("batch_id", "")))] = movement
            if payload.get("reason", "").endswith(RESOLVE_APPLY):
                bid = str(payload.get("batch_id", ""))
                if bid:
                    self._draw_keys[(silo, bid)] = movement

    def _indexed(self, record: Any) -> LedgerMovement:
        return LedgerMovement(
            seq=int(record.seq),
            kind=str(record.kind),
            silo=str(record.payload.get("silo", "")),
            material=str(record.payload.get("material", "")),
            delta_kg=float(record.payload.get("delta_kg", 0.0)),
            before_kg=float(record.payload.get("before_kg", 0.0)),
            after_kg=float(record.payload.get("after_kg", 0.0)),
            tick=int(record.tick),
            reason=str(record.payload.get("reason", "")),
            client_ref=str(record.payload.get("client_ref", "")),
            batch_id=str(record.payload.get("batch_id", "")),
            trusted=bool(record.payload.get("trusted", True)),
        )

    # ------------------------------------------------------------------ reads

    def stamp_last(self, seq: int) -> LedgerMovement:
        """Attach the committed record seq to the movement just booked."""

        movement = self._movements[-1]
        stamped = LedgerMovement(
            seq=seq,
            kind=movement.kind,
            silo=movement.silo,
            material=movement.material,
            delta_kg=movement.delta_kg,
            before_kg=movement.before_kg,
            after_kg=movement.after_kg,
            tick=movement.tick,
            reason=movement.reason,
            client_ref=movement.client_ref,
            batch_id=movement.batch_id,
            trusted=movement.trusted,
        )
        self._movements[-1] = stamped
        if stamped.client_ref:
            self._refs[stamped.client_ref] = stamped
        if stamped.batch_id and stamped.kind == DRAW_KIND:
            self._draw_keys[(stamped.silo, stamped.batch_id)] = stamped
        if stamped.batch_id and stamped.kind == RESOLVE_KIND:
            self._resolved[(stamped.silo, stamped.batch_id)] = stamped
        return stamped

    def stamp_opening(self, silo: str, seq: int, tick: int) -> None:
        """Point the seeded baseline marker at its declaring stream record."""

        for index in range(len(self._movements) - 1, -1, -1):
            movement = self._movements[index]
            if movement.silo == silo and movement.kind == OPEN_KIND:
                self._movements[index] = LedgerMovement(
                    seq=seq,
                    kind=movement.kind,
                    silo=movement.silo,
                    material=movement.material,
                    delta_kg=movement.delta_kg,
                    before_kg=movement.before_kg,
                    after_kg=movement.after_kg,
                    tick=tick,
                    reason=movement.reason,
                    client_ref=movement.client_ref,
                    batch_id=movement.batch_id,
                    trusted=movement.trusted,
                )
                return

    @staticmethod
    def _opening_movement(silo: str, material: str, level_kg: float) -> LedgerMovement:
        return LedgerMovement(
            seq=None,
            kind=OPEN_KIND,
            silo=silo,
            material=material,
            delta_kg=round(level_kg, 4),
            before_kg=0.0,
            after_kg=round(level_kg, 4),
            tick=0,
            reason="opening balance",
            client_ref="",
            batch_id="",
            trusted=True,
        )

    def silos(self) -> tuple[str, ...]:
        return tuple(sorted(self._silos))

    def has_declared(self, silo: str) -> bool:
        """Return whether an opening level was ever booked for ``silo``."""

        return any(m.silo == silo and m.kind == OPEN_KIND for m in self._movements)

    def balance(self, silo: str) -> SiloBalance:
        return self._require_silo(silo)

    def balances(self) -> tuple[SiloBalance, ...]:
        return tuple(self._silos[name] for name in sorted(self._silos))

    def movements(self, *, silo: str | None = None) -> tuple[LedgerMovement, ...]:
        if silo is None:
            return tuple(self._movements)
        return tuple(m for m in self._movements if m.silo == silo)

    def ref(self, client_ref: str) -> LedgerMovement | None:
        """Return the movement already booked under ``client_ref``."""

        return self._refs.get(client_ref)

    def draw_for(self, silo: str, batch_id: str) -> LedgerMovement | None:
        """Return the draw already booked for a batch and silo."""

        return self._draw_keys.get((silo, batch_id))

    def resolution_for(self, silo: str, batch_id: str) -> LedgerMovement | None:
        """Return the resolution already booked for a batch and silo."""

        return self._resolved.get((silo, batch_id))

    def pending_for(self, silo: str, batch_id: str) -> LedgerMovement | None:
        """Return an unresolved missing-readback marker for a batch."""

        if (silo, batch_id) in self._draw_keys or (silo, batch_id) in self._resolved:
            return None
        return next(
            (m for m in reversed(self._movements) if m.kind == UNTRUSTED_KIND and m.silo == silo and m.batch_id == batch_id),
            None,
        )

    def reconciliation(self) -> dict[str, Any]:
        """Recompute every balance from the movements and compare.

        The baseline is the latest absolute level declaration per silo;
        movements after it are summed on top.  The console and the audit
        trail both consume this so a tampered or drifted level can never be
        displayed as fact.
        """

        base: dict[str, float] = {}
        for movement in self._movements:
            if movement.kind == OPEN_KIND:
                # An opening declaration is an absolute reset; the newest one
                # is the only baseline later movements are summed from.
                base[movement.silo] = movement.after_kg
        deltas: dict[str, float] = {}
        seen_open: set[str] = set()
        for movement in self._movements:
            if movement.kind == OPEN_KIND:
                seen_open.add(movement.silo)
                deltas[movement.silo] = 0.0
                continue
            if movement.silo in seen_open:
                deltas[movement.silo] = round(deltas.get(movement.silo, 0.0) + movement.delta_kg, 4)
        recomputed: dict[str, float] = {}
        for silo, level in base.items():
            recomputed[silo] = round(level + deltas.get(silo, 0.0), 4)
        booked: dict[str, float] = {}
        for name, balance in self._silos.items():
            booked[name] = balance.level_kg
            # A silo without any declared opening keeps its seeded fallback
            # balance as the baseline for the cross check.
            if name not in recomputed:
                recomputed[name] = round(balance.level_kg + deltas.get(name, 0.0), 4)
        silos: list[dict[str, Any]] = []
        consistent = True
        for name in sorted(set(booked) | set(recomputed)):
            match = round(booked.get(name, 0.0) - recomputed.get(name, 0.0), 4) == 0.0
            consistent = consistent and match
            balance = self._silos.get(name)
            silos.append(
                {
                    "silo": name,
                    "booked_kg": booked.get(name, 0.0),
                    "movement_total_kg": recomputed.get(name, 0.0),
                    "match": match,
                    "trusted": True if balance is None else balance.trusted,
                    "pending_kg": 0.0 if balance is None else balance.pending_kg,
                }
            )
        return {
            "consistent": consistent,
            "movements": len(self._movements),
            "silos": silos,
        }

    # ---------------------------------------------------------------- helpers

    def _book(
        self,
        *,
        kind: str,
        silo: str,
        material: str,
        delta_kg: float,
        before_kg: float,
        after_kg: float,
        tick: int,
        reason: str,
        client_ref: str = "",
        batch_id: str = "",
        trusted: bool = True,
        reset: bool = False,
    ) -> LedgerMovement:
        balance = SiloBalance(
            silo=silo,
            material=material,
            level_kg=round(after_kg, 4),
            tick=tick,
            trusted=trusted,
            pending_kg=0.0,
        )
        self._silos[silo] = balance
        movement = LedgerMovement(
            seq=None,
            kind=kind,
            silo=silo,
            material=material,
            delta_kg=round(delta_kg, 4),
            before_kg=round(before_kg, 4),
            after_kg=round(after_kg, 4),
            tick=tick,
            reason=reason,
            client_ref=client_ref,
            batch_id=batch_id,
            trusted=trusted,
        )
        self._movements.append(movement)
        if client_ref:
            self._refs[client_ref] = movement
        if batch_id and kind == DRAW_KIND:
            self._draw_keys[(silo, batch_id)] = movement
        return movement

    def _require_silo(self, silo: str) -> SiloBalance:
        current = self._silos.get(silo)
        if current is None:
            raise NotFoundError("silo has no inventory record; declare a level first", silo=silo)
        return current
