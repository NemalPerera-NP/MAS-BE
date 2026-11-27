"""Agent directory derived from the configured building, not identifier conventions."""
def agent_topology(service):
    domain = service.settings.xmpp_domain
    jid = lambda local: f"{local}@{domain}"
    roles = [
        ("gateway", "Telemetry gateway", "Gateway", "Routes recorded observations and confirmed commands."),
        ("weather", "Weather agent", "Weather", "Shares outdoor conditions and the forecast."),
        ("diagnostic", "Diagnostic agent", "Diagnostic", "Qualifies persistent equipment and comfort problems from observations."),
        ("power", "Power budget agent", "Power", "Reserves electricity and validates feasible allocations and swaps."),
    ]
    nodes = [{"id": jid(key), "name": name, "role": role, "purpose": purpose}
             for key, name, role, purpose in roles]
    links = [{"source":jid("gateway"), "target":jid(key), "kind":"request / reply"}
             for key in ("weather", "diagnostic", "power")]
    for area in service.building.areas:
        if len(area.zone_ids) > 1:
            aid = jid(f"area_{area.area_id.lower()}")
            nodes.append({"id":aid, "name":f"{area.name} coordinator", "role":"Area", "area":area.area_id,
                          "purpose":"Aggregates occupancy, demand and operating flexibility in its area."})
            links.extend([{"source":jid("gateway"),"target":aid,"kind":"area observation / assessment"},
                          {"source":jid("diagnostic"),"target":aid,"kind":"diagnostic finding"}])
    zones = {z.zone_id:z for z in service.building.zones}
    for unit in service.building.equipment:
        uid = jid(f"hvac_{unit.zone_id.lower()}")
        nodes.append({"id":uid,"name":f"Room {unit.zone_id} agent", "role":"Unit", "unit":unit.unit_id,
                      "zone":unit.zone_id, "area":zones[unit.zone_id].area_id,
                      "purpose":"Predicts local comfort, requests feasible settings and evaluates peer concessions."})
        links.extend([{"source":jid("gateway"),"target":uid,"kind":"observation / equipment command"},
                      {"source":jid("diagnostic"),"target":uid,"kind":"diagnostic finding"},
                      {"source":uid,"target":jid("power"),"kind":"negotiated swap validation"}])
    units = [n for n in nodes if n["role"] == "Unit"]
    for i,left in enumerate(units):
        for right in units[i+1:]:
            links.append({"source":left["id"],"target":right["id"],"kind":"peer bargaining (when needed)"})
    for node in nodes:
        node["connected"] = service.ready and node["id"] in service.connected_agents
        node["last_message_at"] = service.runtime_info.get("agent_activity",{}).get(node["id"])
    return {"agents":nodes,"links":links,"runtime_ready":service.ready,
            "note":"Links show implemented communication routes. A route does not mean a message is currently being exchanged."}
