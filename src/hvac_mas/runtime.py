"""Run the twelve SPADE agents and route step-scoped MongoDB observations."""
import asyncio
import logging
from time import monotonic
import secrets
import sys
from collections import OrderedDict
from contextlib import suppress
from copy import deepcopy
from pathlib import Path
from tempfile import TemporaryDirectory

import httpx
from loguru import logger as xmpp_logger
from pydantic import ValidationError
from pyjabber.server import Server
from pyjabber.server_parameters import Parameters

from hvac_mas.agents import AreaAgent, DiagnosticAgent, GatewayAgent, PowerAgent, UnitAgent, WeatherAgent
from hvac_mas.control import load_rules
from hvac_mas.models.building import Mode, f_to_c, load_building
from hvac_mas.models.telemetry import Observation
from hvac_mas.settings import Settings
from hvac_mas.storage import Store

LOG = logging.getLogger("hvac_mas.runtime")


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
            LOG.info("AGENT_STARTING agent=%s role=%s", agent.jid, type(agent).__name__)
            for attempt in range(3):
                try:
                    await asyncio.wait_for(agent.start(auto_register=self.settings.xmpp_embedded), timeout=5)
                    break
                except TimeoutError as exc:
                    if agent.client is not None:
                        agent.client.abort()
                    if attempt == 2:
                        raise RuntimeError(f"XMPP startup timed out for {agent.jid}") from exc
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
        LOG.info("AGENTS_READY count=%d transport=XMPP", len(self.agents))

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
        try:
            async with asyncio.timeout(min(2, self.settings.decision_timeout / 4)):
                while any(pid not in self.packets for pid in ids):
                    self.packet_arrival.clear()
                    await self.packet_arrival.wait()
        except TimeoutError:
            # Observations were persisted before this job was published. Reconcile the
            # same packet identities rather than falling back because a stream is late.
            for row in job.get("packets", []):
                if row.get("packetId") in ids:
                    await self.receive(row)
            if any(pid not in self.packets for pid in ids):
                raise
            LOG.warning("TELEMETRY_RECONCILED run=%s step=%s packets=%d source=persisted_job_handoff", job["runId"], job["simulationStep"], len(ids))
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

    def capture_decision_memory(self):
        tracked = [(unit, ("run_id", "last_reading", "waiting_steps", "previous_mode",
                           "diagnostic", "current", "offer")) for unit in self.units.values()]
        tracked.extend((agent, ("run_id", "latest", "events"))
                       for agent in self.agents if isinstance(agent, AreaAgent))
        tracked.extend([(self.diagnostic_agent, ("run_id", "diagnostics")),
                        (self, ("previous_availability",))])
        return [(owner, {name: deepcopy(getattr(owner, name, None)) for name in names})
                for owner, names in tracked]

    @staticmethod
    def restore_decision_memory(snapshot):
        if snapshot is None:
            return
        for owner, attributes in snapshot:
            for name, value in attributes.items():
                setattr(owner, name, value)

    async def decide(self, job):
        self.use_experiment_memory(job.get("experiment", False))
        jid = job["id"]
        packets = await self.fetch_packets(job)
        rows = [p.model_dump(mode="json") for p in packets]
        ask = self.gateway.ask
        weather = await ask(self.weather, jid, "weather", job["weather"])
        zone_by_id = {zone.zone_id: zone for zone in self.building.zones}
        temperatures = {packet.controlZoneId: f_to_c(packet.data.avgTemp) for packet in packets}
        neighbors = {zone.zone_id: {} for zone in self.building.zones}
        for edge in self.building.couplings:
            if edge.zone_b in temperatures:
                neighbors[edge.zone_a][edge.zone_b] = {
                    "temperature_c": temperatures[edge.zone_b],
                    "conductance_kw_per_c": edge.conductance_kw_per_c}
            if edge.zone_a in temperatures:
                neighbors[edge.zone_b][edge.zone_a] = {
                    "temperature_c": temperatures[edge.zone_a],
                    "conductance_kw_per_c": edge.conductance_kw_per_c}
        area_rows = {aid: [] for aid in self.areas}
        for packet, row in zip(packets, rows):
            zone = zone_by_id[packet.controlZoneId]
            if packet.areaId in area_rows:
                area_rows[packet.areaId].append(row | {
                    "designOccupancy": zone.occupancy_policy.design_occupancy,
                    "feasibleModes": job["feasible"][packet.unitId],
                    "equipmentAvailable": bool(job["feasible"][packet.unitId]),
                })
        area_input = await asyncio.gather(*(ask(peer, jid, "area-observe", {
            "runId": job["runId"], "packets": area_rows[aid]},
            performative="inform", protocol="hvac-observation") for aid, peer in self.areas.items()))
        weights = {uid: weight for area in area_input for uid, weight in area["weights"].items()}
        unit_intents = await asyncio.gather(*(ask(self.addresses[p.unitId], jid, "observe",
                                                 {"packet": row, "feasible": job["feasible"][p.unitId],
                                                  "forecast": weather["forecast_c"], "seconds": job["seconds"],
                                                  "area_weight": weights.get(p.unitId, 1),
                                                  "neighbor_temperatures": neighbors[p.controlZoneId]},
                                                 performative="inform", protocol="hvac-observation")
                                              for p, row in zip(packets, rows)))
        area_assessments = await asyncio.gather(*(ask(peer, jid, "area-assess", {
            "intents": {p.unitId: intent for p, intent in zip(packets, unit_intents)
                        if p.areaId == aid},
            "feasible": job["feasible"]}, performative="inform", protocol="hvac-coordination")
            for aid, peer in self.areas.items())) if self.areas else []
        intents = {uid: {"choices": [Mode.OFF], "priority": 0} for uid in self.units}
        intents.update({p.unitId: intent for p, intent in zip(packets, unit_intents)})
        diagnosis = await ask(self.diagnostic, jid, "diagnose", {"packets": rows, "runId": job["runId"]},
                              performative="inform", protocol="hvac-observation")
        events = diagnosis["events"] + [{"kind": "AREA_ASSESSMENT", "status": "OBSERVED"} | item
                                         for item in area_assessments]
        if not hasattr(self, "previous_availability") or self.previous_availability[0] != job["runId"]:
            self.previous_availability = (job["runId"], set(self.units))
        observed = {p.unitId for p in packets}
        for uid in sorted(self.previous_availability[1] ^ observed):
            events.append({"kind": "TELEMETRY_UNAVAILABLE", "status": "RESOLVED" if uid in observed else "CANDIDATE",
                           "unitId": uid, "areaId": self.identities[uid].area_id})
        self.previous_availability = (job["runId"], observed)
        for event in events:
            if event.get("kind") == "AREA_ASSESSMENT":
                continue
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
                # Unit Agents decide whether to bargain and own their SPADE behaviour.
                # Runtime serializes the interval and waits for the selected agent's outcome.
                fsm = self.units[uid].initiate_negotiation(
                    jid, self.power, allocation, job["feasible"], self.addresses,
                    self.rules.control.max_rounds, job["limit"], intents, job["simulationStep"])
                if fsm is None:
                    continue
                try:
                    await fsm.join(timeout=self.settings.decision_timeout / 2)
                except (TimeoutError, asyncio.CancelledError):
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
        conversation_ids = [key for key in self.trace if key == jid or key.startswith(f"{jid}:")]
        messages = [message for key in conversation_ids for message in self.trace.pop(key, [])]
        return {"modes": final["modes"], "intents": intents, "events": events,
                "reservations": final["reservations"], "messages": messages}

    async def run(self):
        ready = asyncio.Event()
        store = Store(self.settings)
        listener = None
        heartbeat_task = None
        busy_job = None
        try:
            await store.connect(indexes=False)
            await self.start()
            if self.settings.storage == "mongo":
                listener = asyncio.create_task(store.watch_telemetry(self.receive, ready))
            else:
                ready.set()
            async with httpx.AsyncClient(base_url=self.settings.api_url, trust_env=False,
                                         timeout=self.settings.decision_timeout + 30,
                                         headers={"X-Runtime-Token": self.settings.runtime_token.get_secret_value()}) as api:
                async def heartbeat():
                    previous = None
                    async with httpx.AsyncClient(base_url=self.settings.api_url, trust_env=False, timeout=3,
                            headers={"X-Runtime-Token": self.settings.runtime_token.get_secret_value()}) as pulse_api:
                        while True:
                            alive = [str(agent.jid) for agent in self.agents if agent.is_alive()]
                            status = (len(alive), ready.is_set())
                            if status != previous:
                                LOG.info("RUNTIME_HEALTH alive=%d expected=%d stream_ready=%s", len(alive),len(self.agents),ready.is_set())
                                previous = status
                            try:
                                response = await pulse_api.post("/internal/heartbeat", json={"agents":alive,
                                    "stream_ready":ready.is_set(), "busy_job":busy_job,
                                    "agent_activity":{str(a.jid):a.last_message_at.isoformat() if a.last_message_at else None for a in self.agents}})
                                response.raise_for_status()
                            except httpx.HTTPError as exc:
                                LOG.warning("HEARTBEAT_FAILED error_type=%s", type(exc).__name__)
                            await asyncio.sleep(1)
                heartbeat_task = asyncio.create_task(heartbeat())
                done = None
                staged_memory = {}
                while True:
                    if listener and listener.done():
                        listener.result()
                    live = all(agent.is_alive() for agent in self.agents)
                    try:
                        response = await api.get("/internal/work", params={"agents": ",".join(
                            str(agent.jid) for agent in self.agents) if live else ""})
                        response.raise_for_status()
                        job = response.json()
                        if live and job and (not done or done[0] != job["id"]):
                            staged_memory[job["id"]] = self.capture_decision_memory()
                            busy_job = job["id"]
                            if self.settings.storage == "memory":
                                for row in job["packets"]:
                                    await self.receive(row)
                            LOG.info("DECISION_STARTED run=%s step=%s job=%s", job["runId"], job["simulationStep"], job["id"])
                            try:
                                payload = await asyncio.wait_for(self.decide(job), timeout=max(1, self.settings.decision_timeout-2))
                            except (ValueError, KeyError, RuntimeError, TimeoutError) as exc:
                                LOG.warning("Decision failed (%s): %s", type(exc).__name__, exc)
                                for conversation in [key for key in self.trace
                                                     if key == job["id"] or key.startswith(f"{job['id']}:")]:
                                    self.trace.pop(conversation, None)
                                self.restore_decision_memory(staged_memory.pop(job["id"], None))
                                payload = {"error": type(exc).__name__}
                            done = (job["id"], payload)
                        if live and job and done and done[0] == job["id"]:
                            response = await api.post(f"/internal/results/{done[0]}", json=done[1])
                            if response.status_code == 409:
                                self.restore_decision_memory(staged_memory.pop(done[0], None))
                                done = None
                            else:
                                response.raise_for_status()
                                staged_memory.pop(done[0], None)
                            busy_job = None
                    except httpx.HTTPError as exc:
                        if done:
                            try:
                                status = await api.get(f"/internal/commit-status/{done[0]}")
                                status.raise_for_status()
                            except httpx.HTTPError:
                                pass
                            else:
                                if status.json().get("committed"):
                                    staged_memory.pop(done[0], None)
                                else:
                                    self.restore_decision_memory(staged_memory.pop(done[0], None))
                                    done = None
                        LOG.warning("Backend unavailable (%s)", type(exc).__name__)
                    await asyncio.sleep(0.05 if live else 0.5)
        finally:
            if heartbeat_task:
                heartbeat_task.cancel()
                await asyncio.gather(heartbeat_task, return_exceptions=True)
            if listener:
                listener.cancel()
                with suppress(asyncio.CancelledError):
                    await listener
            await self.stop()
            await store.close()


