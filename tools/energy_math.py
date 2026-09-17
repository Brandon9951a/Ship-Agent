"""Pure numerical D2 primitives. No vessel parameters, safety approval or tool wiring.

Callers must supply an explicit, consistent measurement boundary. These helpers
do not convert motor-side measurements into battery-side energy.
"""

import math


def number(value, name, *, minimum=None):
    if type(value) not in (int, float):
        raise ValueError(f"{name} must be a finite number, not bool/text")
    try:
        value = float(value)
    except OverflowError as exc:
        raise ValueError(f"{name} must be finite") from exc
    if not math.isfinite(value) or (minimum is not None and value < minimum):
        raise ValueError(f"{name} outside finite permitted range")
    return value


def cubic_power(speed_kmh, coefficient):
    speed = number(speed_kmh, "speed_kmh", minimum=0)
    coefficient = number(coefficient, "coefficient", minimum=0)
    try:
        return number(coefficient * speed ** 3, "power_kw", minimum=0)
    except OverflowError as exc:
        raise ValueError("Power overflow") from exc


def segment_energy(distance_km, speed_kmh, waiting_h, coefficient, auxiliary_kw, *, scope):
    """Synthetic cubic propulsion + explicit auxiliary budget, with no peak claim.

    Assumption: zero propulsion during waiting, constant auxiliary during all time.
    Coefficient is propulsion-only at caller's boundary. Not a total-power fit.
    """
    distance = number(distance_km, "distance_km", minimum=0)
    speed = number(speed_kmh, "speed_kmh", minimum=0)
    waiting = number(waiting_h, "waiting_h", minimum=0)
    auxiliary = number(auxiliary_kw, "auxiliary_kw", minimum=0)
    if scope not in ("propulsion", "total"):
        raise ValueError("Explicit scope propulsion/total required")
    if speed == 0 and distance > 0:
        raise ValueError("Positive distance requires positive speed")
    moving = distance / speed if speed else 0
    duration = number(moving + waiting, "duration_h", minimum=0)
    propulsion = number(cubic_power(speed, coefficient) * moving, "propulsion_kwh", minimum=0)
    aux_energy = number(auxiliary * duration, "auxiliary_kwh", minimum=0)
    energy = number(propulsion + (aux_energy if scope == "total" else 0), "energy_kwh", minimum=0)
    return {"duration_h": duration, "energy_kwh": energy,
            "power_kw": number(energy / duration if duration else 0, "power_kw", minimum=0),
            "propulsion_kwh": propulsion, "auxiliary_kwh": aux_energy,
            "energy_scope": scope, "peak_power_kw": None}


def integrate_power(samples, *, max_gap_seconds):
    """Piecewise-linear signed power integral in kWh; reject ambiguous time grids.

    Samples are (relative_seconds, power_kw). Missing pairs break continuity.
    Gaps over an explicit threshold are skipped/countable, not filled. Duplicate
    or decreasing valid times are errors, including duplicates across missing data.
    Positive/negative areas are split at a linear zero crossing, not clipped ends.
    """
    gap = number(max_gap_seconds, "max_gap_seconds", minimum=0)
    if gap == 0:
        raise ValueError("max_gap_seconds must be positive")
    positive = negative = covered = 0.0
    used = skipped = breaks = 0
    previous = last_time = None
    for time, power in samples:
        if time is not None:
            time = number(time, "time", minimum=0)
            if last_time is not None and time <= last_time:
                raise ValueError("Times must be strictly increasing; align/deduplicate with evidence first")
            last_time = time
        if power is not None:
            power = number(power, "power")
        if time is None or power is None:
            breaks += 1
            previous = None
            continue
        if previous is not None:
            start, p0 = previous
            dt = time - start
            if dt > gap:
                skipped += 1
            else:
                hours = dt / 3600
                if p0 >= 0 and power >= 0:
                    positive += (p0 / 2 + power / 2) * hours
                elif p0 <= 0 and power <= 0:
                    negative += (-p0 / 2 - power / 2) * hours
                else:
                    scale = max(abs(p0), abs(power))
                    fraction = (abs(p0) / scale) / (abs(p0) / scale + abs(power) / scale)
                    first = abs(p0) / 2 * hours * fraction
                    second = abs(power) / 2 * hours * (1 - fraction)
                    positive += first if p0 > 0 else second
                    negative += second if p0 > 0 else first
                covered += dt
                used += 1
        previous = (time, power)
    return {"positive_area_kwh": number(positive, "positive_area"),
            "negative_area_kwh": number(negative, "negative_area"),
            "signed_area_kwh": number(positive - negative, "signed_area"),
            "covered_seconds": number(covered, "covered_seconds"),
            "used_intervals": used, "skipped_gaps": skipped, "missing_breaks": breaks}


def split_groups(records, *, train_groups, validation_groups):
    """Explicit group partition, no random row split or inferred trip independence.

    Records must carry group_id. Caller owns trip identity/time embargo/provenance.
    Every observed group must be assigned once; both splits must be nonempty.
    """
    train, validation = set(train_groups), set(validation_groups)
    if not train or not validation or train & validation:
        raise ValueError("Nonempty disjoint train/validation groups required")
    records = list(records)
    observed = {record["group_id"] for record in records}
    if observed != train | validation:
        raise ValueError("Every observed group must be assigned once; no unknown requested groups")
    return ([record for record in records if record["group_id"] in train],
            [record for record in records if record["group_id"] in validation])
