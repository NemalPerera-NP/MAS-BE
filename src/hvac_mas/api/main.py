"""Local FastAPI control and observation interface."""
import asyncio
import logging
from copy import deepcopy
from datetime import datetime
from contextlib import asynccontextmanager
from hmac import compare_digest
from time import monotonic
from typing import Literal
from uuid import uuid4

from fastapi import APIRouter, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import Field, model_validator
from pymongo.errors import PyMongoError

from hvac_mas.control import load_rules
from hvac_mas.evidence import build_evidence, feature_status
from hvac_mas.models.building import ConfigModel, Mode, load_building
from hvac_mas.models.telemetry import Observation, Override
from hvac_mas.service import Service
from hvac_mas.settings import Settings
from hvac_mas.simulation.environment import EnvironmentChange, Fault, load_scenarios
from hvac_mas.simulation.equipment import stage_for
from hvac_mas.simulation.run import Run
from hvac_mas.storage import Store
from hvac_mas.topology import agent_topology
from hvac_mas.logging_config import configure_logging

LOG = logging.getLogger(__name__)


class Start(ConfigModel):
    scenario: str = "normal_cooling"
    seed: int | None = None
    wide_deadband: bool | None = None
    autoplay: bool = False


class Speed(ConfigModel):
    interval_seconds: float = Field(ge=0.25, le=30)


class ExperimentRequest(ConfigModel):
    scenario: str = "butterfly"
    base_scenario: str = "normal_cooling"
    seed: int = 42
    steps: int = Field(default=48, ge=3, le=120)
    power_limit_kw: float | None = Field(default=None, gt=0)
    perturbation_f: float = Field(default=0.2, gt=0, le=1)
    compare_from_current: bool = False
    disturbance: "Disturbance | None" = None


class Disturbance(ConfigModel):
    kind: Literal["temperature", "occupancy_pulse", "occupancy_transition", "sensor"]
    zone_id: str
    at_step: int = Field(default=1, ge=0, le=119)
    delta_f: float | None = Field(default=None, ge=-5, le=5)
    occupants: int | None = Field(default=None, ge=0, le=1000, strict=True)
    duration_minutes: int | None = Field(default=None, ge=5, le=1440, strict=True)
    sensor_field: Literal["avgTemp", "supplyTemp"] = "avgTemp"

    @model_validator(mode="after")
    def valid_disturbance(self):
        if self.duration_minutes is not None and self.duration_minutes % 5:
            raise ValueError("temporary visits must use five-minute multiples")
        if self.kind == "temperature" and (self.delta_f is None or self.delta_f == 0):
            raise ValueError("temperature disturbance requires a nonzero delta_f")
        if self.kind == "occupancy_pulse" and (self.occupants is None or self.occupants <= 0 or
                                                  self.duration_minutes is None):
            raise ValueError("occupancy_pulse requires positive occupants and duration_minutes")
        if self.kind == "occupancy_transition" and self.occupants is None:
            raise ValueError("occupancy_transition requires occupants")
        if self.kind == "sensor" and (self.delta_f is None or self.delta_f == 0):
            raise ValueError("sensor disturbance requires a nonzero delta_f")
        if self.kind not in ("occupancy_pulse",) and self.duration_minutes is not None:
            raise ValueError("duration_minutes is only valid for occupancy_pulse")
        return self


ExperimentRequest.model_rebuild()


class Decision(ConfigModel):
    error: str | None = None
    modes: dict[str, Mode] = Field(default_factory=dict)
    intents: dict[str, dict] = Field(default_factory=dict)
    messages: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    reservations: list[dict] = Field(default_factory=list)


class Heartbeat(ConfigModel):
    agents: list[str] = Field(default_factory=list)
    stream_ready: bool = False
    busy_job: str | None = None
    agent_activity: dict[str, datetime | None] = Field(default_factory=dict)


class Replay(ConfigModel):
    packet_id: str
    repeats: int = Field(default=1, ge=1, le=240)


class InterventionRequest(ConfigModel):
    kind: Literal["environment", "fault", "telemetry"]
    environment: EnvironmentChange | None = None
    fault: Fault | None = None
    telemetry: Override | None = None

    @model_validator(mode="after")
    def matching_payload(self):
        supplied = {"environment": self.environment, "fault": self.fault, "telemetry": self.telemetry}
        if supplied[self.kind] is None or sum(value is not None for value in supplied.values()) != 1:
            raise ValueError("provide exactly one payload matching intervention kind")
        return self