async def main(settings):
    runtime = Runtime(settings, load_building(settings.building_path), load_rules(settings.rules_path))
    if not settings.xmpp_embedded:
        await runtime.run()
        return

    # PyJabber can resolve both listener hosts to the same address. Use the
    # standard asyncio loop, which deduplicates the resolved socket addresses.
    # Keep registration available if SQLite replaces its connection.
    with TemporaryDirectory(prefix="hvac-xmpp-") as directory:
        server = Server(Parameters(
            host="localhost", database_path=str(Path(directory) / "xmpp.db"),
            message_persistence=False,
        ))
        async with asyncio.TaskGroup() as tasks:
            listener = tasks.create_task(server.start())
            try:
                await asyncio.wait_for(server.ready.wait(), timeout=15)
                await runtime.run()
            finally:
                listener.cancel()


if __name__ == "__main__":
    from hvac_mas.logging_config import configure_logging
    configure_logging(Settings())
    # PyJabber's debug messages include raw XML registration stanzas.
    xmpp_logger.remove()
    xmpp_logger.add(sys.stderr, level="WARNING")
    configuration = Settings()
    if configuration.xmpp_embedded and (configuration.xmpp_domain != "localhost" or configuration.xmpp_port != 5222):
        raise SystemExit("Embedded SPADE requires localhost:5222")
    try:
        asyncio.run(main(configuration))
    except KeyboardInterrupt:
        pass
