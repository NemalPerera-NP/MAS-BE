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
