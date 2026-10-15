"""Run reproducible Tmanagement correctness, comparison, sensitivity and scale cases."""

from __future__ import annotations

import argparse
import copy
import json
import os
import platform
import subprocess
import sys
import time
import tracemalloc
from dataclasses import replace
from pathlib import Path
from typing import Any

from schemas.research_management import BatteryManagementRequest, CostWeights
from scripts.fdp_research_cases import (
    build_full_voyage_request,
    build_request,
    invalid_missing_efficiency_request,
)
from tools.tmanagement_fdp import compare_policies, run_management_fdp


def _measured_run(request: BatteryManagementRequest) -> tuple[dict[str, Any], float, int]:
    tracemalloc.start()
    started = time.perf_counter()
    result = run_management_fdp(request)
    elapsed_ms = (time.perf_counter() - started) * 1000
    _current, peak_bytes = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    return result, round(elapsed_ms, 3), peak_bytes


def _run_automated_tests() -> dict[str, Any]:
    environment = dict(os.environ)
    environment["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    command = [
        sys.executable, "-B", "-m", "pytest", "-q", "-p",
        "no:cacheprovider", "tests/test_fdp_research.py", "--tb=short",
    ]
    result = subprocess.run(
        command, cwd=Path.cwd(), env=environment, capture_output=True,
        text=True, check=False,
    )
    output = (result.stdout + result.stderr).strip()
    summary = next(
        (line for line in output.splitlines() if " passed" in line or " failed" in line),
        output[-500:],
    )
    return {"command": "python -B -m pytest -q -p no:cacheprovider tests/test_fdp_research.py",
            "exit_code": result.returncode, "summary": summary}


def _stable_result(result: dict[str, Any]) -> dict[str, Any]:
    value = copy.deepcopy(result)
    for key in ("started_at", "finished_at", "runtime_ms"):
        value.pop(key, None)
    return value


def _sample_cases() -> dict[str, tuple[dict[str, Any], BatteryManagementRequest]]:
    cases = {}
    for case_id in (
        "success",
        "instantaneous_overload",
        "soc_depletion",
        "grid_budget",
        "charging_unsupported",
    ):
        request = build_request(case_id)
        cases[case_id] = (request.to_dict(), request)
    missing = invalid_missing_efficiency_request()
    cases["missing_efficiency"] = (
        missing, BatteryManagementRequest.from_dict(missing)
    )
    return cases


