"""Shared occupied/unoccupied comfort targets for simulation and agent decisions."""
from dataclasses import asdict, dataclass
from enum import StrEnum

from hvac_mas.models.building import Thermostat


class OccupancyState(StrEnum):
    OCCUPIED = "OCCUPIED"
    VACANCY_PENDING = "VACANCY_PENDING"
    UNOCCUPIED = "UNOCCUPIED"
    PRECONDITIONING = "PRECONDITIONING"


@dataclass(frozen=True)
class EffectiveComfort:
    state: OccupancyState
    heat_setpoint_c: float
    cool_setpoint_c: float
    fan_policy: str
    empty_since_s: float | None
    preconditioning_fraction: float = 0.0

    def persisted(self):
        value = asdict(self)
        value["state"] = self.state.value
        return value


class ComfortPolicyEngine:
    """Tracks vacancy confirmation and ramps targets toward scheduled arrivals."""

    def __init__(self):
        self.states: dict[str, EffectiveComfort] = {}

    @staticmethod
    def _fan_policy(policy):
        return "on" if policy == "continuous" else "auto"

    def evaluate(self, zone, occupied_targets: Thermostat, occupants: int, now_s: float,
                 next_arrival_s: float | None) -> EffectiveComfort:
        policy = zone.occupancy_policy
        previous = self.states.get(zone.zone_id)
        empty_since = previous.empty_since_s if previous else None
        if occupants > 0:
            state = OccupancyState.OCCUPIED
            empty_since = None
            fraction = 1.0
        else:
            if empty_since is None:
                empty_since = now_s
            arrival_delta = None if next_arrival_s is None else next_arrival_s - now_s
            if (policy.preconditioning_enabled and arrival_delta is not None
                    and 0 <= arrival_delta <= policy.preconditioning_minutes * 60):
                state = OccupancyState.PRECONDITIONING
                window_s = policy.preconditioning_minutes * 60
                fraction = 1.0 if window_s == 0 else max(0.0, min(1.0, 1 - arrival_delta / window_s))
            elif now_s - empty_since < policy.unoccupied_confirmation_minutes * 60:
                state = OccupancyState.VACANCY_PENDING
                fraction = 1.0
            else:
                state = OccupancyState.UNOCCUPIED
                fraction = 0.0

        if state == OccupancyState.UNOCCUPIED:
            heat = policy.unoccupied_heat_setpoint_c
            cool = policy.unoccupied_cool_setpoint_c
            fan = self._fan_policy(policy.unoccupied_fan_policy)
        elif state == OccupancyState.PRECONDITIONING:
            heat = policy.unoccupied_heat_setpoint_c + fraction * (
                occupied_targets.heat_setpoint_c - policy.unoccupied_heat_setpoint_c)
            cool = policy.unoccupied_cool_setpoint_c + fraction * (
                occupied_targets.cool_setpoint_c - policy.unoccupied_cool_setpoint_c)
            fan = self._fan_policy(policy.occupied_fan_policy)
        else:
            heat, cool = occupied_targets.heat_setpoint_c, occupied_targets.cool_setpoint_c
            fan = self._fan_policy(policy.occupied_fan_policy)

        result = EffectiveComfort(state, heat, cool, fan, empty_since, fraction)
        self.states[zone.zone_id] = result
        return result

    def persisted_state(self):
        return {zone_id: state.persisted() for zone_id, state in self.states.items()}

    def restore_state(self, payload):
        self.states = {zone_id: EffectiveComfort(
            state=OccupancyState(value["state"]), heat_setpoint_c=value["heat_setpoint_c"],
            cool_setpoint_c=value["cool_setpoint_c"], fan_policy=value["fan_policy"],
            empty_since_s=value.get("empty_since_s"),
            preconditioning_fraction=value.get("preconditioning_fraction", 0.0))
            for zone_id, value in payload.items()}
