from pathlib import Path

import pytest

from hvac_mas.models.building import Mode, load_building
from hvac_mas.simulation.equipment import EquipmentState, commit_mode, output

BUILDING = load_building(Path("config/building.yaml"))


def test_fan_only_does_not_bypass_compressor_restart_protection():
    spec = BUILDING.equipment[0]
    state = EquipmentState()
    assert commit_mode(spec, state, Mode.COOL1, 0)
    assert not commit_mode(spec, state, Mode.FAN, 299)
    assert commit_mode(spec, state, Mode.FAN, 300)
    assert output(spec, state, 25, 35) == pytest.approx((0, 0.25, 0, 25))
    assert not commit_mode(spec, state, Mode.COOL1, 599)
    assert commit_mode(spec, state, Mode.COOL1, 600)


def test_longer_min_off_time_survives_switch_to_a_short_timer_stage():
    base = BUILDING.equipment[0]
    first = next(stage for stage in base.stages if stage.name == Mode.COOL1)
    spec = base.model_copy(update={"stages": tuple(
        stage.model_copy(update={"min_off_seconds": 600}) if stage == first else stage for stage in base.stages
    )})
    state = EquipmentState()
    assert commit_mode(spec, state, Mode.COOL1, 0)
    assert commit_mode(spec, state, Mode.OFF, 300)
    assert not commit_mode(spec, state, Mode.HEAT1, 600)
    assert commit_mode(spec, state, Mode.HEAT1, 900)
