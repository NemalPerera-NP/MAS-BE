"""Controlled comparisons using the same plant and real SPADE runtime as live operation."""
from collections import Counter
from math import sqrt
from statistics import mean

from hvac_mas.models.building import Mode
from hvac_mas.simulation.run import Run


def run_metrics(actions, events=(), messages=(), *, band_f=0.9):
    if not actions:
        return {}
    # Older records were produced by the five-minute simulator.
    actions = sorted({a["simulationStep"]: a for a in actions}.values(), key=lambda a: a["simulationStep"])
    previous = {}
    dwell = Counter({uid: 0 for uid in actions[0]["modes"]})
    discomfort = raw = violation_hours = energy = overlap = switches = 0
    covered_seconds = 0
    for action in actions:
        snap = action["snapshot"]
        end = action.get("interval_end_s", snap["time_s"])
        duration = end - action.get("interval_start_s", end - 300)
        covered_seconds += duration
        dt = duration / 3600
        energy += action["power_kw"] * dt
        errors = []
        for unit in snap["units"].values():
            reading = unit["reading"]
            physical = snap["zones"].get(unit.get("zone_id"), {}).get("temperature_f", reading["avgTemp"])
            error = max(0, physical - reading["coolSetpoint"], reading["heatSetpoint"] - physical)
            errors.append(error)
        raw += sum(errors)
        discomfort += sum(max(0, error - band_f) for error in errors) * dt
        violation_hours += sum(error > band_f for error in errors) * dt
        high = [uid for uid, mode in action["modes"].items() if mode == Mode.COOL2]
        overlap += int(len(high) >= 2)
        dwell.update(high)
        if previous.get("step") == action["simulationStep"] - 1:
            switches += sum(previous["modes"][uid] != mode for uid, mode in action["modes"].items())
        previous = {"step": action["simulationStep"], "modes": action["modes"]}
    n = len(actions)
    negotiations = [e for e in events if e["kind"] == "NEGOTIATION"]
    successes = sum(e["status"] == "COMPLETED" for e in negotiations)
    fairness_divisor = len(dwell) * sum(value ** 2 for value in dwell.values())
    return {"first_step": actions[0]["simulationStep"], "last_step": actions[-1]["simulationStep"],
            "covered_minutes": covered_seconds / 60,
            "missing_steps_through_last": actions[-1]["simulationStep"] + 1 - n,
            "peak_kw": max(a["power_kw"] for a in actions), "energy_kwh": energy,
            "capacity_violations": sum(a["power_kw"] > a["limit_kw"] + 1e-9 for a in actions),
            "discomfort_f_degree_hours": discomfort, "comfort_violation_zone_hours": violation_hours,
            "mean_deviation_f": raw / (n * len(actions[0]["modes"])),
            "stage_switches": switches, "high_stage_overlap_fraction": overlap / n,
            "high_stage_steps_by_unit": dict(dwell), "high_stage_access_jain": sum(dwell.values()) ** 2 / fairness_divisor
            if fairness_divisor else None,
            "message_count": len(messages), "received_messages": sum(m["status"] == "received" for m in messages),
            "mean_message_ms": mean(m["delay_ms"] for m in messages) if messages else None,
            "feasible_commitments": sum(a["outcome"] == "committed" for a in actions),
            "constrained_steps": sum(a["requested"] != a["modes"] for a in actions),
            "negotiations": len(negotiations), "agreements": successes,
            "negotiation_success_rate": successes / len(negotiations) if negotiations else None,
            "average_rounds": mean(e["round"] for e in negotiations) if negotiations else None,
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
    return {"affected_units": sorted(affected), "trajectory": differences,
            "divergent_stage_assignments": sum(len(row["changed_allocations"]) for row in differences),
            "additional_negotiations": perturbed["metrics"]["negotiations"] - control["metrics"]["negotiations"],
            "discomfort_delta_f_degree_hours": perturbed["metrics"]["discomfort_f_degree_hours"]
            - control["metrics"]["discomfort_f_degree_hours"]}


async def trial(service, scenario, controller, steps, perturbation_f=0, experiment_id=None):
    run = Run(service.building, scenario)
    starting_state = run.snapshot()
    if perturbation_f:
        # Compare one disturbance at the same point in the interval following startup.
        from hvac_mas.simulation.environment import EnvironmentChange
        baseline = starting_state["zones"]["B1"]["temperature_f"]
        run.changes.append(EnvironmentChange(at_step=1, zone_temperature_f={"B1": baseline + perturbation_f}))
    await service.record_run(run, "paused")
    actions, events, messages = [], [], []
    try:
        for _ in range(steps):
            if perturbation_f and run.sim.step_number == 1:
                # Apply relative to the naturally reached state so the only change is the declared perturbation.
                from hvac_mas.models.building import c_to_f
                run.changes[-1] = run.changes[-1].model_copy(update={
                    "zone_temperature_f": {"B1": c_to_f(run.sim.temperatures_c["B1"]) + perturbation_f}})
            run, result = await service.step(run, controller)
            messages.extend({"simulationStep": result["simulationStep"], **m} for m in result.pop("messages"))
            events.extend({"simulationStep": result["simulationStep"], **e} for e in result.pop("events"))
            actions.append(result)
            if experiment_id:
                service.active_experiment["steps_done"] = len(actions)
    finally:
        await service.record_run(run, "completed")
    metrics = run_metrics(actions, events, messages, band_f=service.rules.control.comfort_band_c * 9 / 5)
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
            "starting_state": starting_state, "perturbation_f": perturbation_f,
            "actions": actions, "events": events, "messages": messages, "metrics": metrics}


async def experiment(service, experiment_id, request):
    key = request.scenario
    scenario = service.scenarios[key].model_copy(update={"seed": request.seed})
    if request.power_limit_kw is not None:
        initial = scenario.initial.model_copy(update={"power_limit_kw": request.power_limit_kw})
        scenario = scenario.model_copy(update={"initial": initial})
    trials = {}
    cases = [("fixed", "fixed", 0), ("mas", "mas", 0), ("without_negotiation", "mas-no-negotiation", 0)]
    if key == "butterfly":
        cases = [("fixed_control", "fixed", 0), ("fixed_perturbed", "fixed", request.perturbation_f),
                 ("mas_control", "mas", 0), ("mas_perturbed", "mas", request.perturbation_f)]
    for name, controller, perturbation in cases:
        service.active_experiment.update(trial=name, trial_number=len(trials) + 1, trial_count=len(cases),
                                         steps_done=0, steps_total=request.steps)
        trials[name] = await trial(service, scenario, controller, request.steps, perturbation, experiment_id)
    comparisons = {}
    if key == "butterfly":
        for strategy in ("fixed", "mas"):
            comparisons[strategy] = compare(trials[f"{strategy}_control"], trials[f"{strategy}_perturbed"])
        mitigation = (trials["fixed_perturbed"]["metrics"]["discomfort_f_degree_hours"]
                      - trials["mas_perturbed"]["metrics"]["discomfort_f_degree_hours"])
        interpretation = (f"The {request.perturbation_f:+.2f} °F B1 disturbance changed "
                          f"{len(comparisons['mas']['affected_units'])} units’ MAS requests or allocations. "
                          f"Disturbed fixed-priority minus MAS discomfort was {mitigation:+.3f} °F·h. "
                          "The paired comparison measures threshold sensitivity.")
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
    return {"experimentId": experiment_id, "status": "completed", "scenario": key, "steps": request.steps,
            "seed": request.seed, "configuration": scenario.model_dump(mode="json"),
            "comparisons": comparisons, "trials": trials, "interpretation": interpretation}