def build_report() -> tuple[dict[str, Any], dict[str, Any]]:
    automated_tests = _run_automated_tests()
    examples = {}
    for case_id, (raw_request, request) in _sample_cases().items():
        result = run_management_fdp(request)
        examples[case_id] = {
            "request": raw_request,
            "result": _stable_result(result),
        }

    base = build_request("success")
    comparison = compare_policies(base)
    sensitivity = []
    profiles = (
        ("paper_reference", base.weights),
        ("half_soc_balance", replace(
            base.weights, profile_id="half_soc_balance", k_soc=base.weights.k_soc / 2
        )),
        ("double_soc_balance", replace(
            base.weights, profile_id="double_soc_balance", k_soc=base.weights.k_soc * 2
        )),
    )
    for step in (0.1, 0.05, 0.025):
        for profile_id, weights in profiles:
            candidate = replace(
                base,
                grid=replace(base.grid, soc_step=step),
                weights=weights,
            )
            result, elapsed_ms, peak_bytes = _measured_run(candidate)
            payload = result.get("payload") or {}
            summary = payload.get("summary") or {}
            sensitivity.append({
                "soc_step": step,
                "weight_profile": profile_id,
                "outcome_code": result["outcome_code"],
                "safe": payload.get("safe", False),
                "mode_trajectory": payload.get("mode_trajectory"),
                "cost_terms": payload.get("cost_terms"),
                "final_soc": [
                    summary.get("final_soc_1"), summary.get("final_soc_2")
                ],
                "transitions_seen": summary.get("transitions_seen"),
                "peak_states_per_layer": summary.get("peak_states_per_layer"),
                "elapsed_ms": elapsed_ms,
                "python_heap_peak_bytes": peak_bytes,
                "constraint_checks_passed": sum(
                    bool(item.get("passed"))
                    for item in payload.get("constraint_checks", [])
                ),
                "constraint_checks_total": len(payload.get("constraint_checks", [])),
            })

    full_request = build_full_voyage_request()
    full_result, full_elapsed_ms, full_peak_bytes = _measured_run(full_request)
    full_payload = full_result.get("payload") or {}
    full_summary = full_payload.get("summary") or {}
    report = {
        "scope": "named research simulation; not vessel validation",
        "environment": {
            "python": sys.version.split()[0],
            "platform": platform.platform(),
            "cpu": platform.processor() or platform.uname().processor or "not reported",
            "measurement": "Python tracemalloc peak; excludes native allocations",
        },
        "sample_outcomes": {
            key: {
                "outcome_code": value["result"]["outcome_code"],
                "safe": (value["result"].get("payload") or {}).get("safe"),
                "diagnostic_codes": [
                    item["code"] for item in value["result"].get("diagnostics", [])
                ],
            }
            for key, value in examples.items()
        },
        "same_input_policy_comparison": comparison,
        "automated_tests": automated_tests,
        "sensitivity": sensitivity,
        "full_voyage_24h": {
            "interval_count": len(full_request.demand_series),
            "soc_step": full_request.grid.soc_step,
            "grid": full_request.grid.to_dict(),
            "duration_s": sum(point.duration_s for point in full_request.demand_series),
            "outcome_code": full_result["outcome_code"],
            "safe": full_payload.get("safe", False),
            "final_soc": [
                full_summary.get("final_soc_1"), full_summary.get("final_soc_2")
            ],
            "mode_counts": full_summary.get("mode_counts"),
            "transitions_seen": full_summary.get("transitions_seen"),
            "peak_states_per_layer": full_summary.get("peak_states_per_layer"),
            "elapsed_ms": full_elapsed_ms,
            "python_heap_peak_bytes": full_peak_bytes,
            "constraint_checks_passed": sum(
                bool(item.get("passed"))
                for item in full_payload.get("constraint_checks", [])
            ),
            "constraint_checks_total": len(full_payload.get("constraint_checks", [])),
            "assumptions": full_request.assumptions,
        },
    }
    return report, examples


def _fmt(value: Any, digits: int = 4) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.{digits}f}"
    return str(value)


