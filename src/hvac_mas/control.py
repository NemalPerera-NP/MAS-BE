"""Configuration and stage-based allocation rules shared by the actuator and power agent."""
from pathlib import Path

import yaml
from pydantic import Field, model_validator

from hvac_mas.models.building import ConfigModel, Mode


class ControlRules(ConfigModel):
    hysteresis_c: float = Field(default=0.3, gt=0)
    high_stage_error_c: float = Field(default=1.5, gt=0)
    comfort_band_c: float = Field(default=0.5, gt=0)
    max_rounds: int = Field(default=3, ge=1, le=8)

    @model_validator(mode="after")
    def thresholds(self):
        if self.high_stage_error_c <= self.hysteresis_c:
            raise ValueError("high-stage error must exceed hysteresis")
        return self


class DiagnosticRules(ConfigModel):
    g2_f: float = Field(default=2, gt=0)
    g5_f: float = Field(default=5, gt=0)
    window_minutes: int = Field(default=40, ge=5)
    min_occurrences: int = Field(default=5, ge=2)
    recovery_minutes: int = Field(default=20, ge=5)
    fdd_occurrences: int = Field(default=3, ge=2)
    fdd_fraction: float = Field(default=0.55, gt=0, lt=1)
    improvement_f_per_minute: float = Field(default=0.055, gt=0)

    @model_validator(mode="after")
    def thresholds(self):
        if self.g5_f <= self.g2_f or self.min_occurrences > self.window_minutes // 5 + 1:
            raise ValueError("diagnostic thresholds or observation window are inconsistent")
        return self


class Rules(ConfigModel):
    control: ControlRules = ControlRules()
    diagnostics: DiagnosticRules = DiagnosticRules()


def load_rules(path: Path):
    return Rules.model_validate(yaml.safe_load(path.read_text()))


def total_power(allocation, feasible):
    return sum(feasible[uid][mode]["power"] for uid, mode in allocation.items())


def allocate(feasible, intents, limit, *, fixed_priority=False):
    """Reserve unavoidable load first, then rank requested discrete stages."""
    committed = {uid: min(options, key=lambda mode: (options[mode]["power"], mode)) for uid, options in feasible.items()}
    if total_power(committed, feasible) > limit + 1e-9:
        raise ValueError("cap below the minimum protected load")
    order = sorted(feasible, key=lambda uid: (0 if fixed_priority else -intents[uid]["priority"], uid))
    for uid in order:
        for mode in intents[uid]["choices"]:
            if mode not in feasible[uid]:
                continue
            proposal = committed | {uid: mode}
            if total_power(proposal, feasible) <= limit + 1e-9:
                committed = proposal
                break
    return committed


def fixed_intents(packets, feasible, thermostats, rules):
    from hvac_mas.models.building import f_to_c
    from hvac_mas.simulation.thermostat import request_mode

    result = {uid: {"choices": [Mode.OFF], "priority": 0} for uid in feasible}
    for uid, packet in packets.items():
        preferred = request_mode(thermostats[packet.controlZoneId], f_to_c(packet.data.avgTemp), packet.data.mode,
                                 hysteresis_c=rules.hysteresis_c, high_stage_error_c=rules.high_stage_error_c, occupied=packet.data.occupancy > 0)
        lower = {Mode.COOL2: Mode.COOL1, Mode.HEAT2: Mode.HEAT1}.get(preferred, Mode.OFF)
        result[uid]["choices"] = list(dict.fromkeys([preferred, lower, Mode.OFF]))
    return result
