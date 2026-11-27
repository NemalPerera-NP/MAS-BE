import asyncio
from secrets import token_hex

import pytest
from pydantic import ValidationError

from fastapi.testclient import TestClient

from hvac_mas.api.main import create_app
from hvac_mas.settings import Settings


def test_health_distinguishes_storage_and_agent_readiness():
    with TestClient(create_app(Settings(storage="memory"))) as client:
        assert client.get("/health").json() == {"status": "ok", "storage": "memory", "agent_ready": False}
        assert client.post("/simulation/step").status_code == 422
        response = client.post("/telemetry/inject", json={"unit_id": "RTU-B1", "fields": {"avgTemp": 87}})
        assert response.status_code == 202 and response.json()["status"] == "queued"
        assert client.get("/telemetry").json() == []


def test_unified_intervention_is_persisted_and_reported():
    with TestClient(create_app(Settings(storage="memory"))) as client:
        response = client.post("/interventions", json={"kind": "environment", "environment": {
            "occupancy_actions": [{"kind": "arrival", "zone_id": "B1", "count": 2}]}})
        assert response.status_code == 202
        queued = response.json()
        assert queued["status"] == "scheduled" and queued["interventionId"]
        listing = client.get("/interventions").json()["interventions"]
        assert listing[0]["interventionId"] == queued["interventionId"]
        assert listing[0]["status"] == "QUEUED"
        service = client.app.state.service
        persisted = service.run.persisted_state()
        assert persisted["state_schema_version"] == 2
        restored = service.run.from_persisted_state(service.building, persisted)
        assert restored.interventions[0]["status"] == "QUEUED"
        assert restored.changes[-1].intervention_id == queued["interventionId"]


def test_replay_accepts_current_policy_telemetry_schema():
    with TestClient(create_app(Settings(storage="memory"))) as client:
        service = client.app.state.service
        packet = service.run.observations("saved")[0]
        asyncio.run(service.store.insert("telemetry", [packet.model_dump() | {"_id": packet.packetId}]))
        response = client.post("/telemetry/replay", json={"packet_id": packet.packetId})
        assert response.status_code == 202
        assert response.json()["status"] == "queued"


def test_playback_speed_is_validated_and_reported_in_live_state():
    with TestClient(create_app(Settings(storage="memory"))) as client:
        assert client.get("/simulation/state").json()["step_interval_seconds"] == 2
        response = client.post("/simulation/speed", json={"interval_seconds": 0.5})
        assert response.status_code == 200 and response.json() == {"interval_seconds": 0.5}
        assert client.get("/simulation/state").json()["step_interval_seconds"] == 0.5
        assert client.post("/simulation/speed", json={"interval_seconds": 0}).status_code == 422


def test_intervention_history_is_scoped_to_requested_run():
    with TestClient(create_app(Settings(storage="memory"))) as client:
        queued = client.post("/interventions", json={"kind":"environment","environment":{
            "occupancy_actions":[{"kind":"arrival","zone_id":"B1","count":1}]}}).json()
        old_run_id = queued["runId"]
        client.post("/simulation/reset", json={"scenario":"normal_cooling"})
        response = client.get(f"/interventions?run_id={old_run_id}")
        assert response.status_code == 200
        assert response.json()["runId"] == old_run_id
        assert response.json()["interventions"][0]["interventionId"] == queued["interventionId"]


def test_feature_comparison_ids_resolve_to_experiment_trial_evidence():
    with TestClient(create_app(Settings(storage="memory"))) as client:
        service = client.app.state.service
        run_id = service.run.id
        report = {"experimentId":"exp-1","sourceRunId":run_id,"scenario":"butterfly",
            "comparisons":{"mas":{"supports_butterfly_effect":False}},
            "comparisonEvidenceIds":["mas_control:trial-1"],
            "trials":{"mas_control":{"runId":"trial-1","metrics":{"energy_kwh":2.0,
                "occupant_weighted_discomfort_person_degree_hours":3.0}}}}
        asyncio.run(service.store.insert("experiment_results", [report | {"_id":"exp-1"}]))
        evidence = client.get(f"/evidence?run_id={run_id}").json()["evidence"]
        assert any(row["evidenceId"] == "mas_control:trial-1" for row in evidence)
        features = client.get(f"/mas/features?run_id={run_id}").json()["features"]
        butterfly = next(row for row in features if row["feature"] == "butterfly_effect")
        assert butterfly["status"] == "NOT_SUPPORTED"
        assert "mas_control:trial-1" in butterfly["supportingEvidenceIds"]

