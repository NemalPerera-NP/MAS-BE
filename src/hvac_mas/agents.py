"""SPADE roles and their message handlers. Exchanges cross the XMPP socket."""
import asyncio
import json
from collections import OrderedDict
from time import monotonic
from uuid import uuid4

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
            self.agent.seen[identity] = None
            if len(self.agent.seen) > 4096:
                self.agent.seen.popitem(last=False)
            self.agent.trace.setdefault(job, []).append({
                "messageId": identity, "conversationId": job, "sender": sender, "receiver": str(self.agent.jid),
                "protocol": msg.get_metadata("protocol"), "performative": msg.get_metadata("performative"),
                "command": body["command"], "payload": body["payload"], "status": "received",
                "replyTo": body.get("reply_to"), "delay_ms": max(0, (monotonic() - body["sent"]) * 1000),
            })
            if body.get("reply_to"):
                waiting = self.agent.waiting.get(body["reply_to"])
                if waiting:
                    expected, future = waiting
                    if sender == expected and not future.done():
                        future.set_result(body)
            else:
                try:
                    performative, payload = self.agent.handle(body["command"], body["payload"], job, sender)
                except (ValueError, KeyError, TypeError) as exc:
                    performative, payload = "failure", {"reason": str(exc), "error": True}
                await self.agent.send_wire(sender, job, body["command"], payload, performative,
                                           reply_to=identity, protocol=msg.get_metadata("protocol"))
        except (AttributeError, TypeError, KeyError, ValueError):
            return


