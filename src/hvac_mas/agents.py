"""SPADE roles and their message handlers. Exchanges cross the XMPP socket."""
import asyncio
import json
from collections import OrderedDict
from datetime import UTC, datetime
import logging
from itertools import combinations
from math import ceil
from time import monotonic
from uuid import uuid4

LOG = logging.getLogger(__name__)

from spade.agent import Agent
from spade.behaviour import CyclicBehaviour, FSMBehaviour, State
from spade.message import Message
from spade.template import Template

from hvac_mas.control import allocate, total_power
from hvac_mas.diagnostics import Diagnostics
from hvac_mas.models.building import Mode, Thermostat, f_to_c
from hvac_mas.models.telemetry import Observation
from hvac_mas.simulation.equipment import EquipmentState, output
from hvac_mas.simulation.thermostat import request_mode


class Mailbox(CyclicBehaviour):
    async def run(self):
        msg = await self.receive(timeout=1)
        if msg is None:
            return
        try:
            body = json.loads(msg.body)
            sender = str(msg.sender.bare)
            job = msg.get_metadata("conversation-id")
            identity = msg.get_metadata("message-id")
            if not identity or not job or sender not in self.agent.peers:
                return
            if identity in self.agent.seen:
                return
            self.agent.last_message_at = datetime.now(UTC)
            self.agent.seen[identity] = None
            if len(self.agent.seen) > 4096:
                self.agent.seen.popitem(last=False)
            received = {
                "traceId": f"{identity}:received", "messageId": identity, "conversationId": job,
                "sender": sender, "receiver": str(self.agent.jid),
                "protocol": msg.get_metadata("protocol"), "performative": msg.get_metadata("performative"),
                "command": body["command"], "payload": body["payload"], "status": "received",
                "replyTo": body.get("reply_to"), "delay_ms": max(0, (monotonic() - body["sent"]) * 1000),
            }
            self.agent.trace.setdefault(job, []).append(received)
            if body.get("reply_to"):
                waiting = self.agent.waiting.get(body["reply_to"])
                if waiting:
                    expected, future = waiting
                    if sender == expected and not future.done():
                        future.set_result(body)
                        self.agent.trace[job].append(received | {
                            "traceId": f"{identity}:processed", "status": "processed"})
            else:
                try:
                    performative, payload = self.agent.handle(body["command"], body["payload"], job, sender)
                except (ValueError, KeyError, TypeError) as exc:
                    performative, payload = "failure", {"reason": str(exc), "error": True}
                self.agent.trace[job].append(received | {
                    "traceId": f"{identity}:processed" if not payload.get("error") else f"{identity}:failed",
                    "status": "processed" if not payload.get("error") else "failed",
                    "processingResult": payload.get("reason") if payload.get("error") else None})
                await self.agent.send_wire(sender, job, body["command"], payload, performative,
                                           reply_to=identity, protocol=msg.get_metadata("protocol"))
        except (AttributeError, TypeError, KeyError, ValueError):
            return


class BaseAgent(Agent):
    def __init__(self, identity, settings, password, peers, trace):
        super().__init__(identity, password, port=settings.xmpp_port, verify_security=not settings.xmpp_embedded)
        self.peers, self.trace = peers, trace
        self.reply_timeout = settings.agent_reply_timeout
        self.last_message_at = None
        self.seen, self.waiting = OrderedDict(), {}

    async def setup(self):
        self.mail = Mailbox()
        template = Template(metadata={"ontology": "hvac-mas"})
        self.add_behaviour(self.mail, template)

    async def send_wire(self, to, job, command, payload, performative, *, identity=None, reply_to=None, protocol=None):
        msg = Message(to=to, sender=str(self.jid))
        meta = {"ontology": "hvac-mas", "performative": performative, "protocol": protocol or "hvac-control",
                "conversation-id": job, "message-id": identity or uuid4().hex}
        for key, value in meta.items():
            msg.set_metadata(key, value)
        msg.body = json.dumps({"command": command, "payload": payload, "reply_to": reply_to, "sent": monotonic()})
        # SPADE's default send short-circuits when both agents are in the same process.
        await self.mail._xmpp_send(msg)
        self.last_message_at = datetime.now(UTC)
        LOG.debug("MESSAGE_SENT agent=%s to=%s job=%s command=%s", self.jid, to, job, command)
        self.trace.setdefault(job, []).append({
            "traceId": f"{meta['message-id']}:sent", "messageId": meta["message-id"],
            "conversationId": job, "sender": str(self.jid), "receiver": to,
            "protocol": meta["protocol"], "performative": performative,
            "command": command, "payload": payload, "replyTo": reply_to, "status": "sent",
        })

    async def ask(self, to, job, command, payload, *, performative="request", protocol="hvac-control"):
        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.waiting[key] = (to, future)
        try:
            await self.send_wire(to, job, command, payload, performative, identity=key, protocol=protocol)
            response = await asyncio.wait_for(future, timeout=self.reply_timeout)
            if response["payload"].get("error"):
                raise ValueError(response["payload"]["reason"])
            return response["payload"]
        finally:
            self.waiting.pop(key, None)

    def handle(self, command, payload, job, sender):
        if command == "ping":
            return "inform", {"alive": self.is_alive()}
        raise ValueError(f"unsupported command {command}")


