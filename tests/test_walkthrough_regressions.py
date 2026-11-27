from types import SimpleNamespace

import pytest

from hvac_mas.models.building import Mode, load_building
from hvac_mas.models.telemetry import Override
from hvac_mas.simulation.environment import Fault, load_scenarios
from hvac_mas.control import load_rules
from hvac_mas.evaluation import run_metrics
from hvac_mas.runtime import Runtime
from hvac_mas.service import Service
from hvac_mas.settings import Settings
from hvac_mas.storage import Store


@pytest.fixture
def service():
    settings = Settings(storage="memory")
    return Service(settings, load_building(settings.building_path), load_scenarios(settings.scenarios_path),
                   load_rules(settings.rules_path), Store(settings))


@pytest.mark.asyncio
async def test_partial_export_counts_only_recorded_intervals(service):
    run = service.run
    actions = []
    for _ in range(4):
        run, action = await service.step(run, "fixed")
        actions.append(action)
    sample = [actions[1], actions[3], actions[3]]
    metrics = run_metrics(sample)
    assert metrics["covered_minutes"] == 10
    assert metrics["missing_steps_through_last"] == 2
    assert metrics["energy_kwh"] == pytest.approx((actions[1]["power_kw"] + actions[3]["power_kw"]) / 12)
    assert metrics["stage_switches"] == 0
    for a in sample:
        for key in ("interval_start_s", "interval_end_s"):
            a.pop(key, None)
    assert run_metrics(sample) == metrics


@pytest.mark.asyncio
async def test_committed_interval_has_one_authoritative_action(service):
    before = service.run
    await service.step(before, "fixed")
    with pytest.raises(ValueError, match="already committed"):
        await service.step(before, "fixed")
    for name in ("equipment_actions", "simulation_truth", "simulation_state"):
        assert len(await service.store.query(name, {"runId": before.id})) == 1


@pytest.mark.asyncio
async def test_offline_edit_waits_for_real_packet(service):
    run = service.run
    unit = "RTU-B1"
    run.overrides[unit] = Override(unit_id=unit, fields={"avgTemp": 85})
    run.faults.extend([Fault(unit_id=unit, kind="offline"), Fault(unit_id=unit, kind="clear", at_step=1)])
    run, first = await service.step(run, "fixed")
    assert first["decisions"][unit]["observed_f"] is None and unit in run.overrides
    run, second = await service.step(run, "fixed")
    assert second["decisions"][unit]["observed_f"] == 85 and unit not in run.overrides
    assert second["decisions"][unit]["source"] == "MANUAL"


def test_experiment_restores_interactive_agent_memory():
    runtime = Runtime.__new__(Runtime)
    unit = SimpleNamespace(run_id="live", waiting_steps=3, previous_mode=Mode.COOL1,
                           last_reading=77, diagnostic={"COOLING_FDD"}, current={}, offer=None)
    runtime.units = {"B1": unit}
    runtime.agents = []
    runtime.diagnostic_agent = SimpleNamespace(run_id="live", diagnostics={"history": [1, 2]})
    runtime.previous_availability = ("live", {"B1"})
    runtime.live_memory = None
    runtime.use_experiment_memory(True)
    unit.waiting_steps = 0
    unit.diagnostic.clear()
    runtime.diagnostic_agent.diagnostics["history"].clear()
    runtime.previous_availability = ("trial", set())
    runtime.use_experiment_memory(True)
    runtime.use_experiment_memory(False)
    assert unit.waiting_steps == 3 and unit.diagnostic == {"COOLING_FDD"}
    assert runtime.previous_availability == ("live", {"B1"})
    assert runtime.diagnostic_agent.diagnostics == {"history": [1, 2]}