def test_hosted_credentials_protect_dashboard_and_separate_worker_access():
    runtime, evaluator = token_hex(32), token_hex(32)
    for tokens in ({}, {"runtime_token": runtime},
                   {"runtime_token": runtime, "evaluator_token": runtime}):
        with pytest.raises(ValidationError):
            Settings(_env_file=None, deployment_mode="hosted", storage="memory", **tokens)

    settings = Settings(_env_file=None, storage="memory", deployment_mode="hosted",
                        runtime_token=runtime, evaluator_token=evaluator,
                        cors_origins="https://dashboard.example.com")
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").status_code == 200
        assert client.get("/simulation/state").status_code == 401
        assert client.post("/experiments/run", json={}).status_code == 401
        assert client.post("/interventions", json={}).status_code == 401
        assert client.post("/simulation/reset", json={}).status_code == 401
        assert client.get("/simulation/state", headers={"Authorization": f"Bearer {runtime}"}).status_code == 401
        assert client.get("/internal/work", headers={"X-Runtime-Token": evaluator}).status_code == 403
        assert client.get("/internal/work", headers={"X-Runtime-Token": runtime}).status_code == 200
        headers = {"Authorization": f"Bearer {evaluator}"}
        assert client.get("/simulation/state", headers=headers).status_code == 200
        assert client.post("/simulation/speed", headers=headers, json={"interval_seconds": 1}).status_code == 200
        preflight = client.options("/simulation/speed", headers={
            "Origin": "https://dashboard.example.com",
            "Access-Control-Request-Method": "POST",
            "Access-Control-Request-Headers": "authorization,content-type",
        })
        assert preflight.status_code == 200
        assert "authorization" in preflight.headers["access-control-allow-headers"].lower()


def test_database_timeout_returns_retryable_503_without_hiding_failure(monkeypatch):
    from pymongo.errors import NetworkTimeout
    with TestClient(create_app(Settings(storage="memory"))) as client:
        service = client.app.state.service
        run_id = service.run.id
        async def timeout(*args, **kwargs):
            raise NetworkTimeout("private database topology details")
        monkeypatch.setattr(service.store, "query", timeout)
        response = client.get("/agents/messages")
        assert response.status_code == 503
        assert response.headers["retry-after"] == "3"
        assert "operation timeout" in response.json()["detail"]
        assert "private" not in response.text
        assert service.run.id == run_id
        assert service.store.settings.storage == "memory"


def test_feature_status_projection_preserves_classification_and_evidence_ids():
    with TestClient(create_app(Settings(storage="memory"))) as client:
        service = client.app.state.service
        run_id = service.run.id
        asyncio.run(service.store.insert("mas_messages", [{"_id":"message", "runId":run_id,
            "status":"processed", "protocol":"hvac-bargaining", "traceId":"trace",
            "payload":{"large_field":"excluded from feature checks"}}]))
        asyncio.run(service.store.insert("mas_events", [{"_id":"negotiation", "runId":run_id,
            "kind":"NEGOTIATION", "eventId":"negotiation-event"},
            {"_id":"area", "runId":run_id, "kind":"AREA_ASSESSMENT", "eventId":"area-event"}]))
        features = {row["feature"]: row for row in client.get("/mas/features").json()["features"]}
        assert features["communication"]["status"] == "OBSERVED"
        assert features["communication"]["supportingEvidenceIds"] == ["message:trace"]
        assert features["coordination"]["supportingEvidenceIds"] == ["agent-event:area-event"]
        assert features["negotiation"]["status"] == "OBSERVED"
        assert features["negotiation"]["supportingEvidenceIds"] == ["agent-event:negotiation-event"]
        projected = asyncio.run(service.store.query("mas_messages", {"runId":run_id}, fields=("traceId",)))
        assert projected == [{"traceId":"trace"}]
        original = asyncio.run(service.store.query("mas_messages", {"runId":run_id}))
        assert original[0]["payload"]["large_field"]
