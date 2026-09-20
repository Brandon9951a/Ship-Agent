"""Create a deterministic, share-scope-conscious D7 source package candidate."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import subprocess
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
ROOT_FILES = {
    ".env.example", ".gitignore", "README.md", "main.py", "pyproject.toml",
    "requirements.txt",
}
CODE_PREFIXES = ("config/", "configs/", "core/", "schemas/", "scripts/", "tests/", "tools/", "ui/")
DOC_PREFIXES = ("docs/接口/", "docs/算法/", "docs/数据/", "docs/协作/", "docs/验证/")
DOC_ROOT_FILES = {"docs/architecture.md", "docs/M2.md"}
FORBIDDEN_NAMES = {".env", "id_rsa", "id_ed25519"}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".p12", ".pfx"}
SECRET_PATTERNS = {
    "api_token": re.compile(rb"(?:sk|dsk)-[A-Za-z0-9_-]{16,}"),
    "private_key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "nonempty_ship_key": re.compile(
        rb"(?mi)^SHIP_LLM_API_KEY[ \t]*=[ \t]*[^\r\n \t]+"
    ),
}


def _git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], check=True, capture_output=True
    )
    return completed.stdout


def _allowed(relative: str) -> bool:
    path = PurePosixPath(relative)
    if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return False
    if relative in ROOT_FILES or relative in DOC_ROOT_FILES:
        return True
    return relative.startswith(CODE_PREFIXES + DOC_PREFIXES)


def collect_tracked_files(root: Path = ROOT) -> tuple[list[str], list[str]]:
    tracked = [
        item.decode("utf-8")
        for item in _git(root, "ls-files", "-z").split(b"\0") if item
    ]
    included = sorted(item for item in tracked if _allowed(item) and (root / item).is_file())
    excluded = sorted(set(tracked) - set(included))
    if not included:
        raise ValueError("工程候选包没有可收录文件。")
    return included, excluded


def scan_secrets(root: Path, files: list[str]) -> None:
    findings = []
    for relative in files:
        data = (root / relative).read_bytes()
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                findings.append(f"{relative}:{name}")
    if findings:
        raise ValueError("候选包疑似包含敏感凭据：" + ", ".join(findings))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 9, 20, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def build_package(
    output: Path,
    manifest_output: Path,
    *,
    root: Path = ROOT,
) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"候选包已存在，拒绝覆盖：{output}")
    if manifest_output.exists():
        raise FileExistsError(f"候选包清单已存在，拒绝覆盖：{manifest_output}")
    files, excluded = collect_tracked_files(root)
    scan_secrets(root, files)
    commit = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    file_hashes = {relative: _sha256((root / relative).read_bytes()) for relative in files}
    internal_manifest = {
        "format_version": 1,
        "source_commit": commit,
        "file_count": len(files),
        "files": file_hashes,
        "real_ship_validation": False,
        "scope_note": (
            "工程源码候选包；不含原始船舶资料、历史航行数据、官方模板、"
            "虚拟环境、.git、真实密钥及最终赛事材料。"
        ),
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "x", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative in files:
            archive.writestr(_zip_info(f"Ship-Agent/{relative}"), (root / relative).read_bytes())
        archive.writestr(
            _zip_info("Ship-Agent/PACKAGE_MANIFEST.json"),
            (json.dumps(internal_manifest, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        )
    package_hash = _sha256(output.read_bytes())
    report = {
        "status": "candidate_created",
        "source_commit": commit,
        "package_path": str(output.relative_to(root)).replace("\\", "/")
        if output.is_relative_to(root) else str(output),
        "package_size_bytes": output.stat().st_size,
        "package_sha256": package_hash,
        "included_file_count": len(files),
        "excluded_tracked_file_count": len(excluded),
        "secret_scan": "passed",
        "real_ship_validation": False,
        "excluded_scope": [
            "docs/团队资料中的原始船舶资料、历史航行数据、官方模板及待填赛事成果",
            "虚拟环境、.git、本地artifacts、缓存和真实密钥",
            "最终方案PDF、PPT PDF、视频、佐证PDF、网盘信息和提交回执",
        ],
        "limitations": [
            "这是B生成的内部工程候选包，不是A批准的最终提交包。",
            "A合并D4-D7并确定冻结提交后必须重新生成，不能直接沿用本候选包。",
            "原始资料能否公开须由A确认；本包默认不收录。",
        ],
    }
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="生成D7内部工程源码候选包")
    parser.add_argument(
        "--output", type=Path,
        default=ROOT / "artifacts/d7-lwj/Ship-Agent-B-D7-source.zip",
    )
    parser.add_argument(
        "--manifest", type=Path,
        default=ROOT / "docs/验证/D7_B_工程候选包清单.json",
    )
    args = parser.parse_args()
    report = build_package(args.output.resolve(), args.manifest.resolve())
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
