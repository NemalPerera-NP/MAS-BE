"""Regression guarantees from live dashboard review."""
import asyncio
from collections import OrderedDict
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from hvac_mas.api.main import create_app
from hvac_mas.control import load_rules
from hvac_mas.evaluation import run_metrics
from hvac_mas.evidence import feature_status
from hvac_mas.models.building import load_building
from hvac_mas.runtime import Runtime
from hvac_mas.service import Service
from hvac_mas.settings import Settings
from hvac_mas.simulation.environment import EnvironmentChange, load_scenarios
from hvac_mas.simulation.run import Run
from hvac_mas.storage import Store

@pytest.fixture
def service():
    settings=Settings(storage="memory")
    return Service(settings,load_building(settings.building_path),load_scenarios(settings.scenarios_path),
                   load_rules(settings.rules_path),Store(settings))

@pytest.mark.asyncio
async def test_export_handles_real_message_lifecycles_and_terminal_negotiation(service):
    _,action=await service.step(service.run,"fixed")
    messages=[{"messageId":"m","status":"sent"},{"messageId":"m","status":"received","delay_ms":12},
              {"messageId":"m","status":"processed","delay_ms":90},{"messageId":"lost","status":"sent"}]
    metrics=run_metrics([action],[{"kind":"NEGOTIATION","status":"COMPLETED"}],messages)
    assert metrics["message_count"]==2
    assert metrics["message_trace_count"]==4
    assert metrics["received_messages"]==1
    assert metrics["mean_message_ms"]==12
    assert metrics["average_rounds"] is None

@pytest.mark.parametrize("supported",[True,False])
def test_completed_butterfly_result_survives_long_run(supported):
    rows=feature_status([{"simulationStep":i} for i in range(5)],experiment={"scenario":"butterfly",
        "comparisons":{"mas":{"supports_butterfly_effect":supported}}})
    result=next(row for row in rows if row["feature"]=="butterfly_effect")
    assert result["status"]==("SUPPORTED_BY_EVIDENCE" if supported else "NOT_SUPPORTED")

def test_busy_worker_heartbeat_and_directory_are_independent_of_commit():
    with TestClient(create_app(Settings(storage="memory"))) as client:
        topology=client.get("/agents/topology").json()
        ids=[agent["id"] for agent in topology["agents"]]
        assert len(set(ids))==12
        assert {a["role"] for a in topology["agents"]}=={"Unit","Area","Gateway","Weather","Diagnostic","Power"}
        response=client.post("/internal/heartbeat",headers={"X-Runtime-Token":"local-development"},json={
            "agents":ids,"stream_ready":False,"busy_job":"still-deciding"})
        assert response.status_code==200
        state=client.get("/simulation/state").json()
        assert state["agent_ready"] is True and state["runtime"]["busy_job"]=="still-deciding"
        assert all(agent["connected"] for agent in client.get("/agents/topology").json()["agents"])

def test_sub_interval_visits_rejected_but_legacy_completed_history_restores(service):
    run=service.run
    with pytest.raises(ValueError,match="five-minute"):
        run.validate_change(EnvironmentChange(occupancy_actions=[{
            "kind":"temporary_add","zone_id":"A1","count":2,"duration_minutes":1}]))
    persisted=run.persisted_state()
    persisted["step_number"]=1
    persisted["time_s"]=300
    persisted["changes"].append({"at_step":0,"occupancy_actions":[{
        "kind":"temporary_add","zone_id":"A1","count":2,"duration_minutes":1}]})
    assert Run.from_persisted_state(service.building,persisted).sim.step_number==1

@pytest.mark.asyncio
async def test_temporary_visit_uses_interval_people_even_when_expired_at_snapshot(service):
    run=service.run
    before=run.context()[1]["A1"]
    run.changes.append(EnvironmentChange(occupancy_actions=[{
        "kind":"temporary_add","zone_id":"A1","count":2,"duration_minutes":5}]))
    _,action=await service.step(run,"fixed")
    assert action["interval_zone_context"]["A1"]["occupancy"]==before+2
    assert action["snapshot"]["zones"]["A1"]["occupancy"]==before
    altered=dict(action,interval_zone_context={z:dict(context,occupancy=0) for z,context in action["interval_zone_context"].items()})
    assert run_metrics([altered])["occupant_weighted_discomfort_person_degree_hours"]==0

@pytest.mark.asyncio
async def test_delayed_change_stream_reconciles_same_validated_job_packets(service):
    run=service.run
    packets=run.observations("handoff")
    runtime=Runtime.__new__(Runtime)
    runtime.settings=SimpleNamespace(decision_timeout=.04)
    runtime.building=service.building
    runtime.identities={uid:run.zones[spec.zone_id] for uid,spec in run.specs.items()}
    runtime.packets=OrderedDict()
    runtime.packet_arrival=asyncio.Event()
    job={"runId":run.id,"simulationStep":0,"packet_ids":[p.packetId for p in packets],
         "packets":[p.model_dump(mode="json") for p in packets]}
    assert [p.packetId for p in await runtime.fetch_packets(job)]==job["packet_ids"]
    runtime.packets.clear()
    job["packets"][0]["controlZoneId"]="WRONG"
    with pytest.raises(TimeoutError):
        await runtime.fetch_packets(job)

@pytest.mark.asyncio
async def test_invalid_worker_result_releases_commit_ack_without_changing_live_state(service):
    original=service.run
    pending_step=asyncio.create_task(service.step(original))
    while service.pending is None:
        await asyncio.sleep(0)
    _,response,ack=service.pending
    response.set_result({"modes":{},"intents":{},"messages":[],"events":[],"reservations":[]})
    with pytest.raises(ValueError,match="invalid allocation"):
        await pending_step
    with pytest.raises(ValueError,match="invalid allocation"):
        await ack
    assert service.pending is None
    assert service.run is original and service.run.sim.step_number==0
    assert await service.store.query("equipment_actions",{})==[]
