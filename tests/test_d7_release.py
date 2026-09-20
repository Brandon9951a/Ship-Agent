import hashlib
import json
import zipfile

import pytest

from scripts.check_d7_readiness import (
    inspect_final_materials,
    verify_candidate_package,
    write_report,
)
from scripts.package_d7 import build_package, collect_tracked_files, scan_secrets


def test_package_allowlist_excludes_raw_team_materials_and_credentials():
    included, excluded = collect_tracked_files()
    assert "README.md" in included
    assert "scripts/package_d7.py" in included
    assert "docs/验证/D5_B_冻结证据/raw_results.json" in included
    assert any("05_航行数据" in item for item in excluded)
    assert all(".venv" not in item and ".git/" not in item for item in included)
    scan_secrets(__import__("pathlib").Path(__file__).resolve().parents[1], included)


def test_package_is_deterministic_scoped_and_hash_checked(tmp_path):
    output = tmp_path / "candidate.zip"
    manifest = tmp_path / "candidate.json"
    report = build_package(output, manifest)
    assert report["secret_scan"] == "passed"
    assert report["package_sha256"] == hashlib.sha256(output.read_bytes()).hexdigest()
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
        assert "Ship-Agent/README.md" in names
        assert "Ship-Agent/PACKAGE_MANIFEST.json" in names
        assert not any("05_航行数据" in name for name in names)
        internal = json.loads(archive.read("Ship-Agent/PACKAGE_MANIFEST.json"))
        assert internal["file_count"] == report["included_file_count"]
        assert internal["real_ship_validation"] is False
    with pytest.raises(FileExistsError):
        build_package(output, tmp_path / "other.json")


def test_secret_scan_rejects_token(tmp_path):
    fake_token = "sk-" + "abcdefghijklmnop1234"
    (tmp_path / "unsafe.txt").write_text("token=" + fake_token, encoding="utf-8")
    with pytest.raises(ValueError, match="敏感凭据"):
        scan_secrets(tmp_path, ["unsafe.txt"])


def test_final_material_audit_keeps_missing_items_open(tmp_path):
    result = inspect_final_materials(tmp_path)
    assert result["ready"] is False
    assert len(result["missing"]) == 7
    solution = tmp_path / "docs/团队资料/08_参赛成果待填/01_作品方案"
    solution.mkdir(parents=True)
    (solution / "方案.pdf").write_bytes(b"%PDF-1.4\n")
    result = inspect_final_materials(tmp_path)
    assert result["items"]["solution_pdf"]["present"] is True
    assert len(result["missing"]) == 6


def test_candidate_package_verifier_and_report_overwrite(tmp_path):
    package = tmp_path / "artifacts/candidate.zip"
    package.parent.mkdir()
    package.write_bytes(b"candidate")
    manifest = tmp_path / "docs/验证/D7_B_工程候选包清单.json"
    manifest.parent.mkdir(parents=True)
    manifest.write_text(json.dumps({
        "package_path": "artifacts/candidate.zip",
        "package_sha256": hashlib.sha256(b"candidate").hexdigest(),
        "source_commit": "abc",
    }), encoding="utf-8")
    assert verify_candidate_package(tmp_path)["passed"] is True

    output = tmp_path / "readiness.json"
    report = {"status": "waiting_external_deliverables"}
    write_report(report, output)
    with pytest.raises(FileExistsError):
        write_report(report, output)
