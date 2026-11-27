"""Build an auditable causal timeline from persisted simulation evidence."""
from collections import defaultdict


def build_evidence(run_id, telemetry=(), actions=(), events=(), messages=(), interventions=(), experiments=()):
    """Return linked evidence records; every record points to persisted source IDs."""
    result = []
    packet_by_unit_step = {}
    action_by_step = {}
    for packet in telemetry:
        step, unit = packet.get("simulationStep"), packet.get("unitId")
        identity = packet.get("packetId", packet.get("_id"))
        if not identity:
            continue
        packet_by_unit_step[(step, unit)] = packet
        reading = packet.get("data", {})
        result.append({"evidenceId": f"observation:{identity}", "kind": "OBSERVATION",
            "runId": run_id, "simulationStep": step, "sourceId": identity,
            "parentEventIds": [], "unitId": unit, "zoneId": packet.get("controlZoneId"),
            "summary": (f"{unit} reported {reading.get('avgTemp', '—')}°F, "
                        f"{reading.get('occupancy', '—')} occupants, mode {reading.get('mode', '—')}"),
            "evidence": {"temperatureF": reading.get("avgTemp"), "occupancy": reading.get("occupancy"),
                         "expectedOccupancy": reading.get("expectedOccupancy"), "mode": reading.get("mode"),
                         "powerKw": reading.get("power")}})
    for item in interventions:
        iid = item.get("interventionId")
        if not iid:
            continue
        status = item.get("status", "QUEUED")
        result.append({"evidenceId": f"intervention:{iid}:{status.lower()}", "kind": "INTERVENTION",
            "runId": run_id, "simulationStep": item.get("appliedStep") if item.get("appliedStep") is not None else item.get("createdStep"),
            "sourceId": iid, "parentEventIds": [], "status": status, "target": item.get("target"),
            "summary": f"{item.get('kind', 'environment')} intervention {status.lower()} for {item.get('target', 'building')}",
            "evidence": item.get("details", {}) | ({"outcome": item["outcome"]} if item.get("outcome") else {})})
    for message in messages:
        identity = message.get("traceId", message.get("_id", message.get("messageId")))
        if not identity:
            continue
        payload = message.get("payload") or {}
        unit_id = payload.get("unitId")
        step = message.get("simulationStep")
        packet = packet_by_unit_step.get((step, unit_id))
        parents = [f"observation:{packet.get('packetId', packet.get('_id'))}"] if packet else []
        result.append({"evidenceId": f"message:{identity}", "kind": "MESSAGE_" + str(message.get("status", "unknown")).upper(),
            "runId": run_id, "simulationStep": step, "sourceId": identity, "messageId": message.get("messageId"),
            "parentEventIds": parents, "unitId": unit_id, "conversationId": message.get("conversationId"),
            "summary": f"{message.get('sender', 'agent')} {message.get('status', 'sent')} "
                       f"{message.get('command', 'message')} to {message.get('receiver', 'agent')}",
            "evidence": {"protocol": message.get("protocol"), "performative": message.get("performative"),
                         "command": message.get("command"), "payload": payload,
                         "processingResult": message.get("processingResult")}})
    for event in events:
        identity = event.get("eventId", event.get("_id"))
        if not identity:
            continue
        parents = [parent if str(parent).startswith(("observation:", "message:", "intervention:",
                                                      "decision:", "action:", "agent-event:"))
                   else f"observation:{parent}" for parent in event.get("parentEventIds", [])]
        result.append({"evidenceId": f"agent-event:{identity}", "kind": event.get("kind", "AGENT_EVENT"),
            "runId": run_id, "simulationStep": event.get("simulationStep"), "sourceId": identity,
            "parentEventIds": parents, "unitId": event.get("unitId"),
            "conversationId": event.get("conversationId"), "negotiationId": event.get("negotiationId"),
            "status": event.get("status"),
            "summary": f"{event.get('kind', 'Agent event')} {str(event.get('status', 'recorded')).lower()}",
            "evidence": event})
    for action in actions:
        step = action.get("simulationStep")
        action_id = action.get("actionId", action.get("operationId", action.get("_id")))
        action_by_step[step] = action
        decision_ids = []
        for unit_id, decision in action.get("decisions", {}).items():
            did = decision.get("decisionId", decision.get("decision_id", f"{action_id}-{unit_id}"))
            decision_ids.append(did)
            packet_id = decision.get("packetId")
            if not packet_id:
                packet = packet_by_unit_step.get((step, unit_id))
                packet_id = packet.get("packetId", packet.get("_id")) if packet else None
            result.append({"evidenceId": f"decision:{did}", "kind": "LOCAL_DECISION", "runId": run_id,
                "simulationStep": step, "sourceId": did, "unitId": unit_id,
                "parentEventIds": [f"observation:{packet_id}"] if packet_id else [], "actionId": action_id,
                "summary": f"{unit_id} requested {decision.get('preferred_mode', 'an HVAC stage')} "
                           f"and received {decision.get('approved_mode', 'an allocation')}",
                "evidence": decision})
        result.append({"evidenceId": f"action:{action_id}", "kind": "COMMITTED_ACTION", "runId": run_id,
            "simulationStep": step, "sourceId": action_id,
            "parentEventIds": [f"decision:{did}" for did in decision_ids]
                               + [f"intervention:{iid}:applied" for iid in action.get("interventionIds", [])],
            "summary": f"Equipment allocation committed at {action.get('power_kw', '—')} kW "
                       f"against {action.get('limit_kw', '—')} kW",
            "evidence": {"modes": action.get("modes"), "requested": action.get("requested"),
                         "powerKw": action.get("power_kw"), "limitKw": action.get("limit_kw"),
                         "outcome": action.get("outcome"), "controller": action.get("controller")}})
    for packet in telemetry:
        step, unit = packet.get("simulationStep"), packet.get("unitId")
        prior = action_by_step.get(step - 1)
        if prior and unit in prior.get("modes", {}):
            action_id = prior.get("actionId", prior.get("operationId", prior.get("_id")))
            result.append({"evidenceId": f"outcome:{packet.get('packetId', packet.get('_id'))}",
                "kind": "PHYSICAL_OUTCOME", "runId": run_id, "simulationStep": step,
                "sourceId": packet.get("packetId", packet.get("_id")), "unitId": unit,
                "parentEventIds": [f"action:{action_id}"],
                "summary": f"Next observation for {unit} followed the committed action",
                "evidence": {"temperatureF": packet.get("data", {}).get("avgTemp"),
                             "mode": packet.get("data", {}).get("mode")}})
    for experiment in experiments:
        for trial_name, trial in (experiment.get("trials") or {}).items():
            identity = f"{trial_name}:{trial.get('runId')}"
            if not trial.get("runId"):
                continue
            metrics = trial.get("metrics") or {}
            result.append({"evidenceId": identity, "kind": "EXPERIMENT_TRIAL", "runId": run_id,
                "sourceId": experiment.get("experimentId"), "simulationStep": None,
                "summary": f"{experiment.get('scenario', 'experiment')} {trial_name} trial: "
                           f"{metrics.get('energy_kwh', '—')} kWh; "
                           f"{metrics.get('occupant_weighted_discomfort_person_degree_hours', '—')} person·°F·h",
                "parentEventIds": [], "evidence": {"scenario": experiment.get("scenario"),
                    "trial": trial_name, "runId": trial.get("runId"), "metrics": metrics,
                    "interpretation": experiment.get("interpretation")}})
    result.sort(key=lambda row: (row.get("simulationStep") if row.get("simulationStep") is not None else -1,
                                 row["kind"], row["evidenceId"]))
    return result


