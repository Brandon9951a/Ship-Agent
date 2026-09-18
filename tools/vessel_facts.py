"""D1 fact audit. Candidate values never become adopted values implicitly."""

import argparse
import json
from math import isfinite
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
REQUIRED = ("capacity_kwh", "max_power_kw", "auxiliary_power_kw", "soc_min")
UNITS = {"capacity_kwh": "kWh", "max_power_kw": "kW", "auxiliary_power_kw": "kW",
         "max_speed_kmh": "km/h", "economic_speed_kmh": "km/h", "draft_m": "m",
         "charging_power_kw": "kW", "battery_group_capacity_kwh": "kWh"}
LIMIT_FIELDS = {"soc_min", "soc_alarm", "soc_shutdown"}


def finite_number(value: Any) -> bool:
    try:
        return not isinstance(value, bool) and isinstance(value, (int, float)) and isfinite(value)
    except OverflowError:
        return False


def load_config(path: Path) -> dict[str, Any]:
    """The project .yaml files use the JSON subset of YAML 1.2 deliberately."""
    def reject_constant(value: str) -> None:
        raise ValueError(f"Non-finite configuration value: {value}")

    def unique_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError(f"Duplicate configuration key: {key}")
            result[key] = value
        return result

    with path.open(encoding="utf-8-sig") as stream:
        result = json.load(stream, parse_constant=reject_constant, object_pairs_hook=unique_pairs)
    if not isinstance(result, dict):
        raise ValueError("Configuration root must be an object")
    return result


def audit_facts(facts: dict[str, Any], limits: dict[str, Any]) -> dict[str, Any]:
    errors: list[str] = []
    pending: list[str] = []
    adopted: dict[str, float] = {}
    parameters = facts.get("parameters")
    records = limits.get("limits")
    sources = facts.get("sources")
    if not isinstance(parameters, dict) or not isinstance(records, dict) or not isinstance(sources, dict):
        return {"status": "invalid_input", "errors": ["parameters, limits and sources must be objects"], "pending": [], "adopted": {}}
    if not facts.get("vessel_id"):
        errors.append("vessel_id is required")
    if type(facts.get("format_version")) is not int or facts.get("format_version") != 1 or type(limits.get("format_version")) is not int or limits.get("format_version") != 1:
        errors.append("Unsupported format_version")
    if limits.get("soc_unit") != "fraction":
        errors.append("SOC must use fraction (0-1)")
    from schemas.types import SourceRef
    from schemas.validate import require_valid
    for key, definition in sources.items():
        try:
            if not isinstance(definition, dict):
                raise ValueError("source definition must be an object")
            require_valid(SourceRef.from_dict(definition))
        except (ValueError, TypeError) as exc:
            errors.append(f"sources.{key}: {exc}")

    for group, entries in (("parameters", parameters), ("limits", records)):
        for name, record in entries.items():
            if not isinstance(record, dict):
                errors.append(f"{group}.{name}: expected object")
                continue
            unit = ("fraction" if name in LIMIT_FIELDS else None) if group == "limits" else UNITS.get(name)
            if unit is None or record.get("unit") != unit:
                errors.append(f"{name}: unknown field or incorrect unit")
            candidates = record.get("candidates", [])
            if not isinstance(candidates, list):
                errors.append(f"{name}: candidates must be a list")
                candidates = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    errors.append(f"{name}: candidate must be an object")
                    continue
                source_id = candidate.get("source")
                if not isinstance(source_id, str) or source_id not in sources or not isinstance(candidate.get("locator"), str) or not candidate["locator"].strip():
                    errors.append(f"{name}: candidate source/locator is missing")
                candidate_value = candidate.get("value")
                if not finite_number(candidate_value):
                    errors.append(f"{name}: candidate value must be finite numeric")
                elif group == "limits" and not 0 <= candidate_value <= 1:
                    errors.append(f"{name}: candidate SOC must use fraction")
            choice = record.get("adopted")
            if choice is None:
                pending.append(name)
                continue
            # Adopted records must carry a shared SourceRef and an A decision.
            if not isinstance(choice, dict):
                errors.append(f"{name}: adopted must be {{value, source}} or null")
                continue
            if record.get("confirmation_status") != "approved_A":
                errors.append(f"{name}: adopted value requires confirmation_status=approved_A")
            value = choice.get("value")
            if not finite_number(value):
                errors.append(f"{name}: adopted value must be finite numeric")
                continue
            if (group == "limits" and not 0 <= value <= 1) or (group == "parameters" and (value < 0 or (name != "auxiliary_power_kw" and value == 0))):
                errors.append(f"{name}: adopted value out of range")
            source = choice.get("source")
            if not isinstance(source, dict):
                errors.append(f"{name}: adopted source is required")
            else:
                try:
                    ref = SourceRef.from_dict(source)
                    require_valid(ref)
                    if not ref.locator:
                        errors.append(f"{name}: adopted source locator is required")
                    if ref.kind not in ("document", "measurement", "assumption"):
                        errors.append(f"{name}: configuration source must be document, measurement or assumption")
                    if ref.kind != "assumption" and not ref.confirmed:
                        errors.append(f"{name}: adopted evidence is unconfirmed")
                    if ref.kind == "assumption" and not ref.note:
                        errors.append(f"{name}: assumption needs a note")
                except (ValueError, TypeError) as exc:
                    errors.append(f"{name}: {exc}")
            if not isinstance(record.get("confirmed_by"), str) or not record["confirmed_by"].strip():
                errors.append(f"{name}: A decision/confirmed_by is required")
            adopted[name] = float(value)

    for name in REQUIRED:
        if name not in adopted and name not in pending:
            pending.append(name)
    for lower, upper in (("soc_shutdown", "soc_min"), ("soc_min", "soc_alarm")):
        if lower in adopted and upper in adopted and adopted[lower] > adopted[upper]:
            errors.append(f"{lower} must not exceed {upper}")
    policy = limits.get("policy", {})
    if not isinstance(policy, dict):
        errors.append("policy must be an object")
        policy = {}
    for name in ("power_boundary", "capacity_boundary", "battery_topology", "energy_scope"):
        if not isinstance(policy.get(name), str) or not policy[name].strip():
            pending.append(f"policy.{name}")
    if policy.get("energy_scope") is not None and policy["energy_scope"] not in ("propulsion", "total"):
        errors.append("policy.energy_scope must be propulsion, total or null")
    ready = not errors and all(name in adopted for name in REQUIRED) and not any(name.startswith("policy.") for name in pending)
    return {"status": "invalid_input" if errors else ("ok" if ready else "need_clarification"),
            "vessel_id": facts.get("vessel_id"), "errors": errors, "pending": pending,
            "adopted": adopted, "calculation_ready": ready,
            "readiness_scope": "minimum_energy_budget_inputs_only; not complete VesselState or ship safety approval"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Audit B's source and adoption records; no automatic defaults")
    parser.add_argument("--facts", type=Path, default=ROOT / "configs/vessel_facts.yaml")
    parser.add_argument("--limits", type=Path, default=ROOT / "configs/limits.yaml")
    args = parser.parse_args(argv)
    try:
        report = audit_facts(load_config(args.facts), load_config(args.limits))
    except (OSError, ValueError) as exc:
        report = {"status": "invalid_input", "errors": [str(exc)]}
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return {"ok": 0, "need_clarification": 2, "invalid_input": 1}[report["status"]]


if __name__ == "__main__":
    raise SystemExit(main())
