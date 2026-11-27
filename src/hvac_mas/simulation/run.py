"""A reproducible run owns all physical state, schedules and observation overrides."""
from copy import copy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from uuid import uuid4

from hvac_mas.models.building import Building, Mode, Thermostat, c_to_f, f_to_c
from hvac_mas.models.telemetry import Observation, Override, Reading, Source, derive
from hvac_mas.simulation.engine import Simulation
from hvac_mas.simulation.environment import EnvironmentChange, Fault, FaultKind, Scenario, occupancy, weather
from hvac_mas.simulation.equipment import commit_mode, output, stage_for

EPOCH = datetime(2026, 1, 1, 9, tzinfo=UTC)


@dataclass
class Run:
    building: Building
    scenario: Scenario
    id: str = field(default_factory=lambda: uuid4().hex)

    def __post_init__(self):
        self.sim = Simulation(self.building)
        self.specs = {u.unit_id: u for u in self.building.equipment}
        self.zones = {z.zone_id: z for z in self.building.zones}
        self.thermostats = {z.zone_id: z.thermostat for z in self.building.zones}
        if self.scenario.wide_deadband:
            self.thermostats = {zid: Thermostat(thermostat_id=t.thermostat_id, heat_setpoint_c=f_to_c(66),
                                               cool_setpoint_c=f_to_c(72), fan_policy=t.fan_policy)
                                for zid, t in self.thermostats.items()}
        self.overrides: dict[str, Override] = {}
        self.weather_override = {}
        self.occupancy_override = {}
        self.changes = list(self.scenario.changes)
        self.faults = list(self.scenario.faults)
        self.active_faults = {}
        self.operations = {}
        for change in self.changes:
            self.validate_change(change)
        for fault in self.faults:
            self.validate_fault(fault)
        self.result = None
        self.apply_change(self.scenario.initial)

    def validate_fault(self, fault: Fault):
        if fault.unit_id not in self.specs:
            raise ValueError("unknown unit")
        if fault.magnitude is not None:
            if fault.kind in (FaultKind.COOLING_DEGRADATION, FaultKind.HEATING_DEGRADATION):
                if not 0 <= fault.magnitude <= 1:
                    raise ValueError("degradation magnitude is remaining capacity in [0, 1]")
            elif fault.kind in (FaultKind.ROOM_BIAS, FaultKind.SUPPLY_BIAS):
                if abs(fault.magnitude) > 25:
                    raise ValueError("sensor bias must lie between -25 and +25 °F")
            else:
                raise ValueError("this fault does not accept a magnitude")

    def validate_change(self, change: EnvironmentChange):
        for zone in set(change.occupancy) | set(change.zone_temperature_f) | set(change.setpoints_f):
            if zone not in self.zones:
                raise ValueError(f"unknown zone {zone}")
        if any(not 0 <= n <= 1000 for n in change.occupancy.values()):
            raise ValueError("invalid occupancy")
        if any(not isfinite(t) or not 40 <= t <= 120 for t in change.zone_temperature_f.values()):
            raise ValueError("zone temperature must be between 40 and 120 °F")
        if any(not 40 <= heat < cool <= 100 for heat, cool in change.setpoints_f.values()):
            raise ValueError("invalid heating and cooling setpoint pair")
        if change.power_limit_kw is not None:
            floor = sum(min(s["power"] for s in options.values()) for options in self.feasible().values())
            if change.power_limit_kw + 1e-9 < floor:
                raise ValueError(f"power cap is below protected load of {floor:.2f} kW")

    def apply_change(self, change: EnvironmentChange):
        self.validate_change(change)
        if change.outdoor_f is not None:
            t = f_to_c(change.outdoor_f)
            self.weather_override.update(outdoor_c=t, forecast_c=t)
        if change.humidity is not None:
            self.weather_override["humidity"] = change.humidity
        if change.power_limit_kw is not None:
            self.sim.building = self.building.model_copy(update={"power_limit_kw": change.power_limit_kw})
        self.occupancy_override.update(change.occupancy)
        self.sim.temperatures_c.update({z: f_to_c(t) for z, t in change.zone_temperature_f.items()})
        for zid, (heat, cool) in change.setpoints_f.items():
            self.thermostats[zid] = self.thermostats[zid].model_copy(
                update={"heat_setpoint_c": f_to_c(heat), "cool_setpoint_c": f_to_c(cool)})

    def prepare(self):
        step = self.sim.step_number
        for f in self.faults:
            if f.at_step == step:
                self.apply_fault(f)
        for change in self.changes:
            if change.at_step == step:
                self.apply_change(change)
        outdoor = self.context()[0]["outdoor_c"]
        if outdoor < -15:
            for uid, spec in self.specs.items():
                state = self.sim.units[uid]
                if spec.heating_type == "heat_pump" and str(state.actual_mode).startswith("HEAT"):
                    offline = state.offline
                    state.offline = True
                    commit_mode(spec, state, Mode.OFF, self.sim.time_s)
                    state.offline = offline

    def apply_fault(self, fault):
        self.validate_fault(fault)
        fault.apply(self.sim.units[fault.unit_id])
        if fault.kind == FaultKind.CLEAR:
            self.active_faults.pop(fault.unit_id, None)
        else:
            self.active_faults.setdefault(fault.unit_id, {})[fault.kind] = self.sim.step_number

    def context(self):
        w = weather(self.sim.time_s, self.scenario.seed, self.scenario.cold_day) | self.weather_override
        counts = {zid: self.occupancy_override.get(zid, occupancy(z.area_id, self.sim.time_s))
                  for zid, z in self.zones.items()}
        return w, counts

    def feasible(self):
        result = {}
        outdoor = self.context()[0]["outdoor_c"]
        for uid, spec in self.specs.items():
            state = self.sim.units[uid]
            result[uid] = {}
            for mode in [Mode.OFF, *(s.name for s in spec.stages if s.supported)]:
                if spec.heating_type == "heat_pump" and outdoor < -15 and str(mode).startswith("HEAT"):
                    if mode != state.actual_mode:
                        continue
                if commit_mode(spec, copy(state), mode, self.sim.time_s):
                    stage = stage_for(spec, mode)
                    result[uid][mode] = {"power": stage.electrical_power_kw if stage else 0.0}
        return result

    def reading(self, uid):
        weather_state, counts = self.context()
        spec, state = self.specs[uid], self.sim.units[uid]
        temperature = self.sim.temperatures_c[spec.zone_id]
        thermostat = self.thermostats[spec.zone_id]
        _, power, _, supply = output(spec, state, temperature, weather_state["outdoor_c"])
        return Reading.model_validate(derive({
            "avgTemp": c_to_f(temperature + state.room_bias_c),
            "supplyTemp": c_to_f(supply + state.supply_bias_c),
            "heatSetpoint": c_to_f(thermostat.heat_setpoint_c), "coolSetpoint": c_to_f(thermostat.cool_setpoint_c),
            "outsideTemp": c_to_f(weather_state["outdoor_c"]), "outsideHumid": weather_state["humidity"],
            "humidity": 50, "mode": state.actual_mode if not state.offline else Mode.OFF,
            "fanStatus": "ON" if power > 0 else "OFF", "econStatus": "CLOSED", "power": power,
            "occupancy": counts[spec.zone_id],
        }))

    def observations(self, attempt):
        packets = []
        for uid, spec in self.specs.items():
            if self.sim.units[uid].offline:
                continue
            zone = self.zones[spec.zone_id]
            reading = self.reading(uid)
            edit = self.overrides.get(uid)
            if edit:
                reading = edit.read(reading)
            packets.append(Observation(packetId=f"{attempt}-{uid}", runId=self.id, buildingId=self.building.building_id,
                                       areaId=zone.area_id, controlZoneId=zone.zone_id, unitId=uid,
                                       thermostatId=zone.thermostat.thermostat_id,
                                       simulationStep=self.sim.step_number, timestamp=EPOCH + timedelta(seconds=self.sim.time_s),
                                       source=(Source.REPLAY if edit.replay_of else Source.MANUAL) if edit else Source.SIMULATOR,
                                       operationId=edit.operation_id if edit else None,
                                       replayOf=edit.replay_of if edit else None, data=reading))
        return packets

    def commit(self, modes, requested):
        w, occupants = self.context()
        self.result = self.sim.step(modes, outdoor_c=w["outdoor_c"], occupancy=occupants)
        for uid, mode in requested.items():
            if uid in self.sim.units:
                self.sim.units[uid].requested_mode = Mode(mode)
        for uid, edit in list(self.overrides.items()):
            if self.sim.units[uid].offline:
                continue
            if edit.repeats == 1:
                del self.overrides[uid]
            else:
                self.overrides[uid] = replace_override(edit)
        return self.result

    def snapshot(self):
        w, counts = self.context()
        return {"runId": self.id, "step": self.sim.step_number, "time_s": self.sim.time_s,
                "energy_kwh": self.sim.energy_kwh, "power_limit_kw": self.sim.building.power_limit_kw,
                "weather": w, "scenario": self.scenario.title,
                "zones": {zid: {"temperature_f": c_to_f(t), "occupancy": counts[zid]}
                          for zid, t in self.sim.temperatures_c.items()},
                "units": {uid: {"zone_id": self.specs[uid].zone_id, "actual_mode": state.actual_mode, "approved_mode": state.approved_mode,
                                "requested_mode": state.requested_mode, "available": not state.offline,
                                "reading": self.reading(uid).model_dump(mode="json")}
                          for uid, state in self.sim.units.items()},
                "power_kw": 0 if self.result is None else self.result["power_kw"]}

    def persisted_state(self):
        """Return the complete mutable plant state needed to resume after a cold start."""
        return {
            "runId": self.id,
            "scenario": self.scenario.model_dump(mode="json"),
            "time_s": self.sim.time_s,
            "step_number": self.sim.step_number,
            "temperatures_c": self.sim.temperatures_c,
            "energy_kwh": self.sim.energy_kwh,
            "units": {uid: asdict(unit) | {"actual_mode": unit.actual_mode.value,
                                                  "requested_mode": unit.requested_mode.value,
                                                  "approved_mode": unit.approved_mode.value}
                      for uid, unit in self.sim.units.items()},
            "result": self.result,
            "thermostats": {uid: thermostat.model_dump(mode="json")
                            for uid, thermostat in self.thermostats.items()},
            "overrides": {uid: override.model_dump(mode="json") for uid, override in self.overrides.items()},
            "weather_override": self.weather_override,
            "occupancy_override": self.occupancy_override,
            "changes": [change.model_dump(mode="json") for change in self.changes],
            "faults": [fault.model_dump(mode="json") for fault in self.faults],
            "active_faults": {uid: {kind.value: step for kind, step in faults.items()}
                              for uid, faults in self.active_faults.items()},
        }

    @classmethod
    def from_persisted_state(cls, building, payload):
        scenario = Scenario.model_validate(payload["scenario"])
        run = cls(building, scenario, id=payload["runId"])
        run.sim.time_s = payload["time_s"]
        run.sim.step_number = payload["step_number"]
        run.sim.temperatures_c = payload["temperatures_c"]
        run.sim.energy_kwh = payload["energy_kwh"]
        for uid, values in payload["units"].items():
            run.sim.units[uid] = EquipmentState(**(values | {
                "actual_mode": Mode(values["actual_mode"]),
                "requested_mode": Mode(values["requested_mode"]),
                "approved_mode": Mode(values["approved_mode"]),
            }))
        run.result = payload["result"]
        run.thermostats = {uid: Thermostat.model_validate(value)
                           for uid, value in payload["thermostats"].items()}
        run.overrides = {uid: Override.model_validate(value) for uid, value in payload["overrides"].items()}
        run.weather_override = payload["weather_override"]
        run.occupancy_override = payload["occupancy_override"]
        run.changes = [EnvironmentChange.model_validate(value) for value in payload["changes"]]
        run.faults = [Fault.model_validate(value) for value in payload["faults"]]
        run.active_faults = {uid: {FaultKind(kind): step for kind, step in faults.items()}
                             for uid, faults in payload["active_faults"].items()}
        return run

    def truth(self):
        return {"runId": self.id, "simulationStep": self.sim.step_number,
                "temperature_c": self.sim.temperatures_c, "units": {uid: asdict(s) for uid, s in self.sim.units.items()},
                "faults": self.active_faults}


def replace_override(override):
    return override.model_copy(update={"repeats": override.repeats - 1})
