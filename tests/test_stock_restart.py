"""The stock ledger must rebuild identically after a process restart."""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from mixplant.config import StationConfig
from mixplant.runtime import PlantRuntime
from tests.support import run


def _boot(data_dir: Path) -> PlantRuntime:
    config = StationConfig(data_dir=data_dir, web_dir=Path("web"))
    runtime = PlantRuntime(config, storage="file")
    runtime.bootstrap()
    return runtime


def test_stock_rebuilds_and_keeps_idempotency_across_a_restart() -> None:
    data_dir = Path(tempfile.mkdtemp())
    runtime = _boot(data_dir)
    run(
        runtime,
        "recipe.publish",
        {"name": "c30", "grade": "C30", "factors": {"cement": 330.0, "fly-ash": 80.0}},
    )
    run(runtime, "recipe.select", {"name": "c30"})
    run(
        runtime,
        "stock.register",
        {"request_id": "reg-1", "silo": "silo-1", "material": "cement", "opening_kg": 1000.0, "safety_kg": 200.0},
    )
    run(
        runtime,
        "stock.register",
        {"request_id": "reg-2", "silo": "silo-2", "material": "fly-ash", "opening_kg": 500.0, "safety_kg": 50.0},
    )
    run(runtime, "stock.refill", {"request_id": "ref-1", "silo": "silo-1", "ticket": "D-1", "added_kg": 500.0})
    run(
        runtime,
        "stock.dose",
        {
            "request_id": "dose-1",
            "batch_id": "B-1",
            "silo": "silo-1",
            "material": "cement",
            "estimated_kg": 330.0,
            "actual_kg": 330.0,
            "grade": "C30",
        },
    )
    parked = run(
        runtime,
        "stock.dose",
        {
            "request_id": "dose-2",
            "batch_id": "B-2",
            "silo": "silo-2",
            "material": "fly-ash",
            "estimated_kg": 80.0,
            "readback": "missing",
            "grade": "C30",
        },
    )
    digest_before = runtime.stock.ledger.digest()
    runtime.close()

    restored = _boot(data_dir)
    assert restored.stock.ledger.digest() == digest_before
    assert restored.stock.ledger.require_silo("silo-1").balance_g == 1_170_000
    assert restored.stock.ledger.require_silo("silo-2").trusted is False
    assert parked["pending_id"] in restored.stock.ledger.require_silo("silo-2").pending

    # A retried delivery after the restart stays idempotent.
    again = run(restored, "stock.refill", {"request_id": "ref-1", "silo": "silo-1", "ticket": "D-1", "added_kg": 500.0})
    assert again.get("idempotent") is True
    assert again["silo"]["level_kg"] == 1170.0

    confirmed = run(
        restored,
        "stock.confirm",
        {"request_id": "conf-1", "pending_id": parked["pending_id"], "silo": "silo-2", "actual_kg": 80.0},
    )
    assert confirmed["silo"]["level_kg"] == 420.0
    assert run(restored, "stock.reconcile", {})["consistent"] is True
    restored.close()