class UnitAgent(BaseAgent):
    def __init__(self, *args, spec, zone, rules):
        super().__init__(*args)
        self.spec, self.zone, self.rules = spec, zone, rules
        self.current = None
        self.run_id = None
        self.last_reading = None
        self.waiting_steps = 0
        self.previous_mode = Mode.OFF
        self.diagnostic = set()
        self.offer = None

    def needs_resource_negotiation(self, allocation, feasible):
        """Decide locally whether the approved stage leaves useful comfort unmet."""
        if not self.current:
            return False
        preferred = self.current["choices"][0]
        allocated = allocation["modes"].get(self.spec.unit_id)
        if allocated is None or preferred == allocated or preferred not in feasible[self.spec.unit_id]:
            return False
        if feasible[self.spec.unit_id][preferred]["power"] <= feasible[self.spec.unit_id][allocated]["power"]:
            return False
        estimates = self.current.get("predicted_error_c", {})
        return estimates.get(preferred, float("inf")) + 1e-9 < estimates.get(allocated, float("inf"))

    def initiate_negotiation(self, job, power, allocation, feasible, unit_jids, rounds, limit,
                             intents, step):
        if not self.needs_resource_negotiation(allocation, feasible):
            return None
        fsm = Negotiation(job, power, allocation, feasible, unit_jids, rounds,
                          self.current["priority"], limit, intents=intents, step=step)
        self.add_behaviour(fsm, Template(metadata={"ontology": "hvac-fsm-only"}))
        return fsm

    def find_intent(self, packet, feasible, forecast, seconds, area_weight, neighbor_temperatures=None):
        p = packet.data
        occupied_policy = p.occupancyState in {"OCCUPIED", "VACANCY_PENDING", "PRECONDITIONING"}
        configured_fan = (self.zone.occupancy_policy.occupied_fan_policy if occupied_policy
                          else self.zone.occupancy_policy.unoccupied_fan_policy)
        thermostat = Thermostat(thermostat_id=packet.thermostatId, heat_setpoint_c=f_to_c(p.heatSetpoint),
                                cool_setpoint_c=f_to_c(p.coolSetpoint),
                                fan_policy="on" if configured_fan == "continuous" else "auto")
        temp = f_to_c(p.avgTemp)
        desired = request_mode(thermostat, temp, p.mode, rules=self.rules)
        heat_error = thermostat.heat_setpoint_c - temp
        if Mode.HEAT2 in feasible and (heat_error >= self.rules.heating_high_stage_delta_c or
                                       (p.mode == Mode.HEAT2 and heat_error > self.rules.heating_high_stage_release_delta_c)):
            desired = Mode.HEAT2
        occupancy_ratio = self.zone.occupancy_policy.occupancy_ratio(p.occupancy)
        occupied = 0.25 if p.occupancy == 0 else 1 + min(occupancy_ratio, 2.0) / 2
        heat_error = max(0, thermostat.heat_setpoint_c - temp)
        cool_error = max(0, temp - thermostat.cool_setpoint_c)
        need = max(heat_error, cool_error)
        direction = str(desired)[:4]
        candidates = [mode for mode in feasible if mode in (Mode.OFF, Mode.FAN) or str(mode).startswith(direction)]
        if not candidates:
            candidates = list(feasible)
        utility, discomfort, predicted_temperatures = {}, {}, {}
        horizon = self.rules.prediction_horizon_minutes * 60
        steps = max(1, ceil(horizon / seconds))
        dt = horizon / steps
        expected_occupancy = p.expectedOccupancy if p.expectedOccupancy is not None else p.occupancy
        for mode in candidates:
            health = EquipmentState(actual_mode=Mode(mode),
                                    cooling_health=0.35 if "COOLING_FDD" in self.diagnostic else 1,
                                    heating_health=0.35 if "HEATING_FDD" in self.diagnostic else 1)
            predicted = temp
            for index in range(steps):
                heat = output(self.spec, health, predicted, forecast)[0]
                predicted_occupancy = p.occupancy + (expected_occupancy - p.occupancy) * (index + 0.5) / steps
                gains = (self.zone.outdoor_conductance_kw_per_c * (forecast - predicted)
                         + self.zone.base_load_kw + self.zone.occupant_gain_kw * predicted_occupancy)
                gains += sum(item["conductance_kw_per_c"]
                             * (item["temperature_c"] - predicted)
                             for item in (neighbor_temperatures or {}).values())
                predicted += (gains + heat) * dt / 3600 / self.zone.capacitance_kwh_per_c
            error = max(0, predicted - thermostat.cool_setpoint_c, thermostat.heat_setpoint_c - predicted)
            discomfort[mode] = error
            predicted_temperatures[mode] = predicted
            utility[mode] = -occupied * error - 0.012 * feasible[mode]["power"] - 0.015 * (mode != p.mode)
        lower = {Mode.COOL2: Mode.COOL1, Mode.HEAT2: Mode.HEAT1}.get(desired, Mode.OFF)
        allowed = [m for m in dict.fromkeys([desired, lower, Mode.FAN, Mode.OFF, p.mode]) if m in utility]
        continuous_fan = configured_fan == "continuous"
        if continuous_fan:
            allowed = [mode for mode in allowed if mode != Mode.OFF]
        ordered = sorted(allowed if allowed else ([] if continuous_fan else list(utility)),
                         key=lambda mode: (-utility[mode], mode))
        if desired in ordered and (desired != Mode.OFF or p.mode == Mode.OFF):
            ordered.remove(desired)
            ordered.insert(0, desired)
        if desired == Mode.OFF and Mode.OFF in utility:
            ordered.remove(Mode.OFF) if Mode.OFF in ordered else None
            ordered.insert(0, Mode.OFF)
        if need <= self.rules.comfort_band_c:
            self.waiting_steps = 0
        trend = p.avgTemp - self.last_reading if self.last_reading is not None else 0
        self.last_reading = p.avgTemp
        self.previous_mode = p.mode
        urgency = occupied * area_weight * need + 0.08 * min(self.waiting_steps, 20)
        urgency += min(0.3, max(0, trend if cool_error else -trend)) if need else 0
        if continuous_fan:
            minimum = (Mode.FAN if Mode.FAN in feasible else
                       p.mode if p.mode != Mode.OFF and p.mode in feasible else Mode.FAN)
        else:
            minimum = Mode.OFF if Mode.OFF in feasible else min(
                feasible, key=lambda mode: (feasible[mode]["power"], mode))
        if not ordered:
            fallback_mode = p.mode if p.mode in feasible else min(
                feasible, key=lambda mode: (feasible[mode]["power"], mode))
            ordered = [fallback_mode]
        reason_codes = []
        if occupancy_ratio > self.zone.occupancy_policy.overload_warning_ratio:
            reason_codes.append("OCCUPANCY_ABOVE_DESIGN")
        if p.occupancy == 0:
            reason_codes.append("ZONE_UNOCCUPIED")
        if expected_occupancy != p.occupancy:
            reason_codes.append("SCHEDULED_OCCUPANCY_CHANGE")
        if cool_error > self.rules.comfort_band_c:
            reason_codes.append("COOLING_DEMAND")
        elif heat_error > self.rules.comfort_band_c:
            reason_codes.append("HEATING_DEMAND")
        else:
            reason_codes.append("WITHIN_EFFECTIVE_COMFORT_RANGE")
        if desired in (Mode.COOL2, Mode.HEAT2):
            reason_codes.append("HIGH_STAGE_THERMAL_DEMAND")
        alternatives = [{"mode": mode.value, "powerKw": feasible[mode]["power"],
                         "predictedTemperatureF": round(predicted_temperatures[mode] * 9 / 5 + 32, 3),
                         "predictedDiscomfortC": round(discomfort[mode], 4),
                         "utility": round(utility[mode], 5)} for mode in ordered if mode in utility]
        return {"choices": ordered, "minimum_modes": [minimum], "priority": urgency,
                "utility": utility, "predicted_error_c": discomfort,
                "observed_f": p.avgTemp, "packetId": packet.packetId, "desired": desired,
                "unitId": self.spec.unit_id, "occupancy": p.occupancy,
                "designOccupancy": self.zone.occupancy_policy.design_occupancy,
                "occupancyRatio": round(occupancy_ratio, 3),
                "overDesignOccupancy": occupancy_ratio > self.zone.occupancy_policy.overload_warning_ratio,
                "expectedOccupancy": expected_occupancy,
                "occupancyForecastMinutes": p.occupancyForecastMinutes or self.rules.prediction_horizon_minutes,
                "effectiveHeatingTargetF": p.heatSetpoint, "effectiveCoolingTargetF": p.coolSetpoint,
                "reasonCodes": reason_codes, "alternatives": alternatives,
                "predictionHorizonMinutes": self.rules.prediction_horizon_minutes,
                "needsConditioning": need > self.rules.comfort_band_c}

    def handle(self, command, payload, job, sender):
        if command == "observe":
            packet = Observation.model_validate(payload["packet"])
            if packet.unitId != self.spec.unit_id:
                raise ValueError("observation for another unit")
            if self.run_id != packet.runId:
                self.run_id, self.last_reading, self.waiting_steps = packet.runId, None, 0
                self.previous_mode, self.diagnostic = Mode.OFF, set()
            self.offer = None
            self.current = self.find_intent(packet, payload["feasible"], payload["forecast"],
                                            payload["seconds"], payload["area_weight"],
                                            payload.get("neighbor_temperatures"))
            return "request", self.current
        if command == "diagnosis":
            if payload["status"] == "CONFIRMED" and payload["kind"].endswith("FDD"):
                self.diagnostic.add(payload["kind"])
            elif payload["status"] == "RESOLVED":
                self.diagnostic.discard(payload["kind"])
            return "inform", {"acknowledged": True}
        if command == "offer":
            if not self.current or self.offer:
                return "reject-proposal", {"reason": "no available local concession"}
            proposed, current = payload["donor_mode"], payload["current_mode"]
            utility = self.current["utility"]
            error = self.current["predicted_error_c"]
            if current not in utility:
                return "reject-proposal", {"reason": "no current comfort forecast"}
            available = [m for m in utility if m != current and m in payload["lower_modes"] and
                         error[m] < max(4.0, error[current] + 0.38) and
                         utility[current] - utility[m] <= 0.25 + 0.10 * payload["benefit"] and
                         payload["priority"] > self.current["priority"] - 0.65]
            if available:
                preferred = max(available, key=lambda mode: (utility[mode], -error[mode]))
                loss = utility[current] - utility[preferred]
                if proposed == preferred:
                    self.offer = (job, sender, proposed)
                    return "propose", {"mode": proposed, "loss": loss}
                return "counter-propose", {"mode": preferred, "counter": True, "loss": loss}
            return "reject-proposal", {"reason": "comfort or equipment constraint"}
        if command == "accept":
            valid = self.offer == (job, sender, payload["mode"])
            self.offer = None
            return ("confirm", {"confirmed": True}) if valid else ("failure", {"confirmed": False})
        if command == "cancel-offer":
            if self.offer and self.offer[:2] == (job, sender):
                self.offer = None
            return "inform", {"cancelled": True}
        if command == "committed":
            if (self.current and self.current.get("needsConditioning")
                    and self.current["predicted_error_c"].get(payload["mode"], float("inf"))
                    > self.rules.comfort_band_c):
                self.waiting_steps += 1
            else:
                self.waiting_steps = 0
            self.offer = None
            return "request", {"mode": payload["mode"], "unitId": self.spec.unit_id}
        return super().handle(command, payload, job, sender)


