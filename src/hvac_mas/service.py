"""Owns physical state and the decision barrier between backend and MAS."""
import asyncio
import logging
from copy import deepcopy
from datetime import timedelta
from time import monotonic
from uuid import uuid4

from hvac_mas.control import allocate, fixed_intents, total_power
from hvac_mas.models.building import Mode
from hvac_mas.simulation.run import Run
from hvac_mas.storage import now

LOG = logging.getLogger(__name__)


class Service:
    def __init__(self, settings, building, scenarios, rules, store):
        self.settings, self.building, self.scenarios, self.rules, self.store = settings, building, scenarios, rules, store
        self.step_interval = settings.step_interval
        self.run = self.new_run(scenarios["normal_cooling"])
        self.lock = asyncio.Lock()
        self.barrier = asyncio.Lock()
        self.pending = None
        self.committed_attempts = []
        self.last_seen = 0
        self.connected_agents = []
        self.loop_task = None
        self.pause_signal = asyncio.Event()
        self.experiment_task = None
        self.error = None
        self.execution = {"status": "not_started", "fallback_count": 0, "last_mas_step": None}
        self.runtime_info = {}

    def new_run(self, scenario):
        rules = self.rules.control
        return Run(self.building, scenario,
                   max_occupants_per_zone=rules.max_occupants_per_zone,
                   max_temporary_occupancy_minutes=rules.max_temporary_occupancy_minutes,
                   occupancy_forecast_minutes=rules.prediction_horizon_minutes)

    @property
    def ready(self):
        return len(self.connected_agents) == len(self.building.equipment) + 6 and monotonic() - self.last_seen < self.settings.heartbeat_timeout

    async def start_run(self, scenario, seed=None, wide_deadband=None):
        if scenario not in self.scenarios:
            raise ValueError("unknown scenario")
        async with self.lock:
            config = self.scenarios[scenario]
            if seed is not None:
                config = config.model_copy(update={"seed": seed})
            if wide_deadband is not None:
                config = config.model_copy(update={"wide_deadband": wide_deadband})
            next_run = self.new_run(config)
            await self.record_run(next_run, "paused")
            await self.store.put("simulation_state", next_run.id, next_run.snapshot() | {
                "persisted_run": next_run.persisted_state(),
                "restoreCandidate": True,
                "updatedAt": now(),
                "expiresAt": now() + timedelta(hours=2),
            })
            await self.record_run(self.run, "completed")
            self.run = next_run
            self.execution = {"status": "not_started", "fallback_count": 0, "last_mas_step": None}
            LOG.info("RUN_STARTED run=%s scenario=%s", next_run.id, scenario)
            self.error = None
            return self.snapshot()

    async def record_run(self, run, status):
        await self.store.put("simulation_runs", run.id, {
            "runId": run.id, "status": status, "scenario": run.scenario.model_dump(mode="json"),
            "expiresAt": now() + timedelta(hours=2) if status == "completed" else None,
        })

    async def persist_current_state(self, run=None, *, restore_candidate=True):
        """Persist queued interventions immediately so a restart cannot drop them."""
        run = run or self.run
        stamp = now()
        await self.store.put("simulation_state", run.id, run.snapshot() | {
            "persisted_run": run.persisted_state(),
            "restoreCandidate": restore_candidate,
            "updatedAt": stamp,
            "expiresAt": stamp + timedelta(hours=2),
        })

    def snapshot(self):
        return self.run.snapshot() | {"running": self.loop_task is not None and not self.loop_task.done(),
                                     "agent_ready": self.ready, "agents": self.connected_agents if self.ready else [],
                                     "storage": self.settings.storage, "last_error": self.error,
                                     "execution": self.execution, "runtime": self.runtime_info,
                                     "experiment_running": bool(self.experiment_task and not self.experiment_task.done()),
                                     "step_interval_seconds": self.step_interval,
                                     "queued_observations": {uid: edit.repeats for uid, edit in self.run.overrides.items()}}

    async def _loop(self):
        while not self.pause_signal.is_set():
            try:
                await self.advance()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                LOG.error("AUTOPLAY_STOPPED run=%s error_type=%s", self.run.id, type(exc).__name__)
                break
            try:
                await asyncio.wait_for(self.pause_signal.wait(), self.step_interval)
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
            started = monotonic()
            self.execution = {**self.execution, "status": "processing", "step": self.run.sim.step_number}
            try:
                self.run, result = await self.step(self.run)
            except Exception as exc:
                self.error = str(exc) if isinstance(exc, ValueError) else type(exc).__name__
                self.execution = {**self.execution, "status": "failed", "error": self.error}
                LOG.error("INTERVAL_FAILED run=%s step=%s error_type=%s", self.run.id, self.run.sim.step_number, type(exc).__name__)
                raise
            self.execution = {"status": "fallback" if result["outcome"] == "fallback" else "mas_committed",
                "step": result["simulationStep"], "controller": result["controller"],
                "duration_ms": round((monotonic()-started)*1000),
                "fallback_count": self.execution.get("fallback_count",0) + int(result["outcome"]=="fallback"),
                "last_mas_step": result["simulationStep"] if result["outcome"]=="committed" else self.execution.get("last_mas_step")}
            self.error = None
            return result

    async def step(self, run, controller="mas"):
        async with self.barrier:
            return await self._step(run, controller)

    async def _step(self, previous, controller):
        started = monotonic()
        run = deepcopy(previous)
        LOG.info("INTERVAL_STARTED run=%s step=%s simulated_min=%s controller=%s", run.id, run.sim.step_number, run.sim.time_s/60, controller)
        run.prepare()
        attempt = uuid4().hex
        packets = run.observations(attempt)
        feasible = run.feasible()
        limit = run.sim.building.power_limit_kw
        base = {"runId": run.id, "simulationStep": run.sim.step_number}
        await self.store.insert("telemetry", [
            p.model_dump() | {"_id": p.packetId} for p in packets
        ])
        default = {}
        for uid, options in feasible.items():
            zone = run.zones[run.specs[uid].zone_id]
            thermostat = run.effective_thermostats[zone.zone_id]
            minimum = Mode.FAN if thermostat.fan_policy == "on" and Mode.FAN in options else Mode.OFF
            if minimum not in options:
                minimum = run.sim.units[uid].actual_mode if run.sim.units[uid].actual_mode in options else min(
                    options, key=lambda mode: (options[mode]["power"], mode))
            default[uid] = {"priority": 0, "choices": [minimum, run.sim.units[uid].actual_mode, Mode.OFF],
                            "minimum_modes": [minimum]}
        messages, events, reservations = [], [], []
        outcome = "committed"
        commit_ack = None
        try:
            if controller == "fixed":
                intents = fixed_intents({p.unitId: p for p in packets}, feasible,
                                        run.effective_thermostats, self.rules.control)
                modes = allocate(feasible, intents, limit, fixed_priority=True)
            else:
                job = base | {"id": attempt, "experiment": previous is not self.run, "packet_ids": [p.packetId for p in packets],
                              "packets": [p.model_dump(mode="json") for p in packets],
                              "feasible": feasible, "limit": limit, "weather": run.context()[0],
                              "seconds": run.sim.step_seconds, "negotiate": controller == "mas"}
                future = asyncio.get_running_loop().create_future()
                commit_ack = asyncio.get_running_loop().create_future()
                commit_ack.add_done_callback(lambda future: future.exception() if not future.cancelled() else None)
                self.pending = (job, future, commit_ack)
                try:
                    reply = await asyncio.wait_for(future, self.settings.decision_timeout)
                    if "error" in reply:
                        raise RuntimeError(reply["error"])
                    modes, intents, messages, events, reservations = (
                        reply[key] for key in ("modes", "intents", "messages", "events", "reservations"))
                except (TimeoutError, RuntimeError) as exc:
                    LOG.warning("AGENT_DEADLINE_MISSED run=%s step=%s deadline_s=%s error_type=%s", run.id, run.sim.step_number, self.settings.decision_timeout, type(exc).__name__)
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
            interval_context = {zid: {"occupancy": run.context()[1][zid],
                                "heat_target_f": run.reading(run.zones[zid].unit_ids[0]).heatSetpoint,
                                "cool_target_f": run.reading(run.zones[zid].unit_ids[0]).coolSetpoint}
                                for zid in run.zones}
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
            action = base | {"operationId": attempt, "actionId": attempt, "outcome": outcome, "controller": controller, "modes": modes,
                             "requested": {uid: i["choices"][0] for uid, i in intents.items()},
                             "packetIds": [p.packetId for p in packets], "power_kw": result["power_kw"], "limit_kw": limit,
                             "snapshot": run.snapshot(), "reservations": reservations,
                             "interventionIds": [item["interventionId"] for item in run.interventions
                                                  if item.get("appliedStep") == base["simulationStep"]]}
            for intervention in run.interventions:
                if (intervention["status"] == "APPLIED" and intervention.get("appliedStep") == base["simulationStep"]
                        and "outcomeRef" not in intervention):
                    intervention["outcomeRef"] = {"runId": run.id, "simulationStep": base["simulationStep"],
                                                   "actionId": attempt}
            swaps = [e["terms"] for e in events if e.get("kind") == "NEGOTIATION"
                     and e.get("status") == "COMPLETED" and e.get("terms")]
            action["interval_zone_context"] = interval_context
            action["interval_start_s"] = previous.sim.time_s
            action["interval_end_s"] = run.sim.time_s
            action["decisions"] = {}
            observed = {p.unitId: p for p in packets}
            for uid, mode in modes.items():
                packet = observed.get(uid)
                requested = action["requested"][uid]
                exchange = next((swap for swap in reversed(swaps) if uid in (
                    [swap.get("requester")] + [donor.get("unit_id") for donor in swap.get("donors", [])]
                    if swap.get("donors") else [swap.get("donor"), swap.get("requester")])), None)
                if outcome == "fallback":
                    reason = "The agents missed the decision window. The backup controller selected a feasible stage."
                elif packet is None:
                    reason = "The unit was offline and could not report a reading."
                elif exchange:
                    donors = [donor["unit_id"] for donor in exchange.get("donors", [])]
                    if uid == exchange["requester"]:
                        peers = donors or [exchange["donor"]]
                        reason = f"A confirmed exchange with {', '.join(peers)} changed the power allocation."
                    else:
                        reason = f"A confirmed exchange supported request from {exchange['requester']}."
                elif intents[uid].get("desired", requested) not in feasible[uid]:
                    reason = "An equipment constraint limited the stages available this interval."
                elif mode != requested:
                    reason = "The shared power allocation limited the stage this unit received."
                else:
                    reason = "The power budget allowed the requested stage."
                action["decisions"][uid] = {
                    "decision_id": f"{attempt}-{uid}",
                    "decisionId": f"{attempt}-{uid}", "runId": run.id,
                    "simulationStep": base["simulationStep"], "unitId": uid,
                    "packetId": packet.packetId if packet else None, "actionId": attempt,
                    "observed_f": packet.data.avgTemp if packet else None,
                    "source": packet.source if packet else None,
                    "heat_target_f": packet.data.heatSetpoint if packet else None,
                    "cool_target_f": packet.data.coolSetpoint if packet else None,
                    "preferred_mode": str(intents[uid].get("desired", requested)),
                    "approved_mode": str(mode),
                    "acceptable_modes": [str(value) for value in intents[uid].get("choices", [])],
                    "reason_codes": intents[uid].get("reasonCodes", []),
                    "occupancy": intents[uid].get("occupancy", packet.data.occupancy if packet else None),
                    "design_occupancy": intents[uid].get("designOccupancy"),
                    "occupancy_ratio": intents[uid].get("occupancyRatio"),
                    "over_design_occupancy": intents[uid].get("overDesignOccupancy", False),
                    "needs_conditioning": intents[uid].get("needsConditioning", False),
                    "alternatives": intents[uid].get("alternatives", []),
                    "reason": reason,
                }
            for index, event in enumerate(events):
                event.setdefault("eventId", f"{run.id}:{base['simulationStep']}:{attempt}:event-{index}")
                event.update(runId=run.id, simulationStep=base["simulationStep"], actionId=attempt)
                unit_id = event.get("unitId")
                packet = observed.get(unit_id)
                event.setdefault("parentEventIds", [packet.packetId] if packet else [])
            for message in messages:
                trace_id = message.get("traceId", message.get("messageId", uuid4().hex))
                message.update(traceId=trace_id, runId=run.id, simulationStep=base["simulationStep"], actionId=attempt)
            try:
                await self.store.commit_interval(
                    action, [base | m | {"_id": m["traceId"]} for m in messages],
                    [base | e | {"_id": e["eventId"]} for e in events],
                    run.truth(), action["snapshot"] | {"persisted_run": run.persisted_state(),
                                                       "restoreCandidate": previous is self.run,
                                                       "expiresAt": now() + timedelta(hours=2)})
            except Exception as exc:
                if commit_ack and not commit_ack.done():
                    commit_ack.set_exception(exc)
                raise
            else:
                self.committed_attempts.append(attempt)
                self.committed_attempts = self.committed_attempts[-256:]
                if commit_ack and not commit_ack.done():
                    commit_ack.set_result(True)
            LOG.info("INTERVAL_COMMITTED run=%s step=%s outcome=%s power_kw=%.2f limit_kw=%.2f elapsed_ms=%d messages=%d", run.id, base["simulationStep"], outcome, action["power_kw"], limit, (monotonic()-started)*1000, len(messages))
            for uid, mode in modes.items():
                decision = action["decisions"][uid]
                LOG.info("UNIT_DECISION run=%s step=%s unit=%s requested=%s applied=%s outcome=%s signals=%s", run.id, base["simulationStep"], uid, action["requested"][uid], mode, outcome, ",".join(decision["reason_codes"]))
            for event in events:
                if event["kind"] != "AREA_ASSESSMENT":
                    LOG.info("AGENT_EVENT run=%s step=%s kind=%s status=%s unit=%s negotiation=%s", run.id, base["simulationStep"], event["kind"], event.get("status"), event.get("unitId","building"), event.get("negotiationId","-"))
            for item in run.interventions:
                if item.get("appliedStep") == base["simulationStep"]:
                    LOG.info("INTERVENTION_%s run=%s step=%s intervention=%s kind=%s target=%s", item["status"], run.id, base["simulationStep"], item["interventionId"], item["kind"], item["target"])
            return run, action | {"messages": messages, "events": events}
        except BaseException as exc:
            if commit_ack and not commit_ack.done():
                commit_ack.set_exception(exc)
            raise

    async def shutdown(self):
        await self.pause()
        if self.experiment_task:
            self.experiment_task.cancel()
            await asyncio.gather(self.experiment_task, return_exceptions=True)
        await self.record_run(self.run, "completed")
