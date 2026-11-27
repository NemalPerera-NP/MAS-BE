"""Verify real services on a disposable backend and MongoDB database."""
import os
from time import monotonic, sleep
from uuid import uuid4

import httpx
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError

API_URL = os.environ.get("API_URL", "http://127.0.0.1:8000")


def get(client, path, **params):
    response = client.get(path, params=params)
    response.raise_for_status()
    return response.json()


def post(client, path, body=None):
    response = client.post(path, json=body)
    response.raise_for_status()
    return response.json()


def await_value(function, deadline=45):
    until = monotonic() + deadline
    while monotonic() < until:
        result = function()
        if result:
            return result
        sleep(0.2)
    raise AssertionError("expected condition was not reached")


def steps(client, count):
    actions = [post(client, "/simulation/step") for _ in range(count)]
    for action in actions:
        assert action["outcome"] == "committed", action["events"]
        assert action["power_kw"] <= action["limit_kw"] + 1e-9
        assert len(action["reservations"]) == 6
        assert abs(sum(r["committedPowerKw"] for r in action["reservations"]) - action["power_kw"]) < 1e-8
    return actions


def experiment(client, name, count):
    start = post(client, "/experiments/run", {"scenario": name, "steps": count})

    def finished():
        result = get(client, f"/experiments/{start['experimentId']}")
        assert result["status"] != "failed", result
        return result if result["status"] == "completed" else None

    return await_value(finished, 120)


def verify_mongo(collection, db, packet_id):
    from datetime import UTC, datetime, timedelta

    row = collection.find_one({"packetId": packet_id})
    assert isinstance(row["ingestedAt"], datetime)
    assert collection.index_information()["ingestedAt_1"]["expireAfterSeconds"] == 7200
    assert collection.index_information()["packetId_1"]["unique"]
    for name in ["mas_messages", "mas_events", "simulation_truth", "equipment_actions", "experiment_results"]:
        assert db[name].index_information()["ingestedAt_1"]["expireAfterSeconds"] == 7200
    for name in ["building_configurations", "scenario_definitions", "change_stream_checkpoints"]:
        assert not any("expireAfterSeconds" in i for i in db[name].index_information().values())
    try:
        collection.insert_one({**row, "_id": uuid4().hex})
    except DuplicateKeyError:
        pass
    else:
        raise AssertionError("duplicate packet ID was accepted")
    probe = db[f"ttl_probe_{uuid4().hex}"]
    try:
        probe.create_index("ingestedAt", expireAfterSeconds=1)
        probe.insert_one({"ingestedAt": datetime.now(UTC) - timedelta(seconds=2)})
        await_value(lambda: probe.count_documents({}) == 0, 75)
    finally:
        probe.drop()


def main():
    key = os.getenv("EVALUATOR_TOKEN")
    headers = {"Authorization": f"Bearer {key}"} if key else {}
    with httpx.Client(base_url=API_URL, timeout=20, headers=headers, trust_env=False) as client:
        health = get(client, "/health")
        assert health["agent_ready"], health
        assert len(get(client, "/simulation/state")["agents"]) == 12
        post(client, "/simulation/reset", {"scenario": "normal_cooling"})
        body = {"unit_id": "RTU-B1", "fields": {"avgTemp": 86}, "operation_id": uuid4().hex}
        assert post(client, "/telemetry/inject", body) == post(client, "/telemetry/inject", body)
        action = steps(client, 1)[0]
        packet = get(client, "/telemetry", unit="RTU-B1")[0]
        assert packet["source"] == "MANUAL" and packet["data"]["avgTemp"] == 86
        assert abs(action["snapshot"]["zones"]["B1"]["temperature_f"] - 86) > 5
        observers = [m for m in action["messages"] if m["command"] == "observe" and m["replyTo"] is None
                     and m["payload"]["packet"]["packetId"] == packet["packetId"]]
        assert len(observers) == 1 and observers[0]["receiver"].startswith("hvac_b1@")
        assert len(get(client, "/telemetry", unit="RTU-B1")) == 1
        post(client, "/telemetry/replay", {"packet_id": packet["packetId"]})
        steps(client, 1)
        assert get(client, "/telemetry", unit="RTU-B1")[0]["replayOf"] == packet["packetId"]
        assert post(client, "/telemetry/inject", body)["status"] == "queued"
        steps(client, 1)
        assert get(client, "/telemetry", unit="RTU-B1")[0]["source"] == "SIMULATOR"
        print("PASS 12 XMPP agents, addressed telemetry, import/replay idempotency, and applied commands", flush=True)
        if health["storage"] == "mongo":
            with MongoClient(os.environ["MONGODB_URI"]) as mongo:
                db = mongo[os.environ["MONGODB_DATABASE"]]
                verify_mongo(db[os.getenv("MONGODB_TELEMETRY_COLLECTION", "hvac_telemetry")], db, packet["packetId"])
            print("PASS real BSON Date, unique telemetry and 7,200-second TTL indexes; disposable expiry", flush=True)
        post(client, "/simulation/reset", {"scenario": "normal_heating"})
        assert any(str(mode).startswith("HEAT") for mode in steps(client, 1)[0]["modes"].values())
        for scenario, kind in (("cooling_failure", "COOLING_FDD"), ("heating_failure", "HEATING_FDD")):
            post(client, "/simulation/reset", {"scenario": scenario})
            steps(client, 12)
            assert any(e["kind"] == kind and e["status"] == "CONFIRMED" for e in get(client, "/events"))
        post(client, "/faults/inject", {"unit_id": "RTU-A1", "kind": "offline"})
        action = steps(client, 1)[0]
        assert action["modes"]["RTU-A1"] == "OFF"
        assert "TELEMETRY_UNAVAILABLE" in {e["kind"] for e in action["events"]}
        print("PASS heating, cooling, fault qualification and offline isolation", flush=True)
        post(client, "/simulation/reset", {"scenario": "power_constraint"})
        actions = steps(client, 20)
        negotiation = [e for a in actions for e in a["events"] if e["kind"] == "NEGOTIATION"]
        print(f"Negotiation in constrained scenario: {len(negotiation)} attempts, "
              f"{sum(e['status']=='COMPLETED' for e in negotiation)} agreements", flush=True)
        assert negotiation and any(e["status"] == "COMPLETED" for e in negotiation)
        performances = {m["performative"] for a in actions for m in a["messages"] if m["protocol"] == "hvac-bargaining"}
        assert {"accept-proposal", "counter-propose", "reject-proposal", "confirm"} <= performances, performances
        report = experiment(client, "butterfly", 20)
        repeat = experiment(client, "butterfly", 20)
        assert report["comparisons"] == repeat["comparisons"]
        assert report["comparisons"]["mas"]["affected_units"]
        for name, trial in report["trials"].items():
            assert trial["metrics"]["discomfort_f_degree_hours"] == repeat["trials"][name]["metrics"]["discomfort_f_degree_hours"]
        print("PASS reproducible four-trial disturbance experiment:", report["interpretation"], flush=True)
        collective = experiment(client, "emergence", 48)
        print("PASS emergence comparison and negotiation ablation:", collective["interpretation"], flush=True)
        assert collective["trials"]["mas"]["metrics"]["capacity_violations"] == 0
        assert collective["trials"]["without_negotiation"]["metrics"]["negotiations"] == 0


if __name__ == "__main__":
    main()
