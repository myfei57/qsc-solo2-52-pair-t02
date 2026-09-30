"""Binder silo inventory: drawdown, projection, safety line and reconciliation.

The rules fixed here are:

* the safety line is judged per silo (a kg floor), while the reaction is a
  per (silo, grade) policy matrix that defaults to alarm only;
* drawdowns only book a measured, read back mass.  A missing readback is a
  partial failure: no estimated deduction, the silo becomes untrusted and
  feeds are blocked until a manual resolution;
* refills, declarations, draws and resolutions are idempotent;
* the ledger is rebuilt from the stream on restart and reconciles to itself.
"""

from __future__ import annotations

import pytest

from mixplant.cement.inventory import InventoryLedger
from mixplant.cement.projection import project_plant, project_silo
from mixplant.cement.safety import ALARM, BLOCK, SafetyTable
from mixplant.config import StationConfig
from mixplant.errors import DuplicateError, InterlockBlockedError, ValidationError
from mixplant.runtime import PlantRuntime
from tests.support import publish_and_select, run

RECIPE = {
    "name": "c30",
    "grade": "C30",
    "factors": {"sand": 760.0, "gravel": 1010.0, "cement": 330.0, "fly-ash": 100.0, "water": 175.0},
    "moisture_ref_percent": 4.5,
}


# --------------------------------------------------------------------- ledger

def test_drawdown_books_only_the_measured_mass() -> None:
    ledger = InventoryLedger({"silo-1": ("cement", 60000.0)}, movements=False)
    movement = ledger.declare("silo-1", "cement", 60000.0, tick=1, client_ref="open-1")
    drawn = ledger.draw("silo-1", 1980.0, tick=2, batch_id="B-1")
    assert drawn.after_kg == 58020.0
    assert ledger.balance("silo-1").level_kg == 58020.0
    assert movement.kind == "cement.open"


def test_refill_and_draw_are_idempotent() -> None:
    ledger = InventoryLedger({"silo-1": ("cement", 60000.0)}, movements=False)
    ledger.declare("silo-1", "cement", 60000.0, tick=1, client_ref="open-1")
    first = ledger.refill("silo-1", 5000.0, tick=2, client_ref="D-1")
    again = ledger.refill("silo-1", 5000.0, tick=3, client_ref="D-1")
    assert first.after_kg == again.after_kg == 65000.0
    ledger.draw("silo-1", 100.0, tick=4, batch_id="B-1")
    retried = ledger.draw("silo-1", 999.0, tick=5, batch_id="B-1")
    assert retried.after_kg == 64900.0


def test_refill_reference_cannot_cross_silos() -> None:
    ledger = InventoryLedger(
        {"silo-1": ("cement", 60000.0), "silo-2": ("fly-ash", 25000.0)}, movements=False
    )
    ledger.refill("silo-1", 100.0, tick=1, client_ref="D-1")
    with pytest.raises(DuplicateError):
        ledger.refill("silo-2", 100.0, tick=2, client_ref="D-1")


def test_missing_readback_does_not_deduct_and_blocks_draws() -> None:
    ledger = InventoryLedger({"silo-1": ("cement", 8000.0)}, movements=False)
    ledger.declare("silo-1", "cement", 8000.0, tick=1, client_ref="open-1")
    parked = ledger.mark_pending("silo-1", 330.0, tick=2, batch_id="B-9")
    balance = ledger.balance("silo-1")
    assert balance.level_kg == 8000.0
    assert balance.trusted is False
    assert balance.pending_kg == 330.0
    assert parked.kind == "cement.untrusted"
    with pytest.raises(ValidationError):
        ledger.draw("silo-1", 10.0, tick=3, batch_id="B-10")
    resolved = ledger.resolve("silo-1", tick=4, batch_id="B-9", mode="apply")
    assert resolved.after_kg == 7670.0
    assert ledger.balance("silo-1").trusted is True


def test_resolution_can_write_the_consumption_off() -> None:
    ledger = InventoryLedger({"silo-1": ("cement", 8000.0)}, movements=False)
    ledger.declare("silo-1", "cement", 8000.0, tick=1, client_ref="open-1")
    ledger.mark_pending("silo-1", 330.0, tick=2, batch_id="B-9")
    movement = ledger.resolve("silo-1", tick=3, batch_id="B-9", mode="reject")
    assert movement.delta_kg == 0.0
    assert ledger.balance("silo-1").level_kg == 8000.0
    assert ledger.balance("silo-1").trusted is True


