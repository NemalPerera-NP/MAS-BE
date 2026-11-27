"""Local FastAPI control and observation interface."""
import asyncio
from contextlib import asynccontextmanager
from hmac import compare_digest
from time import monotonic
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import Field
from pymongo.errors import PyMongoError

from hvac_mas.control import load_rules
from hvac_mas.models.building import ConfigModel, Mode, load_building
from hvac_mas.models.telemetry import Observation, Override
from hvac_mas.service import Service
from hvac_mas.settings import Settings
from hvac_mas.simulation.environment import EnvironmentChange, Fault, load_scenarios
from hvac_mas.simulation.equipment import stage_for
from hvac_mas.simulation.run import Run
from hvac_mas.storage import Store


class Start(ConfigModel):
    scenario: str = "normal_cooling"
    seed: int | None = None
    wide_deadband: bool | None = None
    autoplay: bool = False


class ExperimentRequest(ConfigModel):
    scenario: str = "butterfly"
    seed: int = 42
    steps: int = Field(default=48, ge=3, le=120)
    power_limit_kw: float | None = Field(default=None, gt=0)
    perturbation_f: float = Field(default=0.2, gt=0, le=1)


class Decision(ConfigModel):
    error: str | None = None
    modes: dict[str, Mode] = Field(default_factory=dict)
    intents: dict[str, dict] = Field(default_factory=dict)
    messages: list[dict] = Field(default_factory=list)
    events: list[dict] = Field(default_factory=list)
    reservations: list[dict] = Field(default_factory=list)


class Replay(ConfigModel):
    packet_id: str
    repeats: int = Field(default=1, ge=1, le=240)


