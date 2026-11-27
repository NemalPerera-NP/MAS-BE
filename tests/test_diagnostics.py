from datetime import timedelta
from pathlib import Path

from hvac_mas.control import DiagnosticRules
from hvac_mas.diagnostics import Diagnostics
from hvac_mas.models.building import Mode, load_building
from hvac_mas.models.telemetry import Override
from hvac_mas.simulation.environment import load_scenarios
from hvac_mas.simulation.run import Run

BUILDING = load_building(Path("config/building.yaml"))
SCENARIOS = load_scenarios(Path("config/scenarios.yaml"))


def observations(temperatures):
    run = Run(BUILDING, SCENARIOS["normal_cooling"])
    base = next(p for p in run.observations("base") if p.unitId == "RTU-B1")
    for i, temperature in enumerate(temperatures):
        reading = Override(unit_id=base.unitId, fields={"avgTemp": temperature, "supplyTemp": temperature - 19,
                                                        "mode": Mode.COOL1, "power": 2, "fanStatus": "ON"}).read(base.data)
        yield base.model_copy(update={"packetId": f"p-{i}", "timestamp": base.timestamp + timedelta(minutes=5 * i),
                                      "simulationStep": i, "data": reading})


def event_statuses(temperatures, duplicate=False):
    detector = Diagnostics(BUILDING, DiagnosticRules())
    result = []
    for packet in observations(temperatures):
        result.extend((event["kind"], event["status"]) for event in detector.observe(packet))
        if duplicate:
            assert detector.observe(packet) == []
    return result


def test_one_reading_does_not_confirm_and_duplicate_step_does_not_count():
    states = event_statuses([77], duplicate=True)
    assert ("G5", "CANDIDATE") in states and ("G5", "CONFIRMED") not in states
    assert ("G5", "CONFIRMED") in event_statuses([77] * 5, duplicate=True)


def test_recovery_window_allows_abnormal_but_improving_room_to_cancel():
    states = event_statuses([77.8, 77.1, 76.4, 75.7, 75.1, 74.7, 74.2])
    assert ("G5", "MONITORING_RECOVERY") in states
    assert ("G5", "CANCELLED") in states
    assert ("G5", "CONFIRMED") not in states


def test_recovery_that_stalls_is_confirmed_then_resolved():
    states = event_statuses([77.8, 77.1, 76.4, 75.7, 75.2] + [75.2] * 5 + [74, 73])
    assert ("G5", "MONITORING_RECOVERY") in states
    assert ("G5", "CONFIRMED") in states
    assert ("G5", "RECOVERING") in states and ("G5", "RESOLVED") in states
