from pathlib import Path
import pytest
from hvac_mas.models.building import Mode, load_building
from hvac_mas.control import load_rules
from hvac_mas.simulation.engine import Simulation
from hvac_mas.simulation.thermostat import request_mode

B = load_building(Path(__file__).resolve().parents[1] / 'config/building.yaml')
OCC = {z.zone_id: 0 for z in B.zones}
OFF = {u.unit_id: Mode.OFF for u in B.equipment}


def test_repeatability_and_energy():
    def run():
        sim = Simulation(B)
        return [sim.step({u.unit_id: Mode.COOL1 for u in B.equipment}, outdoor_c=35, occupancy=OCC) for _ in range(3)]
    a, b = run(), run()
    assert a == b
    assert a[0]['energy_kwh'] == pytest.approx(a[0]['power_kw'] / 12)


def test_invalid_approval_is_atomic():
    sim = Simulation(B)
    invalid = dict(OFF)
    invalid['RTU-A1'] = Mode.COOL2
    with pytest.raises(ValueError): sim.step(invalid, outdoor_c=35, occupancy=OCC)
    assert sim.step_number == 0
    assert all(s.actual_mode == Mode.OFF for s in sim.units.values())


def test_power_limit_rejection_is_atomic():
    data = B.model_dump(mode='json')
    data['power_limit_kw'] = 1
    sim = Simulation(type(B).model_validate(data))
    with pytest.raises(ValueError): sim.step({u.unit_id: Mode.COOL1 for u in B.equipment}, outdoor_c=35, occupancy=OCC)
    assert sim.step_number == 0 and sim.energy_kwh == 0


def test_configured_start_stop_and_stage_hysteresis():
    t = B.zones[0].thermostat.model_copy(update={'fan_policy': 'on'})
    rules = load_rules(Path(__file__).resolve().parents[1] / 'config/control_rules.yaml').control
    assert request_mode(t, t.cool_setpoint_c + rules.cooling_start_delta_c, Mode.OFF, rules=rules) == Mode.COOL1
    assert request_mode(t, t.cool_setpoint_c + rules.cooling_high_stage_delta_c, Mode.COOL1, rules=rules) == Mode.COOL2
    assert request_mode(t, t.cool_setpoint_c + rules.cooling_high_stage_release_delta_c,
                        Mode.COOL2, rules=rules) == Mode.COOL1
    assert request_mode(t, t.cool_setpoint_c - rules.cooling_stop_delta_c, Mode.COOL1, rules=rules) == Mode.FAN
    assert request_mode(t, t.heat_setpoint_c - rules.heating_start_delta_c, Mode.OFF, rules=rules) == Mode.HEAT1


def test_fan_is_the_configured_neutral_mode():
    t = B.zones[0].thermostat.model_copy(update={'fan_policy': 'on'})
    assert request_mode(t, (t.heat_setpoint_c + t.cool_setpoint_c) / 2, Mode.OFF) == Mode.FAN


def test_fan_circulates_without_mechanical_temperature_change():
    fan_sim, off_sim = Simulation(B), Simulation(B)
    fan = {unit.unit_id: Mode.FAN for unit in B.equipment}
    off = {unit.unit_id: Mode.OFF for unit in B.equipment}
    fan_result = fan_sim.step(fan, outdoor_c=23, occupancy=OCC)
    off_result = off_sim.step(off, outdoor_c=23, occupancy=OCC)
    assert fan_result['temperature_c'] == off_result['temperature_c']
    assert fan_result['power_kw'] == pytest.approx(sum(stage.electrical_power_kw
        for unit in B.equipment for stage in unit.stages if stage.name == Mode.FAN))
    assert all(value['heat_kw'] == 0 for value in fan_result['units'].values())
