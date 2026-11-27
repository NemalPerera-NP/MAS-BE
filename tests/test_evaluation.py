from pathlib import Path

from hvac_mas.evaluation import compare, run_metrics
from hvac_mas.models.building import load_building
from hvac_mas.simulation.environment import EnvironmentChange, load_scenarios
from hvac_mas.simulation.run import Run

BUILDING = load_building(Path("config/building.yaml"))
SCENARIOS = load_scenarios(Path("config/scenarios.yaml"))


def action(step, temperature_b1, temperature_d1, occupancy_b1=10, mode="COOL2"):
    zones = {
        "B1": {"temperature_f": temperature_b1, "occupancy": occupancy_b1,
               "effective_heat_setpoint_f": 68, "effective_cool_setpoint_f": 70},
        "D1": {"temperature_f": temperature_d1, "occupancy": 20,
               "effective_heat_setpoint_f": 68, "effective_cool_setpoint_f": 70},
    }
    units = {
        "RTU-B1": {"zone_id": "B1", "reading": {"avgTemp": temperature_b1, "occupancy": occupancy_b1,
                   "heatSetpoint": 68, "coolSetpoint": 70}},
        "RTU-D1": {"zone_id": "D1", "reading": {"avgTemp": temperature_d1, "occupancy": 20,
                   "heatSetpoint": 68, "coolSetpoint": 70}},
    }
    modes = {"RTU-B1": mode, "RTU-D1": "COOL1"}
    return {"simulationStep": step, "interval_start_s": step * 300, "interval_end_s": (step + 1) * 300,
        "power_kw": 4, "limit_kw": 10, "outcome": "committed", "controller": "mas",
        "requested": modes.copy(), "modes": modes, "decisions": {
            "RTU-B1": {"needs_conditioning": True, "preferred_mode": mode},
            "RTU-D1": {"needs_conditioning": True, "preferred_mode": "COOL1"}},
        "snapshot": {"time_s": (step + 1) * 300, "zones": zones, "units": units}}


def test_metrics_use_occupancy_effective_targets_and_current_trial_window():
    actions = [action(15, 74, 73), action(16, 75, 73)]
    metrics = run_metrics(actions, evaluation_start_step=15)
    assert metrics["missing_steps_through_last"] == 0
    assert metrics["occupant_weighted_discomfort_person_degree_hours"] > 0
    assert metrics["worst_zone_discomfort_f_degree_hours"] > 0
    assert metrics["longest_unmet_demand_minutes"] == 0


def test_comparison_reports_downstream_propagation_without_assuming_success():
    control_actions = [action(0, 72, 72), action(1, 72, 72)]
    perturbed_actions = [action(0, 72.2, 72.1), action(1, 72.3, 72.15)]
    control = {"actions": control_actions, "metrics": {"negotiations": 0, "discomfort_f_degree_hours": 0}}
    perturbed = {"actions": perturbed_actions,
                 "metrics": {"negotiations": 0, "discomfort_f_degree_hours": 1},
                 "disturbance": {"zone_id": "B1", "delta_f": 0.2}}
    result = compare(control, perturbed)
    assert result["downstream_zones_affected"] == ["D1"]
    assert result["supports_butterfly_effect"] is False


def test_sensor_disturbance_cannot_be_counted_as_a_plant_butterfly_effect():
    control_actions = [action(0, 72, 72), action(1, 72, 72)]
    perturbed_actions = [action(0, 72.4, 72.2), action(1, 72.5, 72.3)]
    control = {"actions": control_actions, "metrics": {"negotiations": 0, "discomfort_f_degree_hours": 0},
               "starting_state": {"units": {"RTU-B1": {"zone_id": "B1"}}}}
    perturbed = {"actions": perturbed_actions,
                 "metrics": {"negotiations": 0, "discomfort_f_degree_hours": 1},
                 "disturbance": {"kind": "sensor", "zone_id": "B1", "delta_f": 0.2}}
    assert compare(control, perturbed)["supports_butterfly_effect"] is False


def test_typed_occupancy_disturbances_use_the_same_run_policy_manager():
    run = Run(BUILDING, SCENARIOS["unexpected_office_arrival"])
    before = run.context()[1]["C1"]
    run.changes.append(EnvironmentChange(at_step=0,
        occupancy_actions=[{"kind": "temporary_add", "zone_id": "C1", "count": 2, "duration_minutes": 15}]))
    run.prepare()
    assert run.context()[1]["C1"] == before + 2


def test_heating_high_stage_is_included_in_collective_access_metrics():
    metrics = run_metrics([action(0, 68, 69, mode="HEAT2")])
    assert metrics["high_stage_steps_by_unit"]["RTU-B1"] == 1