class AreaAgent(BaseAgent):
    def __init__(self, *args, area_id):
        super().__init__(*args)
        self.area_id = area_id
        self.latest = {}
        self.events = {}

    def handle(self, command, payload, job, sender):
        if command == "area-observe":
            if getattr(self, "run_id", None) != payload["runId"]:
                self.events.clear()
                self.run_id = payload["runId"]
            if any(p["areaId"] != self.area_id for p in payload["packets"]):
                raise ValueError("observation for another area")
            self.latest = {p["unitId"]: p for p in payload["packets"]}
            weights = {}
            for uid, row in self.latest.items():
                data = row["data"]
                ratio = data["occupancy"] / max(1, row["designOccupancy"])
                deviation = abs(data["avgTemp"] - data["setTemp"])
                weights[uid] = (1 + 0.10 * min(ratio, 2.0) + 0.05 * min(deviation / 2, 1.0)
                                + (0.12 if self.events.get(uid, {}).get("status") == "CONFIRMED" else 0))
            return "inform", {"weights": weights, "zones_reporting": len(self.latest),
                              "assessment": self._assessment(), "diagnostics": self.events}
        if command == "area-assess":
            if any(uid not in self.latest for uid in payload["intents"]):
                raise ValueError("area assessment contains a unit outside this area's observations")
            assessment = self._assessment(payload["intents"], payload["feasible"])
            return "inform", assessment
        if command == "diagnosis":
            self.events[payload["unitId"]] = {"kind": payload["kind"], "status": payload["status"]}
            return "inform", {"acknowledged": True}
        return super().handle(command, payload, job, sender)

    def _assessment(self, intents=None, feasible=None):
        rows = self.latest.values()
        people = sum(row["data"]["occupancy"] for row in rows)
        design = sum(row["designOccupancy"] for row in rows)
        deviations = [(row["data"]["occupancy"], abs(row["data"]["avgTemp"] - row["data"]["setTemp"]))
                      for row in rows]
        total_people = sum(count for count, _ in deviations)
        weighted = (sum(count * value for count, value in deviations) / total_people
                    if total_people else max((value for _, value in deviations), default=0.0))
        result = {"areaId": self.area_id, "zonesReporting": len(self.latest),
                  "actualOccupancy": people, "designOccupancy": design,
                  "occupancyRatio": round(people / design, 3) if design else 0.0,
                  "overDesignZones": sorted(uid for uid, row in self.latest.items()
                                             if row["data"]["occupancy"] > row["designOccupancy"]),
                  "occupantWeightedDeviationF": round(weighted, 3),
                  "maximumDeviationF": round(max((value for _, value in deviations), default=0.0), 3),
                  "unitsAvailable": sum(bool(row.get("equipmentAvailable")) for row in rows),
                  "currentElectricalDemandKw": round(sum(
                      row.get("feasibleModes", {}).get(row["data"]["mode"], {}).get("power", 0.0)
                      for row in rows), 3),
                  "maximumFeasibleDemandKw": round(sum(
                      max((option["power"] for option in row.get("feasibleModes", {}).values()), default=0.0)
                      for row in rows), 3),
                  "confirmedDiagnosticUnits": sorted(uid for uid in self.latest
                        if self.events.get(uid, {}).get("status") == "CONFIRMED")}
        if intents is not None and feasible is not None:
            requested = {uid: intent["choices"][0] for uid, intent in intents.items() if uid in feasible}
            result["preferredElectricalDemandKw"] = round(sum(
                feasible[uid][mode]["power"] for uid, mode in requested.items()), 3)
            flexibility = {}
            for uid, intent in intents.items():
                if uid not in feasible or uid not in self.latest:
                    continue
                current = self.latest[uid]["data"]["mode"]
                current_power = feasible[uid].get(current, {}).get("power", 0.0)
                lower = [mode for mode in intent.get("choices", []) if mode in feasible[uid]
                         and feasible[uid][mode]["power"] < current_power]
                if lower:
                    preferred_offer = max(lower, key=lambda mode: (
                        intent.get("utility", {}).get(mode, 0), -feasible[uid][mode]["power"]))
                    flexibility[uid] = {"fromMode": str(current), "toMode": str(preferred_offer),
                                        "powerReleasedKw": round(current_power - feasible[uid][preferred_offer]["power"], 3),
                                        "occupancy": self.latest[uid]["data"]["occupancy"]}
            result["availableTemporaryConcessions"] = flexibility
            result["availableConcessionKw"] = round(sum(
                item["powerReleasedKw"] for item in flexibility.values()), 3)
            result["unitRequests"] = {uid: {"preferredMode": str(mode),
                    "occupancy": self.latest[uid]["data"]["occupancy"],
                    "designOccupancy": self.latest[uid]["designOccupancy"]}
                    for uid, mode in requested.items()}
        return result