def feature_status(actions=(), messages=(), events=(), experiment=None, *, has_intervention=False):
    """Conservative feature indicators that never treat an input alone as proof."""
    processed = [m for m in messages if m.get("status") == "processed"]
    bargaining = [m for m in messages if m.get("protocol") == "hvac-bargaining"
                  and m.get("status") in {"received", "processed"}]
    negotiation_events = [e for e in events if e.get("kind") == "NEGOTIATION"]
    coordination = [e for e in events if e.get("kind") == "AREA_ASSESSMENT"]
    multi_agent = bool(actions and actions[-1].get("modes") and len(actions[-1]["modes"]) > 1)
    butterfly = "NOT_OBSERVED"
    emergence = "NOT_OBSERVED"
    comparison = (experiment or {}).get("comparisons", {})
    if comparison and (experiment or {}).get("scenario") == "butterfly":
        butterfly = "SUPPORTED_BY_EVIDENCE" if comparison.get("mas", {}).get("supports_butterfly_effect") \
            else "NOT_SUPPORTED"
    if (experiment or {}).get("scenario") == "emergence":
        emergence = (experiment or {}).get("emergence_assessment", {}).get("status", "UNDER_EVALUATION")
    if (experiment or {}).get("scenario") not in {"butterfly", "emergence"} and len({a.get("simulationStep") for a in actions}) >= 3:
        butterfly = "UNDER_EVALUATION"
        # A long run with several active RTUs is not evidence of emergent behaviour.
        emergence = "UNDER_EVALUATION"
    result = [
        {"feature": "communication", "status": "OBSERVED" if processed else "IN_PROGRESS" if messages else "NOT_OBSERVED",
         "supportingEvidenceIds": [m.get("traceId", m.get("_id")) for m in processed[:20]]},
        {"feature": "coordination", "status": "OBSERVED" if coordination else "IN_PROGRESS" if multi_agent and actions else "NOT_OBSERVED",
         "supportingEvidenceIds": [e.get("eventId", e.get("_id")) for e in coordination[:20]]},
        {"feature": "negotiation", "status": "OBSERVED" if bargaining and negotiation_events else "IN_PROGRESS" if bargaining or negotiation_events else "NOT_OBSERVED",
         "supportingEvidenceIds": [e.get("eventId", e.get("_id")) for e in negotiation_events[:20]]},
        {"feature": "butterfly_effect", "status": butterfly,
         "supportingEvidenceIds": list((experiment or {}).get("comparisonEvidenceIds", []))},
        {"feature": "emergent_behaviour", "status": emergence,
         "supportingEvidenceIds": list((experiment or {}).get("comparisonEvidenceIds", []))},
    ]

    for item in result:
        prefix = "message:" if item["feature"] == "communication" else "agent-event:"
        if item["feature"] in {"communication", "coordination", "negotiation"}:
            item["supportingEvidenceIds"] = [prefix + str(identity) for identity in item["supportingEvidenceIds"] if identity]
        item["scope"] = "recorded_run_history"
    return result
