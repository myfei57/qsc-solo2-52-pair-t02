"""Binder silo stock accounting: forecast, safety line, partial failure.

The tests are plain functions named ``test_*`` so they also run without
pytest: ``python3 -m tests.run_tests`` executes them with a tiny shim.
"""

from __future__ import annotations

import pytest

from mixplant.errors import InterlockBlockedError, OrderViolationError, ValidationError
from mixplant.runtime import PlantRuntime
from tests.support import run


@pytest.fixture()
def plant() -> PlantRuntime:
    runtime = PlantRuntime.in_memory()
    runtime.bootstrap()
    run(
        runtime,
        "recipe.publish",
        {
            "name": "c30",
            "grade": "C30",
            "factors": {
                "sand": 760.0,
                "gravel": 1010.0,
                "cement": 330.0,
                "fly-ash": 80.0,
                "water": 175.0,
            },
        },
    )
    run(runtime, "recipe.select", {"name": "c30"})
    run(
        runtime,
        "stock.policy",
        {"request_id": "pol-cement", "silo": "silo-1", "grade": "C30", "action": "warn", "warn_lead_batches": 3},
    )
    run(
        runtime,
        "stock.policy",
        {"request_id": "pol-ash", "silo": "silo-2", "grade": "C30", "action": "block"},
    )
    run(
        runtime,
        "stock.register",
        {"request_id": "reg-1", "silo": "silo-1", "material": "cement", "opening_kg": 1000.0, "safety_kg": 200.0},
    )
    run(
        runtime,
        "stock.register",
        {"request_id": "reg-2", "silo": "silo-2", "material": "fly-ash", "opening_kg": 1000.0, "safety_kg": 50.0},
    )
    return runtime


def by_material(plant: PlantRuntime) -> dict:
    silos = run(plant, "stock.forecast", {})["silos"]
    return {item["material"]: item for item in silos}


# ------------------------------------------------------------------ forecast


def test_forecast_counts_batches_above_the_per_silo_safety_line(plant: PlantRuntime) -> None:
    forecast = run(plant, "stock.forecast", {"quantity": 1.0})
    # cement: (1000-200)/330 = 2; fly ash: (1000-50)/80 = 11 -> plant min 2
    assert forecast["trusted"] is True
    assert forecast["safe_batches"] == 2
    assert by_material(plant)["fly-ash"]["safe_batches"] == 11


def test_forecast_scales_with_batch_size(plant: PlantRuntime) -> None:
    forecast = run(plant, "stock.forecast", {"quantity": 2.0})
    # two cubic metre batches need 660 kg cement: (1000-200)/660 = 1
    assert forecast["safe_batches"] == 1