class WeatherAgent(BaseAgent):
    def handle(self, command, payload, job, sender):
        if command == "weather":
            # Agent republishes the shared weather information with its forecast horizon.
            return "inform", payload | {"forecast_minutes": 60}
        return super().handle(command, payload, job, sender)


class DiagnosticAgent(BaseAgent):
    def __init__(self, *args, building, rules):
        super().__init__(*args)
        self.diagnostics = Diagnostics(building, rules)
        self.run_id = None

    def handle(self, command, payload, job, sender):
        if command == "diagnose":
            if self.run_id != payload["runId"]:
                self.run_id = payload["runId"]
                self.diagnostics.tracks.clear()
                self.diagnostics.latest.clear()
                self.diagnostics.samples.clear()
            return "inform", {"events": [event for row in payload["packets"]
                                         for event in self.diagnostics.observe(Observation.model_validate(row))]}
        return super().handle(command, payload, job, sender)


class PowerAgent(BaseAgent):
    def handle(self, command, payload, job, sender):
        if command == "reserve":
            self.job = job
            self.feasible, self.limit = payload["feasible"], payload["limit"]
            self.intents = payload["intents"]
            self.modes = allocate(self.feasible, payload["intents"], self.limit)
            self.version = 0
            self.run, self.step = payload["runId"], payload["simulationStep"]
            self.final = False
            return "confirm", self.allocation()
        if command == "commit-swap":
            if job != self.job or self.final or payload["version"] != self.version:
                return "failure", {"committed": False}
            requester, donor = payload["requester"], payload["donor"]
            if requester == donor:
                return "failure", {"committed": False}
            if payload["request_mode"] not in self.feasible.get(requester, {}) or payload["donor_mode"] not in self.feasible.get(donor, {}):
                return "failure", {"committed": False, "reason": "stage is not physically feasible"}
            if (self.modes.get(requester) != payload.get("requester_current_mode")
                    or self.modes.get(donor) != payload.get("current_mode")
                    or self.feasible[donor][payload["donor_mode"]]["power"]
                       >= self.feasible[donor][payload["current_mode"]]["power"]):
                return "failure", {"committed": False, "reason": "stale or non-concessional swap"}
            changes = {requester: payload["request_mode"], donor: payload["donor_mode"]}
            if (payload["request_mode"] not in self.intents.get(requester, {}).get("choices", [])
                    or payload["donor_mode"] not in self.intents.get(donor, {}).get("choices", [])):
                return "failure", {"committed": False, "reason": "stage is outside an agent-approved alternative"}
            proposal = self.modes | changes
            if any(mode not in self.feasible[uid] for uid, mode in proposal.items()):
                return "failure", {"committed": False, "reason": "stage is not physically feasible"}
            if total_power(proposal, self.feasible) > self.limit + 1e-9:
                return "failure", {"committed": False}
            self.modes, self.version = proposal, self.version + 1
            return "confirm", self.allocation() | {"committed": True}
        if command == "commit-swap-batch":
            if job != self.job or self.final or payload["version"] != self.version:
                return "failure", {"committed": False, "reason": "stale allocation version"}
            requester = payload["requester"]
            donors = payload["donors"]
            if (not donors or requester in {item["unit_id"] for item in donors}
                    or len({item["unit_id"] for item in donors}) != len(donors)):
                return "failure", {"committed": False, "reason": "invalid donor bundle"}
            changes = {requester: payload["request_mode"]}
            for item in donors:
                donor, mode = item["unit_id"], item["mode"]
                if mode not in self.feasible.get(donor, {}) or item.get("current_mode") not in self.feasible.get(donor, {}):
                    return "failure", {"committed": False, "reason": "bundle contains an infeasible stage"}
                if (self.modes.get(donor) != item.get("current_mode")
                        or self.feasible[donor][mode]["power"]
                           >= self.feasible[donor][item["current_mode"]]["power"]):
                    return "failure", {"committed": False, "reason": "stale or non-concessional donor"}
                if mode not in self.intents.get(donor, {}).get("choices", []):
                    return "failure", {"committed": False, "reason": "donor stage was not agent-approved"}
                changes[donor] = mode
            if payload["request_mode"] not in self.intents.get(requester, {}).get("choices", []):
                return "failure", {"committed": False, "reason": "requester stage was not agent-approved"}
            if self.modes.get(requester) != payload.get("requester_current_mode"):
                return "failure", {"committed": False, "reason": "requester allocation changed"}
            if any(mode not in self.feasible.get(uid, {}) for uid, mode in changes.items()):
                return "failure", {"committed": False, "reason": "bundle contains an infeasible stage"}
            proposal = self.modes | changes
            if total_power(proposal, self.feasible) > self.limit + 1e-9:
                return "failure", {"committed": False, "reason": "combined concessions do not meet the power constraint"}
            self.modes, self.version = proposal, self.version + 1
            return "confirm", self.allocation() | {"committed": True}
        if command == "finalize" and job == self.job:
            if total_power(self.modes, self.feasible) > self.limit + 1e-9:
                return "failure", {"reason": "final allocation exceeds the electrical limit"}
            self.final = True
            return "confirm", self.allocation() | {"reservations": [
                {"reservationId": f"{job}-{uid}", "runId": self.run, "unitId": uid, "simulationStep": self.step,
                 "approvedStage": stage, "committedPowerKw": self.feasible[uid][stage]["power"],
                 "validFromStep": self.step, "validUntilStep": self.step + 1, "status": "committed"}
                for uid, stage in self.modes.items()]}
        return super().handle(command, payload, job, sender)

    def allocation(self):
        return {"modes": self.modes, "version": self.version, "kw": total_power(self.modes, self.feasible)}


