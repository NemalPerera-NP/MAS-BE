from pathlib import Path

import pytest
from pydantic import ValidationError

from hvac_mas.control import load_rules
from hvac_mas.models.building import load_building
from hvac_mas.simulation.environment import EnvironmentChange, OccupancyAction, load_scenarios
from hvac_mas.simulation.occupancy import OccupancyManager
from hvac_mas.simulation.run import Run

ROOT = Path(__file__).resolve().parents[1]
BUILDING = load_building(ROOT / "config/building.yaml")
SCENARIOS = load_scenarios(ROOT / "config/scenarios.yaml")
RULES = load_rules(ROOT / "config/control_rules.yaml").control


def test_zone_schedules_do_not_duplicate_area_counts():
    run = Run(BUILDING, SCENARIOS["normal_cooling"])
    counts = run.context()[1]
    assert counts["B1"] == 12 and counts["B2"] == 0
    assert run.occupancy_manager.scheduled("B2", 15 * 60) == 8
    assert run.snapshot()["areas"]["B"]["occupancy"] == 12


def test_atomic_transfer_conserves_occupants_and_can_exceed_design():
    manager = OccupancyManager({}, {"B1", "D1"})
    manager.apply_change({"B1": 20, "D1": 25}, (), 0)
    manager.apply((OccupancyAction(kind="transfer", source_zone_id="B1",
                                   destination_zone_id="D1", count=10),), 0)
    assert manager.counts(0) == {"B1": 10, "D1": 35}


def test_failed_later_action_rolls_back_the_entire_batch():
    manager = OccupancyManager({}, {"B1", "D1"})
    manager.apply_change({"B1": 4, "D1": 0}, (), 0)
    transfer = OccupancyAction(kind="transfer", source_zone_id="B1",
                               destination_zone_id="D1", count=3)
    departure = OccupancyAction(kind="departure", zone_id="B1", count=9)
    with pytest.raises(ValueError):
        manager.apply((transfer, departure), 0)
    assert manager.counts(0) == {"B1": 4, "D1": 0}


def test_temporary_groups_expire_independently_without_losing_later_arrivals():
    manager = OccupancyManager({}, {"B1"})
    manager.apply((OccupancyAction(kind="temporary_add", zone_id="B1", count=2,
                                   duration_minutes=15),), 0)
    manager.apply((OccupancyAction(kind="temporary_add", zone_id="B1", count=3,
                                   duration_minutes=20),), 5 * 60)
    manager.apply((OccupancyAction(kind="arrival", zone_id="B1", count=3),), 10 * 60)
    assert manager.counts(10 * 60)["B1"] == 8
    assert manager.counts(15 * 60)["B1"] == 6
    assert manager.counts(25 * 60)["B1"] == 3
    assert len(manager.pulses) == 2  # A future-time forecast must not prune live state.
    manager.prune_expired(25 * 60)
    assert len(manager.pulses) == 0


def test_reading_forecasts_scheduled_occupancy_without_mutating_live_counts():
    run = Run(BUILDING, SCENARIOS["normal_cooling"])
    run.sim.time_s = 20 * 60
    packet = next(p for p in run.observations("forecast") if p.unitId == "RTU-A1")
    assert packet.data.occupancy == 0
    assert packet.data.expectedOccupancy == 14
    assert packet.data.occupancyForecastMinutes == 15
    assert run.occupancy_manager.scheduled("A1", run.sim.time_s) == 0


def test_restore_schedule_and_future_action():
    run = Run(BUILDING, SCENARIOS["normal_cooling"])
    run.apply_change(EnvironmentChange(occupancy={"B1": 30}))
    run.apply_change(EnvironmentChange(occupancy_actions=[{"kind": "restore_schedule", "zone_id": "B1"}]))
    assert run.context()[1]["B1"] == 12
    run.changes.append(EnvironmentChange(at_step=0,
        occupancy_actions=[{"kind": "arrival", "zone_id": "B1", "count": 1}]))
    run.prepare()
    assert run.context()[1]["B1"] == 13


def test_restart_retains_runtime_power_limit():
    run = Run(BUILDING, SCENARIOS["normal_cooling"])
    run.apply_change(EnvironmentChange(power_limit_kw=12.0))
    restored = Run.from_persisted_state(BUILDING, run.persisted_state())
    assert restored.sim.building.power_limit_kw == 12.0


def test_queued_intervention_moves_to_applied_and_survives_restart():
    run = Run(BUILDING, SCENARIOS["normal_cooling"])
    run.changes.append(EnvironmentChange(intervention_id="arrival-1", at_step=0,
        occupancy_actions=[{"kind": "arrival", "zone_id": "B1", "count": 2}]))
    run.interventions.append({"interventionId": "arrival-1", "runId": run.id, "kind": "environment",
        "target": "B1", "requestedStep": 0, "status": "QUEUED", "createdStep": 0, "appliedStep": None})
    run.prepare()
    assert run.interventions[0]["status"] == "APPLIED"
    assert run.interventions[0]["appliedStep"] == 0
    restored = Run.from_persisted_state(BUILDING, run.persisted_state())
    assert restored.interventions == run.interventions


def test_unknown_zones_and_invalid_action_counts_are_rejected():
    run = Run(BUILDING, SCENARIOS["normal_cooling"])
    with pytest.raises(ValueError, match="unknown zone"):
        run.validate_change(EnvironmentChange(occupancy={"UNKNOWN": 1}))
    with pytest.raises(ValidationError):
        OccupancyAction(kind="transfer", source_zone_id="B1", destination_zone_id="B1", count=1)
    with pytest.raises(ValidationError):
        EnvironmentChange(occupancy={"B1": 1.5})


def test_restore_rejects_corrupted_pulse_without_mutating_existing_state():
    manager = OccupancyManager({}, {"B1"})
    manager.apply_change({"B1": 4}, (), 0)
    with pytest.raises(ValueError, match="unknown zone"):
        manager.restore_state({"overrides": {}, "pulses": [{"zone_id": "UNKNOWN", "count": 1,
                                                              "expires_at_s": 60}]})
    assert manager.counts(0)["B1"] == 4
