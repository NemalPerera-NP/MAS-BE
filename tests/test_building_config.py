from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from hvac_mas.models.building import Building, c_to_f, load_building

PATH = Path(__file__).resolve().parents[1] / 'config' / 'building.yaml'


def test_expected_building_and_equipment():
    b = load_building(PATH)
    assert [len(b.areas), len(b.zones), len(b.equipment)] == [4, 6, 6]
    assert sum(stage.electrical_power_kw for unit in b.equipment for stage in unit.stages if stage.name == 'COOL2') == pytest.approx(19.9)
    assert {z.zone_id for z in b.zones} == {'A1', 'B1', 'B2', 'C1', 'D1', 'D2'}
    assert c_to_f(b.zones[0].thermostat.cool_setpoint_c) == pytest.approx(70)
    assert c_to_f(b.zones[0].thermostat.heat_setpoint_c) == pytest.approx(68)
    assert {z.zone_id: z.occupancy_policy.design_occupancy for z in b.zones} == {
        'A1': 14, 'B1': 20, 'B2': 20, 'C1': 4, 'D1': 25, 'D2': 25,
    }
    assert c_to_f(b.zones[0].occupancy_policy.unoccupied_cool_setpoint_c) == pytest.approx(78)
    assert c_to_f(b.zones[0].occupancy_policy.unoccupied_heat_setpoint_c) == pytest.approx(60)


def test_config_rejects_duplicate_zone_and_invalid_occupancy_policy():
    data = yaml.safe_load(PATH.read_text())
    data['zones'][1]['zone_id'] = data['zones'][0]['zone_id']
    with pytest.raises(ValidationError):
        Building.model_validate(data)

    data = yaml.safe_load(PATH.read_text())
    policy = data['zones'][0]['occupancy_policy']
    policy['unoccupied_heat_setpoint_c'] = policy['unoccupied_cool_setpoint_c']
    with pytest.raises(ValidationError):
        Building.model_validate(data)


def test_telemetry_v2_has_absolute_deviation_and_directional_errors_and_reads_v1():
    from hvac_mas.models.telemetry import Reading, derive

    values = {'avgTemp': 75, 'heatSetpoint': 68, 'coolSetpoint': 70, 'supplyTemp': 55,
              'outsideTemp': 90, 'humidity': 50, 'outsideHumid': 55, 'mode': 'COOL1',
              'fanStatus': 'ON', 'econStatus': 'CLOSED', 'power': 1.2, 'occupancy': 3}
    current = Reading.model_validate(derive(values))
    assert current.telemetrySchemaVersion == 2
    assert current.tempDiff == 5
    assert current.coolingErrorF == 5 and current.heatingErrorF == 0

    heating = Reading.model_validate(derive(values | {"mode": "HEAT1", "coolSetpoint": 75}))
    legacy = heating.model_dump()
    legacy.update(telemetrySchemaVersion=1, tempDiff=-7, coolingErrorF=None, heatingErrorF=None)
    assert Reading.model_validate(legacy).tempDiff == -7