class GatewayAgent(BaseAgent):
    pass


class Negotiation(FSMBehaviour):
    """A step-scoped bilateral exchange initiated by a unit needing more power."""
    def __init__(self, job, power, allocation, feasible, unit_jids, rounds, priority, limit,
                 *, intents=None, step=0):
        super().__init__()
        self.job, self.power, self.allocation, self.feasible = job, power, allocation, feasible
        self.unit_jids, self.rounds, self.priority, self.limit = unit_jids, rounds, priority, limit
        self.intents, self.step = intents or {}, step
        self.negotiation_id = uuid4().hex
        self.conversation_id = f"{job}:{self.negotiation_id}"
        self.events = []
        self.started_at = monotonic()
        self.attempted = 0
        self.result = None
        self.options = []
        self.choice = None
        for name, cls in (("DISCOVERING", Discover), ("PROPOSING", Propose), ("EVALUATING", Evaluate),
                          ("COUNTERING", Counter), ("AGREEING", Agree), ("COMMITTING", Commit),
                          ("COMPLETED", Finish), ("FAILED", Finish)):
            state = cls()
            state.fsm = self
            self.add_state(name, state, initial=name == "DISCOVERING")
        for start, end in (("DISCOVERING", "PROPOSING"), ("DISCOVERING", "FAILED"),
                           ("PROPOSING", "EVALUATING"), ("PROPOSING", "FAILED"),
                           ("EVALUATING", "COUNTERING"), ("EVALUATING", "AGREEING"),
                           ("EVALUATING", "DISCOVERING"), ("COUNTERING", "PROPOSING"),
                           ("COUNTERING", "FAILED"), ("AGREEING", "COMMITTING"),
                           ("AGREEING", "FAILED"), ("COMMITTING", "COMPLETED"),
                           ("COMMITTING", "FAILED")):
            self.add_transition(start, end)

    def record(self, status, **detail):
        self.events.append({"kind": "NEGOTIATION", "status": status, "unitId": self.agent.spec.unit_id,
                            "negotiationId": self.negotiation_id, "conversationId": self.conversation_id,
                            "simulationStep": self.step, "round": self.attempted,
                            "duration_ms": (monotonic() - self.started_at) * 1000, **detail})

    @property
    def receiver(self):
        return self.unit_jids[self.choice["donor"]]

    async def on_end(self):
        if not self.events:
            self.record("FAILED", reason="negotiation ended before reaching a terminal state")

    async def ask_peer(self, command, payload, performative):
        return await self.agent.ask(self.receiver, self.conversation_id, command, payload,
                                    performative=performative, protocol="hvac-bargaining")


