from pathlib import Path
import pytest
from hvac_mas.models.building import Mode, load_building
from hvac_mas.simulation.equipment import EquipmentState, commit_mode, output

B = load_building(Path(__file__).resolve().parents[1] / 'config/building.yaml')
SPEC = B.equipment[0]


def test_protection_and_stage_dependencies():
    s = EquipmentState()
    assert not commit_mode(SPEC, s, Mode.COOL2, 0)
    assert commit_mode(SPEC, s, Mode.COOL1, 0)
    assert not commit_mode(SPEC, s, Mode.COOL2, 299)
    assert commit_mode(SPEC, s, Mode.COOL2, 300)
    assert not commit_mode(SPEC, s, Mode.HEAT1, 600)
    assert commit_mode(SPEC, s, Mode.OFF, 600)
    assert not commit_mode(SPEC, s, Mode.HEAT1, 899)
    assert commit_mode(SPEC, s, Mode.HEAT1, 900)
    with pytest.raises(ValueError): commit_mode(SPEC, s, Mode.HEAT2, 1200)


def test_heat_cool_fault_and_power():
    s = EquipmentState()
    assert commit_mode(SPEC, s, Mode.COOL1, 0)
    heat, power, fuel, supply = output(SPEC, s, 24, 35)
    assert heat < 0 and supply < 24 and power == 1.2 and fuel == 0
    s.cooling_health = 0
    assert output(SPEC, s, 24, 35)[:2] == (0, 1.2)
    s.offline = True
    assert output(SPEC, s, 24, 35)[:3] == (0, 0, 0)
    assert not commit_mode(SPEC, s, Mode.COOL1, 1)
    assert s.actual_mode == Mode.OFF
    s.offline = False
    assert commit_mode(SPEC, s, Mode.HEAT1, 301)
    heat, power, fuel, supply = output(SPEC, s, 20, 5)
    assert heat > 0 and supply > 20 and fuel > 0 and power < heat
