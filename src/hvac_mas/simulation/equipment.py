"""Equipment operation with discrete stages and simulated protection timers."""
from dataclasses import dataclass
import math
from hvac_mas.models.building import Equipment, Mode, Stage


@dataclass
class EquipmentState:
    actual_mode: Mode = Mode.OFF
    requested_mode: Mode = Mode.OFF
    approved_mode: Mode = Mode.OFF
    # Initially off long enough to start; changing modes updates these timestamps.
    mode_since: float = 0.0
    off_since: float = -1e9
    on_since: float | None = None
    offline: bool = False
    cooling_health: float = 1.0
    heating_health: float = 1.0
    stage1_failed: bool = False
    stage2_failed: bool = False
    room_bias_c: float = 0.0
    supply_bias_c: float = 0.0
    min_run_until: float = 0.0
    restart_at: float = 0.0


def stage_for(spec: Equipment, mode: Mode) -> Stage | None:
    if mode == Mode.OFF:
        return None
    stage = next((s for s in spec.stages if s.name == mode and s.supported), None)
    if stage is None:
        raise ValueError(f'{spec.unit_id} does not support {mode}')
    return stage


def commit_mode(spec: Equipment, state: EquipmentState, mode: Mode, now_s: float) -> bool:
    """Apply an approved stage if timer and mode-interlock conditions allow it.

    A failed/offline unit immediately goes OFF as a protective exception. Switching
    heating to cooling requires an OFF transition first. Stage ratings are totals.
    """
    if not math.isfinite(now_s) or now_s < state.mode_since:
        raise ValueError('simulation time must be monotonic and finite')
    new = stage_for(spec, mode)
    state.approved_mode = mode
    old = stage_for(spec, state.actual_mode)
    if state.offline:
        if old:
            state.restart_at = max(state.restart_at, now_s + old.min_off_seconds)
            state.actual_mode, state.off_since, state.mode_since, state.on_since = Mode.OFF, now_s, now_s, None
        return mode == Mode.OFF
    if mode == state.actual_mode:
        return True
    old_cycle = old if old and old.name != Mode.FAN else None
    new_cycle = new if new and new.name != Mode.FAN else None
    if old_cycle and new_cycle and old_cycle.name[:4] != new_cycle.name[:4]:
        return False
    if old and now_s - state.mode_since < old.min_stage_duration_seconds:
        return False
    if old_cycle and not new_cycle and state.on_since is not None:
        if now_s < max(state.min_run_until, state.on_since + old_cycle.min_on_seconds):
            return False
    if new_cycle and not old_cycle and now_s < max(state.restart_at, state.off_since + new_cycle.min_off_seconds):
        return False
    if new_cycle and new_cycle.requires and (not old_cycle or old_cycle.name != new_cycle.requires):
        return False
    state.actual_mode = mode
    state.mode_since = now_s
    if new_cycle and not old_cycle:
        state.on_since = now_s
        state.min_run_until = now_s + new_cycle.min_on_seconds
    elif new_cycle and state.on_since is not None:
        state.min_run_until = max(state.min_run_until, state.on_since + new_cycle.min_on_seconds)
    elif old_cycle:
        state.restart_at = now_s + old_cycle.min_off_seconds
        state.off_since = now_s
        state.on_since = None
    return True


def output(spec: Equipment, state: EquipmentState, zone_c: float, outdoor_c: float) -> tuple[float, float, float, float]:
    """Return signed heat kW, electrical kW, fuel kW and supply-air °C."""
    if any(not math.isfinite(x) for x in (zone_c, outdoor_c, state.cooling_health, state.heating_health)):
        raise ValueError('non-finite equipment input')
    if not (0 <= state.cooling_health <= 1 and 0 <= state.heating_health <= 1):
        raise ValueError('health factors must be between 0 and 1')
    stage = stage_for(spec, state.actual_mode)
    if stage is None or state.offline:
        return 0.0, 0.0, 0.0, zone_c
    if stage.name == Mode.FAN:
        return 0.0, stage.electrical_power_kw, 0.0, zone_c
    cooling = stage.name.value.startswith('COOL')
    if cooling:
        ambient_factor = max(0.6, min(1.1, 1.0 - 0.01 * (outdoor_c - 35)))
        capacity = stage.thermal_capacity_kw
        if state.stage2_failed and stage.name == Mode.COOL2:
            first = next((s for s in spec.stages if s.name == Mode.COOL1 and s.supported), None)
            capacity = first.thermal_capacity_kw if first else 0
        heat = -capacity * ambient_factor * state.cooling_health if not state.stage1_failed else 0.0
    else:
        ambient_factor = max(0.4, min(1.1, 1.0 + 0.02 * (outdoor_c - 7))) if spec.heating_type == 'heat_pump' else 1.0
        heat = stage.thermal_capacity_kw * ambient_factor * state.heating_health
        if spec.heating_type == "heat_pump" and outdoor_c < -15:
            return 0.0, 0.0, 0.0, zone_c
    # Air density 1.2 kg/m³, specific heat 1.006 kJ/(kg K).
    supply = zone_c + heat / (stage.airflow_m3_s * 1.2 * 1.006)
    return heat, stage.electrical_power_kw, stage.fuel_input_kw, supply