class Discover(State):
    async def run(self):
        f = self.fsm
        uid = self.agent.spec.unit_id
        if f.attempted == 0 and not f.options:
            desired = self.agent.current["choices"][0]
            modes = f.allocation["modes"]
            shortage = max(0, f.allocation["kw"] - f.limit + f.feasible[uid][desired]["power"]
                           - f.feasible[uid][modes[uid]]["power"])
            benefit = self.agent.current["predicted_error_c"].get(modes[uid], 0) -                 self.agent.current["predicted_error_c"][desired]
            concessions = []
            donors = [donor for donor in sorted(modes) if donor != uid and donor in f.unit_jids]
            if donors:
                shift = f.step % len(donors)
                donors = donors[shift:] + donors[:shift]
            for donor in donors:
                current = modes[donor]
                if donor == uid or current == Mode.OFF or donor not in f.unit_jids:
                    continue
                local_options = f.intents.get(donor, {}).get("choices", list(f.feasible[donor]))
                viable = [mode for mode in local_options if mode in f.feasible[donor]
                          and mode != current
                          and f.feasible[donor][mode]["power"] < f.feasible[donor][current]["power"]]
                if viable:
                    mode = min(viable, key=lambda m: (
                        f.intents.get(donor, {}).get("utility", {}).get(current, 0)
                        - f.intents.get(donor, {}).get("utility", {}).get(m, 0),
                        -f.feasible[donor][current]["power"] + f.feasible[donor][m]["power"], str(m)))
                    donor_intent = f.intents.get(donor, {})
                    terms = {"requester": uid, "request_mode": desired,
                             "requester_current_mode": modes[uid], "donor": donor, "donor_mode": mode,
                             "current_mode": current, "lower_modes": viable, "benefit": max(0, benefit),
                             "priority": f.priority, "version": f.allocation["version"],
                             "requester_occupancy": self.agent.current["occupancy"],
                             "requester_design_occupancy": self.agent.current["designOccupancy"],
                             "donor_occupancy": donor_intent.get("occupancy", 0),
                             "donor_design_occupancy": donor_intent.get("designOccupancy", 0),
                             "agreement_steps": 1}
                    bundle_mode = min(viable, key=lambda candidate: (
                        -(f.feasible[donor][current]["power"] - f.feasible[donor][candidate]["power"]),
                        f.intents.get(donor, {}).get("utility", {}).get(current, 0)
                        - f.intents.get(donor, {}).get("utility", {}).get(candidate, 0)))
                    release = f.feasible[donor][current]["power"] - f.feasible[donor][bundle_mode]["power"]
                    concessions.append((donor, current, bundle_mode, viable, release, terms))
                    if release >= shortage - 1e-9:
                        direct_modes = [candidate for candidate in viable
                            if f.feasible[donor][current]["power"] - f.feasible[donor][candidate]["power"]
                               >= shortage - 1e-9]
                        direct_mode = min(direct_modes, key=lambda candidate: (
                            f.intents.get(donor, {}).get("utility", {}).get(current, 0)
                            - f.intents.get(donor, {}).get("utility", {}).get(candidate, 0),
                            f.feasible[donor][candidate]["power"], str(candidate)))
                        f.options.append(terms | {"donor_mode": direct_mode, "lower_modes": direct_modes})
            if shortage > 0:
                bundles = []
                for size in range(2, len(concessions) + 1):
                    for group in combinations(concessions, size):
                        if sum(item[4] for item in group) + 1e-9 < shortage:
                            continue
                        donors_for_bundle = [{"unit_id": item[0], "current_mode": item[1],
                            "mode": item[2], "lower_modes": [candidate for candidate in item[3]
                                if f.feasible[item[0]][item[1]]["power"] - f.feasible[item[0]][candidate]["power"]
                                   + sum(other[4] for other in group if other[0] != item[0]) >= shortage - 1e-9],
                            "occupancy": item[5]["donor_occupancy"],
                            "design_occupancy": item[5]["donor_design_occupancy"]} for item in group]
                        loss = sum(f.intents.get(item[0], {}).get("utility", {}).get(item[1], 0)
                                   - f.intents.get(item[0], {}).get("utility", {}).get(item[2], 0)
                                   for item in group)
                        bundles.append((size, loss, {"requester": uid, "request_mode": desired,
                            "requester_current_mode": modes[uid],
                            "donors": donors_for_bundle, "priority": f.priority,
                            "version": f.allocation["version"], "benefit": max(0, benefit),
                            "requester_occupancy": self.agent.current["occupancy"],
                            "requester_design_occupancy": self.agent.current["designOccupancy"],
                            "agreement_steps": 1}))
                bundles.sort(key=lambda row: (row[0], row[1], [item["unit_id"] for item in row[2]["donors"]]))
                if bundles:
                    direct = list(f.options)
                    direct_slots = max(1, f.rounds - 1)
                    f.options = direct[:direct_slots] + [bundles[0][2]] + direct[direct_slots:]
        if f.attempted < f.rounds and f.options:
            f.choice = f.options.pop(0)
            self.set_next_state("PROPOSING")
        else:
            self.set_next_state("FAILED")