def test_reconciliation_matches_booked_balances() -> None:
    ledger = InventoryLedger({"silo-1": ("cement", 60000.0), "silo-2": ("fly-ash", 25000.0)})
    ledger.refill("silo-1", 1000.0, tick=1, client_ref="D-1")
    ledger.draw("silo-1", 500.0, tick=2, batch_id="B-1")
    report = ledger.reconciliation()
    assert report["consistent"] is True
    by_silo = {row["silo"]: row for row in report["silos"]}
    assert by_silo["silo-1"]["booked_kg"] == 60500.0
    assert by_silo["silo-1"]["movement_total_kg"] == 60500.0


# ----------------------------------------------------------------- projection

def test_projection_counts_whole_batches_from_the_limiting_silo() -> None:
    projection = project_plant(
        {"silo-1": _balance("cement", 59670.0, True), "silo-2": _balance("fly-ash", 25000.0, True)},
        {"cement": 330.0, "fly-ash": 100.0},
    )
    assert projection["remaining_batches"] == 180  # 59670 // 330
    assert projection["limiting_silo"] == "silo-1"


def test_projection_is_unknown_while_a_silo_is_untrusted() -> None:
    projection = project_plant(
        {"silo-1": _balance("cement", 8000.0, False), "silo-2": _balance("fly-ash", 25000.0, True)},
        {"cement": 330.0, "fly-ash": 100.0},
    )
    assert projection["remaining_batches"] is None
    assert projection["trusted"] is False
    silo1 = next(row for row in projection["silos"] if row["silo"] == "silo-1")
    assert silo1["remaining_batches"] is None
    assert silo1["reason"] == "untrusted_level"


def test_projection_ignores_materials_the_recipe_does_not_use() -> None:
    projection = project_silo(
        silo="silo-2", material="fly-ash", level_kg=25000.0, demand_kg=0.0, trusted=True
    )
    assert projection.remaining_batches is None
    assert projection.reason == "material_not_in_recipe"


# -------------------------------------------------------------------- safety

def test_safety_line_is_per_silo_but_the_reaction_is_per_grade() -> None:
    table = SafetyTable()
    table.set("silo-1", 5000.0, default_action=ALARM, grade_actions={"C40": BLOCK})
    c30 = table.evaluate("silo-1", 4000.0, trusted=True, grade="C30")
    c40 = table.evaluate("silo-1", 4000.0, trusted=True, grade="C40")
    assert c30.below is True and c30.action == ALARM
    assert c40.below is True and c40.action == BLOCK
    # Same floor for both grades: the line is not grade dependent.
    assert c30.safety_kg == c40.safety_kg == 5000.0


def test_untrusted_level_always_blocks_regardless_of_policy() -> None:
    table = SafetyTable()
    table.set("silo-1", 5000.0, default_action=ALARM)
    verdict = table.evaluate("silo-1", 60000.0, trusted=False, grade="C30")
    assert verdict.blocks is True
    assert verdict.reason == "untrusted_level"


def test_above_the_line_never_alarms_or_blocks() -> None:
    table = SafetyTable()
    table.set("silo-1", 5000.0, default_action=BLOCK, grade_actions={"C40": BLOCK})
    verdict = table.evaluate("silo-1", 9000.0, trusted=True, grade="C40")
    assert verdict.below is False
    assert verdict.alarms is False and verdict.blocks is False


# ------------------------------------------------------------- end to end API

def test_each_charge_draws_the_silo_down_against_the_recipe(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    run(runtime, "cement.declare_level", {"silo": "silo-1", "material": "cement", "level_kg": 10000, "client_ref": "o"})
    projection = run(runtime, "cement.projection")
    assert projection["remaining_batches"] == 30  # 10000 // 330
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 1})
    run(runtime, "mix.start")
    run(runtime, "water.open_valve", {"target_l": 200})
    run(runtime, "water.dispense", {"litres": 200, "rate_lps": 45})
    run(runtime, "mix.feed", {"component": "water"})
    run(runtime, "cement.open_gate", {"silo": "silo-1", "target_kg": 330})
    closed = run(runtime, "cement.close_gate", {"silo": "silo-1", "mass_kg": 330, "rate_kgs": 30})
    assert closed["level_kg"] == 9670.0
    assert run(runtime, "cement.projection")["remaining_batches"] == 29


