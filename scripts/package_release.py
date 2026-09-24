"""Build the reviewed competition source package from tracked files."""

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
PACKAGE_ROOT = "Green-Shipping-Agent"
ROOT_FILES = {
    ".env.example",
    ".gitignore",
    "README.md",
    "THIRD_PARTY_NOTICES.md",
    "main.py",
    "pyproject.toml",
    "render.yaml",
    "requirements.txt",
    "start_local_demo.cmd",
}
CODE_PREFIXES = (
    "config/",
    "configs/",
    "core/",
    "schemas/",
    "scripts/",
    "tests/",
    "tools/",
    "ui/",
)
PACKAGE_EXCLUDED_TESTS = {
    # 依赖不随工程包分发的原始路线证据文件。
    "tests/test_c_route_evidence.py",
    # 验证打包器本身，需要 Git 元数据；ZIP 内不包含 .git。
    "tests/test_release_package.py",
}
DOCUMENT_FILES = {
    "docs/architecture.md",
    "docs/工程交付说明.md",
    "docs/协作/最新开发与验收记录.md",
    "docs/算法/能耗基线与验证设计.md",
    "docs/数据/字段字典.md",
    "docs/数据/航线与取数规则.md",
}
DOCUMENT_PREFIXES = (
    "docs/接口/",
    "docs/验证/D5_B_冻结证据/",
)
FORBIDDEN_NAMES = {".env", "id_rsa", "id_ed25519"}
FORBIDDEN_SUFFIXES = {".key", ".pem", ".p12", ".pfx", ".sqlite", ".sqlite3"}
SECRET_PATTERNS = {
    "api_token": re.compile(rb"(?:sk|dsk)-[A-Za-z0-9_-]{16,}"),
    "private_key": re.compile(rb"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "configured_secret": re.compile(
        rb"(?mi)^(?:SHIP_LLM_API_KEY|XFYUN_APPID|XFYUN_API_KEY|"
        rb"XFYUN_API_SECRET)[ \t]*=[ \t]*[^\r\n \t#]+"
    ),
}
PRIVATE_TEXT = (
    "C:\\Users\\",
    "D:\\Desktop\\",
)


def _git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *args],
        check=True,
        capture_output=True,
    )
    return completed.stdout


def _allowed(relative: str) -> bool:
    path = PurePosixPath(relative)
    if path.name in FORBIDDEN_NAMES or path.suffix.lower() in FORBIDDEN_SUFFIXES:
        return False
    if relative in PACKAGE_EXCLUDED_TESTS:
        return False
    if relative in ROOT_FILES or relative in DOCUMENT_FILES:
        return True
    return relative.startswith(CODE_PREFIXES + DOCUMENT_PREFIXES)


def collect_release_files(root: Path = ROOT) -> tuple[list[str], list[str]]:
    tracked = [
        item.decode("utf-8")
        for item in _git(root, "ls-files", "-z").split(b"\0")
        if item
    ]
    included = sorted(
        item for item in tracked if _allowed(item) and (root / item).is_file()
    )
    excluded = sorted(set(tracked) - set(included))
    if not included:
        raise ValueError("工程资料包没有可收录文件。")
    return included, excluded


def scan_release_files(root: Path, files: list[str]) -> None:
    findings: list[str] = []
    for relative in files:
        data = (root / relative).read_bytes()
        for name, pattern in SECRET_PATTERNS.items():
            if pattern.search(data):
                findings.append(f"{relative}:{name}")
        text = data.decode("utf-8", errors="ignore")
        for marker in PRIVATE_TEXT:
            if marker in text:
                findings.append(f"{relative}:private_text")
                break
    if findings:
        raise ValueError("工程资料包包含敏感信息：" + ", ".join(findings))


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _zip_info(name: str) -> zipfile.ZipInfo:
    info = zipfile.ZipInfo(name, date_time=(2026, 9, 25, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100644 << 16
    return info


def _is_clean(root: Path) -> bool:
    return not _git(root, "status", "--porcelain", "--untracked-files=no").strip()


def build_release_package(
    output: Path,
    manifest_output: Path,
    *,
    root: Path = ROOT,
    require_clean: bool = True,
) -> dict[str, Any]:
    if output.exists():
        raise FileExistsError(f"工程资料包已存在，拒绝覆盖：{output}")
    if manifest_output.exists():
        raise FileExistsError(f"工程资料清单已存在，拒绝覆盖：{manifest_output}")
    if require_clean and not _is_clean(root):
        raise ValueError("工作区存在未提交修改，拒绝生成正式工程资料包。")

    files, excluded = collect_release_files(root)
    scan_release_files(root, files)
    commit = _git(root, "rev-parse", "HEAD").decode("ascii").strip()
    file_hashes = {
        relative: _sha256((root / relative).read_bytes()) for relative in files
    }
    internal_manifest = {
        "format_version": 1,
        "project": "绿航智算：船舶能效管理智能体",
        "source_commit": commit,
        "file_count": len(files),
        "files": file_hashes,
        "scope": "synthetic_demo",
        "real_ship_validation": False,
    }

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(
        output,
        "x",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for relative in files:
            archive.writestr(
                _zip_info(f"{PACKAGE_ROOT}/{relative}"),
                (root / relative).read_bytes(),
            )
        archive.writestr(
            _zip_info(f"{PACKAGE_ROOT}/PACKAGE_MANIFEST.json"),
            (json.dumps(internal_manifest, ensure_ascii=False, indent=2) + "\n").encode(
                "utf-8"
            ),
        )

    report = {
        "status": "release_created",
        "project": internal_manifest["project"],
        "source_commit": commit,
        "package_path": str(output),
        "package_size_bytes": output.stat().st_size,
        "package_sha256": _sha256(output.read_bytes()),
        "included_file_count": len(files),
        "excluded_tracked_file_count": len(excluded),
        "secret_and_privacy_scan": "passed",
        "scope": "synthetic_demo",
        "real_ship_validation": False,
        "excluded_scope": [
            "API密钥、令牌、私钥、本地.env和checkpoint数据库",
            "Git历史、虚拟环境、缓存、日志和生成目录",
            "原始船舶资料、历史航行原始数据和内部协作流水账",
            "作品方案、答辩PPT、演示视频和佐证材料",
        ],
    }
    manifest_output.parent.mkdir(parents=True, exist_ok=True)
    manifest_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n",
        encoding="utf-8",
    )
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description="生成比赛工程资料包")
    parser.add_argument(
        "--output",
        type=Path,
        default=ROOT / "dist/绿航智算-工程资料-20260925.zip",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=ROOT / "dist/绿航智算-工程资料-20260925.json",
    )
    parser.add_argument(
        "--allow-dirty",
        action="store_true",
        help="仅用于本地检查；正式交付不要启用",
    )
    args = parser.parse_args()
    report = build_release_package(
        args.output.resolve(),
        args.manifest.resolve(),
        require_clean=not args.allow_dirty,
    )
    print(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