class Propose(State):
    async def run(self):
        f = self.fsm
        f.attempted += 1
        try:
            if "donors" not in f.choice:
                f.reply = await f.ask_peer("offer", f.choice,
                                           "cfp" if f.attempted == 1 else "propose")
            else:
                accepted = []
                for donor in f.choice["donors"]:
                    terms = f.choice | {"donor": donor["unit_id"], "donor_mode": donor["mode"],
                                        "current_mode": donor["current_mode"],
                                        "lower_modes": donor["lower_modes"],
                                        "donor_occupancy": donor["occupancy"],
                                        "donor_design_occupancy": donor["design_occupancy"]}
                    response = await f.agent.ask(f.unit_jids[donor["unit_id"]], f.conversation_id,
                        "offer", terms, performative="cfp" if f.attempted == 1 else "propose",
                        protocol="hvac-bargaining")
                    if response.get("counter") and response.get("mode") in donor["lower_modes"]:
                        donor["mode"] = response["mode"]
                        terms["donor_mode"] = donor["mode"]
                        response = await f.agent.ask(f.unit_jids[donor["unit_id"]], f.conversation_id,
                            "offer", terms, performative="propose", protocol="hvac-bargaining")
                    if "mode" not in response or response.get("mode") != donor["mode"]:
                        raise ValueError("one or more donors rejected the combined proposal")
                    accepted.append(donor)
                f.bundle_terms = accepted
                f.reply = {"mode": f.choice["request_mode"], "bundle": True}
            self.set_next_state("EVALUATING")
        except (TimeoutError, ValueError):
            if "donors" in f.choice:
                await asyncio.gather(*(f.agent.ask(f.unit_jids[donor["unit_id"]], f.conversation_id,
                    "cancel-offer", {}, performative="cancel", protocol="hvac-bargaining")
                    for donor in f.choice["donors"]), return_exceptions=True)
                self.set_next_state("DISCOVERING")
            else:
                self.set_next_state("FAILED")


