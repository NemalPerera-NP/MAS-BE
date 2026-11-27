"""Deterministic physics step with atomic stage and power validation.

This is a local physical engine, not the asynchronous MAS/telemetry loop.
"""
import math
from copy import deepcopy
from dataclasses import dataclass, field
from hvac_mas.models.building import Building, Mode
from hvac_mas.simulation.equipment import EquipmentState, commit_mode, output
from hvac_mas.simulation.thermal import advance_temperatures


@dataclass
class Simulation:
    building: Building
    step_seconds: float = 300
    time_s: float = 0
    step_number: int = 0
    temperatures_c: dict[str, float] = field(init=False)
    units: dict[str, EquipmentState] = field(init=False)
    energy_kwh: float = 0

    def __post_init__(self):
        if not math.isfinite(self.step_seconds) or self.step_seconds <= 0:
            raise ValueError('step_seconds must be positive')
        self.temperatures_c = {z.zone_id: z.initial_temperature_c for z in self.building.zones}
        self.units = {u.unit_id: EquipmentState() for u in self.building.equipment}

    def step(self, approvals: dict[str, Mode], *, outdoor_c: float, occupancy: dict[str, int]) -> dict:
        specs = {u.unit_id: u for u in self.building.equipment}
        if set(approvals) != set(specs):
            raise ValueError('a stage must be specified for every unit')
        candidate = deepcopy(self.units)
        for uid, mode in approvals.items():
            if not commit_mode(specs[uid], candidate[uid], Mode(mode), self.time_s):
                raise ValueError(f'{uid}: transition denied by equipment protection')
        zone_heat = dict.fromkeys(self.temperatures_c, 0.0)
        unit_results = {}
        total_power = 0.0
        for uid, spec in specs.items():
            heat, power, fuel, supply = output(spec, candidate[uid], self.temperatures_c[spec.zone_id], outdoor_c)
            zone_heat[spec.zone_id] += heat
            total_power += power
            unit_results[uid] = {'heat_kw': heat, 'power_kw': power, 'fuel_kw': fuel, 'supply_c': supply}
        if total_power > self.building.power_limit_kw + 1e-9:
            raise ValueError('approved stages exceed the electrical power limit')
        updated = advance_temperatures(self.building, self.temperatures_c, outdoor_c=outdoor_c,
                                       occupancy=occupancy, hvac_kw=zone_heat, seconds=self.step_seconds)
        self.units, self.temperatures_c = candidate, updated
        self.time_s += self.step_seconds
        self.step_number += 1
        self.energy_kwh += total_power * self.step_seconds / 3600
        return {'step': self.step_number, 'temperature_c': dict(updated), 'power_kw': total_power,
                'energy_kwh': self.energy_kwh, 'units': unit_results}
