"""Necessary bus-energy bound; passing this check does not prove a feasible schedule."""
import math


def selected_demand_budget(batteries, required_bus_kwh):
    if type(required_bus_kwh) not in (int, float) or not math.isfinite(required_bus_kwh) or required_bus_kwh < 0:
        return None
    rows = []
    for name in ("battery_1", "battery_2"):
        battery = batteries.get(name) or {}
        values = [battery.get(k) for k in ("capacity_kwh", "initial_soc", "soc_min", "discharge_efficiency")]
        if not all(type(v) in (int, float) and math.isfinite(v) for v in values):
            return None  # Missing evidence must never become zero available energy.
        capacity, initial, floor, efficiency = values
        if capacity <= 0 or not 0 <= initial <= 1 or not 0 <= floor <= 1 or not 0 < efficiency <= 1:
            return None
        available = capacity * max(0, initial - floor) * efficiency
        rows.append({"battery_id": name, "initial_soc": initial, "soc_min": floor,
                     "capacity_kwh": capacity, "discharge_efficiency": efficiency,
                     "available_bus_kwh": available})
    available = math.fsum(row["available_bus_kwh"] for row in rows)
    return {"scope": "selected_candidate", "required_bus_kwh": required_bus_kwh,
            "available_bus_kwh": available, "shortfall_bus_kwh": max(0, required_bus_kwh - available),
            "insufficient": required_bus_kwh > available + max(1e-6, abs(required_bus_kwh) * 1e-9),
            "batteries": rows, "charging_supported": False}
