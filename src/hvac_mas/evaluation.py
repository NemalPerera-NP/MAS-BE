"""Controlled comparisons using the same plant and real SPADE runtime as live operation."""
from collections import Counter, defaultdict
from copy import deepcopy
from math import ceil, sqrt, isfinite
from statistics import mean

from hvac_mas.models.building import Mode
from hvac_mas.models.telemetry import Override
from hvac_mas.simulation.environment import EnvironmentChange
from hvac_mas.simulation.run import Run


def run_metrics(actions, events=(), messages=(), *, band_f=0.9, evaluation_start_step=0):
    if not actions:
        return {}
    # Older records were produced by the five-minute simulator.
    actions = sorted({a["simulationStep"]: a for a in actions}.values(), key=lambda a: a["simulationStep"])
    previous = {}
    dwell = Counter({uid: 0 for uid in actions[0]["modes"]})
    discomfort = occupant_discomfort = raw = violation_hours = energy = overlap = switches = 0
    unoccupied_conditioning_hours = justified_high = served_high = 0
    covered_seconds = 0
    zone_discomfort = Counter()
    unmet_by_unit = Counter()
    current_unmet = Counter()
    longest_unmet_seconds = 0
    demand_values = []
    for action in actions:
        snap = action["snapshot"]
        end = action.get("interval_end_s", snap["time_s"])
        duration = end - action.get("interval_start_s", end - 300)
        covered_seconds += duration
        dt = duration / 3600
        energy += action["power_kw"] * dt
        demand_values.append(action["power_kw"])
        errors = []
        for uid, unit in snap["units"].items():
            reading = unit["reading"]
            zone_id = unit.get("zone_id")
            zone = snap["zones"].get(zone_id, {})
            physical = zone.get("temperature_f", reading["avgTemp"])
            cool = zone.get("effective_cool_setpoint_f", reading["coolSetpoint"])
            heat = zone.get("effective_heat_setpoint_f", reading["heatSetpoint"])
            interval = action.get("interval_zone_context", {}).get(zone_id, {})
            cool, heat = interval.get("cool_target_f", cool), interval.get("heat_target_f", heat)
            error = max(0, physical - cool, heat - physical)
            errors.append(error)
            excess = max(0, error - band_f)
            occupants = max(0, interval.get("occupancy", zone.get("occupancy", reading.get("occupancy", 0))))
            occupant_discomfort += excess * occupants * dt
            if zone_id:
                zone_discomfort[zone_id] += excess * dt
            mode = action.get("modes", {}).get(uid, "OFF")
            if occupants == 0 and str(mode).startswith(("COOL", "HEAT")):
                unoccupied_conditioning_hours += dt
            decision = action.get("decisions", {}).get(uid, {})
            preferred = action.get("requested", {}).get(uid)
            if decision.get("needs_conditioning") and preferred in (Mode.COOL2, Mode.HEAT2, "COOL2", "HEAT2"):
                justified_high += 1
                served_high += int(mode == preferred)
            unmet = bool(decision.get("needs_conditioning") and preferred != mode)
            if unmet:
                current_unmet[uid] += duration
                unmet_by_unit[uid] += duration
                longest_unmet_seconds = max(longest_unmet_seconds, current_unmet[uid])
            else:
                current_unmet[uid] = 0
        raw += sum(errors)
        discomfort += sum(max(0, error - band_f) for error in errors) * dt
        violation_hours += sum(error > band_f for error in errors) * dt
        high = [uid for uid, mode in action["modes"].items() if mode in (Mode.COOL2, Mode.HEAT2)]
        overlap += int(len(high) >= 2)
        dwell.update(high)
        if previous.get("step") == action["simulationStep"] - 1:
            switches += sum(previous["modes"][uid] != mode for uid, mode in action["modes"].items())
        previous = {"step": action["simulationStep"], "modes": action["modes"]}
    n = len(actions)
    negotiations = [e for e in events if e["kind"] == "NEGOTIATION"]
    successes = sum(e["status"] == "COMPLETED" for e in negotiations)
    fairness_divisor = len(dwell) * sum(value ** 2 for value in dwell.values())
    mean_demand = mean(demand_values)
    variance = mean((value - mean_demand) ** 2 for value in demand_values)
    # Lifecycle records are not separate logical messages. Measure each delivery once.
    deliveries = {m.get("messageId", m.get("traceId", str(i))): m["delay_ms"]
                  for i, m in enumerate(messages) if m.get("status") == "received"
                  and isinstance(m.get("delay_ms"), (int, float))
                  and isfinite(m["delay_ms"]) and m["delay_ms"] >= 0}
    return {"first_step": actions[0]["simulationStep"], "last_step": actions[-1]["simulationStep"],
            "covered_minutes": covered_seconds / 60,
            "missing_steps_through_last": actions[-1]["simulationStep"] - evaluation_start_step + 1 - n,
            "peak_kw": max(a["power_kw"] for a in actions), "energy_kwh": energy,
            "capacity_violations": sum(a["power_kw"] > a["limit_kw"] + 1e-9 for a in actions),
            "discomfort_f_degree_hours": discomfort, "comfort_violation_zone_hours": violation_hours,
            "occupant_weighted_discomfort_person_degree_hours": occupant_discomfort,
            "worst_zone_discomfort_f_degree_hours": max(zone_discomfort.values(), default=0),
            "discomfort_by_zone_f_degree_hours": dict(zone_discomfort),
            "longest_unmet_demand_minutes": longest_unmet_seconds / 60,
            "unmet_demand_minutes_by_unit": {uid: seconds / 60 for uid, seconds in unmet_by_unit.items()},
            "justified_high_stage_service_fraction": served_high / justified_high if justified_high else None,
            "unoccupied_mechanical_conditioning_hours": unoccupied_conditioning_hours,
            "demand_standard_deviation_kw": sqrt(variance),
            "mean_deviation_f": raw / (n * len(actions[0]["modes"])),
            "stage_switches": switches, "high_stage_overlap_fraction": overlap / n,
            "high_stage_steps_by_unit": dict(dwell), "high_stage_access_jain": sum(dwell.values()) ** 2 / fairness_divisor
            if fairness_divisor else None,
            "message_count": len({m.get("messageId", m.get("traceId", str(i))) for i, m in enumerate(messages)}),
            "message_trace_count": len(messages), "received_messages": len(deliveries),
            "mean_message_ms": mean(deliveries.values()) if deliveries else None,
            "feasible_commitments": sum(a["outcome"] == "committed" for a in actions),
            "constrained_steps": sum(a["requested"] != a["modes"] for a in actions),
            "negotiations": len(negotiations), "agreements": successes,
            "negotiation_success_rate": successes / len(negotiations) if negotiations else None,
            "average_rounds": mean(e["round"] for e in negotiations if "round" in e) if any("round" in e for e in negotiations) else None,
            "mean_agreement_ms": mean([e["duration_ms"] for e in negotiations
                                       if e["status"] == "COMPLETED" and "duration_ms" in e]) if
            any(e["status"] == "COMPLETED" and "duration_ms" in e for e in negotiations) else None}


