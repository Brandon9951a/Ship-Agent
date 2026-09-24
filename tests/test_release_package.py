import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts.package_release import (
    PACKAGE_ROOT,
    build_release_package,
    collect_release_files,
    scan_release_files,
)


def test_release_allowlist_excludes_private_materials_and_old_logs():
    included, excluded = collect_release_files()

    assert "README.md" in included
    assert "scripts/package_release.py" in included
    assert "docs/协作/最新开发与验收记录.md" in included
    assert "docs/验证/D5_B_冻结证据/raw_results.json" in included
    assert "docs/团队资料/README_团队资料导航.md" in excluded
    assert "docs/协作/D6_B_数字与证据复核.md" in excluded
    assert all(".venv" not in item and ".git/" not in item for item in included)
    scan_release_files(Path(__file__).resolve().parents[1], included)


def test_release_package_has_manifest_and_stable_hash(tmp_path):
    output = tmp_path / "release.zip"
    manifest = tmp_path / "release.json"
    report = build_release_package(
        output,
        manifest,
        require_clean=False,
    )

    assert report["status"] == "release_created"
    assert report["secret_and_privacy_scan"] == "passed"
    assert report["package_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    assert json.loads(manifest.read_text(encoding="utf-8"))["source_commit"]
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert f"{PACKAGE_ROOT}/README.md" in names
        assert f"{PACKAGE_ROOT}/PACKAGE_MANIFEST.json" in names
        assert not any("团队资料" in name for name in names)
        internal = json.loads(
            archive.read(f"{PACKAGE_ROOT}/PACKAGE_MANIFEST.json")
        )
        assert internal["file_count"] == report["included_file_count"]
        assert internal["real_ship_validation"] is False
    with pytest.raises(FileExistsError):
        build_release_package(
            output,
            tmp_path / "other.json",
            require_clean=False,
        )


def test_release_package_is_deterministic(tmp_path):
    first_output = tmp_path / "first.zip"
    second_output = tmp_path / "second.zip"
    first = build_release_package(
        first_output,
        tmp_path / "first.json",
        require_clean=False,
    )
    second = build_release_package(
        second_output,
        tmp_path / "second.json",
        require_clean=False,
    )

    assert first_output.read_bytes() == second_output.read_bytes()
    assert first["package_sha256"] == second["package_sha256"]


def test_release_scan_rejects_credentials_and_personal_paths(tmp_path):
    (tmp_path / "secret.txt").write_text(
        "XFYUN_API_KEY=not-for-release",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="敏感信息"):
        scan_release_files(tmp_path, ["secret.txt"])


def test_release_scan_rejects_model_api_key(tmp_path):
    (tmp_path / "secret.txt").write_text(
        "SHIP_LLM_API_KEY=sk-live-value",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="敏感信息"):
        scan_release_files(tmp_path, ["secret.txt"])

    (tmp_path / "secret.txt").write_text(
        "C:" + r"\Users\demo\project",
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="敏感信息"):
        scan_release_files(tmp_path, ["secret.txt"])