def create_app(config=None):
    settings = config or Settings()

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
                service.run = Run.from_persisted_state(building, saved["persisted_run"])
            await store.put("building_configurations", building.building_id, building.model_dump(mode="json"))
            for key, scenario in scenarios.items():
                await store.put("scenario_definitions", key, scenario.model_dump(mode="json"))
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
                           allow_headers=["Content-Type"])

    def service(request: Request):
        return request.app.state.service

    def internal(x_runtime_token: str = Header()):
        if not compare_digest(x_runtime_token, settings.runtime_token.get_secret_value()):
            raise HTTPException(403, "invalid runtime credentials")

    @app.exception_handler(ValueError)
    async def invalid(request, exc):
        return JSONResponse(status_code=422, content={"detail": str(exc)})

    @app.exception_handler(PyMongoError)
    async def unavailable(request, exc):
        return JSONResponse(status_code=503, content={"detail": "MongoDB is unavailable"})

    @app.get("/health")
    async def health(s=Depends(service)):
        if s.store.client:
            await s.store.client.admin.command("ping")
        return {"status": "ok", "storage": settings.storage, "agent_ready": s.ready}

    @app.get("/building/configuration")
    async def building(s=Depends(service)):
        return s.building

    @app.get("/scenarios")
    async def scenarios(s=Depends(service)):
        return s.scenarios

    @app.get("/simulation/state")
    async def state(s=Depends(service)):
        return s.snapshot()

    @app.post("/simulation/start")
    @app.post("/simulation/reset")
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

    @app.post("/simulation/pause")
    async def pause(s=Depends(service)):
        await s.pause()
        return s.snapshot()

    @app.post("/simulation/resume")
    async def resume(s=Depends(service)):
        if s.experiment_task and not s.experiment_task.done():
            raise HTTPException(409, "wait for the experiment to end")
        s.resume()
        return s.snapshot()

    @app.post("/simulation/step")
    async def step(s=Depends(service)):
        if s.experiment_task and not s.experiment_task.done():
            raise HTTPException(409, "wait for the experiment to end")
        if s.snapshot()["running"]:
            raise HTTPException(409, "pause before stepping manually")
        return await s.advance()

    async def queue_override(s, body):
        async with s.lock:
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
            reading = body.read(run.reading(body.unit_id))
            stage_for(run.specs[body.unit_id], reading.mode)
            if body.unit_id in run.overrides:
                raise HTTPException(409, "an override is already queued for this unit")
            response = {"operationId": operation, "status": "queued", "simulationStep": run.sim.step_number,
                        "preview": reading}
            run.overrides[body.unit_id] = body.model_copy(update={"operation_id": operation})
            run.operations[operation] = (signature, response)
            return response


    @app.post("/telemetry/inject", status_code=202)
    async def inject(body: Override, s=Depends(service)):
        return await queue_override(s, body)

    @app.post("/telemetry/replay", status_code=202)
    async def replay(body: Replay, s=Depends(service)):
        row = await s.store.get("telemetry", body.packet_id)
        if not row:
            raise HTTPException(404, "packet unavailable")
        packet = Observation.model_validate({k: v for k, v in row.items() if k != "ingestedAt"})
        fields = packet.data.model_dump(exclude={"setTemp", "supplyDelta", "tempDiff"})
        return await queue_override(s, Override(unit_id=packet.unitId, fields=fields,
                                                repeats=body.repeats, replay_of=packet.packetId))

    @app.post("/faults/inject", status_code=202)
    async def faults(body: Fault, s=Depends(service)):
        async with s.lock:
            s.run.validate_fault(body)
            at_step = max(body.at_step, s.run.sim.step_number)
            s.run.faults.append(body.model_copy(update={"at_step": at_step}))
            return {"status": "scheduled", "simulationStep": at_step}

    @app.post("/environment/change", status_code=202)
    async def environment(body: EnvironmentChange, s=Depends(service)):
        async with s.lock:
            s.run.validate_change(body)
            if body.zone_temperature_f and s.snapshot()["running"]:
                raise HTTPException(409, "pause before editing physical zone temperature")
            at_step = max(body.at_step, s.run.sim.step_number)
            s.run.changes.append(body.model_copy(update={"at_step": at_step}))
            return {"status": "scheduled", "simulationStep": at_step}

    def filters(s, run_id, **extra):
        return {"runId": run_id or s.run.id} | {k: v for k, v in extra.items() if v is not None}

    @app.get("/telemetry")
    async def telemetry(run_id: str | None = None, unit: str | None = None,
                        limit: int = Query(300, ge=1, le=5000), s=Depends(service)):
        return await s.store.query("telemetry", filters(s, run_id, unitId=unit), limit)

    @app.get("/agents/messages")
    async def messages(run_id: str | None = None, sender: str | None = None, receiver: str | None = None,
                       conversation: str | None = None, limit: int = Query(300, ge=1, le=5000), s=Depends(service)):
        return await s.store.query("mas_messages", filters(s, run_id, sender=sender, receiver=receiver,
                                                            conversationId=conversation), limit)

    @app.get("/events")
    async def events(run_id: str | None = None, unit: str | None = None,
                     limit: int = Query(300, ge=1, le=5000), s=Depends(service)):
        return await s.store.query("mas_events", filters(s, run_id, unitId=unit), limit)

    @app.get("/simulation/actions")
    async def actions(run_id: str | None = None, limit: int = Query(300, ge=1, le=5000), s=Depends(service)):
        return await s.store.query("equipment_actions", filters(s, run_id), limit)

    @app.get("/simulation/export")
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

    @app.post("/experiments/run", status_code=202)
    async def run_experiment(body: ExperimentRequest, s=Depends(service)):
        from hvac_mas.evaluation import experiment
        if body.scenario not in s.scenarios:
            raise HTTPException(404, "scenario unavailable")
        if not s.ready:
            raise HTTPException(503, "the SPADE runtime is not ready")
        if s.experiment_task and not s.experiment_task.done():
            raise HTTPException(409, "an experiment is already running")
        await s.pause()
        if s.experiment_task and not s.experiment_task.done():
            raise HTTPException(409, "an experiment is already running")
        identity = uuid4().hex
        s.active_experiment = {"experimentId": identity, "status": "running"}

        async def execute():
            try:
                report = await experiment(s, identity, body)
                # Per-trial observations, messages and actions are stored separately.
                # Keep one bounded document for the summary and reconstruct the full report on read.
                summary = {**report, "trials": {name: {k: v for k, v in trial.items()
                    if k not in {"actions", "messages", "events"}} for name, trial in report["trials"].items()}}
                await s.store.insert("experiment_results", [summary | {"_id": identity}])
                s.active_experiment = None
            except Exception as exc:
                s.active_experiment = {"experimentId": identity, "status": "failed",
                                       "error": str(exc) if isinstance(exc, ValueError) else type(exc).__name__}

        s.experiment_task = asyncio.create_task(execute())
        return {"experimentId": identity, "status": "running"}

    @app.get("/experiments/{identity}")
    async def experiment_report(identity: str, s=Depends(service)):
        active = getattr(s, "active_experiment", None)
        if active and active["experimentId"] == identity:
            return active
        summary = await s.store.get("experiment_results", identity)
        if not summary:
            raise HTTPException(404, "experiment unavailable or expired")
        for trial in summary["trials"].values():
            select = {"runId": trial["runId"]}
            trial["actions"] = list(reversed(await s.store.query("equipment_actions", select, 120)))
            trial["messages"] = await s.store.query("mas_messages", select, 25000)
            trial["events"] = await s.store.query("mas_events", select, 25000)
        return summary

    @app.get("/internal/work", dependencies=[Depends(internal)])
    async def work(agents: str = "", s=Depends(service)):
        s.connected_agents = sorted(set(agents.split(",")) - {""})
        s.last_seen = monotonic()
        return s.pending[0] if s.pending else None

    @app.post("/internal/results/{job_id}", dependencies=[Depends(internal)])
    async def result(job_id: str, body: Decision, s=Depends(service)):
        if not s.pending or s.pending[0]["id"] != job_id:
            raise HTTPException(409, "decision window closed")
        if not s.pending[1].done():
            s.pending[1].set_result(body.model_dump(mode="json", exclude_none=True))
        return {"accepted": True}

    return app


app = create_app()