def compare(control, perturbed):
    differences = []
    affected = set()
    for left, right in zip(control["actions"], perturbed["actions"], strict=True):
        zones = {z: right["snapshot"]["zones"][z]["temperature_f"] - value["temperature_f"]
                 for z, value in left["snapshot"]["zones"].items()}
        changed = [uid for uid, mode in left["modes"].items() if mode != right["modes"][uid]]
        requests = [uid for uid, mode in left["requested"].items() if mode != right["requested"][uid]]
        affected.update(changed)
        affected.update(requests)
        differences.append({"step": right["simulationStep"], "changed_allocations": changed,
                            "changed_requests": requests, "temperature_delta_f": zones,
                            "power_delta_kw": right["power_kw"] - left["power_kw"],
                            "temperature_rms_f": sqrt(mean(value**2 for value in zones.values()))})
    source_zone = (perturbed.get("disturbance") or {}).get("zone_id")
    downstream_deltas = defaultdict(list)
    for row in differences:
        for zone_id, delta in row["temperature_delta_f"].items():
            if zone_id != source_zone:
                downstream_deltas[zone_id].append(abs(delta))
    downstream_peak = {zone: max(values, default=0) for zone, values in downstream_deltas.items()}
    downstream_affected = sorted(zone for zone, delta in downstream_peak.items() if delta >= 0.05)
    disturbance = perturbed.get("disturbance") or {}
    source = disturbance.get("delta_f")
    source = abs(source) if source is not None else None
    disturbance_kind = disturbance.get("kind")
    # Sensor-only changes test observation robustness; they do not perturb the plant.
    # For physical disturbances, require propagation beyond the source and measurable
    # thermal divergence or coordinated allocation changes in multiple downstream units.
    source_units = {uid for uid, unit in control.get("starting_state", {}).get("units", {}).items()
                    if unit.get("zone_id") == source_zone}
    downstream_units = {uid for row in differences for uid in row["changed_allocations"] + row["changed_requests"]
                        if uid not in source_units}
    thermal_propagation = len(downstream_affected) >= 2 and (
        source is None or max((downstream_peak[z] for z in downstream_affected), default=0) > source)
    allocation_cascade = len(downstream_units) >= 2
    supports = (disturbance_kind != "sensor" and disturbance_kind in
                {"temperature", "occupancy_pulse", "occupancy_transition"}
                and (thermal_propagation or allocation_cascade))
    return {"affected_units": sorted(affected), "trajectory": differences,
            "divergent_stage_assignments": sum(len(row["changed_allocations"]) for row in differences),
            "max_downstream_temperature_delta_f": max(downstream_peak.values(), default=0),
            "downstream_zones_affected": downstream_affected,
            "initial_temperature_perturbation_f": source,
            "downstream_units_with_changed_allocation": sorted(downstream_units),
            "disturbance_kind": disturbance_kind,
            "supports_butterfly_effect": supports,
            "additional_negotiations": perturbed["metrics"]["negotiations"] - control["metrics"]["negotiations"],
            "discomfort_delta_f_degree_hours": perturbed["metrics"]["discomfort_f_degree_hours"]
            - control["metrics"]["discomfort_f_degree_hours"]}