def create_app(config=None):
    settings = config or Settings()
    configure_logging(settings)

    @asynccontextmanager
    async def lifespan(app):
        store = Store(settings)
        try:
            await store.connect()
            building = load_building(settings.building_path)
            scenarios = load_scenarios(settings.scenarios_path)
            service = app.state.service = Service(settings, building, scenarios, load_rules(settings.rules_path), store)
            saved = await store.latest_simulation_state()
            if saved and saved.get("persisted_run"):
                control = service.rules.control
                service.run = Run.from_persisted_state(
                    building, saved["persisted_run"],
                    max_occupants_per_zone=control.max_occupants_per_zone,
                    max_temporary_occupancy_minutes=control.max_temporary_occupancy_minutes,
                    occupancy_forecast_minutes=control.prediction_horizon_minutes,
                    scenarios=scenarios)
            await store.put("building_configurations", building.building_id, building.model_dump(mode="json"))
            for key, scenario in scenarios.items():
                await store.put("scenario_definitions", key, scenario.model_dump(mode="json"))
            history = await store.query("equipment_actions", {"runId":service.run.id}, 20000,
                                        fields=("simulationStep","outcome","controller"))
            if history:
                latest = max(history,key=lambda a:a["simulationStep"])
                successful = [a["simulationStep"] for a in history if a.get("controller")=="mas" and a.get("outcome")=="committed"]
                service.execution = {"status":"fallback" if latest["outcome"]=="fallback" else "mas_committed",
                    "step":latest["simulationStep"], "controller":latest["controller"],
                    "fallback_count":sum(a["outcome"]=="fallback" for a in history),
                    "last_mas_step":max(successful,default=None)}
            LOG.info("API_READY run=%s storage=%s restored_step=%s", service.run.id, settings.storage, service.run.sim.step_number)
            await service.record_run(service.run, "paused")
            try:
                yield
            finally:
                await service.shutdown()
        finally:
            await store.close()

    app = FastAPI(title="HVAC MAS", version="0.1.0", lifespan=lifespan)
    origins = [origin.strip() for origin in settings.cors_origins.split(",") if origin.strip()]
    if origins:
        app.add_middleware(CORSMiddleware, allow_origins=origins, allow_methods=["GET", "POST"],
                           allow_headers=["Content-Type", "Authorization"])

    def service(request: Request):
        return request.app.state.service

    def internal(x_runtime_token: str = Header()):
        if not compare_digest(x_runtime_token.encode(), settings.runtime_token.get_secret_value().encode()):
            raise HTTPException(403, "invalid runtime credentials")

    def evaluator(authorization: str = Header(default="")):
        if settings.evaluator_token is None:
            return
        scheme, _, token = authorization.partition(" ")
        expected = settings.evaluator_token.get_secret_value()
        if scheme.lower() != "bearer" or not compare_digest(token.encode(), expected.encode()):
            raise HTTPException(401, "Evaluator access key required", headers={"WWW-Authenticate": "Bearer"})

    dashboard = APIRouter(dependencies=[Depends(evaluator)])

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(PyMongoError)
    async def unavailable(request, exc):
        # Keep credentials and topology details out of client responses and logs.
        LOG.warning("MongoDB request failed (%s)", type(exc).__name__)
        detail = ("MongoDB did not respond within the database operation timeout. Retry the connection."
                  if exc.timeout else "MongoDB is unavailable")
        return JSONResponse(status_code=503, content={"detail": detail}, headers={"Retry-After": "3"})

    @app.get("/health")
    async def health(s=Depends(service)):
        if s.store.client:
            await s.store.client.admin.command("ping")
        return {"status": "ok", "storage": settings.storage, "agent_ready": s.ready}

    @dashboard.get("/agents/topology")
    async def topology(s=Depends(service)):
        return agent_topology(s)

    @dashboard.get("/building/configuration")
    async def building(s=Depends(service)):
        return s.building

    @dashboard.get("/scenarios")
    async def scenarios(s=Depends(service)):
        return s.scenarios

    @dashboard.get("/simulation/state")
    async def state(s=Depends(service)):
        return s.snapshot()

    @dashboard.post("/simulation/start")
    @dashboard.post("/simulation/reset")
    async def reset(body: Start, s=Depends(service)):
        if body.scenario not in s.scenarios:
            raise HTTPException(404, "scenario unavailable")
        if s.experiment_task and not s.experiment_task.done():
            raise HTTPException(409, "wait for the experiment to end")
        if body.autoplay and not s.ready:
            raise HTTPException(503, "the SPADE runtime is not ready")
        await s.pause()
        result = await s.start_run(body.scenario, body.seed, body.wide_deadband)
        if body.autoplay:
            s.resume()
        return s.snapshot() if body.autoplay else result

    @dashboard.post("/simulation/pause")
    async def pause(s=Depends(service)):
        await s.pause()
        return s.snapshot()

    @dashboard.post("/simulation/resume")
    async def resume(s=Depends(service)):
        if s.experiment_task and not s.experiment_task.done():
            raise HTTPException(409, "wait for the experiment to end")
        s.resume()
        return s.snapshot()

    @dashboard.post("/simulation/speed")
    async def simulation_speed(body: Speed, s=Depends(service)):
        s.step_interval = body.interval_seconds
        return {"interval_seconds": s.step_interval}

    @dashboard.post("/simulation/step")
    async def step(s=Depends(service)):
        if s.experiment_task and not s.experiment_task.done():
            raise HTTPException(409, "wait for the experiment to end")
        if s.snapshot()["running"]:
            raise HTTPException(409, "pause before stepping manually")
        return await s.advance()

    async def queue_override(s, body):
        async with s.lock:
            if s.experiment_task and not s.experiment_task.done():
                raise HTTPException(409, "wait for the experiment to end before changing the live building")
            run = s.run
            if body.unit_id not in run.specs or run.sim.units[body.unit_id].offline:
                raise ValueError("unit is unknown or offline")
            operation = body.operation_id or uuid4().hex
            signature = (body.unit_id, body.fields, body.repeats, body.replay_of)
            if operation in run.operations:
                previous, response = run.operations[operation]
                if previous != signature:
                    raise HTTPException(409, "operation ID already used for a different override")
                return response
            if any(item["interventionId"] == operation for item in run.interventions):
                raise HTTPException(409, "intervention ID already used")
            reading = body.read(run.reading(body.unit_id))
            stage_for(run.specs[body.unit_id], reading.mode)
            if body.unit_id in run.overrides:
                raise HTTPException(409, "an override is already queued for this unit")
            response = {"operationId": operation, "interventionId": operation, "runId": run.id,
                        "status": "queued", "simulationStep": run.sim.step_number, "preview": reading,
                        "apply_at_minutes":run.sim.time_s/60,
                        "visible_after_minutes":(run.sim.time_s+run.sim.step_seconds)/60}
            candidate = deepcopy(run)
            candidate.overrides[body.unit_id] = body.model_copy(update={"operation_id": operation})
            candidate.operations[operation] = (signature, response)
            candidate.interventions.append({"interventionId": operation, "runId": run.id,
                "kind": "telemetry", "target": body.unit_id, "requestedStep": run.sim.step_number,
                "status": "QUEUED", "createdStep": run.sim.step_number, "appliedStep": None,
                "details": {"fields": body.fields, "repeats": body.repeats, "replayOf": body.replay_of}})
            await s.persist_current_state(candidate)
            s.run = candidate
            LOG.info("INTERVENTION_QUEUED run=%s step=%s intervention=%s kind=telemetry", run.id, run.sim.step_number, operation)
            return response


    @dashboard.post("/telemetry/inject", status_code=202)
    async def inject(body: Override, s=Depends(service)):
        return await queue_override(s, body)

    @dashboard.post("/telemetry/replay", status_code=202)
    async def replay(body: Replay, s=Depends(service)):
        row = await s.store.get("telemetry", body.packet_id)
        if not row:
            raise HTTPException(404, "packet unavailable")
        packet = Observation.model_validate({k: v for k, v in row.items() if k != "ingestedAt"})
        fields = packet.data.model_dump(exclude={"setTemp", "supplyDelta", "tempDiff", "coolingErrorF",
            "heatingErrorF", "telemetrySchemaVersion", "expectedOccupancy", "occupancyForecastMinutes",
            "occupiedHeatSetpoint", "occupiedCoolSetpoint", "occupancyState"})
        return await queue_override(s, Override(unit_id=packet.unitId, fields=fields,
                                                repeats=body.repeats, replay_of=packet.packetId))

    async def queue_fault(s, body):
        async with s.lock:
            if s.experiment_task and not s.experiment_task.done():
                raise HTTPException(409, "wait for the experiment to end before changing the live building")
            s.run.validate_fault(body)
            run = s.run
            at_step = max(body.at_step, run.sim.step_number)
            identity = body.intervention_id or uuid4().hex
            if any(item["interventionId"] == identity for item in run.interventions):
                raise HTTPException(409, "intervention ID already used")
            candidate = deepcopy(run)
            scheduled = body.model_copy(update={"at_step": at_step, "intervention_id": identity})
            candidate.faults.append(scheduled)
            candidate.interventions.append({"interventionId": identity, "runId": run.id, "kind": "fault",
                "target": body.unit_id, "requestedStep": at_step, "status": "QUEUED",
                "createdStep": run.sim.step_number, "appliedStep": None,
                "details": scheduled.model_dump(mode="json", exclude={"intervention_id"})})
            await s.persist_current_state(candidate)
            s.run = candidate
            LOG.info("INTERVENTION_QUEUED run=%s step=%s intervention=%s", run.id, at_step, identity)
            return {"interventionId": identity, "runId": run.id, "status": "scheduled", "simulationStep": at_step,
                    "apply_at_minutes":at_step*run.sim.step_seconds/60,
                    "visible_after_minutes":(at_step+1)*run.sim.step_seconds/60}

    @dashboard.post("/faults/inject", status_code=202)
    async def faults(body: Fault, s=Depends(service)):
        return await queue_fault(s, body)

    async def queue_environment(s, body):
        async with s.lock:
            if s.experiment_task and not s.experiment_task.done():
                raise HTTPException(409, "wait for the experiment to end before changing the live building")
            run = s.run
            run.validate_change(body)
            if body.zone_temperature_f and s.snapshot()["running"]:
                raise HTTPException(409, "pause before editing physical zone temperature")
            at_step = max(body.at_step, run.sim.step_number)
            identity = body.intervention_id or uuid4().hex
            if any(item["interventionId"] == identity for item in run.interventions):
                raise HTTPException(409, "intervention ID already used")
            candidate = deepcopy(run)
            scheduled = body.model_copy(update={"at_step": at_step, "intervention_id": identity})
            candidate.changes.append(scheduled)
            target_zones = (set(body.occupancy) | set(body.zone_temperature_f) | set(body.setpoints_f) |
                            {zone for action in body.occupancy_actions
                             for zone in (action.zone_id, action.source_zone_id, action.destination_zone_id)
                             if zone})
            target = sorted(target_zones) or "building"
            candidate.interventions.append({"interventionId": identity, "runId": run.id, "kind": "environment",
                "target": target, "requestedStep": at_step, "status": "QUEUED",
                "createdStep": run.sim.step_number, "appliedStep": None,
                "details": scheduled.model_dump(mode="json", exclude={"intervention_id"})})
            await s.persist_current_state(candidate)
            s.run = candidate
            LOG.info("INTERVENTION_QUEUED run=%s step=%s intervention=%s", run.id, at_step, identity)
            return {"interventionId": identity, "runId": run.id, "status": "scheduled", "simulationStep": at_step,
                    "apply_at_minutes":at_step*run.sim.step_seconds/60,
                    "visible_after_minutes":(at_step+1)*run.sim.step_seconds/60}

    @dashboard.post("/environment/change", status_code=202)
    async def environment(body: EnvironmentChange, s=Depends(service)):
        return await queue_environment(s, body)

    @dashboard.post("/interventions", status_code=202)
    async def interventions(body: InterventionRequest, s=Depends(service)):
        if body.kind == "environment":
            return await queue_environment(s, body.environment)
        if body.kind == "fault":
            return await queue_fault(s, body.fault)
        return await queue_override(s, body.telemetry)

    @dashboard.get("/interventions")
    async def intervention_status(run_id: str | None = None, s=Depends(service)):
        selected = run_id or s.run.id
        if selected == s.run.id:
            items = s.run.interventions
        else:
            saved = await s.store.get("simulation_state", selected)
            items = (saved or {}).get("persisted_run", {}).get("interventions", [])
        return {"runId": selected, "interventions": list(reversed(items[-100:]))}

    @dashboard.get("/evidence")
    async def causal_evidence(run_id: str | None = None,
                              limit: int = Query(300, ge=1, le=5000), s=Depends(service)):
        selected = run_id or s.run.id
        query = {"runId": selected}
        through_step = s.run.sim.step_number if selected == s.run.id else None
        actions, messages, events, telemetry = await asyncio.gather(
            s.store.query("equipment_actions", query, limit), s.store.query("mas_messages", query, limit),
            s.store.query("mas_events", query, limit), s.store.query("telemetry", query, limit))
        if through_step is not None:
            actions, messages, events, telemetry = ([r for r in rows if r.get("simulationStep",-1)<through_step]
                                                   for rows in (actions,messages,events,telemetry))
        if selected == s.run.id:
            intervention_rows = list(s.run.interventions)
        else:
            saved = await s.store.get("simulation_state", selected)
            intervention_rows = (saved or {}).get("persisted_run", {}).get("interventions", [])
        summaries = await s.store.query("experiment_results", {"sourceRunId": selected}, 1)
        experiment = summaries[0] if summaries else None
        evidence = build_evidence(selected, telemetry, actions, events, messages, intervention_rows,
                                  [experiment] if experiment else [])
        features = feature_status(actions, messages, events, experiment)
        return {"runId": selected, "through_step":through_step, "evidence": evidence, "features": features}

    @dashboard.get("/mas/features")
    async def mas_features(run_id: str | None = None, s=Depends(service)):
        selected = run_id or s.run.id
        query = {"runId": selected}
        actions, messages, events = await asyncio.gather(
            s.store.query("equipment_actions", query, 300, fields=("simulationStep", "modes")),
            s.store.query("mas_messages", query, 1000, fields=("status", "protocol", "traceId")),
            s.store.query("mas_events", query, 1000, fields=("kind", "eventId")))
        summaries = await s.store.query("experiment_results", {"sourceRunId": selected}, 1)
        experiment = summaries[0] if summaries else None
        has_intervention = any(item.get("status") in {"APPLIED", "QUEUED"} for item in
                               (s.run.interventions if selected == s.run.id else []))
        return {"runId": selected, "features": feature_status(
            actions, messages, events, experiment, has_intervention=has_intervention)}

    def filters(s, run_id, **extra):
        return {"runId": run_id or s.run.id} | {k: v for k, v in extra.items() if v is not None}

    @dashboard.get("/telemetry")
    async def telemetry(run_id: str | None = None, unit: str | None = None,
                        limit: int = Query(300, ge=1, le=5000), s=Depends(service)):
        return await s.store.query("telemetry", filters(s, run_id, unitId=unit), limit)

    @dashboard.get("/agents/messages")
    async def messages(run_id: str | None = None, sender: str | None = None, receiver: str | None = None,
                       conversation: str | None = None, limit: int = Query(300, ge=1, le=5000), s=Depends(service)):
        return await s.store.query("mas_messages", filters(s, run_id, sender=sender, receiver=receiver,
                                                            conversationId=conversation), limit)

    @dashboard.get("/events")
    async def events(run_id: str | None = None, unit: str | None = None,
                     limit: int = Query(300, ge=1, le=5000), s=Depends(service)):
        return await s.store.query("mas_events", filters(s, run_id, unitId=unit), limit)

    @dashboard.get("/simulation/actions")
    async def actions(run_id: str | None = None, limit: int = Query(300, ge=1, le=5000), s=Depends(service)):
        return await s.store.query("equipment_actions", filters(s, run_id), limit)

    @dashboard.get("/simulation/export")
    async def export(run_id: str | None = None, s=Depends(service)):
        from hvac_mas.evaluation import run_metrics
        select = filters(s, run_id)
        actions = list(reversed(await s.store.query("equipment_actions", select, 20000)))
        messages = await s.store.query("mas_messages", select, 20000)
        events = await s.store.query("mas_events", select, 20000)
        return {"run": await s.store.get("simulation_runs", select["runId"]), "runId": select["runId"],
                "telemetry": await s.store.query("telemetry", select, 20000),
                "messages": messages, "events": events,
                "actions": actions, "metrics": run_metrics(actions, events, messages, band_f=s.rules.control.comfort_band_c * 9 / 5)}

    @dashboard.post("/experiments/run", status_code=202)
    async def run_experiment(body: ExperimentRequest, s=Depends(service)):
        from hvac_mas.evaluation import experiment
        if body.scenario not in s.scenarios and body.scenario != "sensor_observation":
            raise HTTPException(404, "scenario unavailable")
        if body.scenario == "sensor_observation" and body.base_scenario not in s.scenarios:
            raise HTTPException(404, "base scenario unavailable")
        if not s.ready:
            raise HTTPException(503, "the SPADE runtime is not ready")
        if body.disturbance:
            if body.scenario == "sensor_observation" and body.disturbance.kind != "sensor":
                raise HTTPException(422, "sensor_observation requires a sensor-only disturbance")
            if body.scenario == "butterfly" and body.disturbance.kind == "sensor":
                raise HTTPException(422, "sensor-only disturbances require the sensor_observation experiment")
            if body.disturbance.zone_id not in {zone.zone_id for zone in s.building.zones}:
                raise ValueError("disturbance references an unknown zone")
            if body.disturbance.at_step >= body.steps:
                raise ValueError("disturbance at_step must occur within each trial")
        elif body.scenario == "sensor_observation":
            raise HTTPException(422, "sensor_observation requires a typed sensor disturbance")
        if s.experiment_task and not s.experiment_task.done():
            raise HTTPException(409, "an experiment is already running")
        await s.pause()
        if s.experiment_task and not s.experiment_task.done():
            raise HTTPException(409, "an experiment is already running")
        identity = uuid4().hex
        starting_state = deepcopy(s.run.persisted_state()) if body.compare_from_current else None
        source_run_id = s.run.id
        s.active_experiment = {"experimentId": identity, "status": "running"}

        async def execute():
            try:
                report = await experiment(s, identity, body, starting_state)
                report["sourceRunId"] = source_run_id
                LOG.info("EXPERIMENT_COMPLETED experiment=%s source_run=%s scenario=%s trials=%d",identity,source_run_id,body.scenario,len(report["trials"]))
                # Per-trial observations, messages and actions are stored separately.
                # Keep one bounded document for the summary and reconstruct the full report on read.
                summary = {**report, "trials": {name: {k: v for k, v in trial.items()
                    if k not in {"actions", "messages", "events"}} for name, trial in report["trials"].items()}}
                await s.store.insert("experiment_results", [summary | {"_id": identity}])
                s.active_experiment = None
            except Exception as exc:
                LOG.error("EXPERIMENT_FAILED experiment=%s scenario=%s error_type=%s", identity, body.scenario, type(exc).__name__)
                s.active_experiment = {"experimentId": identity, "status": "failed",
                                       "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}

        s.experiment_task = asyncio.create_task(execute())
        return {"experimentId": identity, "status": "running"}

    @dashboard.get("/experiments/{identity}")
    async def experiment_report(identity: str, include_messages: bool = False, s=Depends(service)):
        active = getattr(s, "active_experiment", None)
        if active and active["experimentId"] == identity:
            return active
        summary = await s.store.get("experiment_results", identity)
        if not summary:
            raise HTTPException(404, "experiment unavailable or expired")
        for trial in summary["trials"].values():
            select = {"runId": trial["runId"]}
            trial["actions"] = list(reversed(await s.store.query("equipment_actions", select, 120)))
            if include_messages:
                trial["messages"] = await s.store.query("mas_messages", select, 25000)
            trial["events"] = await s.store.query("mas_events", select, 25000)
        return summary

    app.include_router(dashboard)

    @app.post("/internal/heartbeat", dependencies=[Depends(internal)])
    async def heartbeat(body: Heartbeat, s=Depends(service)):
        previously_ready = s.ready
        s.connected_agents = sorted(set(body.agents))
        s.last_seen = monotonic()
        s.runtime_info = body.model_dump(mode="json", exclude={"agents"})
        if s.ready != previously_ready:
            LOG.info("RUNTIME_%s agents=%d stream_ready=%s", "READY" if s.ready else "NOT_READY",len(s.connected_agents),body.stream_ready)
        return {"accepted":True}

    @app.get("/internal/work", dependencies=[Depends(internal)])
    async def work(agents: str = "", s=Depends(service)):
        s.connected_agents = sorted(set(agents.split(",")) - {""})
        s.last_seen = monotonic()
        return s.pending[0] if s.pending else None

    @app.post("/internal/results/{job_id}", dependencies=[Depends(internal)])
    async def result(job_id: str, body: Decision, s=Depends(service)):
        pending = s.pending
        if not pending or pending[0]["id"] != job_id:
            raise HTTPException(409, "decision window closed")
        if not pending[1].done():
            pending[1].set_result(body.model_dump(mode="json", exclude_none=True))
        try:
            await asyncio.shield(pending[2])
        except Exception:
            raise HTTPException(503, "interval was not committed; the runtime may retry the decision") from None
        return {"accepted": True}

    @app.get("/internal/commit-status/{job_id}", dependencies=[Depends(internal)])
    async def commit_status(job_id: str, s=Depends(service)):
        return {"committed": job_id in s.committed_attempts}

    return app


app = create_app()
