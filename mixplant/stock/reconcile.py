"""Reconcile the live stock ledger against the durable journal.

Audit never trusts an in memory balance on its own.  It folds the committed
``stock.*`` records again from scratch into a fresh ledger and compares it
with the live ledger.  Both sides must agree on the digest, on the last
published balance snapshot and on the set of open pending dosings.  Any
difference is reported as a mismatch instead of being repaired silently, so
the console view and the audit view can never silently diverge.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from ..store.repository import RecordRepository
from .ledger import BALANCE_KIND, INPUT_KINDS, StockLedger


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """The outcome of one consistency check."""

    consistent: bool
    checked_silos: int
    replayed_records: int
    live_digest: str
    rebuilt_digest: str
    mismatches: tuple[str, ...]

    def describe(self) -> dict[str, Any]:
        return {
            "consistent": self.consistent,
            "checked_silos": self.checked_silos,
            "replayed_records": self.replayed_records,
            "live_digest": self.live_digest,
            "rebuilt_digest": self.rebuilt_digest,
            "mismatches": list(self.mismatches),
        }


def rebuild_ledger(records: Any) -> tuple[StockLedger, int]:
    """Fold committed stock records into a brand new ledger."""

    rebuilt = StockLedger()
    count = 0
    for record in records:
        if record.kind in INPUT_KINDS:
            rebuilt.apply_record(
                {"kind": record.kind, "payload": record.payload}
            )
            count += 1
    return rebuilt, count


def reconcile(
    *,
    repository: RecordRepository,
    ledger: StockLedger,
) -> ReconcileReport:
    """Compare the live books with a fresh fold of the journal."""

    rebuilt, replayed = rebuild_ledger(repository.committed_records())
    mismatches: list[str] = []

    live_digest = ledger.digest()
    rebuilt_digest = rebuilt.digest()
    if live_digest != rebuilt_digest:
        mismatches.append("digest")

    live_balance = ledger.balance()
    snapshot = repository.latest(BALANCE_KIND)
    if snapshot is None:
        mismatches.append("missing_balance_snapshot")
    elif snapshot.payload.get("digest") != live_digest:
        mismatches.append("balance_snapshot_stale")
    if snapshot is not None and snapshot.payload.get("digest") != rebuilt_digest:
        mismatches.append("balance_snapshot_journal")

    for name in sorted(set(ledger.silos()) | set(rebuilt.silos())):
        if name not in ledger.silos() or name not in rebuilt.silos():
            mismatches.append(f"silo_presence:{name}")
            continue
        live_silo = ledger.require_silo(name).describe()
        rebuilt_silo = rebuilt.require_silo(name).describe()
        for field in ("level_g", "safety_g", "trusted", "material"):
            if live_silo[field] != rebuilt_silo[field]:
                mismatches.append(f"{name}:{field}")
        if live_silo["pending_ids"] != rebuilt_silo["pending_ids"]:
            mismatches.append(f"{name}:pending_ids")

    report = ReconcileReport(
        consistent=not mismatches,
        checked_silos=len(ledger.silos()),
        replayed_records=replayed,
        live_digest=live_digest,
        rebuilt_digest=rebuilt_digest,
        mismatches=tuple(mismatches),
    )
    return report
