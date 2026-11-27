import pytest

from hvac_mas.control import allocate, total_power
from hvac_mas.models.building import Mode


def test_discrete_allocation_never_displaces_minimum_protected_load():
    protected = {"compressor": {Mode.COOL2: {"power": 4.3}}}
    options = {Mode.OFF: {"power": 0.0}, Mode.COOL1: {"power": 2.0}}
    feasible = protected | {"A": options, "B": options}
    intents = {unit: {"priority": value, "choices": [Mode.COOL1, Mode.OFF]}
               for unit, value in [("compressor", 0), ("A", 1), ("B", 2)]}
    modes = allocate(feasible, intents, 6.3)
    assert modes == {"compressor": Mode.COOL2, "A": Mode.OFF, "B": Mode.COOL1}
    assert total_power(modes, feasible) == pytest.approx(6.3)
    with pytest.raises(ValueError, match="protected load"):
        allocate(feasible, intents, 4.0)


def test_policy_minimum_modes_protect_continuous_fan_and_report_infeasible_cap():
    feasible = {"A": {Mode.OFF: {"power": 0.0}, Mode.FAN: {"power": 0.25},
                       Mode.COOL1: {"power": 1.2}}}
    intents = {"A": {"minimum_modes": [Mode.FAN], "choices": [Mode.COOL1, Mode.FAN], "priority": 1}}
    assert allocate(feasible, intents, 0.25) == {"A": Mode.FAN}
    with pytest.raises(ValueError, match="POWER_LIMIT_INFEASIBLE"):
        allocate(feasible, intents, 0.1)


def test_allocation_rejects_unavailable_mandatory_mode():
    feasible = {"A": {Mode.OFF: {"power": 0.0}}}
    intents = {"A": {"minimum_modes": [Mode.FAN], "choices": [Mode.OFF], "priority": 0}}
    with pytest.raises(ValueError, match="mandatory operating mode is unavailable"):
        allocate(feasible, intents, 1.0)
