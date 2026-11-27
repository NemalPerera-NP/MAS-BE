from pathlib import Path

from hvac_mas.agents import UnitAgent
from hvac_mas.control import load_rules
from hvac_mas.models.building import Mode, load_building
from hvac_mas.models.telemetry import Reading, derive
from hvac_mas.simulation.environment import load_scenarios
from hvac_mas.simulation.run import Run

ROOT = Path(__file__).resolve().parents[1]
BUILDING = load_building(ROOT / "config/building.yaml")
SCENARIOS = load_scenarios(ROOT / "config/scenarios.yaml")
RULES = load_rules(ROOT / "config/control_rules.yaml").control


def test_unit_intent_uses_occupancy_policy_and_exposes_comparable_alternatives():
    run = Run(BUILDING, SCENARIOS["normal_cooling"])
    packet = next(p for p in run.observations("intent") if p.unitId == "RTU-B1")
    data = Reading.model_validate(derive(packet.data.model_dump() | {
        "avgTemp": 76.0, "heatSetpoint": 68.0, "coolSetpoint": 72.0,
        "occupancy": 40,
    }))
    packet = packet.model_copy(update={"data": data})
    agent = UnitAgent.__new__(UnitAgent)
    agent.spec = next(unit for unit in BUILDING.equipment if unit.unit_id == "RTU-B1")
    agent.zone = next(zone for zone in BUILDING.zones if zone.zone_id == "B1")
    agent.rules = RULES
    agent.diagnostic = set()
    agent.waiting_steps = 0
    agent.last_reading = None
    agent.previous_mode = data.mode

    intent = agent.find_intent(packet, run.feasible()["RTU-B1"], 30.0, 300, 1.0)

    assert intent["overDesignOccupancy"] is True
    assert intent["occupancyRatio"] == 2.0
    assert "OCCUPANCY_ABOVE_DESIGN" in intent["reasonCodes"]
    assert intent["predictionHorizonMinutes"] == 15
    assert intent["alternatives"]
    assert all("predictedTemperatureF" in option and "powerKw" in option
               for option in intent["alternatives"])
    assert "OFF" not in [str(mode) for mode in intent["choices"]]
    without_neighbor = next(option["predictedTemperatureF"] for option in intent["alternatives"]
                            if option["mode"] == "COOL1")
    neighbor_intent = agent.find_intent(packet, run.feasible()["RTU-B1"], 30.0, 300, 1.0,
        {"B2": {"temperature_c": 30.0, "conductance_kw_per_c": 0.22}})
    with_neighbor = next(option["predictedTemperatureF"] for option in neighbor_intent["alternatives"]
                         if option["mode"] == "COOL1")
    assert with_neighbor > without_neighbor


def test_unit_waiting_history_survives_stage_change_until_need_is_served():
    run = Run(BUILDING, SCENARIOS["normal_cooling"])
    packet = next(p for p in run.observations("intent") if p.unitId == "RTU-B1")
    data = Reading.model_validate(derive(packet.data.model_dump() | {"avgTemp": 78.0}))
    packet = packet.model_copy(update={"data": data})
    agent = UnitAgent.__new__(UnitAgent)
    agent.spec = next(unit for unit in BUILDING.equipment if unit.unit_id == "RTU-B1")
    agent.zone = next(zone for zone in BUILDING.zones if zone.zone_id == "B1")
    agent.rules = RULES
    agent.diagnostic = set()
    agent.waiting_steps = 4
    agent.last_reading = packet.data.avgTemp - 1
    agent.previous_mode = packet.data.mode

    agent.find_intent(packet, run.feasible()["RTU-B1"], 30.0, 300, 1.0)
    assert agent.waiting_steps == 4


def test_unit_agent_only_bargains_when_preferred_stage_reduces_discomfort():
    agent = UnitAgent.__new__(UnitAgent)
    agent.spec = next(unit for unit in BUILDING.equipment if unit.unit_id == "RTU-B1")
    agent.current = {"choices": [Mode.COOL2], "predicted_error_c": {
        Mode.COOL2: 0.2, Mode.COOL1: 1.1, Mode.FAN: 2.4}}
    feasible = {"RTU-B1": {Mode.FAN: {"power": 0.25}, Mode.COOL1: {"power": 1.6},
                           Mode.COOL2: {"power": 4.3}}}
    allocation = {"modes": {"RTU-B1": Mode.COOL1}}
    assert agent.needs_resource_negotiation(allocation, feasible)
    agent.current["predicted_error_c"][Mode.COOL2] = 1.5
    assert not agent.needs_resource_negotiation(allocation, feasible)
