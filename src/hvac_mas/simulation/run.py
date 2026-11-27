"""A reproducible run owns all physical state, schedules and observation overrides."""
from copy import copy, deepcopy
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from math import isfinite
from uuid import uuid4

from hvac_mas.models.building import Building, Mode, Thermostat, c_to_f, f_to_c
from hvac_mas.models.telemetry import Observation, Override, Reading, Source, derive
from hvac_mas.simulation.engine import Simulation
from hvac_mas.simulation.environment import EnvironmentChange, Fault, FaultKind, Scenario, weather
from hvac_mas.simulation.equipment import EquipmentState, commit_mode, output, stage_for
from hvac_mas.simulation.occupancy import OccupancyManager
from hvac_mas.simulation.comfort_policy import ComfortPolicyEngine

EPOCH = datetime(2026, 1, 1, 9, tzinfo=UTC)


@dataclass
class Run:
    building: Building
    scenario: Scenario
    id: str = field(default_factory=lambda: uuid4().hex)
    max_occupants_per_zone: int = 1000
    max_temporary_occupancy_minutes: int = 1440
    occupancy_forecast_minutes: int = 15

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
        self.occupancy_manager = OccupancyManager(self.scenario.occupancy_schedule, self.zones,
                                                  self.max_occupants_per_zone)
        self.comfort_policy = ComfortPolicyEngine()
        self.effective_thermostats = {}
        self.comfort_states = {}
        self.changes = list(self.scenario.changes)
        self.faults = list(self.scenario.faults)
        self.active_faults = {}
        self.operations = {}
        self.interventions = []
        for change in self.changes:
            self.validate_change(change)
        for fault in self.faults:
            self.validate_fault(fault)
        self.result = None
        self.apply_change(self.scenario.initial)
        self.refresh_comfort_policy()

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
        if any(action.duration_minutes and action.duration_minutes % 5 for action in change.occupancy_actions):
            raise ValueError("temporary visits must use five-minute multiples")
        action_zones = {zone for action in change.occupancy_actions
                        for zone in (action.zone_id, action.source_zone_id, action.destination_zone_id)
                        if zone is not None}
        for zone in set(change.occupancy) | action_zones | set(change.zone_temperature_f) | set(change.setpoints_f):
            if zone not in self.zones:
                raise ValueError(f"unknown zone {zone}")
        if any(isinstance(n, bool) or not isinstance(n, int) or not 0 <= n <= self.max_occupants_per_zone
               for n in change.occupancy.values()):
            raise ValueError("invalid occupancy")
        if any(action.duration_minutes and action.duration_minutes > self.max_temporary_occupancy_minutes
               for action in change.occupancy_actions):
            raise ValueError("temporary occupancy duration exceeds configured limit")
        occupancy_manager = self.occupancy_manager
        if change.at_step <= self.sim.step_number and (change.occupancy or change.occupancy_actions):
            occupancy_manager = deepcopy(self.occupancy_manager)
            occupancy_manager.apply_change(change.occupancy, change.occupancy_actions, self.sim.time_s)
        if any(not isfinite(t) or not 40 <= t <= 120 for t in change.zone_temperature_f.values()):
            raise ValueError("zone temperature must be between 40 and 120 °F")
        if any(not 40 <= heat < cool <= 100 for heat, cool in change.setpoints_f.values()):
            raise ValueError("invalid heating and cooling setpoint pair")
        if change.power_limit_kw is not None:
            floor = 0.0
            feasible = self.feasible()
            comfort_engine = deepcopy(self.comfort_policy)
            projected_counts = occupancy_manager.counts(self.sim.time_s)
            projected_fan = {}
            for zid, zone in self.zones.items():
                state = comfort_engine.evaluate(
                    zone, self.thermostats[zid], projected_counts[zid], self.sim.time_s,
                    occupancy_manager.next_scheduled_arrival(zid, self.sim.time_s))
                projected_fan[zid] = state.fan_policy
            for uid, options in feasible.items():
                zone = self.zones[self.specs[uid].zone_id]
                minimum = (Mode.FAN if projected_fan[zone.zone_id] == "on" and Mode.FAN in options else
                           min(options, key=lambda mode: (options[mode]["power"], mode)))
                floor += options[minimum]["power"]
            if change.power_limit_kw + 1e-9 < floor:
                raise ValueError(f"power cap is below mandatory policy/equipment load of {floor:.2f} kW")

    def apply_change(self, change: EnvironmentChange):
        self.validate_change(change)
        self.occupancy_manager.apply_change(change.occupancy, change.occupancy_actions, self.sim.time_s)
        if change.outdoor_f is not None:
            t = f_to_c(change.outdoor_f)
            self.weather_override.update(outdoor_c=t, forecast_c=t)
        if change.humidity is not None:
            self.weather_override["humidity"] = change.humidity
        if change.power_limit_kw is not None:
            self.sim.building = self.building.model_copy(update={"power_limit_kw": change.power_limit_kw})
        self.sim.temperatures_c.update({z: f_to_c(t) for z, t in change.zone_temperature_f.items()})
        for zid, (heat, cool) in change.setpoints_f.items():
            self.thermostats[zid] = self.thermostats[zid].model_copy(
                update={"heat_setpoint_c": f_to_c(heat), "cool_setpoint_c": f_to_c(cool)})
        self.refresh_comfort_policy()

    def refresh_comfort_policy(self):
        _, counts = self.context()
        effective, states = {}, {}
        for zid, zone in self.zones.items():
            targets = self.comfort_policy.evaluate(
                zone, self.thermostats[zid], counts[zid], self.sim.time_s,
                self.occupancy_manager.next_scheduled_arrival(zid, self.sim.time_s))
            effective[zid] = Thermostat(
                thermostat_id=self.thermostats[zid].thermostat_id,
                heat_setpoint_c=targets.heat_setpoint_c,
                cool_setpoint_c=targets.cool_setpoint_c,
                fan_policy=targets.fan_policy)
            states[zid] = targets
        self.effective_thermostats, self.comfort_states = effective, states

    def prepare(self):
        step = self.sim.step_number
        for f in list(self.faults):
            if f.at_step == step:
                try:
                    self.apply_fault(f)
                    self.mark_intervention_applied(f.intervention_id, step)
                except ValueError as exc:
                    if not self.reject_intervention(f.intervention_id, step, exc):
                        raise
                    self.faults.remove(f)
        for change in list(self.changes):
            if change.at_step == step:
                try:
                    self.apply_change(change)
                    self.mark_intervention_applied(change.intervention_id, step)
                except ValueError as exc:
                    if not self.reject_intervention(change.intervention_id, step, exc):
                        raise
                    self.changes.remove(change)
        self.refresh_comfort_policy()
        outdoor = self.context()[0]["outdoor_c"]
        if outdoor < -15:
            for uid, spec in self.specs.items():
                state = self.sim.units[uid]
                if spec.heating_type == "heat_pump" and str(state.actual_mode).startswith("HEAT"):
                    offline = state.offline
                    state.offline = True
                    commit_mode(spec, state, Mode.OFF, self.sim.time_s)
                    state.offline = offline

    def mark_intervention_applied(self, intervention_id, step):
        if intervention_id:
            for item in self.interventions:
                if item["interventionId"] == intervention_id and item["status"] == "QUEUED":
                    item.update(status="APPLIED", appliedStep=step)

    def reject_intervention(self, intervention_id, step, error):
        if not intervention_id:
            return False
        for item in self.interventions:
            if item["interventionId"] == intervention_id and item["status"] == "QUEUED":
                item.update(status="REJECTED", appliedStep=step, outcome=str(error))
                return True
        return False

    def apply_fault(self, fault):
        self.validate_fault(fault)
        fault.apply(self.sim.units[fault.unit_id])
        if fault.kind == FaultKind.CLEAR:
            self.active_faults.pop(fault.unit_id, None)
        else:
            self.active_faults.setdefault(fault.unit_id, {})[fault.kind] = self.sim.step_number

    def context(self):
        w = weather(self.sim.time_s, self.scenario.seed, self.scenario.cold_day) | self.weather_override
        self.occupancy_manager.prune_expired(self.sim.time_s)
        counts = self.occupancy_manager.counts(self.sim.time_s)
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
        thermostat = self.effective_thermostats[spec.zone_id]
        occupied_thermostat = self.thermostats[spec.zone_id]
        comfort = self.comfort_states[spec.zone_id]
        _, power, _, supply = output(spec, state, temperature, weather_state["outdoor_c"])
        return Reading.model_validate(derive({
            "avgTemp": c_to_f(temperature + state.room_bias_c),
            "supplyTemp": c_to_f(supply + state.supply_bias_c),
            "heatSetpoint": c_to_f(thermostat.heat_setpoint_c), "coolSetpoint": c_to_f(thermostat.cool_setpoint_c),
            "occupiedHeatSetpoint": c_to_f(occupied_thermostat.heat_setpoint_c),
            "occupiedCoolSetpoint": c_to_f(occupied_thermostat.cool_setpoint_c),
            "occupancyState": comfort.state.value,
            "outsideTemp": c_to_f(weather_state["outdoor_c"]), "outsideHumid": weather_state["humidity"],
            "humidity": 50, "mode": state.actual_mode if not state.offline else Mode.OFF,
            "fanStatus": "ON" if power > 0 else "OFF", "econStatus": "CLOSED", "power": power,
            "occupancy": counts[spec.zone_id],
            "expectedOccupancy": self.occupancy_manager.counts(
                self.sim.time_s + self.occupancy_forecast_minutes * 60)[spec.zone_id],
            "occupancyForecastMinutes": self.occupancy_forecast_minutes,
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
        applied_step = self.sim.step_number
        w, occupants = self.context()
        self.result = self.sim.step(modes, outdoor_c=w["outdoor_c"], occupancy=occupants)
        for uid, mode in requested.items():
            if uid in self.sim.units:
                self.sim.units[uid].requested_mode = Mode(mode)
        for uid, edit in list(self.overrides.items()):
            if self.sim.units[uid].offline:
                continue
            self.mark_intervention_applied(edit.operation_id, applied_step)
            if edit.repeats == 1:
                del self.overrides[uid]
            else:
                self.overrides[uid] = replace_override(edit)
        self.refresh_comfort_policy()
        return self.result

    def snapshot(self):
        w, counts = self.context()
        zones = {}
        for zid, temperature in self.sim.temperatures_c.items():
            policy = self.zones[zid].occupancy_policy
            ratio = policy.occupancy_ratio(counts[zid])
            zones[zid] = {"temperature_f": c_to_f(temperature), "occupancy": counts[zid],
                          "design_occupancy": policy.design_occupancy, "occupancy_load_ratio": ratio,
                          "over_design_occupancy": policy.is_over_design(counts[zid]),
                          "occupancy_state": self.comfort_states[zid].state.value,
                          "occupied_heat_setpoint_f": c_to_f(self.thermostats[zid].heat_setpoint_c),
                          "occupied_cool_setpoint_f": c_to_f(self.thermostats[zid].cool_setpoint_c),
                          "effective_heat_setpoint_f": c_to_f(self.effective_thermostats[zid].heat_setpoint_c),
                          "effective_cool_setpoint_f": c_to_f(self.effective_thermostats[zid].cool_setpoint_c)}
        areas = {area.area_id: {"occupancy": sum(counts[zid] for zid in area.zone_ids),
                                "zone_count": len(area.zone_ids)} for area in self.building.areas}
        building_occupancy = sum(counts.values())
        return {"runId": self.id, "step": self.sim.step_number, "time_s": self.sim.time_s,
                "energy_kwh": self.sim.energy_kwh, "power_limit_kw": self.sim.building.power_limit_kw,
                "weather": w, "scenario": self.scenario.title,
                "zones": zones, "areas": areas, "building_occupancy": building_occupancy,
                "units": {uid: {"zone_id": self.specs[uid].zone_id, "actual_mode": state.actual_mode, "approved_mode": state.approved_mode,
                                "requested_mode": state.requested_mode, "available": not state.offline,
                                "equipment_condition": ("OFFLINE" if state.offline else "FAULTED" if
                                    state.stage1_failed or state.stage2_failed else "DEGRADED" if
                                    state.cooling_health < 0.999 or state.heating_health < 0.999 else "HEALTHY"),
                                "reading": self.reading(uid).model_dump(mode="json")}
                          for uid, state in self.sim.units.items()},
                "power_kw": 0 if self.result is None else self.result["power_kw"]}

    def persisted_state(self):
        """Return the complete mutable plant state needed to resume after a cold start."""
        return {
            "state_schema_version": 2,
            "runId": self.id,
            "scenario": self.scenario.model_dump(mode="json"),
            "time_s": self.sim.time_s,
            "step_number": self.sim.step_number,
            "power_limit_kw": self.sim.building.power_limit_kw,
            "occupancy_forecast_minutes": self.occupancy_forecast_minutes,
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
            "occupancy_override": self.occupancy_manager.overrides,
            "occupancy_manager": self.occupancy_manager.persisted_state(),
            "comfort_policy": self.comfort_policy.persisted_state(),
            "changes": [change.model_dump(mode="json") for change in self.changes],
            "faults": [fault.model_dump(mode="json") for fault in self.faults],
            "active_faults": {uid: {kind.value: step for kind, step in faults.items()}
                              for uid, faults in self.active_faults.items()},
            "interventions": deepcopy(self.interventions),
            "operations": [{"id": identity, "signature": signature,
                             "response": response | {"preview": response["preview"].model_dump(mode="json")}}
                            for identity, (signature, response) in self.operations.items()],
        }

    @classmethod
    def from_persisted_state(cls, building, payload, max_occupants_per_zone=1000,
                             max_temporary_occupancy_minutes=1440, occupancy_forecast_minutes=15,
                             scenarios=None):
        version = payload.get("state_schema_version", 1)
        if version not in (1, 2):
            raise ValueError(f"unsupported persisted run schema version: {version}")
        scenario_data = dict(payload["scenario"])
        if not scenario_data.get("occupancy_schedule") and scenarios:
            current = next((item for item in scenarios.values()
                            if item.title == scenario_data.get("title")), None)
            if current is not None:
                scenario_data["occupancy_schedule"] = current.occupancy_schedule
        scenario = Scenario.model_validate(scenario_data)
        run = cls(building, scenario, id=payload["runId"],
                  max_occupants_per_zone=max_occupants_per_zone,
                  max_temporary_occupancy_minutes=max_temporary_occupancy_minutes,
                  occupancy_forecast_minutes=payload.get("occupancy_forecast_minutes",
                                                         occupancy_forecast_minutes))
        run.sim.time_s = payload["time_s"]
        run.sim.step_number = payload["step_number"]
        run.sim.building = building.model_copy(update={
            "power_limit_kw": payload.get("power_limit_kw", building.power_limit_kw)})
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
        if "occupancy_manager" in payload:
            run.occupancy_manager.restore_state(payload["occupancy_manager"], run.sim.time_s)
        else:
            run.occupancy_manager.apply_change(payload.get("occupancy_override", {}), (), run.sim.time_s)
        if "comfort_policy" in payload:
            run.comfort_policy.restore_state(payload["comfort_policy"])
        else:
            run.comfort_policy = ComfortPolicyEngine()
        run.refresh_comfort_policy()
        run.changes = [EnvironmentChange.model_validate(value) for value in payload["changes"]]
        run.faults = [Fault.model_validate(value) for value in payload["faults"]]
        run.active_faults = {uid: {FaultKind(kind): step for kind, step in faults.items()}
                             for uid, faults in payload["active_faults"].items()}
        run.interventions = deepcopy(payload.get("interventions", []))
        for operation in payload.get("operations", []):
            response = dict(operation["response"])
            response["preview"] = Reading.model_validate(response["preview"])
            run.operations[operation["id"]] = (tuple(operation["signature"]), response)
        return run

    def truth(self):
        return {"runId": self.id, "simulationStep": self.sim.step_number,
                "temperature_c": self.sim.temperatures_c, "units": {uid: asdict(s) for uid, s in self.sim.units.items()},
                "faults": self.active_faults}


def replace_override(override):
    return override.model_copy(update={"repeats": override.repeats - 1})
