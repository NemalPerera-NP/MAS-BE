from pathlib import Path

from hvac_mas.models.building import load_building
from hvac_mas.simulation.environment import load_scenarios
from hvac_mas.simulation.run import Run

BUILDING = load_building(Path("config/building.yaml"))
SCENARIOS = load_scenarios(Path("config/scenarios.yaml"))


def test_overcrowding_preset_changes_actual_occupancy_without_capping_at_design():
    run = Run(BUILDING, SCENARIOS["unexpected_overcrowding"])
    run.sim.step_number = 12
    run.sim.time_s = 12 * run.sim.step_seconds
    run.prepare()
    assert run.context()[1]["D1"] == 40
    assert run.zones["D1"].occupancy_policy.is_over_design(40)


def test_demand_response_preset_changes_only_the_shared_capacity_input():
    run = Run(BUILDING, SCENARIOS["demand_response"])
    run.sim.step_number = 6
    run.sim.time_s = 6 * run.sim.step_seconds
    occupancy_before = run.context()[1]
    run.prepare()
    assert run.sim.building.power_limit_kw == 10
    assert run.context()[1] == occupancy_before
