"""Run the twelve SPADE agents and route step-scoped MongoDB observations."""
import asyncio
import logging
import secrets
from collections import OrderedDict
from contextlib import suppress
from copy import deepcopy

import httpx
import spade
from spade.template import Template
from pydantic import ValidationError

from hvac_mas.agents import AreaAgent, DiagnosticAgent, GatewayAgent, Negotiation, PowerAgent, UnitAgent, WeatherAgent
from hvac_mas.control import load_rules
from hvac_mas.models.building import Mode, load_building
from hvac_mas.models.telemetry import Observation
from hvac_mas.settings import Settings
from hvac_mas.storage import Store

LOG = logging.getLogger(__name__)


class Runtime:
    def __init__(self, settings, building, rules):
        self.settings, self.building, self.rules = settings, building, rules
        domain = settings.xmpp_domain
        self.addresses = {u.unit_id: f"hvac_{u.zone_id.lower()}@{domain}" for u in building.equipment}
        self.areas = {a.area_id: f"area_{a.area_id.lower()}@{domain}" for a in building.areas if len(a.zone_ids) > 1}
        peers = set(self.addresses.values()) | set(self.areas.values()) | {
            f"{name}@{domain}" for name in ("gateway", "diagnostic", "power", "weather")}
        self.trace = {}
        self.packets = OrderedDict()
        self.packet_arrival = asyncio.Event()
        self.live_memory = None
        self.previous_availability = (None, set())
        self.units, self.agents = {}, []
        if not settings.xmpp_embedded and not settings.xmpp_password:
            raise ValueError("Set XMPP_PASSWORD for the external XMPP accounts")
        password = settings.xmpp_password.get_secret_value() if settings.xmpp_password else secrets.token_urlsafe(20)

        def make(cls, jid, **kwargs):
            agent = cls(jid, settings, password, peers, self.trace, **kwargs)
            self.agents.append(agent)
            return agent

        zones = {z.zone_id: z for z in building.zones}
        self.identities = {u.unit_id: zones[u.zone_id] for u in building.equipment}
        for spec in building.equipment:
            self.units[spec.unit_id] = make(UnitAgent, self.addresses[spec.unit_id], spec=spec,
                                           zone=zones[spec.zone_id], rules=rules.control)
        for aid, jid in self.areas.items():
            make(AreaAgent, jid, area_id=aid)
        self.power = str(make(PowerAgent, f"power@{domain}").jid)
        self.weather = str(make(WeatherAgent, f"weather@{domain}").jid)
        self.diagnostic_agent = make(DiagnosticAgent, f"diagnostic@{domain}", building=building, rules=rules.diagnostics)
        self.diagnostic = str(self.diagnostic_agent.jid)
        self.gateway = make(GatewayAgent, f"gateway@{domain}")

    async def start(self):
        for agent in self.agents:
            await asyncio.wait_for(agent.start(auto_register=self.settings.xmpp_embedded), timeout=12)
        probes = []
        for agent in self.agents:
            if agent is not self.gateway:
                try:
                    probes.append(await self.gateway.ask(str(agent.jid), "readiness", "ping", {}))
                except TimeoutError as exc:
                    raise RuntimeError(f"XMPP readiness probe timed out for {agent.jid}") from exc
        if not all(p.get("alive") for p in probes):
            raise RuntimeError("SPADE readiness probe failed")
        self.trace.clear()

    async def stop(self):
        await asyncio.gather(*(agent.stop() for agent in self.agents if agent.is_alive()), return_exceptions=True)

    async def receive(self, document):
        try:
            packet = Observation.model_validate({k: v for k, v in document.items() if k != "ingestedAt"})
            zone = self.identities[packet.unitId]
            if (packet.buildingId, packet.areaId, packet.controlZoneId, packet.thermostatId) != (
                self.building.building_id, zone.area_id, zone.zone_id, zone.thermostat.thermostat_id
            ):
                raise ValueError("invalid building, area, zone or thermostat identity")
        except (ValidationError, ValueError, KeyError):
            LOG.warning("Ignored invalid telemetry identity or reading")
            return
        self.packets.setdefault(packet.packetId, packet)
        if len(self.packets) > 12000:
            self.packets.popitem(last=False)
        self.packet_arrival.set()

    async def fetch_packets(self, job):
        ids = job["packet_ids"]
        async with asyncio.timeout(self.settings.decision_timeout / 2):
            while any(pid not in self.packets for pid in ids):
                self.packet_arrival.clear()
                await self.packet_arrival.wait()
        packets = [self.packets[pid] for pid in ids]
        if any(p.runId != job["runId"] or p.simulationStep != job["simulationStep"] for p in packets):
            raise ValueError("observation does not match current run and step")
        return packets

    def use_experiment_memory(self, experiment):
        """Preserve the interactive controller while the serialized trials reuse XMPP."""
        if experiment and self.live_memory is None:
            tracked = [(unit, ("run_id", "last_reading", "waiting_steps", "previous_mode",
                               "diagnostic", "current", "offer")) for unit in self.units.values()]
            tracked.extend((agent, ("run_id", "latest", "events"))
                           for agent in self.agents if isinstance(agent, AreaAgent))
            tracked.extend([(self.diagnostic_agent, ("run_id", "diagnostics")),
                            (self, ("previous_availability",))])
            self.live_memory = [(owner, {name: deepcopy(getattr(owner, name, None)) for name in names})
                                for owner, names in tracked]
        elif not experiment and self.live_memory is not None:
            for owner, attributes in self.live_memory:
                for name, value in attributes.items():
                    setattr(owner, name, value)
            self.live_memory = None

    async def decide(self, job):
        self.use_experiment_memory(job.get("experiment", False))
        jid = job["id"]
        packets = await self.fetch_packets(job)
        rows = [p.model_dump(mode="json") for p in packets]
        ask = self.gateway.ask
        weather = await ask(self.weather, jid, "weather", job["weather"])
        area_input = await asyncio.gather(*(ask(peer, jid, "area-observe", {"runId": job["runId"], "packets": [
            row for row in rows if row["areaId"] == aid]}, performative="inform", protocol="hvac-observation")
            for aid, peer in self.areas.items()))
        weights = {uid: weight for area in area_input for uid, weight in area["weights"].items()}
        unit_intents = await asyncio.gather(*(ask(self.addresses[p.unitId], jid, "observe",
                                                 {"packet": row, "feasible": job["feasible"][p.unitId],
                                                  "forecast": weather["forecast_c"], "seconds": job["seconds"],
                                                  "area_weight": weights.get(p.unitId, 1)},
                                                 performative="inform", protocol="hvac-observation")
                                              for p, row in zip(packets, rows)))
        intents = {uid: {"choices": [Mode.OFF], "priority": 0} for uid in self.units}
        intents.update({p.unitId: intent for p, intent in zip(packets, unit_intents)})
        diagnosis = await ask(self.diagnostic, jid, "diagnose", {"packets": rows, "runId": job["runId"]},
                              performative="inform", protocol="hvac-observation")
        events = diagnosis["events"]
        if not hasattr(self, "previous_availability") or self.previous_availability[0] != job["runId"]:
            self.previous_availability = (job["runId"], set(self.units))
        observed = {p.unitId for p in packets}
        for uid in sorted(self.previous_availability[1] ^ observed):
            events.append({"kind": "TELEMETRY_UNAVAILABLE", "status": "RESOLVED" if uid in observed else "CANDIDATE",
                           "unitId": uid, "areaId": self.identities[uid].area_id})
        self.previous_availability = (job["runId"], observed)
        for event in events:
            targets = [self.addresses[event["unitId"]]]
            if event["areaId"] in self.areas:
                targets.append(self.areas[event["areaId"]])
            await asyncio.gather(*(self.diagnostic_agent.ask(to, jid, "diagnosis", event, performative="inform",
                                                       protocol="hvac-diagnostic") for to in targets))
        allocation = await ask(self.power, jid, "reserve", {
            "feasible": job["feasible"], "limit": job["limit"], "intents": intents,
            "runId": job["runId"], "simulationStep": job["simulationStep"],
        })
        if job["negotiate"]:
            order = sorted(self.units, key=lambda uid: (-intents[uid]["priority"], uid))
            for uid in order:
                intent = intents[uid]
                preferred = intent["choices"][0]
                allocated = allocation["modes"][uid]
                power = job["feasible"][uid]
                if preferred == allocated or preferred not in power or power[preferred]["power"] <= power[allocated]["power"]:
                    continue
                # A single owner serializes conversations. The power agent independently verifies version and capacity.
                fsm = Negotiation(jid, self.power, allocation, job["feasible"], self.addresses,
                                  self.rules.control.max_rounds, intent["priority"], job["limit"])
                self.units[uid].add_behaviour(fsm, Template(metadata={"ontology": "hvac-fsm-only"}))
                try:
                    await fsm.join(timeout=self.settings.decision_timeout / 2)
                except TimeoutError:
                    fsm.kill()
                    raise
                events.extend(fsm.events)
                allocation = fsm.result or allocation
        final = await ask(self.power, jid, "finalize", {})
        commands = await asyncio.gather(*(ask(self.addresses[p.unitId], jid, "committed",
            {"mode": final["modes"][p.unitId]}, performative="confirm") for p in packets))
        if any(command.get("unitId") != packet.unitId or command.get("mode") != final["modes"][packet.unitId]
               for packet, command in zip(packets, commands)):
            raise ValueError("unit command does not match its reservation")
        return {"modes": final["modes"], "intents": intents, "events": events,
                "reservations": final["reservations"], "messages": self.trace.pop(jid, [])}

    async def run(self):
        ready = asyncio.Event()
        store = Store(self.settings)
        listener = None
        try:
            await store.connect(indexes=False)
            await self.start()
            if self.settings.storage == "mongo":
                listener = asyncio.create_task(store.watch_telemetry(self.receive, ready))
            else:
                ready.set()
            async with httpx.AsyncClient(base_url=self.settings.api_url, timeout=3,
                                         headers={"X-Runtime-Token": self.settings.runtime_token.get_secret_value()}) as api:
                done = None
                while True:
                    if listener and listener.done():
                        listener.result()
                    live = ready.is_set() and all(agent.is_alive() for agent in self.agents)
                    try:
                        response = await api.get("/internal/work", params={"agents": ",".join(
                            str(agent.jid) for agent in self.agents) if live else ""})
                        response.raise_for_status()
                        job = response.json()
                        if live and job and (not done or done[0] != job["id"]):
                            for row in job["packets"]:
                                await self.receive(row)
                            try:
                                payload = await self.decide(job)
                            except (ValueError, KeyError, RuntimeError, TimeoutError) as exc:
                                LOG.warning("Decision failed (%s): %s", type(exc).__name__, exc)
                                self.trace.pop(job["id"], None)
                                payload = {"error": type(exc).__name__}
                            done = (job["id"], payload)
                        if live and job and done and done[0] == job["id"]:
                            response = await api.post(f"/internal/results/{done[0]}", json=done[1])
                            if response.status_code != 409:
                                response.raise_for_status()
                    except httpx.HTTPError as exc:
                        LOG.warning("Backend unavailable (%s)", type(exc).__name__)
                    await asyncio.sleep(0.05 if live else 0.5)
        finally:
            if listener:
                listener.cancel()
                with suppress(asyncio.CancelledError):
                    await listener
            await self.stop()
            await store.close()


async def main(settings):
    runtime = Runtime(settings, load_building(settings.building_path), load_rules(settings.rules_path))
    await runtime.run()


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    configuration = Settings()
    if configuration.xmpp_embedded and (configuration.xmpp_domain != "localhost" or configuration.xmpp_port != 5222):
        raise SystemExit("Embedded SPADE requires localhost:5222")
    spade.run(main(configuration), embedded_xmpp_server=configuration.xmpp_embedded)
