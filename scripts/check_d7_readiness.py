"""Report D7 submission readiness without treating missing A/C outputs as done."""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

from scripts.audit_d6 import run_audit


ROOT = Path(__file__).resolve().parents[1]
FINAL_ROOT = Path("docs/团队资料/08_参赛成果待填")


def _first(root: Path, patterns: list[str]) -> Path | None:
    for pattern in patterns:
        matches = sorted(path for path in root.glob(pattern) if path.is_file())
        if matches:
            return matches[0]
    return None


def inspect_final_materials(root: Path) -> dict[str, Any]:
    final_root = root / FINAL_ROOT
    definitions = {
        "solution_pdf": (["01_作品方案/*.pdf"], 10_000_000),
        "ppt_pdf": (["03_答辩PPT/*.pdf"], None),
        "demo_video": (["02_演示视频/*.mp4"], 300_000_000),
        "evidence_pdf": (["04_佐证PDF/*.pdf"], None),
        "engineering_zip": (["05_工程交付/*.zip"], None),
        "netdisk_record": (["05_工程交付/*网盘*.md", "05_工程交付/*网盘*.txt"], None),
        "submission_receipt": (["*提交回执*", "*成功截图*"], None),
    }
    items = {}
    for name, (patterns, limit) in definitions.items():
        path = _first(final_root, patterns)
        size = path.stat().st_size if path else None
        size_ok = path is not None and (limit is None or size <= limit)
        items[name] = {
            "present": path is not None,
            "path": str(path.relative_to(root)).replace("\\", "/") if path else None,
            "size_bytes": size,
            "size_limit_bytes": limit,
            "size_ok": size_ok,
        }
    return {
        "ready": all(item["present"] and item["size_ok"] for item in items.values()),
        "items": items,
        "missing": [name for name, item in items.items() if not item["present"]],
        "invalid_size": [
            name for name, item in items.items()
            if item["present"] and not item["size_ok"]
        ],
    }


def verify_candidate_package(root: Path) -> dict[str, Any]:
    manifest_path = root / "docs/验证/D7_B_工程候选包清单.json"
    if not manifest_path.is_file():
        return {"passed": False, "reason": "D7工程候选包清单不存在。"}
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    package = root / manifest["package_path"]
    if not package.is_file():
        return {"passed": False, "reason": "D7本地工程候选包不存在。", "manifest": manifest}
    digest = hashlib.sha256(package.read_bytes()).hexdigest()
    passed = digest == manifest.get("package_sha256")
    return {
        "passed": passed,
        "reason": None if passed else "D7候选包SHA-256与清单不一致。",
        "package_path": manifest["package_path"],
        "package_size_bytes": package.stat().st_size,
        "package_sha256": digest,
        "source_commit": manifest.get("source_commit"),
        "candidate_only": True,
    }


def run_readiness(root: Path = ROOT) -> dict[str, Any]:
    d6_report_path = root / "docs/验证/D6_B_审计报告.json"
    snapshot_path = root / "docs/验证/D6_B_源码快照复现.json"
    d6_report = json.loads(d6_report_path.read_text(encoding="utf-8"))
    snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    current_audit = run_audit(root)
    package = verify_candidate_package(root)
    b_checks = {
        "d6_frozen_report_passed": d6_report.get("status") == "passed",
        "d6_snapshot_reproduction_passed": snapshot.get("status") == "passed",
        "current_d6_audit_passed": current_audit.get("status") == "passed",
        "candidate_package_hash_passed": package.get("passed") is True,
        "all_outputs_non_real_ship": all(
            item.get("real_ship_validation") is False
            for item in (d6_report, snapshot, current_audit)
        ),
    }
    materials = inspect_final_materials(root)
    b_ready = all(b_checks.values())
    submission_ready = b_ready and materials["ready"]
    return {
        "status": "ready" if submission_ready else "waiting_external_deliverables",
        "b_scope_status": "passed" if b_ready else "failed",
        "submission_ready": submission_ready,
        "real_ship_validation": False,
        "b_checks": b_checks,
        "candidate_package": package,
        "final_materials": materials,
        "external_actions_required": [
            "A按顺序审核并合并D4、D5、D6、D7，确定最终冻结提交。",
            "A/C生成并核验作品方案PDF、PPT PDF、演示视频和佐证PDF。",
            "A以最终冻结提交重新生成正式工程包，C上传网盘，A检查链接和提取码。",
            "A核对报名字段、截止时刻和匿名要求，完成正式提交并保存回执。",
        ],
        "limitations": [
            "B侧通过不等于整套参赛材料已完成或已提交。",
            "内部工程候选包不得替代A从最终冻结提交生成的正式工程包。",
            "缺失文件保持missing，不以占位文件冒充完成。",
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
    parser = argparse.ArgumentParser(description="检查D7提交就绪状态")
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--strict", action="store_true")
    args = parser.parse_args()
    report = run_readiness(args.root.resolve())
    if args.output is not None:
        write_report(report, args.output)
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 1 if args.strict and not report["submission_ready"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
