from pathlib import Path

import pytest

from hvac_mas.models.building import load_building
from hvac_mas.simulation.comfort_policy import ComfortPolicyEngine, OccupancyState

BUILDING = load_building(Path(__file__).resolve().parents[1] / "config/building.yaml")
ZONE = next(zone for zone in BUILDING.zones if zone.zone_id == "C1")


def test_vacancy_confirmation_preserves_occupied_targets_then_sets_back():
    engine = ComfortPolicyEngine()
    thermostat = ZONE.thermostat
    occupied = engine.evaluate(ZONE, thermostat, 3, 0, None)
    pending = engine.evaluate(ZONE, thermostat, 0, 300, None)
    unoccupied = engine.evaluate(ZONE, thermostat, 0, 900, None)
    arrived = engine.evaluate(ZONE, thermostat, 1, 1200, None)
    assert occupied.state == OccupancyState.OCCUPIED
    assert pending.state == OccupancyState.VACANCY_PENDING
    assert pending.cool_setpoint_c == thermostat.cool_setpoint_c
    assert unoccupied.state == OccupancyState.UNOCCUPIED
    assert unoccupied.cool_setpoint_c == ZONE.occupancy_policy.unoccupied_cool_setpoint_c
    assert arrived.state == OccupancyState.OCCUPIED
    assert arrived.empty_since_s is None


def test_preconditioning_interpolates_to_occupied_targets_at_arrival():
    engine = ComfortPolicyEngine()
    thermostat = ZONE.thermostat
    first = engine.evaluate(ZONE, thermostat, 0, 0, 15 * 60)
    middle = engine.evaluate(ZONE, thermostat, 0, 7.5 * 60, 15 * 60)
    assert first.state == OccupancyState.PRECONDITIONING
    assert first.preconditioning_fraction == 0
    assert middle.state == OccupancyState.PRECONDITIONING
    assert middle.preconditioning_fraction == pytest.approx(0.5)
    assert thermostat.cool_setpoint_c < middle.cool_setpoint_c < ZONE.occupancy_policy.unoccupied_cool_setpoint_c
    assert ZONE.occupancy_policy.unoccupied_heat_setpoint_c < middle.heat_setpoint_c < thermostat.heat_setpoint_c


def test_short_absence_cancels_and_restores_original_vacancy_timer():
    engine = ComfortPolicyEngine()
    thermostat = ZONE.thermostat
    engine.evaluate(ZONE, thermostat, 1, 0, None)
    pending = engine.evaluate(ZONE, thermostat, 0, 300, None)
    returned = engine.evaluate(ZONE, thermostat, 1, 600, None)
    next_pending = engine.evaluate(ZONE, thermostat, 0, 900, None)
    assert pending.state == OccupancyState.VACANCY_PENDING
    assert returned.state == OccupancyState.OCCUPIED
    assert next_pending.empty_since_s == 900
