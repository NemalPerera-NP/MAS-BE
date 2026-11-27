"""Owns physical state and the decision barrier between backend and MAS."""
import asyncio
from copy import deepcopy
from datetime import timedelta
from time import monotonic
from uuid import uuid4

from hvac_mas.control import allocate, fixed_intents, total_power
from hvac_mas.models.building import Mode
from hvac_mas.simulation.run import Run
from hvac_mas.storage import now


class Service:
    def __init__(self, settings, building, scenarios, rules, store):
        self.settings, self.building, self.scenarios, self.rules, self.store = settings, building, scenarios, rules, store
        self.run = Run(building, scenarios["normal_cooling"])
        self.lock = asyncio.Lock()
        self.barrier = asyncio.Lock()
        self.pending = None
        self.last_seen = 0
        self.connected_agents = []
        self.loop_task = None
        self.pause_signal = asyncio.Event()
        self.experiment_task = None
        self.error = None

    @property
    def ready(self):
        return len(self.connected_agents) == len(self.building.equipment) + 6 and monotonic() - self.last_seen < 5

    async def start_run(self, scenario, seed=None, wide_deadband=None):
        if scenario not in self.scenarios:
            raise ValueError("unknown scenario")
        async with self.lock:
            config = self.scenarios[scenario]
            if seed is not None:
                config = config.model_copy(update={"seed": seed})
            if wide_deadband is not None:
                config = config.model_copy(update={"wide_deadband": wide_deadband})
            next_run = Run(self.building, config)
            await self.record_run(next_run, "paused")
            await self.store.put("simulation_state", next_run.id, next_run.snapshot() | {
                "persisted_run": next_run.persisted_state(),
                "restoreCandidate": True,
                "updatedAt": now(),
                "expiresAt": now() + timedelta(hours=2),
            })
            await self.record_run(self.run, "completed")
            self.run = next_run
            self.error = None
            return self.snapshot()

    async def record_run(self, run, status):
        await self.store.put("simulation_runs", run.id, {
            "runId": run.id, "status": status, "scenario": run.scenario.model_dump(mode="json"),
            "expiresAt": now() + timedelta(hours=2) if status == "completed" else None,
        })

    def snapshot(self):
        return self.run.snapshot() | {"running": self.loop_task is not None and not self.loop_task.done(),
                                     "agent_ready": self.ready, "agents": self.connected_agents if self.ready else [],
                                     "storage": self.settings.storage, "last_error": self.error,
                                     "experiment_running": bool(self.experiment_task and not self.experiment_task.done()),
                                     "queued_observations": {uid: edit.repeats for uid, edit in self.run.overrides.items()}}

    async def _loop(self):
        while not self.pause_signal.is_set():
            try:
                await self.advance()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                break
            try:
                await asyncio.wait_for(self.pause_signal.wait(), self.settings.step_interval)
            except TimeoutError:
                pass

    def resume(self):
        if not self.ready:
            raise ValueError("the SPADE runtime is not ready")
        if self.loop_task is None or self.loop_task.done():
            self.pause_signal.clear()
            self.loop_task = asyncio.create_task(self._loop())

    async def pause(self):
        if self.loop_task and not self.loop_task.done():
            self.pause_signal.set()
            await asyncio.shield(self.loop_task)

    async def advance(self):
        async with self.lock:
            if not self.ready:
                raise ValueError("the SPADE runtime is not ready")
            self.run, result = await self.step(self.run)
            self.error = None
            return result

    async def step(self, run, controller="mas"):
        async with self.barrier:
            return await self._step(run, controller)

    async def _step(self, previous, controller):
        run = deepcopy(previous)
        run.prepare()
        attempt = uuid4().hex
        packets = run.observations(attempt)
        feasible = run.feasible()
        limit = run.sim.building.power_limit_kw
        base = {"runId": run.id, "simulationStep": run.sim.step_number}
        await self.store.insert("telemetry", [
            p.model_dump() | {"_id": p.packetId} for p in packets
        ])
        default = {uid: {"priority": 0, "choices": [run.sim.units[uid].actual_mode, Mode.OFF]} for uid in feasible}
        messages, events, reservations = [], [], []
        outcome = "committed"
        if controller == "fixed":
            intents = fixed_intents({p.unitId: p for p in packets}, feasible, run.thermostats, self.rules.control)
            modes = allocate(feasible, intents, limit, fixed_priority=True)
        else:
            job = base | {"id": attempt, "experiment": previous is not self.run, "packet_ids": [p.packetId for p in packets],
                          "packets": [p.model_dump(mode="json") for p in packets] if self.settings.storage == "memory" else [],
                          "feasible": feasible, "limit": limit, "weather": run.context()[0],
                          "seconds": run.sim.step_seconds, "negotiate": controller == "mas"}
            future = asyncio.get_running_loop().create_future()
            self.pending = (job, future)
            try:
                reply = await asyncio.wait_for(future, self.settings.decision_timeout)
                if "error" in reply:
                    raise RuntimeError(reply["error"])
                modes, intents, messages, events, reservations = (
                    reply[key] for key in ("modes", "intents", "messages", "events", "reservations"))
            except (TimeoutError, RuntimeError):
                if previous is not self.run:
                    raise ValueError("agent decision did not complete during experiment") from None
                outcome = "fallback"
                events = [{"kind": "AGENT_DECISION_UNAVAILABLE", "status": "fallback", "id": attempt}]
                intents = default
                modes = allocate(feasible, intents, limit, fixed_priority=True)
            finally:
                self.pending = None
        if (set(modes) != set(feasible) or set(intents) != set(feasible)
                or any(mode not in feasible[uid] for uid, mode in modes.items())
                or total_power(modes, feasible) > limit + 1e-9):
            raise ValueError("invalid allocation received")
        result = run.commit(modes, {uid: intent["choices"][0] for uid, intent in intents.items()})
        if reservations:
            if len(reservations) != len(modes) or any(
                r["approvedStage"] != modes.get(r["unitId"]) or r["runId"] != run.id
                or r["validFromStep"] != base["simulationStep"]
                or r["committedPowerKw"] != feasible[r["unitId"]][r["approvedStage"]]["power"]
                for r in reservations
            ) or len({r["unitId"] for r in reservations}) != len(modes):
                raise ValueError("reservation does not match approved allocation")
            actual = result["power_kw"]
            if abs(sum(r["committedPowerKw"] for r in reservations) - actual) > 1e-8:
                raise ValueError("reserved demand does not match actual demand")
        action = base | {"operationId": attempt, "outcome": outcome, "controller": controller, "modes": modes,
                         "requested": {uid: i["choices"][0] for uid, i in intents.items()},
                         "packetIds": [p.packetId for p in packets], "power_kw": result["power_kw"], "limit_kw": limit,
                         "snapshot": run.snapshot(), "reservations": reservations}
        swaps = [e["terms"] for e in events if e.get("kind") == "NEGOTIATION"
                 and e.get("status") == "COMPLETED" and e.get("terms")]
        action["interval_start_s"] = previous.sim.time_s
        action["interval_end_s"] = run.sim.time_s
        action["decisions"] = {}
        observed = {p.unitId: p for p in packets}
        for uid, mode in modes.items():
            packet = observed.get(uid)
            requested = action["requested"][uid]
            exchange = next((swap for swap in reversed(swaps) if uid in (swap["donor"], swap["requester"])), None)
            if outcome == "fallback":
                reason = "The agents missed the decision window. The backup controller selected a feasible stage."
            elif packet is None:
                reason = "The unit was offline and could not report a reading."
            elif exchange:
                peer = exchange["donor"] if uid == exchange["requester"] else exchange["requester"]
                reason = f"A confirmed exchange with {peer} changed the power allocation."
            elif intents[uid].get("desired", requested) not in feasible[uid]:
                reason = "An equipment constraint limited the stages available this interval."
            elif mode != requested:
                reason = "The shared power allocation limited the stage this unit received."
            else:
                reason = "The power budget allowed the requested stage."
            action["decisions"][uid] = {
                "observed_f": packet.data.avgTemp if packet else None,
                "source": packet.source if packet else None,
                "heat_target_f": packet.data.heatSetpoint if packet else None,
                "cool_target_f": packet.data.coolSetpoint if packet else None,
                "reason": reason,
            }
        await self.store.commit_interval(
            action, [base | m | {"_id": m["messageId"]} for m in messages],
            [base | e | {"_id": f"{attempt}-{i}"} for i, e in enumerate(events)],
            run.truth(), action["snapshot"] | {"persisted_run": run.persisted_state(),
                                               "restoreCandidate": previous is self.run,
                                               "expiresAt": now() + timedelta(hours=2)})
        return run, action | {"messages": messages, "events": events}

    async def shutdown(self):
        await self.pause()
        if self.experiment_task:
            self.experiment_task.cancel()
            await asyncio.gather(self.experiment_task, return_exceptions=True)
        await self.record_run(self.run, "completed")
