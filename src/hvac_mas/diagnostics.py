"""Observation-only FDD and recovery-aware comfort qualification."""
from dataclasses import dataclass, field
from datetime import datetime

from hvac_mas.models.building import Mode, f_to_c
from hvac_mas.simulation.equipment import EquipmentState, output, stage_for


@dataclass
class Track:
    status: str = "NORMAL"
    history: list[tuple[datetime, float]] = field(default_factory=list)
    recovery_deadline: float | None = None
    clean: int = 0


class Diagnostics:
    def __init__(self, building, rules):
        self.equipment = {unit.unit_id: unit for unit in building.equipment}
        self.rules = rules
        self.tracks = {}
        self.latest = {}
        self.samples = {}

    def observe(self, packet):
        key = (packet.runId, packet.unitId)
        previous = self.latest.get(key, -1)
        if packet.simulationStep <= previous:
            return []
        self.latest[key] = packet.simulationStep
        p = packet.data
        spec = self.equipment[packet.unitId]
        stage = stage_for(spec, p.mode)
        expected, actual = 0.0, 0.0
        prior = self.samples.get(key)
        self.samples[key] = p
        if stage:
            room = f_to_c(p.avgTemp)
            _, _, _, supply = output(spec, EquipmentState(actual_mode=p.mode), room, f_to_c(p.outsideTemp))
            expected = abs(room - supply) * 9 / 5
            actual = p.supplyDelta if str(p.mode).startswith("COOL") else -p.supplyDelta
        energetic = stage and p.mode not in (Mode.OFF, Mode.FAN) and p.power >= stage.electrical_power_kw * 0.6 and p.fanStatus == "ON"
        sensor = prior is not None and abs(p.avgTemp - prior.avgTemp) > 4 or (p.mode in (Mode.OFF, Mode.FAN) and abs(p.supplyDelta) > 4)
        events = []
        for kind, active, measured in (
            ("SENSOR_ANOMALY", sensor, abs(p.supplyDelta)),
            ("COOLING_FDD", bool(energetic and not sensor and str(p.mode).startswith("COOL") and expected > 0
                                 and actual < expected * self.rules.fdd_fraction), actual),
            ("HEATING_FDD", bool(energetic and not sensor and str(p.mode).startswith("HEAT") and expected > 0
                                 and actual < expected * self.rules.fdd_fraction), actual),
        ):
            event = self.update(packet, kind, active, measured, self.rules.fdd_occurrences,
                                {"expectedDeltaF": expected, "observedDeltaF": actual}, allow_recovery=False)
            if event:
                events.append(event)
        heating = str(p.mode).startswith("HEAT") or (p.mode in (Mode.OFF, Mode.FAN) and p.avgTemp < p.heatSetpoint)
        error = p.heatSetpoint - p.avgTemp if heating else p.avgTemp - p.coolSetpoint
        for kind, threshold in (("G2", self.rules.g2_f), ("G5", self.rules.g5_f)):
            event = self.update(packet, kind, not sensor and error >= threshold, error, self.rules.min_occurrences,
                                {"deviationF": error, "context": "heating" if heating else "cooling"},
                                allow_recovery=True)
            if event:
                events.append(event)
        return events

    def update(self, packet, kind, active, value, required, evidence, *, allow_recovery):
        t = packet.timestamp
        key = (packet.runId, packet.unitId, kind)
        track = self.tracks.setdefault(key, Track())
        old = track.status
        if active:
            track.clean = 0
            if old == "RECOVERING":
                track.status = "CONFIRMED"
            elif old in ("NORMAL", "RESOLVED", "CANCELLED"):
                track.status = "CANDIDATE"
            track.history.append((t, value))
        else:
            track.clean += 1
            if not allow_recovery:
                track.history.clear()
            if old == "CONFIRMED":
                track.status = "RECOVERING"
            elif old == "RECOVERING" and track.clean >= 2:
                track.status = "RESOLVED"
                track.history.clear()
            elif old in ("CANDIDATE", "MONITORING_RECOVERY") and track.clean >= 2:
                track.status = "CANCELLED"
                track.history.clear()
            if track.status in ("CANCELLED", "RESOLVED"):
                track.recovery_deadline = None
        track.history = [(when, val) for when, val in track.history
                         if 0 <= (t - when).total_seconds() <= self.rules.window_minutes * 60]
        count = len(track.history)
        minutes = (t - track.history[0][0]).total_seconds() / 60 if count else 0
        trend = (track.history[0][1] - value) / minutes if minutes else 0
        if active and count >= required and track.status in ("CANDIDATE", "MONITORING_RECOVERY"):
            qualifies_for_monitoring = allow_recovery and trend >= self.rules.improvement_f_per_minute
            if track.status == "CANDIDATE" and qualifies_for_monitoring:
                track.status = "MONITORING_RECOVERY"
                track.recovery_deadline = t.timestamp() + self.rules.recovery_minutes * 60
            elif not qualifies_for_monitoring or t.timestamp() >= (track.recovery_deadline or 0):
                track.status = "CONFIRMED"
        if track.status == old:
            return None
        return {"kind": kind, "status": track.status, "previous": old, "unitId": packet.unitId,
                "areaId": packet.areaId, "packetId": packet.packetId,
                "occurrences": count, "trendFPerMinute": trend, "evidence": evidence}
