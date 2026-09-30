"""The append only stock ledger.

Every movement of binder stock is one record applied in journal order:

``stock.silo``    a silo registered with its opening level and safety line
``stock.route``   which material a silo currently feeds (a silo change)
``stock.draw``    a dosing whose scale readback was confirmed (hard draw)
``stock.pending`` a dosing whose scale readback is missing or invalid; the
                  silo goes untrusted and the estimated mass is NOT deducted
``stock.resolve`` manual resolution of a pending dosing (hard draw)
``stock.refill``  a delivery added to a silo, or a measured level reset

After every committed mutation the ledger republishes one ``stock.balance``
snapshot.  The snapshot is the only stock document the console renders and
the only one audit reconciliation trusts, so both sides compare identical
bytes.  Mass only ever leaves a silo through ``draw`` or ``resolve``, and a
draw can never push a level below zero; that floor is enforced here, below
every policy decision.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any, Mapping

from ..errors import (
    DuplicateError,
    NotFoundError,
    OrderViolationError,
    ValidationError,
)
from .units import to_kg

REGISTER_KIND = "stock.silo"
ROUTE_KIND = "stock.route"
DRAW_KIND = "stock.draw"
PENDING_KIND = "stock.pending"
RESOLVE_KIND = "stock.resolve"
REFILL_KIND = "stock.refill"
BALANCE_KIND = "stock.balance"

# Records the ledger fold understands, mapped to the method that applies
# them.  Balance snapshots are fold output, not fold input.
INPUT_KINDS = frozenset(
    {REGISTER_KIND, ROUTE_KIND, DRAW_KIND, PENDING_KIND, RESOLVE_KIND, REFILL_KIND}
)


@dataclass(frozen=True, slots=True)
class PendingDosing:
    """One dosing that is waiting for a human decision.

    The estimated mass is recorded for display only.  It never leaves the
    trusted balance while the entry stays open.
    """

    pending_id: str
    batch_id: str
    silo: str
    material: str
    estimated_g: int
    tick: int
    reason: str

    def describe(self) -> dict[str, Any]:
        return {
            "active": True,
            "pending_id": self.pending_id,
            "batch_id": self.batch_id,
            "silo": self.silo,
            "material": self.material,
            "estimated_kg": to_kg(self.estimated_g),
            "estimated_g": self.estimated_g,
            "tick": self.tick,
            "reason": self.reason,
        }


@dataclass(slots=True)
class SiloStock:
    """The current stock picture of one silo."""

    name: str
    material: str
    balance_g: int
    safety_g: int
    pending: dict[str, PendingDosing]

    @property
    def trusted(self) -> bool:
        """A silo with an unresolved dosing cannot be trusted."""

        return not self.pending

    def pending_for_batch(self, batch_id: str, material: str) -> PendingDosing | None:
        """Return this silo's open entry for one batch dosing."""

        for entry in self.pending.values():
            if entry.batch_id == batch_id and entry.material == material:
                return entry
        return None

    def describe(self) -> dict[str, Any]:
        pending = tuple(sorted(self.pending))
        return {
            "silo": self.name,
            "material": self.material,
            "level_kg": to_kg(self.balance_g),
            "level_g": self.balance_g,
            "safety_kg": to_kg(self.safety_g),
            "safety_g": self.safety_g,
            "trusted": self.trusted,
            "pending_ids": list(pending),
            "pending_kg": to_kg(sum(item.estimated_g for item in self.pending.values())),
        }


