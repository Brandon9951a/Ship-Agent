"""Build D5 batch evidence and same-constraint energy comparisons.

The experiment is deterministic except for recorded wall-clock timings.  It
uses the D4 software-demo calculation path and independently enumerates the
finite candidate grid to verify status, infeasible type and selected optimum.
No result is a real-vessel efficiency or navigation claim.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import html
import json
import platform
from itertools import product
from math import isclose
from pathlib import Path
from time import perf_counter
from typing import Any

from schemas.types import EnergyResult
from scripts.validate_d4 import evaluate_scenario, load_runtime_configs


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG = ROOT / "configs/examples/d5_experiments.json"


def load_experiments(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("mode") != "software_demo_batch_evidence":
        raise ValueError("D5实验必须明确标记software_demo_batch_evidence。")
    if payload.get("real_ship_validation") is not False:
        raise ValueError("D5实验不得标记为实船验证。")
    routes = payload.get("routes")
    profiles = payload.get("profiles")
    speeds = payload.get("candidate_speeds_kmh")
    if not isinstance(routes, list) or not routes:
        raise ValueError("D5至少需要一条实验航线。")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("D5至少需要一组实验条件。")
    if len(routes) * len(profiles) < 30:
        raise ValueError("D5批量实验必须至少展开30项用例。")
    if not isinstance(speeds, list) or not speeds or any(
        not isinstance(value, (int, float)) or value <= 0 for value in speeds
    ):
        raise ValueError("候选航速必须是非空的正数列表。")
    if len(set(speeds)) != len(speeds):
        raise ValueError("候选航速不得重复。")
    for key, items in (("routes", routes), ("profiles", profiles)):
        ids = [item.get("id") for item in items]
        if any(not isinstance(item, str) or not item for item in ids):
            raise ValueError(f"{key}中的id不能为空。")
        if len(set(ids)) != len(ids):
            raise ValueError(f"{key}中的id不得重复。")
    for route in routes:
        if not route.get("origin") or not route.get("destination"):
            raise ValueError("实验航线必须提供起终点。")
        if not isinstance(route.get("expected_distance_km"), (int, float)) \
                or route["expected_distance_km"] <= 0:
            raise ValueError("实验航线必须提供正的核对距离。")
    for profile_item in profiles:
        factor = profile_item.get("deadline_factor_vs_fastest")
        soc = profile_item.get("soc_initial")
        if not isinstance(factor, (int, float)) or factor < 1:
            raise ValueError("时间系数不得小于网格最快耗时。")
        if not isinstance(soc, (int, float)) or not 0.30 <= soc <= 1:
            raise ValueError("D5初始SOC必须位于已采用规划下限0.30至1之间。")
    return payload


def _reference_grid(
    result: dict[str, Any], *, capacity_kwh: float, soc_min: float,
    power_limit_kw: float, time_limit_h: float,
) -> dict[str, Any]:
    segments = result["segments"]
    candidates = [
        EnergyResult.from_dict(item)
        for item in result["tenergy"]["payload"]["candidate_results"]
    ]
    auxiliary_kw = float(result["model"]["auxiliary_power_kw"])
    eligible: list[list[EnergyResult]] = []
    for segment in segments:
        rows = [
            item for item in candidates
            if item.segment_id == segment["segment_id"]
            and item.speed_kmh <= segment["max_speed_kmh"] + 1e-12
            and item.peak_power_kw <= power_limit_kw + 1e-12
        ]
        if not rows:
            raise AssertionError(f"参考穷举没有航段{segment['segment_id']}的合格候选。")
        eligible.append(rows)

    budget_kwh = capacity_kwh * (result["request"]["soc_initial"] - soc_min)
    rows: list[dict[str, Any]] = []
    for choices in product(*eligible):
        duration_h = sum(item.duration_h for item in choices)
        propulsion_kwh = sum(item.energy_kwh for item in choices)
        required_kwh = propulsion_kwh + auxiliary_kw * duration_h
        rows.append({
            "speeds_kmh": [item.speed_kmh for item in choices],
            "duration_h": duration_h,
            "propulsion_energy_kwh": propulsion_kwh,
            "auxiliary_energy_kwh": auxiliary_kw * duration_h,
            "required_energy_kwh": required_kwh,
            "time_feasible": duration_h <= time_limit_h + 1e-9,
            "soc_feasible": required_kwh <= budget_kwh + 1e-9,
        })
    feasible = [item for item in rows if item["time_feasible"] and item["soc_feasible"]]
    if feasible:
        expected_status, expected_type = "ok", None
        optimum = min(
            feasible,
            key=lambda item: (
                item["required_energy_kwh"], item["duration_h"], item["speeds_kmh"]
            ),
        )
        baseline = min(
            feasible,
            key=lambda item: (
                item["duration_h"], item["required_energy_kwh"], item["speeds_kmh"]
            ),
        )
    else:
        time_pass = any(item["time_feasible"] for item in rows)
        soc_pass = any(item["soc_feasible"] for item in rows)
        expected_status = "infeasible"
        if time_pass and not soc_pass:
            expected_type = "soc"
        elif soc_pass and not time_pass:
            expected_type = "time"
        else:
            expected_type = "combined"
        optimum = baseline = None
    return {
        "candidate_combinations": len(rows),
        "available_energy_above_soc_min_kwh": budget_kwh,
        "expected_status": expected_status,
        "expected_infeasible_type": expected_type,
        "optimum": optimum,
        "baseline": baseline,
    }


def _selected_plan(result: dict[str, Any]) -> dict[str, Any] | None:
    if result["status"] != "ok":
        return None
    optimization = result["tspeed"]["payload"]
    management = result["tmanagement"]["payload"]
    return {
        "speeds_kmh": [item["speed_kmh"] for item in optimization["energy_results"]],
        "duration_h": optimization["total_duration_h"],
        "propulsion_energy_kwh": optimization["total_energy_kwh"],
        "auxiliary_energy_kwh": management["auxiliary_energy_kwh"],
        "required_energy_kwh": management["required_energy_kwh"],
        "soc_final": management["soc_final"],
    }


def _same_plan(left: dict[str, Any] | None, right: dict[str, Any] | None) -> bool:
    if left is None or right is None:
        return left is right
    return (
        left["speeds_kmh"] == right["speeds_kmh"]
        and isclose(left["duration_h"], right["duration_h"], rel_tol=1e-9, abs_tol=1e-9)
        and isclose(
            left["required_energy_kwh"], right["required_energy_kwh"],
            rel_tol=1e-9, abs_tol=1e-9,
        )
    )


def run_batch(config_path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    definition = load_experiments(config_path)
    configs = load_runtime_configs()
    policy = configs["demo_policy_config"]
    capacity_kwh = float(policy["battery"]["total_effective_capacity_kwh"])
    soc_min = float(policy["soc"]["planning_min"])
    power_limit_kw = float(policy["power_and_energy"]["operational_propulsion_max_kw"])
    speeds = [float(item) for item in definition["candidate_speeds_kmh"]]
    results: list[dict[str, Any]] = []

    for route in definition["routes"]:
        fastest_h = float(route["expected_distance_km"]) / max(speeds)
        for profile_item in definition["profiles"]:
            case_id = f"{route['id']}__{profile_item['id']}"
            max_duration_h = fastest_h * float(
                profile_item["deadline_factor_vs_fastest"]
            )
            scenario = {
                "id": case_id,
                "soc_initial": float(profile_item["soc_initial"]),
                "max_duration_h": max_duration_h,
            }
            route_input = {
                "origin": route["origin"],
                "destination": route["destination"],
                "load_state": "半载",
                "candidate_speeds_kmh": speeds,
            }
            started = perf_counter()
            actual = evaluate_scenario(scenario, route_input, configs)
            elapsed_ms = (perf_counter() - started) * 1000
            if not isclose(
                actual["route_distance_km"], float(route["expected_distance_km"]),
                rel_tol=0, abs_tol=1e-9,
            ):
                raise AssertionError(
                    f"{case_id}资料距离{actual['route_distance_km']}与配置核对值"
                    f"{route['expected_distance_km']}不一致。"
                )
            reference = _reference_grid(
                actual,
                capacity_kwh=capacity_kwh,
                soc_min=soc_min,
                power_limit_kw=power_limit_kw,
                time_limit_h=max_duration_h,
            )
            selected = _selected_plan(actual)
            status_match = (
                actual["status"] == reference["expected_status"]
                and actual["infeasible_type"] == reference["expected_infeasible_type"]
            )
            optimum_match = _same_plan(selected, reference["optimum"])
            if not status_match or not optimum_match:
                raise AssertionError(f"{case_id}未通过独立参考穷举核对。")
            baseline = reference["baseline"]
            comparison = None
            if selected is not None and baseline is not None:
                saving = baseline["required_energy_kwh"] - selected["required_energy_kwh"]
                comparison = {
                    "baseline_id": definition["baseline"]["id"],
                    "same_constraints": True,
                    "baseline_required_energy_kwh": baseline["required_energy_kwh"],
                    "optimized_required_energy_kwh": selected["required_energy_kwh"],
                    "energy_difference_kwh": saving,
                    "energy_difference_percent": (
                        saving / baseline["required_energy_kwh"] * 100
                    ),
                }
            results.append({
                "case_id": case_id,
                "route_id": route["id"],
                "profile_id": profile_item["id"],
                "origin": route["origin"],
                "destination": route["destination"],
                "route_distance_km": actual["route_distance_km"],
                "soc_initial": scenario["soc_initial"],
                "soc_min": soc_min,
                "max_duration_h": max_duration_h,
                "candidate_speeds_kmh": speeds,
                "status": actual["status"],
                "infeasible_type": actual["infeasible_type"],
                "selected": selected,
                "reference": reference,
                "comparison": comparison,
                "verification": {
                    "status_match": status_match,
                    "optimum_match": optimum_match,
                    "distance_match": True,
                },
                "elapsed_ms": elapsed_ms,
                "real_ship_validation": False,
            })

    feasible = [item for item in results if item["status"] == "ok"]
    comparable = [item for item in feasible if item["comparison"] is not None]
    baseline_sum = sum(
        item["comparison"]["baseline_required_energy_kwh"] for item in comparable
    )
    optimized_sum = sum(
        item["comparison"]["optimized_required_energy_kwh"] for item in comparable
    )
    status_counts: dict[str, int] = {}
    for item in results:
        key = item["status"] if item["infeasible_type"] is None else (
            f"{item['status']}/{item['infeasible_type']}"
        )
        status_counts[key] = status_counts.get(key, 0) + 1
    aggregate = {
        "case_count": len(results),
        "verified_case_count": sum(
            all(item["verification"].values()) for item in results
        ),
        "status_counts": status_counts,
        "comparable_case_count": len(comparable),
        "baseline_required_energy_sum_kwh": baseline_sum,
        "optimized_required_energy_sum_kwh": optimized_sum,
        "aggregate_energy_difference_kwh": baseline_sum - optimized_sum,
        "aggregate_energy_difference_percent": (
            (baseline_sum - optimized_sum) / baseline_sum * 100
            if baseline_sum else None
        ),
        "mean_case_elapsed_ms": sum(item["elapsed_ms"] for item in results) / len(results),
        "max_case_elapsed_ms": max(item["elapsed_ms"] for item in results),
        "comparison_interpretation": (
            "同一批软件演示任务的重复样本汇总，仅比较离散网格内最快可行策略与"
            "最低能耗策略；不是实船节能率、运营收益或模型精度。"
        ),
    }
    return {
        "status": "passed" if aggregate["verified_case_count"] == len(results) else "failed",
        "mode": definition["mode"],
        "real_ship_validation": False,
        "config_source": str(config_path.relative_to(ROOT)).replace("\\", "/")
        if config_path.is_relative_to(ROOT) else str(config_path),
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "processor": platform.processor() or "unreported_by_platform",
            "model_service_used": False,
            "gpu_required": False,
        },
        "units": {
            "distance": "km", "speed": "km/h", "power": "kW",
            "energy": "kWh", "duration": "h", "soc": "fraction",
            "elapsed": "ms",
        },
        "baseline": definition["baseline"],
        "adopted_demo_boundaries": {
            "effective_capacity_kwh": capacity_kwh,
            "soc_min": soc_min,
            "propulsion_power_limit_kw": power_limit_kw,
            "auxiliary_power_kw": float(
                policy["power_and_energy"]["demo_auxiliary_power_kw"]
            ),
            "source": "configs/demo_policy.yaml",
        },
        "aggregate": aggregate,
        "results": results,
        "limitations": definition["limitations"],
    }


def _csv_rows(report: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in report["results"]:
        selected = item["selected"] or {}
        comparison = item["comparison"] or {}
        rows.append({
            "case_id": item["case_id"],
            "route_id": item["route_id"],
            "profile_id": item["profile_id"],
            "origin": item["origin"],
            "destination": item["destination"],
            "route_distance_km": item["route_distance_km"],
            "soc_initial": item["soc_initial"],
            "max_duration_h": item["max_duration_h"],
            "status": item["status"],
            "infeasible_type": item["infeasible_type"] or "",
            "selected_speeds_kmh": "/".join(
                str(value) for value in selected.get("speeds_kmh", [])
            ),
            "selected_duration_h": selected.get("duration_h", ""),
            "optimized_required_energy_kwh": comparison.get(
                "optimized_required_energy_kwh", ""
            ),
            "baseline_required_energy_kwh": comparison.get(
                "baseline_required_energy_kwh", ""
            ),
            "energy_difference_kwh": comparison.get("energy_difference_kwh", ""),
            "energy_difference_percent": comparison.get(
                "energy_difference_percent", ""
            ),
            "elapsed_ms": item["elapsed_ms"],
            "verified": str(all(item["verification"].values())).lower(),
            "real_ship_validation": "false",
        })
    return rows


def _svg(report: dict[str, Any]) -> str:
    items = [item for item in report["results"] if item["comparison"] is not None]
    width, height = 1800, 720
    left, right, top, bottom = 90, 40, 90, 190
    plot_w, plot_h = width - left - right, height - top - bottom
    maximum = max(
        item["comparison"]["baseline_required_energy_kwh"] for item in items
    )
    group_w = plot_w / len(items)
    bar_w = max(min(group_w * 0.32, 18), 5)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">',
        '<rect width="100%" height="100%" fill="#ffffff"/>',
        '<text x="900" y="34" text-anchor="middle" font-size="24" font-family="sans-serif" font-weight="700">D5 同条件能耗对照（软件演示，非实船节能率）</text>',
        '<text x="900" y="61" text-anchor="middle" font-size="14" font-family="sans-serif" fill="#555">蓝：最快可行基线　绿：最低能耗策略；两者使用相同航线、网格、时间、SOC、功率和限速约束</text>',
        f'<line x1="{left}" y1="{top + plot_h}" x2="{left + plot_w}" y2="{top + plot_h}" stroke="#333"/>',
        f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#333"/>',
    ]
    for tick in range(6):
        value = maximum * tick / 5
        y = top + plot_h - plot_h * tick / 5
        parts.append(f'<line x1="{left}" y1="{y:.2f}" x2="{left + plot_w}" y2="{y:.2f}" stroke="#e5e7eb"/>')
        parts.append(f'<text x="{left - 10}" y="{y + 5:.2f}" text-anchor="end" font-size="12" font-family="sans-serif">{value:.0f}</text>')
    for index, item in enumerate(items):
        comparison = item["comparison"]
        center = left + group_w * (index + 0.5)
        for offset, key, color in (
            (-bar_w, "baseline_required_energy_kwh", "#2563eb"),
            (0, "optimized_required_energy_kwh", "#16a34a"),
        ):
            value = comparison[key]
            bar_h = plot_h * value / maximum
            x = center + offset
            y = top + plot_h - bar_h
            parts.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{bar_w:.2f}" height="{bar_h:.2f}" fill="{color}"/>')
        label = html.escape(item["case_id"])
        parts.append(f'<text transform="translate({center:.2f},{top + plot_h + 14}) rotate(65)" font-size="9" font-family="sans-serif">{label}</text>')
    parts.append(f'<text transform="translate(24,{top + plot_h / 2}) rotate(-90)" text-anchor="middle" font-size="14" font-family="sans-serif">总需求能量（kWh）</text>')
    parts.append('</svg>')
    return "\n".join(parts) + "\n"


def write_evidence(report: dict[str, Any], output_dir: Path) -> None:
    if output_dir.exists():
        raise FileExistsError(f"输出目录已存在，拒绝覆盖：{output_dir}")
    output_dir.mkdir(parents=True)
    raw_path = output_dir / "raw_results.json"
    aggregate_path = output_dir / "aggregate.json"
    csv_path = output_dir / "cases.csv"
    svg_path = output_dir / "energy_comparison.svg"
    raw_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    aggregate_path.write_text(
        json.dumps(report["aggregate"], ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    rows = _csv_rows(report)
    with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    svg_path.write_text(_svg(report), encoding="utf-8")

    files = [raw_path, aggregate_path, csv_path, svg_path]
    manifest = {
        "algorithm": "SHA-256",
        "real_ship_validation": False,
        "files": {
            item.name: hashlib.sha256(item.read_bytes()).hexdigest()
            for item in files
        },
    }
    (output_dir / "evidence_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="运行D5批量验证与同条件能耗对照")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--output-dir", type=Path)
    args = parser.parse_args()
    report = run_batch(args.config)
    if args.output_dir is not None:
        write_evidence(report, args.output_dir)
    print(json.dumps(report["aggregate"], ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