def test_drawdown_follows_the_recipe_after_every_dosing(plant: PlantRuntime) -> None:
    run(
        plant,
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
    assert by_material(plant)["cement"]["level_g"] == 670_000
    assert by_material(plant)["cement"]["safe_batches"] == 1


# --------------------------------------------------------------- safety line


def test_block_policy_stops_the_dosing_that_would_cross_the_line(plant: PlantRuntime) -> None:
    for index in range(11):
        run(
            plant,
            "stock.dose",
            {
                "request_id": f"ash-{index}",
                "batch_id": f"A-{index}",
                "silo": "silo-2",
                "material": "fly-ash",
                "estimated_kg": 80.0,
                "actual_kg": 80.0,
                "grade": "C30",
            },
        )
    # 1000 - 11*80 = 120 kg; another 80 kg lands on 40 kg, below the 50 line
    assert by_material(plant)["fly-ash"]["level_g"] == 120_000
    with pytest.raises(InterlockBlockedError):
        run(
            plant,
            "stock.dose",
            {
                "request_id": "ash-cross",
                "batch_id": "A-X",
                "silo": "silo-2",
                "material": "fly-ash",
                "estimated_kg": 80.0,
                "actual_kg": 80.0,
                "grade": "C30",
            },
        )


def test_warn_policy_allows_crossing_but_never_the_zero_floor(plant: PlantRuntime) -> None:
    run(
        plant,
        "stock.dose",
        {
            "request_id": "cem-warn",
            "batch_id": "W-1",
            "silo": "silo-1",
            "material": "cement",
            "estimated_kg": 900.0,
            "actual_kg": 900.0,
            "grade": "C30",
        },
    )
    assert by_material(plant)["cement"]["level_g"] == 100_000
    with pytest.raises(InterlockBlockedError):
        run(
            plant,
            "stock.dose",
            {
                "request_id": "cem-empty",
                "batch_id": "W-2",
                "silo": "silo-1",
                "material": "cement",
                "estimated_kg": 500.0,
                "actual_kg": 500.0,
                "grade": "C30",
            },
        )


def test_precheck_reports_the_block_before_dosing(plant: PlantRuntime) -> None:
    decision = run(plant, "stock.precheck", {"batch_id": "NEXT", "grade": "C30", "quantity": 10.0})
    assert decision["allowed"] is False
    assert "silo-1" in decision["forecast"]["blocked_by"]


# --------------------------------------------------------- partial failure


def test_missing_readback_parks_the_estimate_and_marks_the_silo_untrusted(plant: PlantRuntime) -> None:
    outcome = run(
        plant,
        "stock.dose",
        {
            "request_id": "dose-missing",
            "batch_id": "P-1",
            "silo": "silo-2",
            "material": "fly-ash",
            "estimated_kg": 80.0,
            "readback": "missing",
            "grade": "C30",
        },
    )
    assert outcome["deducted"] is False
    assert outcome["trusted"] is False
    assert outcome["silo"]["level_kg"] == 1000.0
    assert outcome["silo"]["pending_kg"] == 80.0
    forecast = run(plant, "stock.forecast", {})
    assert forecast["trusted"] is False
    assert forecast["safe_batches"] is None


def test_untrusted_silo_cannot_be_dosed_or_refilled(plant: PlantRuntime) -> None:
    run(
        plant,
        "stock.dose",
        {
            "request_id": "dose-missing",
            "batch_id": "P-1",
            "silo": "silo-2",
            "material": "fly-ash",
            "estimated_kg": 80.0,
            "readback": "missing",
            "grade": "C30",
        },
    )
    with pytest.raises(InterlockBlockedError):
        run(
            plant,
            "stock.dose",
            {
                "request_id": "dose-next",
                "batch_id": "P-2",
                "silo": "silo-2",
                "material": "fly-ash",
                "estimated_kg": 80.0,
                "actual_kg": 80.0,
                "grade": "C30",
            },
        )
    with pytest.raises(InterlockBlockedError):
        run(
            plant,
            "stock.refill",
            {"request_id": "refill-blocked", "silo": "silo-2", "ticket": "D-9", "added_kg": 100.0},
        )
    with pytest.raises(OrderViolationError):
        run(
            plant,
            "stock.switch",
            {"request_id": "switch-blocked", "material": "fly-ash", "silo": "silo-2"},
        )


def test_manual_confirmation_applies_the_real_mass_and_restores_trust(plant: PlantRuntime) -> None:
    parked = run(
        plant,
        "stock.dose",
        {
            "request_id": "dose-missing",
            "batch_id": "P-1",
            "silo": "silo-2",
            "material": "fly-ash",
            "estimated_kg": 80.0,
            "readback": "missing",
            "grade": "C30",
        },
    )
    confirmed = run(
        plant,
        "stock.confirm",
        {
            "request_id": "confirm-1",
            "pending_id": parked["pending_id"],
            "silo": "silo-2",
            "actual_kg": 79.5,
        },
    )
    assert confirmed["deducted"] is True
    assert confirmed["silo"]["level_kg"] == 920.5
    assert confirmed["silo"]["trusted"] is True


def test_late_scale_readback_resolves_a_pending_dosing(plant: PlantRuntime) -> None:
    parked = run(
        plant,
        "stock.dose",
        {
            "request_id": "dose-missing",
            "batch_id": "P-1",
            "silo": "silo-2",
            "material": "fly-ash",
            "estimated_kg": 80.0,
            "readback": "missing",
            "grade": "C30",
        },
    )
    resolved = run(
        plant,
        "stock.readback",
        {
            "request_id": "readback-1",
            "pending_id": parked["pending_id"],
            "silo": "silo-2",
            "actual_kg": 80.2,
        },
    )
    assert resolved["silo"]["level_kg"] == 919.8
    assert resolved["trusted"] is True


def test_accepting_the_estimate_needs_an_explicit_flag(plant: PlantRuntime) -> None:
    parked = run(
        plant,
        "stock.dose",
        {
            "request_id": "dose-missing",
            "batch_id": "P-1",
            "silo": "silo-2",
            "material": "fly-ash",
            "estimated_kg": 80.0,
            "readback": "missing",
            "grade": "C30",
        },
    )
    with pytest.raises(ValidationError):
        run(
            plant,
            "stock.confirm",
            {"request_id": "confirm-empty", "pending_id": parked["pending_id"], "silo": "silo-2"},
        )
    accepted = run(
        plant,
        "stock.confirm",
        {
            "request_id": "confirm-est",
            "pending_id": parked["pending_id"],
            "silo": "silo-2",
            "accept_estimate": True,
        },
    )
    assert accepted["silo"]["level_kg"] == 920.0


# --------------------------------------------------------------- idempotency


def test_repeated_dosing_with_a_new_request_id_does_not_deduct_twice(plant: PlantRuntime) -> None:
    payload = {
        "batch_id": "I-1",
        "silo": "silo-1",
        "material": "cement",
        "estimated_kg": 330.0,
        "actual_kg": 330.0,
        "grade": "C30",
    }
    first = run(plant, "stock.dose", {"request_id": "req-1", **payload})
    second = run(plant, "stock.dose", {"request_id": "req-2", **payload})
    assert first["silo"]["level_kg"] == 670.0
    assert second.get("idempotent") is True
    assert second["silo"]["level_kg"] == 670.0


def test_repeated_refill_ticket_never_doubles_the_delivery(plant: PlantRuntime) -> None:
    payload = {"silo": "silo-1", "ticket": "D-1", "added_kg": 500.0}
    first = run(plant, "stock.refill", {"request_id": "ref-1", **payload})
    second = run(plant, "stock.refill", {"request_id": "ref-2", **payload})
    assert first["silo"]["level_kg"] == 1500.0
    assert second.get("idempotent") is True
    assert second["silo"]["level_kg"] == 1500.0


def test_missing_request_id_is_refused(plant: PlantRuntime) -> None:
    with pytest.raises(ValidationError):
        run(
            plant,
            "stock.refill",
            {"silo": "silo-1", "ticket": "D-x", "added_kg": 10.0},
        )


# ------------------------------------------------------ refill and switching


def test_measured_refill_resets_the_level_and_keeps_the_delta(plant: PlantRuntime) -> None:
    run(
        plant,
        "stock.dose",
        {
            "request_id": "dose-before",
            "batch_id": "M-1",
            "silo": "silo-1",
            "material": "cement",
            "estimated_kg": 330.0,
            "actual_kg": 330.0,
            "grade": "C30",
        },
    )
    outcome = run(
        plant,
        "stock.refill",
        {
            "request_id": "measured-1",
            "silo": "silo-1",
            "ticket": "D-M",
            "mode": "measured",
            "measured_kg": 30000.0,
        },
    )
    assert outcome["silo"]["level_kg"] == 30000.0


def test_switching_silos_conserves_mass_and_repoints_the_route(plant: PlantRuntime) -> None:
    run(
        plant,
        "stock.register",
        {"request_id": "reg-3", "silo": "silo-3", "material": "fly-ash", "opening_kg": 0.0, "safety_kg": 0.0},
    )
    outcome = run(
        plant,
        "stock.switch",
        {
            "request_id": "switch-1",
            "material": "fly-ash",
            "silo": "silo-3",
            "transfer_kg": 50.0,
        },
    )
    assert outcome["balance"]["routes"]["fly-ash"] == "silo-3"
    assert outcome["balance"]["silos"]["silo-2"]["level_kg"] == 950.0
    assert outcome["balance"]["silos"]["silo-3"]["level_kg"] == 50.0


# -------------------------------------------------- console/audit consistency


def test_console_state_carries_the_canonical_balance_and_forecast(plant: PlantRuntime) -> None:
    state = plant.state()
    stock = state["subsystems"]["stock"]
    assert stock["balance"]["silos"]["silo-1"]["level_kg"] == 1000.0
    assert stock["forecast"]["safe_batches"] == 2
    assert stock["forecast"]["grade"] == "C30"


def test_reconcile_agrees_with_the_journal(plant: PlantRuntime) -> None:
    run(
        plant,
        "stock.dose",
        {
            "request_id": "dose-r1",
            "batch_id": "R-1",
            "silo": "silo-1",
            "material": "cement",
            "estimated_kg": 330.0,
            "actual_kg": 330.0,
            "grade": "C30",
        },
    )
    report = run(plant, "stock.reconcile", {})
    assert report["consistent"] is True
    assert report["mismatches"] == []


def test_reconcile_detects_a_live_ledger_that_diverged(plant: PlantRuntime) -> None:
    plant.stock.ledger.require_silo("silo-1").balance_g -= 42_000
    report = run(plant, "stock.reconcile", {})
    assert report["consistent"] is False
    assert "digest" in report["mismatches"]
    assert "silo-1:level_g" in report["mismatches"]
