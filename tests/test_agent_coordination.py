from hvac_mas.agents import AreaAgent, PowerAgent
from hvac_mas.models.building import Mode
from hvac_mas.models.building import load_building
from hvac_mas.simulation.environment import load_scenarios
from hvac_mas.simulation.run import Run
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_area_agent_reports_occupancy_deviation_and_electrical_context():
    agent = AreaAgent.__new__(AreaAgent)
    agent.area_id = "B"
    agent.events = {}
    agent.latest = {
        "RTU-B1": {"designOccupancy": 20, "equipmentAvailable": True,
            "feasibleModes": {"OFF": {"power": 0.0}, "COOL1": {"power": 1.6},
                              "COOL2": {"power": 3.0}},
            "data": {"occupancy": 15, "avgTemp": 72.0, "setTemp": 70.0, "mode": "COOL2"}},
        "RTU-B2": {"designOccupancy": 20, "equipmentAvailable": True,
            "feasibleModes": {"OFF": {"power": 0.0}, "COOL2": {"power": 4.3}},
            "data": {"occupancy": 18, "avgTemp": 76.0, "setTemp": 70.0, "mode": "OFF"}},
    }
    intents = {"RTU-B1": {"choices": ["COOL1"]}, "RTU-B2": {"choices": ["COOL2"]}}
    feasible = {uid: row["feasibleModes"] for uid, row in agent.latest.items()}

    assessment = agent._assessment(intents, feasible)

    assert assessment["actualOccupancy"] == 33
    assert assessment["designOccupancy"] == 40
    assert assessment["overDesignZones"] == []
    assert assessment["occupantWeightedDeviationF"] == 4.182
    assert assessment["preferredElectricalDemandKw"] == 5.9
    assert assessment["availableConcessionKw"] == 1.4
    assert assessment["unitsAvailable"] == 2


def test_power_agent_commits_multi_donor_concessions_atomically():
    agent = PowerAgent.__new__(PowerAgent)
    agent.job, agent.final, agent.version, agent.limit = "step-1", False, 0, 4.25
    agent.modes = {"REQ": Mode.FAN, "D1": Mode.COOL2, "D2": Mode.COOL2}
    agent.feasible = {
        "REQ": {Mode.FAN: {"power": 0.25}, Mode.COOL1: {"power": 1.5}, Mode.COOL2: {"power": 5.0}},
        "D1": {Mode.COOL2: {"power": 2.0}, Mode.COOL1: {"power": 1.0}},
        "D2": {Mode.COOL2: {"power": 2.0}, Mode.COOL1: {"power": 1.0}},
    }
    agent.intents = {"REQ": {"choices": [Mode.COOL1, Mode.COOL2]},
                     "D1": {"choices": [Mode.COOL1]}, "D2": {"choices": [Mode.COOL1]}}
    proposal = {
        "requester": "REQ", "request_mode": Mode.COOL1,
        "requester_current_mode": Mode.FAN, "version": 0,
        "donors": [{"unit_id": "D1", "mode": Mode.COOL1, "current_mode": Mode.COOL2},
                   {"unit_id": "D2", "mode": Mode.COOL1, "current_mode": Mode.COOL2}],
    }
    before = dict(agent.modes)
    failed_proposal = proposal | {"request_mode": Mode.COOL2}
    failed = agent.handle("commit-swap-batch", failed_proposal, "step-1", "gateway")
    assert failed[0] == "failure"
    assert agent.modes == before
    assert agent.version == 0

    response = agent.handle("commit-swap-batch", proposal, "step-1", "gateway")

    assert response[0] == "confirm"
    assert response[1]["committed"] is True
    assert response[1]["modes"] == {"REQ": Mode.COOL1, "D1": Mode.COOL1, "D2": Mode.COOL1}
    assert response[1]["kw"] == 3.5


def test_environment_rejects_power_cap_below_continuous_fan_floor():
    building = load_building(ROOT / "config/building.yaml")
    scenario = load_scenarios(ROOT / "config/scenarios.yaml")["normal_cooling"]
    run = Run(building, scenario)
    from hvac_mas.simulation.environment import EnvironmentChange
    try:
        run.validate_change(EnvironmentChange(power_limit_kw=1.0))
    except ValueError as exc:
        assert "mandatory policy/equipment load" in str(exc)
    else:
        raise AssertionError("a 1 kW cap cannot satisfy six continuous FAN loads")
    from hvac_mas.simulation.environment import EnvironmentChange
    undersized_scenario = scenario.model_copy(update={"initial": EnvironmentChange(power_limit_kw=1.0)})
    try:
        Run(building, undersized_scenario)
    except ValueError as exc:
        assert "mandatory policy/equipment load" in str(exc)
    else:
        raise AssertionError("scenario startup must enforce the same mandatory fan floor")
