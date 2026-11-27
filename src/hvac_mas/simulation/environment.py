"""Seeded environment and explicitly scheduled physical disturbances."""
from enum import StrEnum
from math import cos, pi
from pathlib import Path
from random import Random

import yaml
from pydantic import Field

from hvac_mas.models.building import ConfigModel, c_to_f, f_to_c


class FaultKind(StrEnum):
    COOLING_DEGRADATION = "cooling_degradation"
    HEATING_DEGRADATION = "heating_degradation"
    COOL1_FAILURE = "cool1_failure"
    COOL2_FAILURE = "cool2_failure"
    OFFLINE = "offline"
    ROOM_BIAS = "room_sensor_bias"
    SUPPLY_BIAS = "supply_sensor_bias"
    CLEAR = "clear"


class Fault(ConfigModel):
    unit_id: str
    kind: FaultKind
    magnitude: float | None = None
    at_step: int = Field(default=0, ge=0)

    def apply(self, state):
        if self.kind == FaultKind.CLEAR:
            state.cooling_health = state.heating_health = 1
            state.offline = state.stage1_failed = state.stage2_failed = False
            state.room_bias_c = state.supply_bias_c = 0
        elif self.kind in (FaultKind.COOLING_DEGRADATION, FaultKind.HEATING_DEGRADATION):
            setattr(state, "cooling_health" if self.kind == FaultKind.COOLING_DEGRADATION else "heating_health",
                    0.15 if self.magnitude is None else self.magnitude)
        elif self.kind in (FaultKind.COOL1_FAILURE, FaultKind.COOL2_FAILURE, FaultKind.OFFLINE):
            setattr(state, {FaultKind.COOL1_FAILURE: "stage1_failed", FaultKind.COOL2_FAILURE: "stage2_failed",
                            FaultKind.OFFLINE: "offline"}[self.kind], True)
        else:
            setattr(state, "room_bias_c" if self.kind == FaultKind.ROOM_BIAS else "supply_bias_c",
                    (3 if self.magnitude is None else self.magnitude) * 5 / 9)


class EnvironmentChange(ConfigModel):
    outdoor_f: float | None = Field(default=None, ge=-60, le=150)
    humidity: float | None = Field(default=None, ge=0, le=100)
    power_limit_kw: float | None = Field(default=None, gt=0)
    occupancy: dict[str, int] = Field(default_factory=dict)
    zone_temperature_f: dict[str, float] = Field(default_factory=dict)
    setpoints_f: dict[str, tuple[float, float]] = Field(default_factory=dict)
    at_step: int = Field(default=0, ge=0)


class Scenario(ConfigModel):
    title: str
    description: str
    seed: int = 42
    cold_day: bool = False
    wide_deadband: bool = False
    initial: EnvironmentChange = EnvironmentChange()
    changes: tuple[EnvironmentChange, ...] = ()
    faults: tuple[Fault, ...] = ()


def load_scenarios(path: Path) -> dict[str, Scenario]:
    return {key: Scenario.model_validate(value) for key, value in yaml.safe_load(path.read_text()).items()}


def weather(seconds: float, seed: int, cold: bool) -> dict:
    hour = (9 + seconds / 3600) % 24
    offset = Random(seed).uniform(-0.5, 0.5)
    midpoint, amplitude = (3, 5) if cold else (30, 6)
    value = lambda h: midpoint + amplitude * cos((h - 15) * 2 * pi / 24) + offset
    return {"outdoor_c": value(hour), "humidity": 55 - 10 * cos((hour - 15) * 2 * pi / 24),
            "forecast_c": value(hour + 1)}


def occupancy(area_id: str, seconds: float) -> int:
    hour = (9 + seconds / 3600) % 24
    if area_id == "A":
        return 14 if 9.5 <= hour < 10.5 or 14 <= hour < 15 else 0
    if area_id == "B":
        return max(0, min(18, int((hour - 7) * 9), int((19 - hour) * 9)))
    if area_id == "C":
        return int(8 <= hour < 17) * 3
    return 25 if 9 <= hour < 12 or 13 <= hour < 16 else 0
