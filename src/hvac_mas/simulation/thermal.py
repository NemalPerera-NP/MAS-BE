"""First-order sensible heat balance. Heat flows are kW and capacitance kWh/K."""
import math
from collections.abc import Mapping

from hvac_mas.models.building import Building


def advance_temperatures(building: Building, temperatures_c: Mapping[str, float], *,
                         outdoor_c: float, occupancy: Mapping[str, int],
                         hvac_kw: Mapping[str, float], seconds: float = 300) -> dict[str, float]:
    """Advance all zones simultaneously. HVAC heat is positive for heating, negative for cooling.

    Euler integration is subdivided to avoid overshooting due to conductive exchange.
    """
    zones = {zone.zone_id: zone for zone in building.zones}
    if set(temperatures_c) != set(zones) or set(occupancy) != set(zones) or set(hvac_kw) != set(zones):
        raise ValueError('inputs must specify every configured zone exactly once')
    values = [outdoor_c, seconds, *temperatures_c.values(), *hvac_kw.values()]
    if not all(math.isfinite(v) for v in values) or seconds <= 0:
        raise ValueError('inputs must be finite and time must be positive')
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in occupancy.values()):
        raise ValueError('occupancy must be nonnegative integers')
    conductances = {zid: zone.outdoor_conductance_kw_per_c for zid, zone in zones.items()}
    for edge in building.couplings:
        conductances[edge.zone_a] += edge.conductance_kw_per_c
        conductances[edge.zone_b] += edge.conductance_kw_per_c
    duration_h = seconds / 3600
    # Ensure every Euler coefficient is at most 0.5, even for user-provided parameters.
    max_ratio = max(duration_h * conductances[zid] / zone.capacitance_kwh_per_c for zid, zone in zones.items())
    steps = max(1, math.ceil(max_ratio / 0.5))
    if steps > 10000:
        raise ValueError('integration interval requires too many substeps')
    dt = duration_h / steps
    temps = dict(temperatures_c)
    for _ in range(steps):
        flows = {zid: zone.outdoor_conductance_kw_per_c * (outdoor_c - temps[zid])
                 + zone.base_load_kw + zone.occupant_gain_kw * occupancy[zid] + hvac_kw[zid]
                 for zid, zone in zones.items()}
        for edge in building.couplings:
            flow = edge.conductance_kw_per_c * (temps[edge.zone_b] - temps[edge.zone_a])
            flows[edge.zone_a] += flow
            flows[edge.zone_b] -= flow
        temps = {zid: temps[zid] + dt * flows[zid] / zone.capacitance_kwh_per_c
                 for zid, zone in zones.items()}
    return temps
