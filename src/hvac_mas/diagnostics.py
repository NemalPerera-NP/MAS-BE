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
        self.zones = {zone.zone_id: zone for zone in building.zones}
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
        zone = self.zones[spec.zone_id]
        occupancy_policy = zone.occupancy_policy
        occupancy_evidence = {
            "occupancy": p.occupancy,
            "expectedOccupancy": p.expectedOccupancy,
            "designOccupancy": occupancy_policy.design_occupancy,
            "occupancyRatio": occupancy_policy.occupancy_ratio(p.occupancy),
            "overDesignOccupancy": occupancy_policy.is_over_design(p.occupancy),
        }
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
            category = "sensor_quality" if kind == "SENSOR_ANOMALY" else "equipment_performance"
            event = self.update(packet, kind, active, measured, self.rules.fdd_occurrences,
                                {"category": category, "expectedDeltaF": expected,
                                 "observedDeltaF": actual, **occupancy_evidence}, allow_recovery=False)
            if event:
                events.append(event)
        heating = str(p.mode).startswith("HEAT") or (p.mode in (Mode.OFF, Mode.FAN) and p.avgTemp < p.heatSetpoint)
        error = (p.heatingErrorF if heating else p.coolingErrorF)
        if error is None:  # legacy telemetry schema
            error = max(0.0, p.heatSetpoint - p.avgTemp) if heating else max(0.0, p.avgTemp - p.coolSetpoint)
        # G2 and G5 are mutually exclusive states. G2 owns 2.. <5°F;
        # reaching the G5 threshold escalates the same deviation to G5.
        context = {"category": "comfort_deviation", "deviationF": error,
                   "context": "heating" if heating else "cooling", **occupancy_evidence}
        g2_key = (packet.runId, packet.unitId, "G2")
        g2_track = self.tracks.setdefault(g2_key, Track())
        g5_track = self.tracks.setdefault((packet.runId, packet.unitId, "G5"), Track())
        if not sensor and error >= self.rules.g5_f:
            if g2_track.status not in ("NORMAL", "CANCELLED", "RESOLVED", "ESCALATED"):
                previous_status = g2_track.status
                g2_track.status = "ESCALATED"
                g2_track.history.clear()
                g2_track.recovery_deadline = None
                events.append({"kind": "G2", "status": "ESCALATED", "previous": previous_status,
                               "unitId": packet.unitId, "areaId": packet.areaId, "packetId": packet.packetId,
                               "occurrences": 0, "trendFPerMinute": 0, "evidence": context})
        else:
            if g2_track.status == "ESCALATED" and g5_track.status in ("NORMAL", "CANCELLED", "RESOLVED"):
                g2_track.status = "NORMAL"
                g2_track.history.clear()
            if g2_track.status != "ESCALATED":
                g2_event = self.update(packet, "G2", not sensor and self.rules.g2_f <= error < self.rules.g5_f,
                                       error, self.rules.min_occurrences, context, allow_recovery=True)
                if g2_event:
                    events.append(g2_event)
        g5_event = self.update(packet, "G5", not sensor and error >= self.rules.g5_f, error,
                               self.rules.min_occurrences, context, allow_recovery=True)
        if g5_event:
            events.append(g5_event)
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