class Evaluate(State):
    async def run(self):
        f = self.fsm
        if f.reply.get("bundle") and len(f.bundle_terms) == len(f.choice["donors"]):
            self.set_next_state("AGREEING")
        elif f.reply.get("counter") and f.reply.get("mode") in f.choice["lower_modes"]:
            self.set_next_state("COUNTERING")
        elif "mode" in f.reply:
            self.set_next_state("AGREEING")
        else:
            self.set_next_state("DISCOVERING")


class Counter(State):
    async def run(self):
        f = self.fsm
        if f.attempted < f.rounds:
            f.choice["donor_mode"] = f.reply["mode"]
            self.set_next_state("PROPOSING")
        else:
            self.set_next_state("FAILED")


class Agree(State):
    async def run(self):
        f = self.fsm
        try:
            if "donors" in f.choice:
                responses = await asyncio.gather(*(f.agent.ask(f.unit_jids[donor["unit_id"]], f.conversation_id,
                    "accept", {"mode": donor["mode"]}, performative="accept-proposal",
                    protocol="hvac-bargaining") for donor in f.choice["donors"]))
                valid = all(response.get("confirmed") for response in responses)
                if not valid:
                    await asyncio.gather(*(f.agent.ask(f.unit_jids[donor["unit_id"]], f.conversation_id,
                        "cancel-offer", {}, performative="cancel", protocol="hvac-bargaining")
                        for donor in f.choice["donors"]), return_exceptions=True)
                self.set_next_state("COMMITTING" if valid else "FAILED")
            else:
                response = await f.ask_peer("accept", {"mode": f.reply["mode"]}, "accept-proposal")
                self.set_next_state("COMMITTING" if response.get("confirmed") else "FAILED")
        except TimeoutError:
            self.set_next_state("FAILED")


class Commit(State):
    async def run(self):
        f = self.fsm
        try:
            if "donors" in f.choice:
                result = await self.agent.ask(f.power, f.job, "commit-swap-batch", {
                    "requester": f.choice["requester"], "request_mode": f.choice["request_mode"],
                    "requester_current_mode": f.choice["requester_current_mode"],
                    "donors": [{"unit_id": donor["unit_id"], "mode": donor["mode"],
                                "current_mode": donor["current_mode"]}
                               for donor in f.choice["donors"]],
                    "version": f.choice["version"]}, protocol="hvac-bargaining", performative="confirm")
                terms = f.choice | {"donor": f.choice["donors"][0]["unit_id"],
                    "donors": [{"unit_id": donor["unit_id"], "mode": donor["mode"]}
                               for donor in f.choice["donors"]]}
            else:
                result = await self.agent.ask(f.power, f.job, "commit-swap", f.choice,
                                              protocol="hvac-bargaining", performative="confirm")
                terms = f.choice
            if result.get("committed"):
                f.result = result
                f.terms = terms
            self.set_next_state("COMPLETED" if f.result else "FAILED")
        except TimeoutError:
            self.set_next_state("FAILED")


class Finish(State):
    async def run(self):
        f = self.fsm
        f.record("COMPLETED" if f.result else "FAILED", terms=getattr(f, "terms", f.choice))
