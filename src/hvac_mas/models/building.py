"""Validated building configuration. All thermal temperatures are in degrees C."""
from enum import StrEnum
from pathlib import Path
from typing import Annotated

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConfigModel(BaseModel):
    model_config = ConfigDict(extra='forbid', frozen=True, allow_inf_nan=False)


Identifier = Annotated[str, Field(min_length=1, pattern=r'^[A-Za-z0-9_-]+$')]
Positive = Annotated[float, Field(gt=0)]
NonNegative = Annotated[float, Field(ge=0)]


class Mode(StrEnum):
    OFF = 'OFF'
    FAN = 'FAN'
    COOL1 = 'COOL1'
    COOL2 = 'COOL2'
    HEAT1 = 'HEAT1'
    HEAT2 = 'HEAT2'


class Stage(ConfigModel):
    name: Mode
    thermal_capacity_kw: NonNegative
    electrical_power_kw: NonNegative
    fuel_input_kw: NonNegative = 0
    airflow_m3_s: Positive
    min_on_seconds: NonNegative = 300
    min_off_seconds: NonNegative = 300
    min_stage_duration_seconds: NonNegative = 300
    supported: bool = True
    requires: Mode | None = None

    @model_validator(mode='after')
    def valid_stage(self):
        if self.name == Mode.FAN and (self.thermal_capacity_kw or self.fuel_input_kw or self.requires):
            raise ValueError('fan mode cannot provide thermal conditioning')
        if self.name not in (Mode.OFF, Mode.FAN) and self.thermal_capacity_kw == 0:
            raise ValueError('active conditioning requires positive rated capacity')
        if self.name == Mode.OFF:
            raise ValueError('OFF is not an active stage')
        if self.requires == self.name or self.requires == Mode.OFF:
            raise ValueError('invalid stage prerequisite')
        return self


class Equipment(ConfigModel):
    unit_id: Identifier
    zone_id: Identifier
    nominal_tonnage: Positive
    heating_type: str
    stages: tuple[Stage, ...]

    @model_validator(mode='after')
    def valid_equipment(self):
        names = [stage.name for stage in self.stages]
        if len(names) != len(set(names)) or not names:
            raise ValueError('stage names must be unique and nonempty')
        if self.heating_type not in {'gas', 'heat_pump', 'electric'}:
            raise ValueError('unsupported heating type')
        for stage in self.stages:
            if stage.requires is not None:
                prerequisite = next((s for s in self.stages if s.name == stage.requires), None)
                if prerequisite is None or not prerequisite.supported or stage.name[:4] != stage.requires[:4]:
                    raise ValueError('invalid stage prerequisite')
                if prerequisite.requires is not None:
                    raise ValueError('nested stage prerequisites are not supported')
        return self


class Area(ConfigModel):
    area_id: Identifier
    name: Annotated[str, Field(min_length=1)]
    zone_ids: tuple[Identifier, ...] = Field(min_length=1)
    priority: Positive = 1


class Thermostat(ConfigModel):
    thermostat_id: Identifier
    heat_setpoint_c: float = Field(default=20.0, ge=(40 - 32) * 5 / 9, le=(100 - 32) * 5 / 9)
    cool_setpoint_c: float = Field(default=(70 - 32) * 5 / 9, ge=(40 - 32) * 5 / 9, le=(100 - 32) * 5 / 9)
    fan_policy: str = 'auto'

    @model_validator(mode='after')
    def valid_setpoints(self):
        if self.heat_setpoint_c >= self.cool_setpoint_c:
            raise ValueError('heating setpoint must be below cooling setpoint')
        if self.fan_policy not in {'auto', 'on'}:
            raise ValueError('invalid fan policy')
        return self


class Zone(ConfigModel):
    zone_id: Identifier
    area_id: Identifier
    thermostat: Thermostat
    unit_ids: tuple[Identifier, ...] = Field(min_length=1)
    floor_area_m2: Positive
    capacitance_kwh_per_c: Positive
    outdoor_conductance_kw_per_c: NonNegative
    base_load_kw: NonNegative
    occupant_gain_kw: NonNegative = 0.1
    initial_temperature_c: float = 23.0


class Coupling(ConfigModel):
    zone_a: Identifier
    zone_b: Identifier
    conductance_kw_per_c: Positive


class Building(ConfigModel):
    building_id: Identifier
    name: Annotated[str, Field(min_length=1)]
    power_limit_kw: Positive
    areas: tuple[Area, ...] = Field(min_length=1)
    zones: tuple[Zone, ...] = Field(min_length=1)
    equipment: tuple[Equipment, ...] = Field(min_length=1)
    couplings: tuple[Coupling, ...] = ()

    @model_validator(mode='after')
    def references_are_consistent(self):
        def unique(items, label):
            if len(items) != len(set(items)):
                raise ValueError(f'duplicate {label}')
        unique([a.area_id for a in self.areas], 'area')
        unique([z.zone_id for z in self.zones], 'zone')
        unique([z.thermostat.thermostat_id for z in self.zones], 'thermostat')
        unique([u.unit_id for u in self.equipment], 'unit')
        zones = {z.zone_id: z for z in self.zones}
        units = {u.unit_id: u for u in self.equipment}
        listed_zones = []
        for area in self.areas:
            unique(list(area.zone_ids), 'area zone reference')
            for zid in area.zone_ids:
                if zid not in zones or zones[zid].area_id != area.area_id:
                    raise ValueError('inconsistent area-zone relationship')
                listed_zones.append(zid)
        if set(listed_zones) != set(zones) or len(listed_zones) != len(zones):
            raise ValueError('each zone must belong to exactly one area')
        listed_units = []
        for zone in self.zones:
            unique(list(zone.unit_ids), 'zone unit reference')
            for uid in zone.unit_ids:
                if uid not in units or units[uid].zone_id != zone.zone_id:
                    raise ValueError('inconsistent zone-unit relationship')
                listed_units.append(uid)
        if set(listed_units) != set(units) or len(listed_units) != len(units):
            raise ValueError('each unit must serve exactly one zone')
        pairs = []
        for edge in self.couplings:
            if edge.zone_a not in zones or edge.zone_b not in zones or edge.zone_a == edge.zone_b:
                raise ValueError('invalid coupling')
            pairs.append(tuple(sorted((edge.zone_a, edge.zone_b))))
        unique(pairs, 'coupling')
        return self


def load_building(path: str | Path) -> Building:
    with Path(path).open(encoding='utf-8') as file:
        return Building.model_validate(yaml.safe_load(file))


def f_to_c(value: float) -> float:
    return (value - 32) * 5 / 9


def c_to_f(value: float) -> float:
    return value * 9 / 5 + 32