def apply_disturbance(run, disturbance):
    if disturbance is None:
        return
    values = (disturbance.model_dump(mode="python") if hasattr(disturbance, "model_dump") else dict(disturbance))
    zone_id, step = values["zone_id"], values["at_step"]
    if zone_id not in run.zones:
        raise ValueError(f"disturbance references unknown zone {zone_id}")
    kind = values["kind"]
    if kind == "temperature":
        from hvac_mas.models.building import c_to_f
        temperature = c_to_f(run.sim.temperatures_c[zone_id]) + values["delta_f"]
        run.changes.append(EnvironmentChange(at_step=step, zone_temperature_f={zone_id: temperature}))
    elif kind == "occupancy_pulse":
        run.changes.append(EnvironmentChange(at_step=step, occupancy_actions=[{
            "kind": "temporary_add", "zone_id": zone_id, "count": values["occupants"],
            "duration_minutes": values["duration_minutes"]}]))
    elif kind == "occupancy_transition":
        run.changes.append(EnvironmentChange(at_step=step, occupancy_actions=[{
            "kind": "set", "zone_id": zone_id, "count": values["occupants"]}]))
    elif kind == "sensor":
        unit_id = run.zones[zone_id].unit_ids[0]
        reading = run.reading(unit_id)
        field = values["sensor_field"]
        run.overrides[unit_id] = Override(unit_id=unit_id,
            fields={field: getattr(reading, field) + values["delta_f"]},
            repeats=1, operation_id=f"experiment_sensor_{step}")


