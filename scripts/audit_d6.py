"""Audit frozen D5 evidence against current code and documentation for D6.

The audit is read-only by default.  It verifies file hashes, JSON/CSV
consistency, adopted demo boundaries, documentation claims and a fresh D5
rerun after removing timing/environment fields.  It does not certify real-vessel
performance or replace C's independent dependency installation.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import platform
from copy import deepcopy
from pathlib import Path
from typing import Any, Callable

from scripts.validate_d5 import run_batch
from tools.tdata import load_config


ROOT = Path(__file__).resolve().parents[1]
EVIDENCE_DIR = Path("docs/验证/D5_B_冻结证据")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def verify_manifest(evidence_dir: Path) -> dict[str, Any]:
    manifest_path = evidence_dir / "evidence_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("algorithm") != "SHA-256":
        raise AssertionError("D5证据清单算法不是SHA-256。")
    if manifest.get("real_ship_validation") is not False:
        raise AssertionError("D5证据清单不得标记为实船验证。")
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise AssertionError("D5证据清单没有待核文件。")
    actual = {}
    for name, recorded in expected.items():
        path = evidence_dir / name
        if not path.is_file():
            raise AssertionError(f"D5证据文件缺失：{name}")
        actual[name] = _digest(path)
        if actual[name] != recorded:
            raise AssertionError(f"D5证据哈希不一致：{name}")
    return {"algorithm": "SHA-256", "verified_files": actual}


def _round_floats(value: Any) -> Any:
    if isinstance(value, float):
        return round(value, 9)
    if isinstance(value, list):
        return [_round_floats(item) for item in value]
    if isinstance(value, dict):
        return {key: _round_floats(item) for key, item in value.items()}
    return value


def deterministic_projection(report: dict[str, Any]) -> dict[str, Any]:
    projected = deepcopy(report)
    projected.pop("environment", None)
    aggregate = projected.get("aggregate", {})
    aggregate.pop("mean_case_elapsed_ms", None)
    aggregate.pop("max_case_elapsed_ms", None)
    for item in projected.get("results", []):
        item.pop("elapsed_ms", None)
    return _round_floats(projected)


def _load_csv(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def _assert_close(left: Any, right: Any, *, name: str, tolerance: float = 1e-8) -> None:
    if abs(float(left) - float(right)) > tolerance:
        raise AssertionError(f"{name}不一致：{left} != {right}")


def _verify_frozen_content(root: Path) -> dict[str, Any]:
    evidence = root / EVIDENCE_DIR
    raw = json.loads((evidence / "raw_results.json").read_text(encoding="utf-8"))
    aggregate = json.loads((evidence / "aggregate.json").read_text(encoding="utf-8"))
    rows = _load_csv(evidence / "cases.csv")
    if raw["aggregate"] != aggregate:
        raise AssertionError("raw_results.json与aggregate.json汇总不一致。")
    if aggregate["case_count"] != 40 or aggregate["verified_case_count"] != 40:
        raise AssertionError("D5冻结证据应为40项且全部核对通过。")
    if aggregate["status_counts"] != {"ok": 30, "infeasible/soc": 10}:
        raise AssertionError("D5冻结证据状态计数不一致。")
    if len(rows) != len(raw["results"]) or len(rows) != 40:
        raise AssertionError("D5 CSV与JSON用例数不一致。")
    by_id = {item["case_id"]: item for item in raw["results"]}
    if len(by_id) != 40:
        raise AssertionError("D5 JSON存在重复case_id。")
    for row in rows:
        item = by_id.get(row["case_id"])
        if item is None:
            raise AssertionError(f"CSV用例未出现在JSON：{row['case_id']}")
        for key in ("route_id", "profile_id", "origin", "destination", "status"):
            if row[key] != str(item[key]):
                raise AssertionError(f"{row['case_id']}字段{key}不一致。")
        if row["infeasible_type"] != (item["infeasible_type"] or ""):
            raise AssertionError(f"{row['case_id']}不可行分类不一致。")
        _assert_close(row["route_distance_km"], item["route_distance_km"], name="距离")
        _assert_close(row["soc_initial"], item["soc_initial"], name="初始SOC")
        _assert_close(row["max_duration_h"], item["max_duration_h"], name="最长耗时")
        if row["verified"] != "true" or row["real_ship_validation"] != "false":
            raise AssertionError(f"{row['case_id']}验证或实船标记不正确。")
        if item["real_ship_validation"] is not False:
            raise AssertionError(f"{row['case_id']}不得标记为实船验证。")
    if raw["units"] != {
        "distance": "km", "speed": "km/h", "power": "kW",
        "energy": "kWh", "duration": "h", "soc": "fraction", "elapsed": "ms",
    }:
        raise AssertionError("D5冻结证据单位表不符合统一口径。")
    return {"raw": raw, "aggregate": aggregate, "csv_rows": rows}


def _verify_boundaries(root: Path, raw: dict[str, Any]) -> dict[str, Any]:
    policy = load_config(root / "configs/demo_policy.yaml")
    expected = {
        "effective_capacity_kwh": float(policy["battery"]["total_effective_capacity_kwh"]),
        "soc_min": float(policy["soc"]["planning_min"]),
        "propulsion_power_limit_kw": float(
            policy["power_and_energy"]["operational_propulsion_max_kw"]
        ),
        "auxiliary_power_kw": float(policy["power_and_energy"]["demo_auxiliary_power_kw"]),
        "source": "configs/demo_policy.yaml",
    }
    if raw["adopted_demo_boundaries"] != expected:
        raise AssertionError("D5冻结参数与当前A批准演示配置不一致。")
    return expected


def _verify_documents(root: Path) -> dict[str, list[str]]:
    requirements = {
        "README.md": ["40项批量实验", "16.6703%", "不是实船节能率"],
        "docs/协作/D5_B_完成汇总.md": [
            "40项全部通过", "30项可比案例", "不能写成实船节能率",
        ],
        "docs/参赛/D5_B_测试与验证供稿.md": [
            "40项状态", "16.6703%", "不能解释为实船节能率",
        ],
    }
    for relative, phrases in requirements.items():
        text = (root / relative).read_text(encoding="utf-8")
        missing = [phrase for phrase in phrases if phrase not in text]
        if missing:
            raise AssertionError(f"{relative}缺少冻结结论或边界：{missing}")
    return requirements


def _check(name: str, function: Callable[[], Any]) -> tuple[dict[str, Any], Any]:
    try:
        detail = function()
    except (AssertionError, OSError, ValueError, KeyError, TypeError) as exc:
        return {"name": name, "passed": False, "detail": str(exc)}, None
    return {"name": name, "passed": True, "detail": detail}, detail


def run_audit(root: Path = ROOT) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    manifest_check, manifest = _check(
        "d5_manifest_hashes", lambda: verify_manifest(root / EVIDENCE_DIR)
    )
    checks.append(manifest_check)
    content_check, content = _check("json_csv_counts_units", lambda: _verify_frozen_content(root))
    if content_check["passed"]:
        content_check["detail"] = {
            "case_count": content["aggregate"]["case_count"],
            "verified_case_count": content["aggregate"]["verified_case_count"],
            "status_counts": content["aggregate"]["status_counts"],
            "csv_row_count": len(content["csv_rows"]),
            "units": content["raw"]["units"],
        }
    checks.append(content_check)
    if content is not None:
        boundary_check, boundaries = _check(
            "adopted_demo_boundaries",
            lambda: _verify_boundaries(root, content["raw"]),
        )
    else:
        boundary_check, boundaries = ({
            "name": "adopted_demo_boundaries", "passed": False,
            "detail": "前置JSON/CSV检查失败。",
        }, None)
    checks.append(boundary_check)
    document_check, documents = _check("document_claims_and_limitations", lambda: _verify_documents(root))
    checks.append(document_check)

    if content is not None:
        rerun_check, rerun = _check(
            "current_code_deterministic_rerun",
            lambda: deterministic_projection(run_batch(root / "configs/examples/d5_experiments.json")),
        )
        if rerun_check["passed"] and rerun != deterministic_projection(content["raw"]):
            rerun_check = {
                "name": "current_code_deterministic_rerun",
                "passed": False,
                "detail": "当前代码重跑与D5冻结结果的确定性字段不一致。",
            }
        elif rerun_check["passed"]:
            rerun_check["detail"] = {
                "case_count": rerun["aggregate"]["case_count"],
                "timing_fields_ignored": True,
                "environment_fields_ignored": True,
            }
    else:
        rerun_check = {
            "name": "current_code_deterministic_rerun", "passed": False,
            "detail": "前置冻结内容检查失败。",
        }
    checks.append(rerun_check)

    return {
        "status": "passed" if all(item["passed"] for item in checks) else "failed",
        "scope": "B_D6_numeric_evidence_audit",
        "real_ship_validation": False,
        "environment": {
            "python_version": platform.python_version(),
            "platform": platform.platform(),
            "model_service_used": False,
        },
        "checks": checks,
        "summary": {
            "passed_checks": sum(item["passed"] for item in checks),
            "total_checks": len(checks),
            "d5_case_count": content["aggregate"]["case_count"] if content else None,
            "d5_verified_case_count": (
                content["aggregate"]["verified_case_count"] if content else None
            ),
            "manifest_file_count": (
                len(manifest["verified_files"]) if manifest else None
            ),
            "boundary_count": len(boundaries) if boundaries else None,
            "document_count": len(documents) if documents else None,
        },
        "limitations": [
            "本审计验证软件证据一致性，不验证真实船舶能耗精度或航行安全。",
            "当前代码重跑忽略环境描述和计时字段；数值、状态、单位与比较字段必须一致。",
            "依赖独立安装与展示材料打开检查仍由C执行，最终版本和提交由A批准。",
        ],
    }


def write_report(report: dict[str, Any], output: Path) -> None:
    if output.exists():
        raise FileExistsError(f"输出文件已存在，拒绝覆盖：{output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="运行D6数字、单位与冻结证据审计")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    report = run_audit(args.root.resolve())
    if args.output is not None:
        write_report(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
