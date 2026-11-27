"""Simple hysteretic thermostat requests; approval is a separate concern."""
import math
from hvac_mas.models.building import Mode, Thermostat


def request_mode(config: Thermostat, temperature_c: float, previous: Mode,
                 *, hysteresis_c: float = 0.3, high_stage_error_c: float = 1.5, occupied: bool = True) -> Mode:
    if not all(math.isfinite(v) for v in (temperature_c, hysteresis_c, high_stage_error_c)) or hysteresis_c <= 0 or high_stage_error_c <= hysteresis_c:
        raise ValueError('invalid hysteresis thresholds')
    setback = 1.0 if not occupied else 0.0
    cool_error = temperature_c - config.cool_setpoint_c - setback
    heat_error = config.heat_setpoint_c - temperature_c - setback
    if previous in (Mode.COOL1, Mode.COOL2) and cool_error > -hysteresis_c:
        if previous == Mode.COOL2 and cool_error > high_stage_error_c - hysteresis_c:
            return Mode.COOL2
        return Mode.COOL2 if cool_error >= high_stage_error_c + hysteresis_c else Mode.COOL1
    if previous in (Mode.HEAT1, Mode.HEAT2) and heat_error > -hysteresis_c:
        return Mode.HEAT1
    if cool_error >= hysteresis_c:
        return Mode.COOL1  # Start at the prerequisite stage.
    if heat_error >= hysteresis_c:
        return Mode.HEAT1
    return Mode.FAN if config.fan_policy == "on" else Mode.OFF
