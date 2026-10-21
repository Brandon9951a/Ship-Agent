"""Versioned, deterministic river scenarios; numerical values are simulation assumptions."""
from __future__ import annotations

import copy
import json
import math
from dataclasses import replace
from pathlib import Path

from research.agent.store import digest
from research.contracts import Diagnostic, OutcomeCode
from research.tspeed.predictor import AnalyticPowerPredictor, PredictionOutcome

ROOT = Path(__file__).resolve().parents[2]


class EnvironmentError(ValueError):
    pass


def number(value, name, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise EnvironmentError(f"{name}: expected finite value in [{low}, {high}]")
    return float(value)


def load_environment(scenario_id, load_state=None, *, path=None):
    raw = json.loads(Path(path or ROOT / "configs/environment_scenarios.json").read_text(encoding="utf-8"))
    for key in ("ordinary_water_floor_kmh", "ground_ceiling_kmh", "water_max_kmh"):
        number(raw[key], key, .05, 50)
    if scenario_id not in raw["scenarios"]:
        raise EnvironmentError("environment_scenario_not_registered")
    selected = raw["scenarios"][scenario_id]
    load = load_state or raw["reference_load_state"]
    if load not in raw["load_profiles"]:
        raise EnvironmentError("load_state_not_registered")
    factor = number(raw["load_profiles"][load], "load_factor", .1, 3)
    current_scale = number(selected["current_scale"], "current_scale", 0, 10)
    wind_factor = number(selected["wind_factor"], "wind_factor", 1, 3)
    canonical_wind = number(selected["wind_along_canonical_mps"], "wind_along_canonical_mps", -30, 30)
    channel_scale = number(selected["channel_scale"], "channel_scale", 0, 5)
    wait = number(selected["lock_wait_s"], "lock_wait_s", 0, 86400)
    routes = json.loads((ROOT / "configs/route_facts.yaml").read_text(encoding="utf-8"))
    identities = {item["id"]: item for item in routes["segments"]}
    resolved = {}
    for physical_id, row in raw["physical_segments"].items():
        if physical_id not in identities:
            raise EnvironmentError(f"unknown_physical_segment:{physical_id}")
        canonical = identities[physical_id]
        current = number(row["current_mps"], physical_id + ".current_mps", -3, 3) * current_scale
        channel = 1 + (number(row["channel_factor"], "channel_factor", 1, 3) - 1) * channel_scale
        for direction, segment_id in ((1, physical_id), (-1, physical_id + "_reverse")):
            if segment_id not in identities:
                raise EnvironmentError(f"missing_reverse_segment:{segment_id}")
            actual = identities[segment_id]
            if (actual["origin"], actual["destination"]) != ((canonical["origin"], canonical["destination"]) if direction == 1 else (canonical["destination"], canonical["origin"])):
                raise EnvironmentError(f"direction_identity_mismatch:{segment_id}")
            short = bool(row["berthing"])
            resolved[segment_id] = {
                "physical_segment_id": physical_id, "direction": direction,
                "current_along_route_mps": current * direction,
                "channel_factor": channel,
                "wind_along_route_mps": canonical_wind * direction,
                "wind_factor": wind_factor if canonical_wind * direction < 0 else 1.0,
                "cruise_water_floor_kmh": 2.0 if short else raw["ordinary_water_floor_kmh"],
                "ground_ceiling_kmh": 11.112 if short else raw["ground_ceiling_kmh"],
                "source_mode": "simulation", "depth_m": None,
            }
    # Synthetic acceptance segments have explicit identities and zero-current conditions.
    for segment_id in ("simulation-leg-1", "simulation-leg-2"):
        resolved[segment_id] = {
            "physical_segment_id": segment_id, "direction": 1,
            "current_along_route_mps": 0.0, "channel_factor": 1.0,
            "wind_factor": wind_factor, "cruise_water_floor_kmh": 2.0,
            "ground_ceiling_kmh": raw["ground_ceiling_kmh"], "source_mode": "simulation", "depth_m": None}
    required = set(identities)
    if required - resolved.keys():
        raise EnvironmentError("missing_segment_environment:" + ",".join(sorted(required - resolved.keys())))
    snapshot = {
        "schema_version": raw["schema_version"], "scenario_id": scenario_id,
        "version": raw["version"], "label": selected["label"],
        "source_mode": "simulation", "source_ref": "configs/environment_scenarios.json",
        "assumption": raw["assumption"], "wind_scope": "named_headwind_factor_tailwind_no_credit_no_meteorological_prediction",
        "load_state": load, "load_factor": factor, "load_state_defaulted": load_state is None,
        "segments": resolved, "lock_wait_s": wait,
        "water_max_kmh": number(raw["water_max_kmh"], "water_max_kmh", .1, 50),
        "motor_limit_kw": number(raw["motor_limit_kw"], "motor_limit_kw", 1, 10000),
        "unlocated_current_along_route_mps": number(selected["unlocated_current_mps"], "unlocated_current_mps", -3, 3),
        "waiting_model": "moored_zero_propulsion_auxiliary_continues",
        "maneuver_model": "lock_acceleration_deceleration_in_named_zero_current_shelter",
    }
    snapshot["content_hash"] = digest(snapshot)
    return snapshot


def validate_snapshot(snapshot):
    if digest({k: v for k, v in snapshot.items() if k != "content_hash"}) != snapshot.get("content_hash"):
        raise EnvironmentError("environment_snapshot_hash_mismatch")


def resolve_route_environment(snapshot, selected):
    """Count only intermediate locks; starting/ending at a lock does not imply passage."""
    validate_snapshot(snapshot)
    result = []
    for index, leg in enumerate(selected):
        if leg["id"] not in snapshot["segments"]:
            raise EnvironmentError("missing_segment_environment:" + leg["id"])
        env = copy.deepcopy(snapshot["segments"][leg["id"]])
        crosses_lock = index + 1 < len(selected) and "船闸" in leg["destination"]
        previous_lock = index > 0 and "船闸" in leg["origin"]
        env["waiting_duration_s"] = snapshot["lock_wait_s"] if crosses_lock else 0.0
        env["lock_event_id"] = f"{index}:{leg['destination']}" if crosses_lock else None
        env["sheltered_maneuvers"] = bool(env["waiting_duration_s"] or (previous_lock and snapshot["lock_wait_s"]))
        result.append(env)
    return result


class RiverPowerPredictor(AnalyticPowerPredictor):
    """Keep ground-speed geometry; transform only the power model's water-speed input."""
    def __init__(self, *, coefficient, environment, segments=None, allow_unlocated=False):
        validate_snapshot(environment)
        super().__init__(coefficient=coefficient, model_version="river-analytic/1.0")
        self.environment = copy.deepcopy(environment)
        self.segments = copy.deepcopy(segments if segments is not None else environment["segments"])
        self.allow_unlocated = allow_unlocated

    @property
    def config_hash(self):
        return digest({"base": super().config_hash, "environment": self.environment,
                       "segments": self.segments, "allow_unlocated": self.allow_unlocated})

    def conditions(self, segment_id):
        if segment_id in self.segments:
            return self.segments[segment_id]
        if not self.allow_unlocated:
            raise EnvironmentError("missing_segment_environment:" + segment_id)
        return {"current_along_route_mps": self.environment["unlocated_current_along_route_mps"],
                "channel_factor": 1.0, "wind_factor": 1.0, "sheltered_maneuvers": False}

    def interval_values(self, speed, segment_id, phase, kind=None):
        env = self.conditions(segment_id)
        if phase != "sailing":
            return 0.0, 0.0, 0.0
        if speed is None:
            raise EnvironmentError("ground_speed_missing")
        current = env["current_along_route_mps"]
        if env.get("sheltered_maneuvers") and kind in ("accel", "decel"):
            current = 0.0
        water = speed - 3.6 * current
        if speed <= 0 or water <= 0 or water > self.environment["water_max_kmh"] + 1e-7:
            raise EnvironmentError("water_speed_outside_supported_domain")
        factor = self.environment["load_factor"] * env["channel_factor"] * env["wind_factor"]
        power = self.power_kw(water) * factor
        if power > self.environment["motor_limit_kw"] + 1e-6:
            raise EnvironmentError("motor_power_limit_exceeded")
        return water, current, power

    def interval_power_kw(self, speed, segment_id, phase, kind=None):
        return self.interval_values(speed, segment_id, phase, kind)[2]

    def ground_bounds(self, segment_id):
        env = self.conditions(segment_id)
        current = 3.6 * env["current_along_route_mps"]
        factor = self.environment["load_factor"] * env["channel_factor"] * env["wind_factor"]
        power_speed = (self.environment["motor_limit_kw"] / (self._coefficient * factor)) ** (1 / 3)
        low = max(.05, env["cruise_water_floor_kmh"] + current)
        high = min(env["ground_ceiling_kmh"], self.environment["water_max_kmh"] + current, power_speed + current)
        return low, high

    def predict_profile(self, profile, *, candidate_id, grid, keep_power_points=True):
        values, diagnostics = [], []
        for i, segment in enumerate(profile.segment_ids):
            try:
                values.append(self.interval_values(profile.speeds[i], segment, profile.phases[i], profile.kinds[i] if profile.kinds else None))
            except EnvironmentError as exc:
                diagnostics.append(Diagnostic(stage="predict", code=str(exc), segment_id=segment,
                    time_interval_s=(profile.starts[i], profile.starts[i] + profile.durations[i]),
                    detail="环境修正后的对水航速或推进功率不满足情景边界。", retryable=False))
                break
        if diagnostics:
            return PredictionOutcome(candidate_id, OutcomeCode.MODEL_OUT_OF_DOMAIN.value,
                model_version=self.model_version, diagnostics=tuple(diagnostics))
        # Encode the per-segment factors as an equivalent speed for the common
        # cubic integrator. Geometry and exported speeds always stay over ground.
        powered = replace(profile, speeds=tuple((p / self._coefficient) ** (1 / 3) if phase == "sailing" else 0.0
                          for (_, _, p), phase in zip(values, profile.phases)))
        outcome = super().predict_profile(powered, candidate_id=candidate_id, grid=grid,
                                         keep_power_points=keep_power_points)
        points = tuple(replace(point, planned_speed_kmh=profile.speeds[i],
            speed_through_water_kmh=values[i][0], current_along_route_mps=values[i][1],
            environment_hash=self.environment["content_hash"]) for i, point in enumerate(outcome.power_points))
        return replace(outcome, power_points=points)


def verify_environment_demand(environment, route_conditions, trajectory, demand, *, coefficient, efficiency, auxiliary_kw):
    """Independent replay of exported rows; does not invoke the predictor or DE."""
    validate_snapshot(environment)
    if len(trajectory) != len(demand.points):
        raise EnvironmentError("environment_demand_row_count_mismatch")
    energy = 0.0
    distance = 0.0
    for row, point in zip(trajectory, demand.points):
        env = route_conditions[row["segment_id"]]
        speed = row["planned_speed_kmh"]
        if (point.segment_id != row["segment_id"] or point.phase != row["phase"] or
                not math.isclose(point.duration_s, row["duration_s"], abs_tol=1e-7) or
                not math.isclose(point.start_offset_s, row["start_offset_s"], abs_tol=1e-7)):
            raise EnvironmentError("environment_demand_timeline_mismatch")
        if row["phase"] == "sailing":
            current = 0.0 if env.get("sheltered_maneuvers") and row.get("interval_kind") in ("accel", "decel") else env["current_along_route_mps"]
            water = speed - current * 3.6
            motor = coefficient * water ** 3 * environment["load_factor"] * env["channel_factor"] * env["wind_factor"]
            if water <= 0 or water > environment["water_max_kmh"] + 1e-6 or motor > environment["motor_limit_kw"] + 1e-5:
                raise EnvironmentError("environment_replay_domain_failed")
            distance += speed * row["duration_s"] / 3600
        else:
            if speed != 0:
                raise EnvironmentError("waiting_ground_speed_not_zero")
            motor = 0.0
        bus = motor / efficiency + auxiliary_kw
        if not math.isclose(motor, point.motor_power_kw, rel_tol=1e-7, abs_tol=1e-6) or not math.isclose(bus, point.demand_bus_kw, rel_tol=1e-7, abs_tol=1e-6):
            raise EnvironmentError("environment_power_replay_mismatch")
        energy += bus * row["duration_s"] / 3600
    return {"passed": True, "distance_km": distance, "bus_energy_kwh": energy,
            "environment_hash": environment["content_hash"], "rows": len(trajectory)}
