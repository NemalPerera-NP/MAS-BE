"""Hysteretic operating requests; equipment protection and power approval stay separate."""
import math

from hvac_mas.models.building import Mode, Thermostat


def request_mode(config: Thermostat, temperature_c: float, previous: Mode, *, rules=None,
                 hysteresis_c: float = 0.3, high_stage_error_c: float = 1.5,
                 occupied: bool = True) -> Mode:
    """Choose a feasible comfort direction using independent start/stop bands.

    ``occupied`` remains accepted for older callers, but occupancy setback is applied
    by the shared comfort-policy engine before this function is called.
    """
    values = (temperature_c, hysteresis_c, high_stage_error_c,
              config.heat_setpoint_c, config.cool_setpoint_c)
    if not all(math.isfinite(v) for v in values) or hysteresis_c <= 0 or high_stage_error_c <= hysteresis_c:
        raise ValueError("invalid thermostat input or hysteresis thresholds")
    if rules is None:
        cool_start = heat_start = hysteresis_c
        cool_stop = heat_stop = hysteresis_c
        cool_high = heat_high = high_stage_error_c
        cool_release = heat_release = max(hysteresis_c, high_stage_error_c - hysteresis_c)
    else:
        cool_start, cool_stop = rules.cooling_start_delta_c, rules.cooling_stop_delta_c
        cool_high, cool_release = rules.cooling_high_stage_delta_c, rules.cooling_high_stage_release_delta_c
        heat_start, heat_stop = rules.heating_start_delta_c, rules.heating_stop_delta_c
        heat_high, heat_release = rules.heating_high_stage_delta_c, rules.heating_high_stage_release_delta_c

    cool_error = temperature_c - config.cool_setpoint_c
    heat_error = config.heat_setpoint_c - temperature_c
    neutral = Mode.FAN if config.fan_policy == "on" else Mode.OFF

    if previous == Mode.COOL2:
        if cool_error <= -cool_stop + 1e-9:
            return neutral
        return Mode.COOL1 if cool_error <= cool_release else Mode.COOL2
    if previous == Mode.COOL1:
        if cool_error <= -cool_stop + 1e-9:
            return neutral
        return Mode.COOL2 if cool_error + 1e-9 >= cool_high else Mode.COOL1
    if previous == Mode.HEAT2:
        if heat_error <= -heat_stop + 1e-9:
            return neutral
        return Mode.HEAT1 if heat_error <= heat_release else Mode.HEAT2
    if previous == Mode.HEAT1:
        if heat_error <= -heat_stop + 1e-9:
            return neutral
        return Mode.HEAT2 if heat_error + 1e-9 >= heat_high else Mode.HEAT1

    # Start at stage one. Stage two can be requested after the prerequisite stage
    # has run for an interval and the error remains high.
    if cool_error + 1e-9 >= cool_start:
        return Mode.COOL1
    if heat_error + 1e-9 >= heat_start:
        return Mode.HEAT1
    return neutral
