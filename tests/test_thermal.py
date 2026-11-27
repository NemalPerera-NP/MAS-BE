from pathlib import Path
import pytest
from hvac_mas.models.building import load_building
from hvac_mas.simulation.thermal import advance_temperatures

B = load_building(Path(__file__).resolve().parents[1] / 'config/building.yaml')
IDS = [z.zone_id for z in B.zones]


def advance(temperature=22, outdoor=30, occupied=0, heat=0, seconds=300):
    return advance_temperatures(B, dict.fromkeys(IDS, temperature), outdoor_c=outdoor,
                                occupancy=dict.fromkeys(IDS, occupied), hvac_kw=dict.fromkeys(IDS, heat), seconds=seconds)


def test_outdoor_warming_and_cooling_heating_effects():
    natural = advance()
    assert natural['A1'] > 22
    assert advance(heat=-10)['A1'] < natural['A1']
    assert advance(heat=10)['A1'] > natural['A1']
    assert advance(occupied=10)['A1'] > natural['A1']


def test_coupling_is_equal_and_opposite_and_local():
    data = B.model_dump(mode='json')
    for z in data['zones']:
        z['outdoor_conductance_kw_per_c'] = 0
        z['base_load_kw'] = 0
    b = type(B).model_validate(data)
    temps = dict.fromkeys(IDS, 20.0)
    temps['B1'] = 30.0
    result = advance_temperatures(b, temps, outdoor_c=20, occupancy=dict.fromkeys(IDS, 0), hvac_kw=dict.fromkeys(IDS, 0))
    assert result['B1'] < 30 < 31 and result['B2'] > 20
    assert result['A1'] == 20
    zones = {z.zone_id: z for z in b.zones}
    assert sum((result[k]-temps[k])*zones[k].capacitance_kwh_per_c for k in IDS) == pytest.approx(0, abs=1e-12)


def test_long_step_does_not_overshoot_outdoor():
    data = B.model_dump(mode='json')
    for z in data['zones']: z['base_load_kw'] = 0
    b = type(B).model_validate(data)
    result = advance_temperatures(b, dict.fromkeys(IDS, 20), outdoor_c=30, occupancy=dict.fromkeys(IDS, 0), hvac_kw=dict.fromkeys(IDS, 0), seconds=86400*20)
    assert all(20 <= t <= 30 for t in result.values())


def test_over_design_occupancy_uses_the_full_physical_heat_gain():
    temperatures = dict.fromkeys(IDS, 24.0)
    hvac = dict.fromkeys(IDS, 0.0)
    outside = 24.0
    base = dict.fromkeys(IDS, 0)
    at_design = advance_temperatures(B, temperatures, outdoor_c=outside,
                                     occupancy=base | {"D1": 25}, hvac_kw=hvac)
    over_design = advance_temperatures(B, temperatures, outdoor_c=outside,
                                       occupancy=base | {"D1": 40}, hvac_kw=hvac)
    assert over_design["D1"] > at_design["D1"]
    assert (over_design["D1"] - at_design["D1"]) > 0


def test_occupancy_above_technical_limit_is_rejected():
    with pytest.raises(ValueError, match="occupancy must be integers"):
        advance_temperatures(B, dict.fromkeys(IDS, 24.0), outdoor_c=24.0,
                             occupancy={**dict.fromkeys(IDS, 0), "D1": 1001},
                             hvac_kw=dict.fromkeys(IDS, 0.0))