class BaseAgent(Agent):
    def __init__(self, identity, settings, password, peers, trace):
        super().__init__(identity, password, port=settings.xmpp_port, verify_security=not settings.xmpp_embedded)
        self.peers, self.trace = peers, trace
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

    async def ask(self, to, job, command, payload, *, performative="request", protocol="hvac-control"):
        key = uuid4().hex
        future = asyncio.get_running_loop().create_future()
        self.waiting[key] = (to, future)
        try:
            await self.send_wire(to, job, command, payload, performative, identity=key, protocol=protocol)
            response = await asyncio.wait_for(future, timeout=1.2)
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

    def find_intent(self, packet, feasible, forecast, seconds, area_weight):
        p = packet.data
        thermostat = Thermostat(thermostat_id=packet.thermostatId, heat_setpoint_c=f_to_c(p.heatSetpoint),
                                cool_setpoint_c=f_to_c(p.coolSetpoint), fan_policy=self.zone.thermostat.fan_policy)
        temp = f_to_c(p.avgTemp)
        desired = request_mode(thermostat, temp, p.mode, hysteresis_c=self.rules.hysteresis_c,
                               high_stage_error_c=self.rules.high_stage_error_c, occupied=p.occupancy > 0)
        if desired == Mode.HEAT1 and Mode.HEAT2 in feasible and thermostat.heat_setpoint_c - temp > self.rules.high_stage_error_c:
            desired = Mode.HEAT2
        if self.previous_mode != p.mode and p.mode != Mode.OFF:
            self.waiting_steps = 0
        occupied = 1 + min(p.occupancy, 25) / 30
        heat_error = max(0, thermostat.heat_setpoint_c - temp)
        cool_error = max(0, temp - thermostat.cool_setpoint_c)
        need = max(heat_error, cool_error)
        direction = str(desired)[:4]
        candidates = [mode for mode in feasible if mode in (Mode.OFF, Mode.FAN) or str(mode).startswith(direction)]
        if not candidates:
            candidates = list(feasible)
        gains = self.zone.outdoor_conductance_kw_per_c * (forecast - temp) + self.zone.base_load_kw
        gains += self.zone.occupant_gain_kw * p.occupancy
        utility, discomfort = {}, {}
        for mode in candidates:
            health = EquipmentState(actual_mode=Mode(mode),
                                    cooling_health=0.35 if "COOLING_FDD" in self.diagnostic else 1,
                                    heating_health=0.35 if "HEATING_FDD" in self.diagnostic else 1)
            heat = output(self.spec, health, temp, forecast)[0]
            predicted = temp + (gains + heat) * seconds / 3600 / self.zone.capacitance_kwh_per_c
            error = max(0, predicted - thermostat.cool_setpoint_c, thermostat.heat_setpoint_c - predicted)
            discomfort[mode] = error
            utility[mode] = -occupied * error - 0.012 * feasible[mode]["power"] - 0.015 * (mode != p.mode)
        lower = {Mode.COOL2: Mode.COOL1, Mode.HEAT2: Mode.HEAT1}.get(desired, Mode.OFF)
        allowed = [m for m in dict.fromkeys([desired, lower, Mode.OFF, p.mode]) if m in utility]
        ordered = sorted(allowed or list(utility), key=lambda mode: (-utility[mode], mode))
        if desired in ordered and (desired != Mode.OFF or p.mode == Mode.OFF):
            ordered.remove(desired)
            ordered.insert(0, desired)
        if desired == Mode.OFF and Mode.OFF in utility:
            ordered = [Mode.OFF]
        trend = p.avgTemp - self.last_reading if self.last_reading is not None else 0
        self.last_reading = p.avgTemp
        self.previous_mode = p.mode
        urgency = occupied * area_weight * need + 0.08 * min(self.waiting_steps, 20)
        urgency += min(0.3, max(0, trend if cool_error else -trend)) if need else 0
        return {"choices": ordered, "priority": urgency, "utility": utility, "predicted_error_c": discomfort,
                "observed_f": p.avgTemp, "packetId": packet.packetId, "desired": desired}

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
                                            payload["seconds"], payload["area_weight"])
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
        if command == "committed":
            if self.current and payload["mode"] != self.current["choices"][0]:
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
            self.latest = {p["unitId"]: p["data"] for p in payload["packets"]}
            return "inform", {"weights": {uid: 1 + min(data["occupancy"], 25) / 250 +
                                           (0.12 if self.events.get(uid, {}).get("status") == "CONFIRMED" else 0)
                                           for uid, data in self.latest.items()},
                              "zones_reporting": len(self.latest), "diagnostics": self.events}
        if command == "diagnosis":
            self.events[payload["unitId"]] = {"kind": payload["kind"], "status": payload["status"]}
            return "inform", {"acknowledged": True}
        return super().handle(command, payload, job, sender)


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
            changes = {requester: payload["request_mode"], donor: payload["donor_mode"]}
            proposal = self.modes | changes
            if total_power(proposal, self.feasible) > self.limit + 1e-9:
                return "failure", {"committed": False}
            self.modes, self.version = proposal, self.version + 1
            return "confirm", self.allocation() | {"committed": True}
        if command == "finalize" and job == self.job:
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
    def __init__(self, job, power, allocation, feasible, unit_jids, rounds, priority, limit):
        super().__init__()
        self.job, self.power, self.allocation, self.feasible = job, power, allocation, feasible
        self.unit_jids, self.rounds, self.priority, self.limit = unit_jids, rounds, priority, limit
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
                            "conversationId": self.job, "round": self.attempted,
                            "duration_ms": (monotonic() - self.started_at) * 1000, **detail})

    @property
    def receiver(self):
        return self.unit_jids[self.choice["donor"]]

    async def on_end(self):
        if not self.events:
            self.record("FAILED", reason="negotiation ended before reaching a terminal state")

    async def ask_peer(self, command, payload, performative):
        return await self.agent.ask(self.receiver, self.job, command, payload,
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
            for donor, current in sorted(modes.items()):
                if donor == uid or current == Mode.OFF or donor not in f.unit_jids:
                    continue
                viable = [mode for mode, option in f.feasible[donor].items()
                          if option["power"] < f.feasible[donor][current]["power"]
                          and f.feasible[donor][current]["power"] - option["power"] >= shortage - 1e-9]
                if viable:
                    mode = min(viable, key=lambda m: f.feasible[donor][m]["power"])
                    f.options.append({"requester": uid, "request_mode": desired, "donor": donor, "donor_mode": mode,
                                      "current_mode": current, "lower_modes": viable, "benefit": max(0, benefit),
                                      "priority": f.priority, "version": f.allocation["version"]})
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
            f.reply = await f.ask_peer("offer", f.choice, "cfp" if f.attempted == 1 else "propose")
            self.set_next_state("EVALUATING")
        except TimeoutError:
            self.set_next_state("FAILED")


class Evaluate(State):
    async def run(self):
        f = self.fsm
        if f.reply.get("counter") and f.reply.get("mode") in f.choice["lower_modes"]:
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
            response = await f.ask_peer("accept", {"mode": f.reply["mode"]}, "accept-proposal")
            self.set_next_state("COMMITTING" if response.get("confirmed") else "FAILED")
        except TimeoutError:
            self.set_next_state("FAILED")


class Commit(State):
    async def run(self):
        f = self.fsm
        try:
            result = await self.agent.ask(f.power, f.job, "commit-swap", f.choice,
                                          protocol="hvac-bargaining", performative="confirm")
            if result.get("committed"):
                f.result = result
            self.set_next_state("COMPLETED" if f.result else "FAILED")
        except TimeoutError:
            self.set_next_state("FAILED")


class Finish(State):
    async def run(self):
        f = self.fsm
        f.record("COMPLETED" if f.result else "FAILED", terms=f.choice)