async def trial(service, scenario, controller, steps, disturbance=None, experiment_id=None, starting_state=None):
    control = service.rules.control
    if starting_state:
        run = Run.from_persisted_state(service.building, starting_state,
            max_occupants_per_zone=control.max_occupants_per_zone,
            max_temporary_occupancy_minutes=control.max_temporary_occupancy_minutes,
            occupancy_forecast_minutes=control.prediction_horizon_minutes,
            scenarios=service.scenarios)
        from uuid import uuid4
        queued_interventions = [item for item in run.interventions if item.get("status") == "QUEUED"]
        run.id = uuid4().hex
        run.interventions = queued_interventions
        for item in run.interventions:
            item["runId"] = run.id
    else:
        run = Run(service.building, scenario,
                  max_occupants_per_zone=control.max_occupants_per_zone,
                  max_temporary_occupancy_minutes=control.max_temporary_occupancy_minutes,
                  occupancy_forecast_minutes=control.prediction_horizon_minutes)
    starting_snapshot = run.snapshot()
    await service.record_run(run, "paused")
    actions, events, messages = [], [], []
    disturbance_values = (disturbance.model_dump(mode="python") if hasattr(disturbance, "model_dump")
                          else dict(disturbance) if disturbance else None)
    if disturbance_values:
        disturbance_values["at_step"] += starting_snapshot["step"]
    try:
        for _ in range(steps):
            if disturbance_values and run.sim.step_number == disturbance_values["at_step"]:
                apply_disturbance(run, disturbance_values)
            run, result = await service.step(run, controller)
            messages.extend({"simulationStep": result["simulationStep"], **m} for m in result.pop("messages"))
            events.extend({"simulationStep": result["simulationStep"], **e} for e in result.pop("events"))
            actions.append(result)
            if experiment_id:
                service.active_experiment["steps_done"] = len(actions)
    finally:
        await service.record_run(run, "completed")
    metrics = run_metrics(actions, events, messages, band_f=service.rules.control.comfort_band_c * 9 / 5,
                          evaluation_start_step=starting_snapshot["step"])
    if scenario.faults and controller != "fixed":
        predicted = {(e["unitId"], e["kind"]): e["simulationStep"] for e in reversed(events)
                     if e["status"] == "CONFIRMED" and e["kind"].endswith("FDD")}
        expected = {}
        for fault in scenario.faults:
            category = ("HEATING_FDD" if fault.kind == "heating_degradation" else "COOLING_FDD"
                        if fault.kind in ("cooling_degradation", "cool1_failure", "cool2_failure") else None)
            if category and fault.at_step < steps:
                expected[(fault.unit_id, category)] = fault.at_step
        correct = {key for key in predicted.keys() & expected.keys() if predicted[key] >= expected[key]}
        metrics.update(fdd_precision=len(correct) / len(predicted) if predicted else None,
                       fdd_recall=len(correct) / len(expected) if expected else None,
                       fdd_false_positives=len(predicted) - len(correct),
                       fdd_detection_delay_minutes={f"{unit}:{kind}": (predicted[(unit, kind)] -
                                                    expected[(unit, kind)]) * 5 for unit, kind in sorted(correct)})
    return {"runId": run.id, "controller": controller, "experimentId": experiment_id,
            "starting_state": starting_snapshot,
            "disturbance": (disturbance.model_dump(mode="json") if hasattr(disturbance, "model_dump")
                            else disturbance),
            "actions": actions, "events": events, "messages": messages, "metrics": metrics}


