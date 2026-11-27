from copy import deepcopy
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hvac_mas.models.building import Building, c_to_f, f_to_c, load_building

PATH = Path(__file__).resolve().parents[1] / 'config' / 'building.yaml'


def test_expected_building_and_equipment():
    b = load_building(PATH)
    assert [len(b.areas), len(b.zones), len(b.equipment)] == [4, 6, 6]
    assert sum(stage.electrical_power_kw for unit in b.equipment for stage in unit.stages if stage.name == 'COOL2') == pytest.approx(19.9)
    assert {z.zone_id for z in b.zones} == {'A1', 'B1', 'B2', 'C1', 'D1', 'D2'}
    assert c_to_f(b.zones[0].thermostat.cool_setpoint_c) == pytest.approx(70)
    assert c_to_f(b.zones[0].thermostat.heat_setpoint_c) == pytest.approx(68)


@pytest.mark.parametrize('value', [-40, 0, 68, 70, 120])
def test_temperature_roundtrip(value):
    assert c_to_f(f_to_c(value)) == pytest.approx(value)


@pytest.mark.parametrize('change', ['duplicate_zone', 'bad_unit', 'bad_area', 'duplicate_thermostat', 'negative_power', 'missing_id', 'invalid_stage', 'duplicate_coupling'])
def test_invalid_config_rejected(change):
    data = deepcopy(yaml.safe_load(PATH.read_text()))
    if change == 'duplicate_zone': data['zones'][1]['zone_id'] = data['zones'][0]['zone_id']
    elif change == 'bad_unit': data['zones'][0]['unit_ids'] = ['RTU-B1']
    elif change == 'bad_area': data['zones'][0]['area_id'] = 'B'
    elif change == 'duplicate_thermostat': data['zones'][1]['thermostat']['thermostat_id'] = data['zones'][0]['thermostat']['thermostat_id']
    elif change == 'negative_power': data['equipment'][0]['stages'][0]['electrical_power_kw'] = -1
    elif change == 'missing_id': del data['zones'][0]['zone_id']
    elif change == 'invalid_stage': data['equipment'][0]['stages'][0]['name'] = 'FAKE'
    elif change == 'duplicate_coupling': data['couplings'].append(data['couplings'][0])
    with pytest.raises(ValidationError): Building.model_validate(data)
