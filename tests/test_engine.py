from pathlib import Path
import pytest
from hvac_mas.models.building import Mode, load_building
from hvac_mas.simulation.engine import Simulation
from hvac_mas.simulation.thermostat import request_mode

B = load_building(Path(__file__).resolve().parents[1] / 'config/building.yaml')
OCC = {z.zone_id: 0 for z in B.zones}
OFF = {u.unit_id: Mode.OFF for u in B.equipment}


def test_thermostat_hysteresis():
    t = B.zones[0].thermostat
    assert request_mode(t, t.cool_setpoint_c + 0.4, Mode.OFF) == Mode.COOL1
    assert request_mode(t, t.cool_setpoint_c, Mode.COOL1) == Mode.COOL1
    assert request_mode(t, t.cool_setpoint_c - 0.4, Mode.COOL1) == Mode.OFF
    assert request_mode(t, t.heat_setpoint_c - 0.4, Mode.OFF) == Mode.HEAT1


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
