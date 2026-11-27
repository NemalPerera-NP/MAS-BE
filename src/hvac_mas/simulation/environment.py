"""Seeded environment and explicitly scheduled physical disturbances."""
from enum import StrEnum
from math import cos, pi
from pathlib import Path
from random import Random

import yaml
from pydantic import Field, model_validator

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
    intervention_id: str | None = None
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


class OccupancyInterval(ConfigModel):
    start_minute: int = Field(ge=0, le=10080, strict=True)
    end_minute: int = Field(gt=0, le=10080, strict=True)
    occupants: int = Field(ge=0, le=1000, strict=True)

    @model_validator(mode="after")
    def valid_interval(self):
        if self.end_minute <= self.start_minute:
            raise ValueError("occupancy interval end must follow its start")
        return self


class OccupancyActionKind(StrEnum):
    SET = "set"
    ARRIVAL = "arrival"
    DEPARTURE = "departure"
    TRANSFER = "transfer"
    TEMPORARY_ADD = "temporary_add"
    RESTORE_SCHEDULE = "restore_schedule"


class OccupancyAction(ConfigModel):
    kind: OccupancyActionKind
    zone_id: str | None = None
    count: int | None = Field(default=None, ge=0, le=1000, strict=True)
    source_zone_id: str | None = None
    destination_zone_id: str | None = None
    duration_minutes: int | None = Field(default=None, gt=0, le=10080, strict=True)

    @model_validator(mode="after")
    def valid_action(self):
        if self.kind == OccupancyActionKind.TRANSFER:
            if not self.source_zone_id or not self.destination_zone_id or self.source_zone_id == self.destination_zone_id:
                raise ValueError("transfer requires distinct source and destination zones")
            if self.count is None or self.count <= 0:
                raise ValueError("transfer count must be positive")
        elif self.kind == OccupancyActionKind.RESTORE_SCHEDULE:
            if not self.zone_id:
                raise ValueError("restore_schedule requires a zone")
        else:
            if not self.zone_id:
                raise ValueError(f"{self.kind.value} requires a zone")
            if self.kind == OccupancyActionKind.SET and self.count is None:
                raise ValueError("set requires an occupant count")
            if self.kind in (OccupancyActionKind.ARRIVAL, OccupancyActionKind.DEPARTURE,
                             OccupancyActionKind.TEMPORARY_ADD) and (self.count is None or self.count <= 0):
                raise ValueError(f"{self.kind.value} count must be positive")
            if self.kind == OccupancyActionKind.TEMPORARY_ADD and self.duration_minutes is None:
                raise ValueError("temporary_add requires duration_minutes")
            if self.kind != OccupancyActionKind.TEMPORARY_ADD and self.duration_minutes is not None:
                raise ValueError("duration_minutes is only valid for temporary_add")
        return self


class EnvironmentChange(ConfigModel):
    intervention_id: str | None = None
    outdoor_f: float | None = Field(default=None, ge=-60, le=150)
    humidity: float | None = Field(default=None, ge=0, le=100)
    power_limit_kw: float | None = Field(default=None, gt=0)
    occupancy: dict[str, int] = Field(default_factory=dict)
    occupancy_actions: tuple[OccupancyAction, ...] = ()
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
    occupancy_schedule: dict[str, tuple[OccupancyInterval, ...]] = Field(default_factory=dict)


def load_scenarios(path: Path) -> dict[str, Scenario]:
    raw = yaml.safe_load(path.read_text())
    defaults = raw.pop("_defaults", {})
    return {key: Scenario.model_validate(defaults | value) for key, value in raw.items()}


def weather(seconds: float, seed: int, cold: bool) -> dict:
    hour = (9 + seconds / 3600) % 24
    offset = Random(seed).uniform(-0.5, 0.5)
    midpoint, amplitude = (3, 5) if cold else (30, 6)
    value = lambda h: midpoint + amplitude * cos((h - 15) * 2 * pi / 24) + offset
    return {"outdoor_c": value(hour), "humidity": 55 - 10 * cos((hour - 15) * 2 * pi / 24),
            "forecast_c": value(hour + 1)}