async def experiment(service, experiment_id, request, starting_state=None):
    key = request.scenario
    scenario_key = request.base_scenario if key == "sensor_observation" else key
    if starting_state:
        from hvac_mas.simulation.environment import Scenario
        scenario = Scenario.model_validate(starting_state["scenario"])
    else:
        scenario = service.scenarios[scenario_key].model_copy(update={"seed": request.seed})
    if request.power_limit_kw is not None and starting_state:
        starting_state = deepcopy(starting_state)
        starting_state["power_limit_kw"] = request.power_limit_kw
    if request.power_limit_kw is not None and not starting_state:
        initial = scenario.initial.model_copy(update={"power_limit_kw": request.power_limit_kw})
        scenario = scenario.model_copy(update={"initial": initial})
    trials = {}
    disturbance = request.disturbance
    if key in ("butterfly", "sensor_observation"):
        if disturbance is None:
            disturbance = {"kind": "temperature", "zone_id": "B1", "at_step": 1,
                           "delta_f": request.perturbation_f, "sensor_field": "avgTemp"}
        if key == "sensor_observation":
            cases = [("control", "mas", None), ("sensor_perturbed", "mas", disturbance)]
        else:
            cases = [("fixed_control", "fixed", None), ("fixed_perturbed", "fixed", disturbance),
                     ("mas_control", "mas", None), ("mas_perturbed", "mas", disturbance)]
    else:
        cases = [("fixed", "fixed", None), ("mas", "mas", None), ("without_negotiation", "mas-no-negotiation", None)]
    for name, controller, trial_disturbance in cases:
        service.active_experiment.update(trial=name, trial_number=len(trials) + 1, trial_count=len(cases),
                                         steps_done=0, steps_total=request.steps)
        trials[name] = await trial(service, scenario, controller, request.steps, trial_disturbance,
                                   experiment_id, starting_state)
    comparisons = {}
    if key == "butterfly":
        for strategy in ("fixed", "mas"):
            comparisons[strategy] = compare(trials[f"{strategy}_control"], trials[f"{strategy}_perturbed"])
        fixed_effect = (trials["fixed_perturbed"]["metrics"]["discomfort_f_degree_hours"]
                        - trials["fixed_control"]["metrics"]["discomfort_f_degree_hours"])
        mas_effect = (trials["mas_perturbed"]["metrics"]["discomfort_f_degree_hours"]
                      - trials["mas_control"]["metrics"]["discomfort_f_degree_hours"])
        mitigation = fixed_effect - mas_effect
        disturbance_label = disturbance.get("kind") if isinstance(disturbance, dict) else disturbance.kind
        interpretation = (f"The paired {disturbance_label} disturbance affected "
                          f"{len(comparisons['mas']['affected_units'])} units’ requests or allocations. "
                          f"Disturbance-related discomfort change was {fixed_effect:+.3f} °F·h for fixed priority "
                          f"and {mas_effect:+.3f} °F·h for MAS; estimated mitigation was {mitigation:+.3f} °F·h. "
                          f"Measured downstream temperature change peaked at "
                          f"{comparisons['mas']['max_downstream_temperature_delta_f']:.3f}°F. "
                          f"Butterfly evidence: {'supported' if comparisons['mas']['supports_butterfly_effect'] else 'not supported'}.")
    elif key == "sensor_observation":
        comparisons["sensor_observation"] = compare(trials["control"], trials["sensor_perturbed"])
        interpretation = ("Sensor-only observational comparison completed. The simulated plant was not directly "
                          "perturbed; compare telemetry and control responses without classifying this as a "
                          "butterfly-effect trial.")
    elif key in ("cooling_failure", "heating_failure"):
        metrics = trials["mas"]["metrics"]
        delays = metrics.get("fdd_detection_delay_minutes", {})
        detected = ", ".join(f"{unit.split(':')[0]} after {delay} simulated minutes" for unit, delay in delays.items())
        interpretation = (f"The diagnostic agent confirmed the modeled fault on {detected}." if detected else
                          "The diagnostic agent did not confirm the modeled fault during this trial.")
        interpretation += f" False equipment-fault confirmations: {metrics.get('fdd_false_positives', 0)}."
    else:
        mas, ablation = (trials[key]["metrics"] for key in ("mas", "without_negotiation"))
        difference = mas["high_stage_overlap_fraction"] - ablation["high_stage_overlap_fraction"]
        interpretation = (f"Negotiation produced {mas['agreements']} peer agreements. "
                          f"High-stage overlap differed by {difference * 100:+.1f} percentage points from the "
                          "same controller with negotiation disabled. See the stage timelines and access by unit.")
    evidence_ids = [f"{name}:{trials[name]['runId']}" for name in trials]
    mas_trials = [trial for trial in trials.values() if trial["controller"] in ("mas", "mas-no-negotiation")]
    interaction_evidence = any(trial["metrics"].get("negotiations", 0) > 0 for trial in mas_trials)
    emergence_assessment = {
        "status": "UNDER_EVALUATION",
        "evidenceRequirements": ["repeated collective allocation pattern", "need-adjusted service comparison",
                                 "agent interaction trace", "stable behavior across matched runs"],
        "interactionObserved": interaction_evidence,
        "note": "Multiple active units alone do not establish emergent behaviour.",
    } if key == "emergence" else None
    report = {"experimentId": experiment_id, "status": "completed", "scenario": key, "steps": request.steps,
            "seed": request.seed, "configuration": scenario.model_dump(mode="json"),
            "starting_state_source": "live_snapshot" if starting_state else "scenario",
            "sourceRunId": starting_state.get("runId") if starting_state else None,
            "comparisons": comparisons, "comparisonEvidenceIds": evidence_ids,
            "trials": trials, "interpretation": interpretation}
    if emergence_assessment:
        report["emergence_assessment"] = emergence_assessment
    return report