def test_below_safety_blocks_the_gate_for_the_blocking_grade(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    run(runtime, "cement.declare_level", {"silo": "silo-1", "material": "cement", "level_kg": 4000.0, "client_ref": "o"})
    run(runtime, "cement.set_safety", {"silo": "silo-1", "safety_kg": 5000, "default_action": "block"})
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 1})
    run(runtime, "mix.start")
    run(runtime, "water.open_valve", {"target_l": 200})
    run(runtime, "water.dispense", {"litres": 200, "rate_lps": 45})
    run(runtime, "mix.feed", {"component": "water"})
    with pytest.raises(InterlockBlockedError):
        run(runtime, "cement.open_gate", {"silo": "silo-1", "target_kg": 330})


def test_partial_failure_flow_through_the_console_commands(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    run(runtime, "cement.declare_level", {"silo": "silo-1", "material": "cement", "level_kg": 8000, "client_ref": "o"})
    run(runtime, "batch.declare", {"batch_id": "B-1", "quantity": 1})
    run(runtime, "mix.start")
    run(runtime, "water.open_valve", {"target_l": 200})
    run(runtime, "water.dispense", {"litres": 200, "rate_lps": 45})
    run(runtime, "mix.feed", {"component": "water"})
    run(runtime, "cement.open_gate", {"silo": "silo-1", "target_kg": 330})
    partial = run(runtime, "cement.close_gate", {"silo": "silo-1", "readback": False})
    assert partial["partial_failure"] is True
    assert partial["silo"]["level_kg"] == 8000.0
    assert partial["silo"]["trusted"] is False
    alarms_after_first = runtime.alarms.count()
    # A retried completion with the readback still missing books nothing twice.
    retried = run(runtime, "cement.close_gate", {"silo": "silo-1", "readback": False})
    assert retried["idempotent"] is True
    assert retried["silo"]["level_kg"] == 8000.0
    assert runtime.alarms.count() == alarms_after_first
    # The alarm page surfaces the missing readback.
    assert any(alarm["name"] == "cement.readback_missing" for alarm in runtime.alarms.recent())
    # Feeding again is refused because the level cannot be trusted.
    with pytest.raises(InterlockBlockedError):
        run(runtime, "cement.open_gate", {"silo": "silo-1", "target_kg": 330})
    resolved = run(runtime, "cement.resolve_draw", {"silo": "silo-1", "batch_id": "B-1", "mode": "apply"})
    assert resolved["movement"]["after_kg"] == 7670.0
    assert run(runtime, "cement.reconcile")["consistent"] is True


def test_audit_rollup_quotes_the_same_reconciliation(runtime: PlantRuntime) -> None:
    publish_and_select(runtime)
    run(runtime, "cement.declare_level", {"silo": "silo-1", "material": "cement", "level_kg": 12000, "client_ref": "o"})
    rollup = run(runtime, "audit.rollup")
    assert rollup["binder_reconciliation"]["consistent"] is True
    assert rollup["binder_reconciliation"] == run(runtime, "cement.reconcile")


def test_inventory_survives_restart_and_keeps_reconciling(tmp_path) -> None:
    config = StationConfig(data_dir=tmp_path / "state")
    first = PlantRuntime(config)
    first.bootstrap()
    publish_and_select(first)
    run(first, "cement.declare_level", {"silo": "silo-1", "material": "cement", "level_kg": 9000, "client_ref": "o"})
    run(first, "cement.set_safety", {"silo": "silo-1", "safety_kg": 5000, "default_action": "alarm", "grade_actions": {"C40": "block"}})
    run(first, "batch.declare", {"batch_id": "B-1", "quantity": 1})
    run(first, "mix.start")
    run(first, "water.open_valve", {"target_l": 200})
    run(first, "water.dispense", {"litres": 200, "rate_lps": 45})
    run(first, "mix.feed", {"component": "water"})
    run(first, "cement.open_gate", {"silo": "silo-1", "target_kg": 330})
    run(first, "cement.close_gate", {"silo": "silo-1", "mass_kg": 330, "rate_kgs": 30})
    first.close()

    second = PlantRuntime(config)
    second.bootstrap()
    assert second.cement.ledger.balance("silo-1").level_kg == 8670.0
    assert second.cement.reconciliation()["consistent"] is True
    assert second.cement.safety.policy("silo-1").action_for("C40") == BLOCK
    assert second.cement.safety.policy("silo-1").action_for("C30") == ALARM
    second.close()


def _balance(material: str, level_kg: float, trusted: bool):
    from mixplant.cement.inventory import SiloBalance

    return SiloBalance(silo="x", material=material, level_kg=level_kg, tick=0, trusted=trusted)
