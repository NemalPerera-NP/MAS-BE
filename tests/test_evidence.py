from hvac_mas.evidence import build_evidence, feature_status


def test_multiple_running_units_do_not_claim_emergent_behaviour():
    actions = [{"simulationStep": step, "modes": {"A": "COOL1", "B": "COOL1"}}
               for step in range(3)]
    statuses = {row["feature"]: row["status"] for row in feature_status(actions)}
    assert statuses["emergent_behaviour"] == "UNDER_EVALUATION"
    assert statuses["butterfly_effect"] == "UNDER_EVALUATION"


def test_processed_bargaining_attempt_is_recognized_as_negotiation_evidence():
    messages = [{"protocol": "hvac-bargaining", "status": "processed", "traceId": "m1"}]
    events = [{"kind": "NEGOTIATION", "status": "FAILED", "eventId": "e1"}]
    statuses = {row["feature"]: row["status"] for row in feature_status([], messages, events)}
    assert statuses["negotiation"] == "OBSERVED"


def test_sensor_observation_experiment_does_not_support_butterfly_claim():
    experiment = {"scenario": "sensor_observation", "comparisons": {
        "sensor_observation": {"supports_butterfly_effect": True}}}
    statuses = {row["feature"]: row["status"] for row in feature_status(experiment=experiment)}
    assert statuses["butterfly_effect"] == "NOT_OBSERVED"


def test_causal_trace_links_a_committed_action_to_its_intervention():
    rows = build_evidence("run-1", actions=[{
        "simulationStep": 2, "actionId": "action-1", "interventionIds": ["i-1"],
        "decisions": {}, "power_kw": 1.0, "limit_kw": 2.0, "modes": {},
    }], interventions=[{
        "interventionId": "i-1", "status": "APPLIED", "appliedStep": 2,
    }])
    action = next(row for row in rows if row["kind"] == "COMMITTED_ACTION")
    assert "intervention:i-1:applied" in action["parentEventIds"]
    assert any(row["evidenceId"] == "intervention:i-1:applied" for row in rows)