class StockLedger:
    """Folds stock records into the current level of every silo."""

    __slots__ = (
        "_silos",
        "_materials",
        "_batch_silos",
        "_dosed",
        "_resolved",
        "_tickets",
        "_version",
    )

    def __init__(self) -> None:
        self._silos: dict[str, SiloStock] = {}
        self._materials: dict[str, str] = {}
        self._batch_silos: dict[tuple[str, str], str] = {}
        # Natural keys already booked, so a duplicated record never moves
        # mass twice even when the caller lost its in memory idempotency map.
        self._dosed: set[tuple[str, str]] = set()
        self._resolved: set[str] = set()
        self._tickets: dict[str, set[str]] = {}
        self._version = 0

    # ------------------------------------------------------------------ reads

    def require_silo(self, silo: str) -> SiloStock:
        found = self._silos.get(silo)
        if found is None:
            raise NotFoundError("silo is not registered", silo=silo)
        return found

    def silos(self) -> tuple[str, ...]:
        return tuple(sorted(self._silos))

    def route_for_material(self, material: str) -> str | None:
        """Return the silo currently feeding ``material``, if any."""

        return self._materials.get(material)

    def batch_route(self, batch_id: str, material: str) -> str | None:
        """Return the silo a batch first took ``material`` from."""

        return self._batch_silos.get((batch_id, material))

    def is_dosed(self, batch_id: str, material: str) -> bool:
        """Return whether a batch's dosing of a material is already booked."""

        return (batch_id, material) in self._dosed

    def dosed_silo(self, batch_id: str, material: str) -> str | None:
        """Return the silo the already booked dosing used."""

        if (batch_id, material) in self._dosed:
            return self._batch_silos.get((batch_id, material))
        return None

    def ticket_used(self, silo: str, ticket: str) -> bool:
        """Return whether a delivery ticket was already booked on a silo."""

        return ticket in self._tickets.get(silo, set())

    def pending_for_batch(self, batch_id: str, material: str) -> PendingDosing | None:
        """Return the open pending entry of a batch dosing, if any."""

        silo_name = self._batch_silos.get((batch_id, material))
        if silo_name is None:
            return None
        return self._silos[silo_name].pending_for_batch(batch_id, material)

    def balance(self) -> dict[str, Any]:
        """Return the canonical balance document.

        The digest is a sha256 over the sorted per silo numbers; the console
        publishes this document and audit reconciliation recomputes it from
        the raw journal, so equality proves the two views agree.
        """

        silos = {name: self._silos[name].describe() for name in self.silos()}
        return {
            "active": True,
            "version": self._version,
            "digest": self.digest(),
            "routes": dict(sorted(self._materials.items())),
            "silos": silos,
        }

    def digest(self) -> str:
        """Hash the authoritative numbers into one comparable string."""

        hasher = hashlib.sha256()
        for name in self.silos():
            silo = self._silos[name]
            pending = ",".join(
                f"{pid}:{silo.pending[pid].estimated_g}" for pid in sorted(silo.pending)
            )
            row = (
                f"{name}|{silo.material}|{silo.balance_g}|{silo.safety_g}"
                f"|{int(silo.trusted)}|{pending}"
            )
            hasher.update(row.encode("utf-8"))
            hasher.update(b"\n")
        return hasher.hexdigest()

    # -------------------------------------------------------------- mutations

    def register(
        self,
        *,
        silo: str,
        material: str,
        opening_g: int,
        safety_g: int,
    ) -> SiloStock:
        """Register a silo at its opening level (manual entry / adoption)."""

        if not silo:
            raise ValidationError("silo name must not be empty")
        if not material:
            raise ValidationError("material must not be empty", silo=silo)
        if opening_g < 0:
            raise ValidationError("opening level must not be negative", silo=silo)
        if safety_g < 0:
            raise ValidationError("safety line must not be negative", silo=silo)
        if safety_g > opening_g:
            raise ValidationError(
                "safety line must not sit above the opening level",
                silo=silo,
                safety_g=safety_g,
                opening=opening_g,
            )
        if silo in self._silos:
            raise DuplicateError("silo was registered twice", silo=silo)
        stock = SiloStock(
            name=silo,
            material=material,
            balance_g=opening_g,
            safety_g=safety_g,
            pending={},
        )
        self._silos[silo] = stock
        if material not in self._materials:
            self._materials[material] = silo
        self._bump()
        return stock

    def switch_route(self, *, silo: str, material: str, transfer_g: int = 0) -> SiloStock:
        """Point ``material`` at ``silo`` (a silo change).

        ``transfer_g`` only moves the residual a change physically carries
        from the old silo onto the new one; mass stays conserved.
        """

        stock = self.require_silo(silo)
        if stock.material != material:
            raise OrderViolationError(
                "the target silo does not physically hold this material",
                silo=silo,
                holds=stock.material,
                material=material,
            )
        if not stock.trusted:
            raise OrderViolationError(
                "cannot switch onto a silo that has an unresolved dosing",
                silo=silo,
                pending=sorted(stock.pending),
            )
        old = self._materials.get(material)
        if transfer_g < 0:
            raise ValidationError("a transferred residual must not be negative", silo=silo)
        if old is not None and old != silo and transfer_g:
            donor = self.require_silo(old)
            if not donor.trusted:
                raise OrderViolationError(
                    "cannot move the residual out of an untrusted silo",
                    silo=old,
                    pending=sorted(donor.pending),
                )
            if transfer_g > donor.balance_g:
                raise ValidationError(
                    "transferred residual exceeds the old silo level",
                    silo=old,
                    transfer_g=transfer_g,
                    balance=donor.balance_g,
                )
            donor.balance_g -= transfer_g
            stock.balance_g += transfer_g
        self._materials[material] = silo
        self._bump()
        return stock

    def draw(
        self,
        *,
        batch_id: str,
        silo: str,
        material: str,
        grams: int,
        request_id: str,
        tick: int,
        source: str,
        pending_id: str | None = None,
        pending_reason: str | None = None,
    ) -> tuple[SiloStock, PendingDosing | None]:
        """Apply one dosing to the ledger.

        With a positive confirmed mass it is a hard draw.  With a missing
        readback the estimated mass is parked on a pending entry and the
        balance is deliberately left untouched.
        """

        if not batch_id:
            raise ValidationError("batch id must not be empty")
        stock = self.require_silo(silo)
        if grams <= 0:
            raise ValidationError("dosing mass must be positive", silo=silo, grams=grams)
        key = (batch_id, material)
        if key in self._dosed:
            raise DuplicateError(
                "this batch dosing was already booked", batch_id=batch_id, material=material
            )
        routed = self._batch_silos.get(key)
        if routed is None:
            self._batch_silos[key] = silo
        elif routed != silo:
            raise OrderViolationError(
                "a batch may not switch silo mid dosing",
                batch_id=batch_id,
                material=material,
                silo=silo,
                routed=routed,
            )
        pending: PendingDosing | None = None
        if source == "scale":
            if grams > stock.balance_g:
                raise ValidationError(
                    "dosing would push the silo below zero",
                    silo=silo,
                    dosing=grams,
                    balance=stock.balance_g,
                )
            stock.balance_g -= grams
            self._dosed.add(key)
        elif source == "pending":
            if pending_id is None:
                raise ValidationError("a pending draw needs a pending id")
            if pending_id in self._resolved:
                raise DuplicateError("pending dosing was already resolved", pending_id=pending_id)
            entry = stock.pending.pop(pending_id, None)
            if entry is None:
                raise NotFoundError("pending dosing does not exist", pending_id=pending_id, silo=silo)
            if grams > stock.balance_g:
                stock.pending[pending_id] = entry
                raise ValidationError(
                    "confirmed dosing would push the silo below zero",
                    silo=silo,
                    dosing=grams,
                    balance=stock.balance_g,
                )
            stock.balance_g -= grams
            self._dosed.add(key)
            self._resolved.add(pending_id)
        else:  # readback missing: park the estimate, never hard deduct it
            if pending_id is None:
                raise ValidationError("a partial failure needs a pending id")
            if stock.pending_for_batch(batch_id, material) is not None:
                raise DuplicateError(
                    "this batch dosing is already awaiting confirmation",
                    batch_id=batch_id,
                    material=material,
                )
            entry = PendingDosing(
                pending_id=pending_id,
                batch_id=batch_id,
                silo=silo,
                material=material,
                estimated_g=grams,
                tick=tick,
                reason=pending_reason or "scale_readback_missing",
            )
            stock.pending[pending_id] = entry
            pending = entry
        self._bump()
        return stock, pending

    def refill(
        self,
        *,
        silo: str,
        mode: str,
        ticket: str | None = None,
        added_g: int = 0,
        measured_g: int | None = None,
    ) -> tuple[SiloStock, int]:
        """Apply a delivery or a measured level reset, return silo and delta."""

        stock = self.require_silo(silo)
        if not stock.trusted:
            raise OrderViolationError(
                "unresolved dosing must be confirmed before a refill",
                silo=silo,
                pending=sorted(stock.pending),
            )
        if ticket:
            used = self._tickets.setdefault(silo, set())
            if ticket in used:
                raise DuplicateError("delivery ticket was booked twice", silo=silo, ticket=ticket)
        before = stock.balance_g
        if mode == "additive":
            if added_g <= 0:
                raise ValidationError("a delivery must be positive", silo=silo, kg=added_g)
            stock.balance_g += added_g
        elif mode == "measured":
            if measured_g is None or measured_g < 0:
                raise ValidationError("a measured refill needs a non negative level", silo=silo)
            stock.balance_g = measured_g
        else:
            raise ValidationError("unknown refill mode", mode=mode)
        if ticket is not None:
            self._tickets.setdefault(silo, set()).add(ticket)
        self._bump()
        return stock, stock.balance_g - before

    # --------------------------------------------------------------- replay

    def apply_record(self, record: Mapping[str, Any]) -> None:
        """Fold one committed journal record back into the ledger."""

        kind = record["kind"]
        payload = record.get("payload", {})
        if kind == REGISTER_KIND:
            if payload.get("silo") in self._silos:
                self._bump()
                return
            self.register(
                silo=str(payload["silo"]),
                material=str(payload["material"]),
                opening_g=int(payload["opening_g"]),
                safety_g=int(payload["safety_g"]),
            )
        elif kind == ROUTE_KIND:
            self.switch_route(
                silo=str(payload["silo"]),
                material=str(payload["material"]),
                transfer_g=int(payload.get("transfer_g", 0)),
            )
        elif kind == DRAW_KIND:
            self.draw(
                batch_id=str(payload["batch_id"]),
                silo=str(payload["silo"]),
                material=str(payload["material"]),
                grams=int(payload["grams_g"]),
                request_id=str(payload["request_id"]),
                tick=int(payload.get("tick", 0)),
                source=str(payload["source"]),
                pending_id=payload.get("pending_id"),
            )
        elif kind == PENDING_KIND:
            self._batch_silos[(str(payload["batch_id"]), str(payload["material"]))] = str(payload["silo"])
            stock = self.require_silo(str(payload["silo"]))
            entry = PendingDosing(
                pending_id=str(payload["pending_id"]),
                batch_id=str(payload["batch_id"]),
                silo=str(payload["silo"]),
                material=str(payload["material"]),
                estimated_g=int(payload["estimated_g"]),
                tick=int(payload.get("tick", 0)),
                reason=str(payload.get("reason", "scale_readback_missing")),
            )
            stock.pending[entry.pending_id] = entry
            self._bump()
        elif kind == RESOLVE_KIND:
            self.draw(
                batch_id=str(payload["batch_id"]),
                silo=str(payload["silo"]),
                material=str(payload["material"]),
                grams=int(payload["grams_g"]),
                request_id=str(payload["request_id"]),
                tick=int(payload.get("tick", 0)),
                source="pending",
                pending_id=str(payload["pending_id"]),
            )
        elif kind == REFILL_KIND:
            if str(payload.get("mode")) == "additive":
                self.refill(
                    silo=str(payload["silo"]),
                    mode="additive",
                    ticket=str(payload.get("ticket", "")),
                    added_g=int(payload["delta_g"]),
                )
            else:
                self.refill(
                    silo=str(payload["silo"]),
                    mode="measured",
                    ticket=str(payload.get("ticket", "")),
                    measured_g=int(payload["level_g"]),
                )
        # BALANCE_KIND is fold output and is ignored by the fold.

    def pending(self, silo: str, pending_id: str) -> PendingDosing:
        stock = self.require_silo(silo)
        entry = stock.pending.get(pending_id)
        if entry is None:
            raise NotFoundError("pending dosing does not exist", pending_id=pending_id, silo=silo)
        return entry

    # ------------------------------------------------------------- internals

    def _bump(self) -> None:
        self._version += 1

    @property
    def version(self) -> int:
        return self._version
