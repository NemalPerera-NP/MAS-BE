"""Authoritative zone-level schedules and live occupancy interventions."""
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite

from hvac_mas.simulation.environment import OccupancyAction, OccupancyActionKind, OccupancyInterval


@dataclass(frozen=True)
class TemporaryPulse:
    zone_id: str
    count: int
    expires_at_s: float


class OccupancyManager:
    """Occupancy precedence: schedule < persistent override < additive temporary groups."""

    def __init__(self, schedules, zone_ids, max_occupants=1000):
        self.schedules = dict(schedules)
        self.zone_ids = set(zone_ids)
        self.max_occupants = max_occupants
        self.overrides = {}
        self.pulses = []
        unknown = set(self.schedules) - self.zone_ids
        if unknown:
            raise ValueError(f"occupancy schedule references unknown zones: {sorted(unknown)}")
        for intervals in self.schedules.values():
            ordered = sorted(intervals, key=lambda x: x.start_minute)
            if any(a.end_minute > b.start_minute for a, b in zip(ordered, ordered[1:])):
                raise ValueError("occupancy schedule intervals may not overlap")

    def scheduled(self, zone_id, time_s):
        minute = time_s / 60
        return next((interval.occupants for interval in self.schedules.get(zone_id, ())
                     if interval.start_minute <= minute < interval.end_minute), 0)

    def next_scheduled_arrival(self, zone_id, time_s):
        """Return the next scheduled nonzero interval start in simulation seconds."""
        if zone_id in self.overrides:
            return None
        starts = [interval.start_minute * 60 for interval in self.schedules.get(zone_id, ())
                  if interval.occupants > 0 and interval.start_minute * 60 > time_s]
        return min(starts, default=None)

    def counts(self, time_s):
        result = {zone: self.overrides.get(zone, self.scheduled(zone, time_s)) for zone in self.zone_ids}
        for pulse in self.pulses:
            if pulse.expires_at_s > time_s:
                result[pulse.zone_id] += pulse.count
        if any(not 0 <= count <= self.max_occupants for count in result.values()):
            raise ValueError(f"occupancy must remain between 0 and {self.max_occupants} per zone")
        return result

    def prune_expired(self, time_s):
        """Discard expired pulses at the actual simulation clock, not forecast times."""
        self.pulses = [pulse for pulse in self.pulses if pulse.expires_at_s > time_s]

    def _check_zone(self, zone):
        if zone not in self.zone_ids:
            raise ValueError(f"unknown zone {zone}")

    def apply_change(self, set_counts, actions, time_s):
        candidate = deepcopy(self)
        for zone, count in set_counts.items():
            candidate._check_zone(zone)
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= candidate.max_occupants:
                raise ValueError("occupancy must be an integer within the configured technical limit")
            candidate.overrides[zone] = count
        candidate.apply(actions, time_s)
        self.overrides, self.pulses = candidate.overrides, candidate.pulses

    def _base(self, zone, time_s):
        return self.overrides.get(zone, self.scheduled(zone, time_s))

    def _total(self, zone, base, pulses):
        return base[zone] + sum(p.count for p in pulses if p.zone_id == zone)

    def apply(self, actions, time_s):
        base = {zone: self._base(zone, time_s) for zone in self.zone_ids}
        overrides = dict(self.overrides)
        pulses = [p for p in self.pulses if p.expires_at_s > time_s]

        def remove(zone, amount, move_to=None):
            from_base = min(base[zone], amount)
            base[zone] -= from_base
            left = amount - from_base
            for index in range(len(pulses) - 1, -1, -1):
                pulse = pulses[index]
                if pulse.zone_id != zone or left == 0:
                    continue
                moved = min(pulse.count, left)
                left -= moved
                if moved == pulse.count:
                    pulses.pop(index)
                else:
                    pulses[index] = TemporaryPulse(zone, pulse.count - moved, pulse.expires_at_s)
                if move_to:
                    pulses.append(TemporaryPulse(move_to, moved, pulse.expires_at_s))
            if left:
                raise ValueError("not enough occupants in source zone")

        for action in actions:
            for zone in (action.zone_id, action.source_zone_id, action.destination_zone_id):
                if zone:
                    self._check_zone(zone)
            kind = action.kind
            if kind == OccupancyActionKind.SET:
                base[action.zone_id] = action.count
                overrides[action.zone_id] = action.count
            elif kind == OccupancyActionKind.ARRIVAL:
                base[action.zone_id] += action.count
                overrides[action.zone_id] = base[action.zone_id]
            elif kind == OccupancyActionKind.DEPARTURE:
                if self._total(action.zone_id, base, pulses) < action.count:
                    raise ValueError("cannot remove more occupants than are present in zone")
                remove(action.zone_id, action.count)
                overrides[action.zone_id] = base[action.zone_id]
            elif kind == OccupancyActionKind.TRANSFER:
                source, destination = action.source_zone_id, action.destination_zone_id
                if self._total(source, base, pulses) < action.count:
                    raise ValueError("cannot transfer more occupants than are present in source zone")
                if self._total(destination, base, pulses) + action.count > self.max_occupants:
                    raise ValueError("destination exceeds configured technical occupancy limit")
                from_base = min(base[source], action.count)
                base[source] -= from_base
                base[destination] += from_base
                remove(source, action.count - from_base, destination)
                overrides[source], overrides[destination] = base[source], base[destination]
            elif kind == OccupancyActionKind.TEMPORARY_ADD:
                if self._total(action.zone_id, base, pulses) + action.count > self.max_occupants:
                    raise ValueError("temporary occupancy exceeds configured technical limit")
                pulses.append(TemporaryPulse(action.zone_id, action.count,
                                             time_s + action.duration_minutes * 60))
            elif kind == OccupancyActionKind.RESTORE_SCHEDULE:
                base[action.zone_id] = self.scheduled(action.zone_id, time_s)
                overrides.pop(action.zone_id, None)
            else:  # pragma: no cover
                raise ValueError("unsupported occupancy action")
            if any(self._total(zone, base, pulses) > self.max_occupants or base[zone] < 0
                   for zone in self.zone_ids):
                raise ValueError("occupancy action batch exceeds configured limit")
        if any(self._total(zone, base, pulses) > self.max_occupants or base[zone] < 0
               for zone in self.zone_ids):
            raise ValueError("occupancy action batch exceeds configured limit")
        self.overrides, self.pulses = overrides, pulses

    def persisted_state(self):
        return {"overrides": self.overrides, "pulses": [p.__dict__ for p in self.pulses]}

    def restore_state(self, state, time_s=0):
        overrides = dict(state.get("overrides", state.get("occupancy_override", {})))
        for zone, count in overrides.items():
            self._check_zone(zone)
            if isinstance(count, bool) or not isinstance(count, int) or not 0 <= count <= self.max_occupants:
                raise ValueError("persisted occupancy override is invalid")
        pulses = [TemporaryPulse(**pulse) for pulse in state.get("pulses", [])]
        for pulse in pulses:
            self._check_zone(pulse.zone_id)
            if (isinstance(pulse.count, bool) or not isinstance(pulse.count, int) or pulse.count <= 0
                    or isinstance(pulse.expires_at_s, bool)
                    or not isinstance(pulse.expires_at_s, (int, float)) or not isfinite(pulse.expires_at_s)):
                raise ValueError("persisted temporary occupancy pulse is invalid")
        prior_overrides, prior_pulses = self.overrides, self.pulses
        self.overrides = overrides
        self.pulses = pulses
        try:
            self.counts(time_s)
        except Exception:
            self.overrides, self.pulses = prior_overrides, prior_pulses
            raise
