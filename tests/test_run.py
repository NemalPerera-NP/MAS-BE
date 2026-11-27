from pathlib import Path

from hvac_mas.models.building import Mode, load_building
from hvac_mas.models.telemetry import Override, Source
from hvac_mas.simulation.environment import Fault, FaultKind, load_scenarios
from hvac_mas.simulation.run import Run

BUILDING = load_building(Path("config/building.yaml"))
SCENARIOS = load_scenarios(Path("config/scenarios.yaml"))


def test_observations_are_reproducible_and_overrides_do_not_change_truth():
    left, right = (Run(BUILDING, SCENARIOS["normal_cooling"], id="fixed") for _ in range(2))
    assert left.observations("a") == right.observations("a")
    left.overrides["RTU-B1"] = Override(unit_id="RTU-B1", fields={"avgTemp": 90}, repeats=2)
    packet = next(p for p in left.observations("b") if p.unitId == "RTU-B1")
    assert packet.source == Source.MANUAL
    assert packet.data.supplyDelta == round(90 - packet.data.supplyTemp, 4)
    assert left.truth() == right.truth()
    left.commit(dict.fromkeys(left.specs, Mode.OFF), {})
    assert left.overrides["RTU-B1"].repeats == 1


def test_fault_changes_only_its_observable_effect_and_clear_restores_it():
    run = Run(BUILDING, SCENARIOS["normal_cooling"])
    run.commit(dict.fromkeys(run.specs, Mode.COOL1), {})
    normal = run.reading("RTU-B1")
    run.apply_fault(Fault(unit_id="RTU-B1", kind=FaultKind.COOL1_FAILURE))
    failed = run.reading("RTU-B1")
    assert failed.supplyDelta == 0 and failed.power == normal.power
    run.apply_fault(Fault(unit_id="RTU-B1", kind=FaultKind.CLEAR))
    assert run.reading("RTU-B1") == normal
    run.apply_fault(Fault(unit_id="RTU-B1", kind=FaultKind.OFFLINE))
    assert set(run.feasible()["RTU-B1"]) == {Mode.OFF}
    assert "RTU-B1" not in {p.unitId for p in run.observations("x")}