def render_markdown(report: dict[str, Any]) -> tuple[str, str]:
    outcomes = report["sample_outcomes"]
    lines = [
        "# Tmanagement FDP 验收与实验记录",
        "",
        "本记录覆盖研究仿真工具的正确性、失败语义、同口径策略对照和计算开销。",
        "参数均为具名夹具假设；结果不代表实船运行、安全认证或运营批准。",
        "",
        "## 样例结果",
        "",
        "| 样例 | outcome_code | safe | 诊断 |",
        "| --- | --- | --- | --- |",
    ]
    for name, item in outcomes.items():
        lines.append(
            f"| {name} | {item['outcome_code']} | {item['safe']} | "
            f"{', '.join(item['diagnostic_codes']) or '—'} |"
        )

    cmp = report["same_input_policy_comparison"]
    lines.extend(["", "## 相同输入下的 FDP 与状态机对照", ""])
    if cmp["fdp"].get("feasible") and cmp["state_machine"].get("feasible"):
        lines.extend([
            "| 指标 | FDP | 状态机 | FDP − 状态机 |",
            "| --- | ---: | ---: | ---: |",
        ])
        for title, key, delta_key in (
            ("末端 SOC 1", "final_soc_1", None),
            ("末端 SOC 2", "final_soc_2", None),
            ("末端 SOC 差", "final_soc_difference", "final_soc_difference_delta"),
            ("最大 SOC 差", "max_soc_difference", "max_soc_difference_delta"),
            ("电池 1 功率变化总量 (kW)", "power_variation_1_kw", None),
            ("电池 2 功率变化总量 (kW)", "power_variation_2_kw", None),
            ("模式切换次数", "switch_count", "switch_count_delta"),
        ):
            left = cmp["fdp"][key]
            right = cmp["state_machine"][key]
            delta = cmp["comparison"].get(delta_key) if delta_key else (
                left - right if isinstance(left, (int, float)) else None
            )
            lines.append(
                f"| {title} | {_fmt(left)} | {_fmt(right)} | {_fmt(delta)} |"
            )
        lines.extend([
            "",
            "| 成本分项 | FDP | 状态机 | FDP − 状态机 |",
            "| --- | ---: | ---: | ---: |",
        ])
        for key in ("power", "switch", "soc_balance", "low_load_mode3", "total"):
            lines.append(
                f"| {key} | {_fmt(cmp['fdp']['cost_terms'][key])} | "
                f"{_fmt(cmp['state_machine']['cost_terms'][key])} | "
                f"{_fmt(cmp['comparison']['cost_term_delta'][key])} |"
            )
        violations = cmp["comparison"]["constraint_violations"]
        lines.extend([
            "",
            f"独立检查器约束违例数：FDP {violations['fdp']}，"
            f"状态机 {violations['state_machine']}。",
        ])
    else:
        lines.append("至少一个策略没有可比较的可行轨迹；未计算改进率。")

    lines.extend([
        "",
        "## 自动测试",
        "",
        f"- {report['automated_tests']['command']}",
        f"- 结果：{report['automated_tests']['summary']}，"
        f"退出码 {report['automated_tests']['exit_code']}。",
        "",
        "## SOC 网格与权重敏感性",
        "",
        "| SOC 步长 | 权重方案 | outcome | safe | 轨迹 | FDP 成本 | 状态峰值 | 转移数 | 耗时 ms | Python 峰值字节 |",
        "| ---: | --- | --- | --- | --- | ---: | ---: | ---: | ---: | ---: |",
    ])
    for item in report["sensitivity"]:
        total = (item.get("cost_terms") or {}).get("total")
        lines.append(
            f"| {_fmt(item['soc_step'])} | {item['weight_profile']} | "
            f"{item['outcome_code']} | {item['safe']} | "
            f"{item.get('mode_trajectory')} | {_fmt(total)} | "
            f"{_fmt(item['peak_states_per_layer'], 0)} | "
            f"{_fmt(item['transitions_seen'], 0)} | "
            f"{_fmt(item['elapsed_ms'], 3)} | "
            f"{_fmt(item['python_heap_peak_bytes'], 0)} |"
        )

    full = report["full_voyage_24h"]
    lines.extend([
        "",
        "## 24 小时代表性全程计算",
        "",
        f"- 区间数：{full['interval_count']}；规划时长："
        f"{full['duration_s'] / 3600:.1f} 小时；SOC 网格步长："
        f"{full['soc_step']}。",
        f"- 结果：{full['outcome_code']}，safe={full['safe']}；"
        f"末端 SOC={full['final_soc']}。",
        f"- 峰值状态层：{full['peak_states_per_layer']}；"
        f"转移数：{full['transitions_seen']}；耗时：{full['elapsed_ms']} ms。",
        f"- Python 峰值分配：{full['python_heap_peak_bytes']} bytes；"
        "tracemalloc 不含本机原生分配。",
        f"- 独立检查：{full['constraint_checks_passed']}/"
        f"{full['constraint_checks_total']} 项通过。",
        "- 该情景采用具名的仿真电池与需求参数，非实测全航程。",
        "",
        "## 复现",
        "",
        "从仓库根目录运行：",
        "",
        "    python -m pytest -q tests/test_fdp_research.py",
        "    python -m scripts.fdp_research_report --output-dir docs/验收",
        "",
        f"测量环境：Python {report['environment']['python']}；"
        f"{report['environment']['platform']}；CPU：{report['environment']['cpu']}。",
        "",
        "## 已知限制",
        "",
        "- 接口仍标记为 research-tools/2.0-draft，尚未完成跨工具契约冻结或端到端联调。",
        "- 网格状态向下取整以避免高估 SOC；网格上没有路径不证明连续问题或实船全局无解。",
        "- 首版不支持充电；负母线需求返回明确的不支持结果。",
        "- 24 小时输入使用本工具内的具名仿真假设；不能用于声称实船 SOC、节能或安全。",
        "- Python 峰值内存使用 tracemalloc 测得，不包含本机原生库分配。",
        "",
    ])
    report_md = "\n".join(lines)

    comparison = report["same_input_policy_comparison"]
    full = report["full_voyage_24h"]
    pr_lines = [
        "# Tmanagement FDP 双电池能量管理工具",
        "",
        "## 修改内容",
        "",
        "- 补齐 FDP 需求分项来源验证、逐时母线能量、反馈关联信息和候选失败范围。",
        "- 将计算预算耗尽与当前离散网格无路径分开诊断；SOC 路径失败不冒充通用功率上限。",
        "- 加入独立轨迹复核、2–4 时步三模式穷举对照、状态机对比和 24 小时性能实验。",
        "",
        "## 验证结果",
        "",
        f"- 自动测试：{report['automated_tests']['summary']}，"
        f"退出码 {report['automated_tests']['exit_code']}；覆盖穷举路径、功率平衡、SOC、失败分类、来源缺失和 JSON 有效性。",
        f"- 24 小时夹具：{full['interval_count']} 个时步，{full['outcome_code']}，"
        f"safe={full['safe']}，SOC 网格 {full['soc_step']}，"
        f"峰值状态层 {full['peak_states_per_layer']}，"
        f"{full['transitions_seen']} 次转移，{full['elapsed_ms']} ms。",
        f"- 独立复核：{full['constraint_checks_passed']}/"
        f"{full['constraint_checks_total']} 项通过；Python 峰值分配 "
        f"{full['python_heap_peak_bytes']} bytes。",
        "- SOC 网格/权重敏感性与状态机同口径比较见 FDP_研究实验记录.md。",
        "- 机器可读成功及失败样例见 fdp_research_examples.json。",
        "",
        "## 接口与配置",
        "",
        "- 使用双电池独立容量、SOC、功率界限、效率及来源；需求工件保留推进/辅助分项与共同适配器来源。",
        "- 维持 research-tools/2.0-draft，不自行冻结跨工具字段或实船参数。",
        "",
        "## 已知限制",
        "",
        "- 本结果属于研究仿真，不是实船验证、完整安全方案或控制器接入。",
        "- SOC 离散网格可能返回 grid_unresolved；其结论只限请求情景与网格。",
        "- 首版不支持充电。容量、效率、功率和 24 小时负载均为具名测试假设。",
        "- 主智能体接入、跨工具重搜和项目级接口裁决仍由项目负责人统筹。",
        "",
    ]
    return report_md, "\n".join(pr_lines)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", default="docs/验收")
    args = parser.parse_args()
    report, examples = build_report()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    (output / "FDP_研究实验记录.md").write_text(
        render_markdown(report)[0], encoding="utf-8", newline="\n"
    )
    (output / "Tmanagement_FDP_PR说明.md").write_text(
        render_markdown(report)[1], encoding="utf-8", newline="\n"
    )
    (output / "fdp_research_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n"
    )
    (output / "fdp_research_examples.json").write_text(
        json.dumps(examples, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8", newline="\n"
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0 if report["automated_tests"]["exit_code"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
