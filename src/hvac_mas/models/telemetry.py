"""Wire schema for immutable IoT observations. External temperatures are Fahrenheit."""
from datetime import datetime
from enum import StrEnum
from typing import Literal

from pydantic import AwareDatetime, Field, model_validator

from hvac_mas.models.building import ConfigModel, Identifier, Mode


class Source(StrEnum):
    SIMULATOR = "SIMULATOR"
    MANUAL = "MANUAL"
    REPLAY = "REPLAY"


class Reading(ConfigModel):
    avgTemp: float = Field(ge=-60, le=165)
    heatSetpoint: float = Field(ge=40, le=100)
    coolSetpoint: float = Field(ge=40, le=100)
    supplyTemp: float = Field(ge=-100, le=220)
    outsideTemp: float = Field(ge=-60, le=150)
    humidity: float = Field(ge=0, le=100)
    outsideHumid: float = Field(ge=0, le=100)
    mode: Mode
    fanStatus: Literal["ON", "OFF"]
    econStatus: Literal["OPEN", "CLOSED"]
    power: float = Field(ge=0, le=1000)
    occupancy: int = Field(ge=0, le=1000)
    setTemp: float
    supplyDelta: float
    tempDiff: float

    @model_validator(mode="after")
    def validate_derived(self):
        if self.heatSetpoint >= self.coolSetpoint:
            raise ValueError("heatSetpoint must be below coolSetpoint")
        expected = derive(self.model_dump())
        for field in ("setTemp", "supplyDelta", "tempDiff"):
            if abs(getattr(self, field) - expected[field]) > 0.011:
                raise ValueError(f"inconsistent {field}")
        return self


def derive(values: dict) -> dict:
    values = dict(values)
    room = values["avgTemp"]
    heat = values["heatSetpoint"]
    cool = values["coolSetpoint"]
    mode = values["mode"]
    heating = str(mode).startswith("HEAT") or (mode in (Mode.OFF, Mode.FAN) and room < heat)
    values["setTemp"] = heat if heating else cool
    values["tempDiff"] = round(heat - room if heating else room - cool, 4)
    values["supplyDelta"] = round(room - values["supplyTemp"], 4)
    return values


class Observation(ConfigModel):
    packetId: Identifier
    runId: Identifier
    buildingId: Identifier
    areaId: Identifier
    controlZoneId: Identifier
    unitId: Identifier
    thermostatId: Identifier
    simulationStep: int = Field(ge=0)
    source: Source
    timestamp: AwareDatetime
    operationId: Identifier | None = None
    replayOf: Identifier | None = None
    data: Reading


class Override(ConfigModel):
    unit_id: Identifier
    fields: dict
    repeats: int = Field(default=1, ge=1, le=240)
    operation_id: Identifier | None = None
    replay_of: Identifier | None = None

    def read(self, base: Reading) -> Reading:
        values = base.model_dump()
        allowed = set(values) - {"supplyDelta", "tempDiff"}
        if not self.fields or set(self.fields) - allowed:
            raise ValueError("override contains no fields or includes unsupported fields")
        values.update(self.fields)
        for key in set(self.fields) & {"avgTemp", "heatSetpoint", "coolSetpoint", "supplyTemp", "setTemp"}:
            value = values[key]
            if not isinstance(value, (float, int)) or isinstance(value, bool):
                raise ValueError(f"{key} must be numeric")
        if "setTemp" in self.fields:
            mode = Mode(values["mode"])
            key = "heatSetpoint" if str(mode).startswith("HEAT") or (
                mode in (Mode.OFF, Mode.FAN) and values["avgTemp"] < values["heatSetpoint"]) else "coolSetpoint"
            if key in self.fields and self.fields[key] != values["setTemp"]:
                raise ValueError("setTemp conflicts with the selected setpoint")
            values[key] = self.fields["setTemp"]
        return Reading.model_validate(derive(values))
