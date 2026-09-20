import json

import pytest

from scripts.audit_d6 import (
    deterministic_projection,
    run_audit,
    verify_manifest,
    write_report,
)


@pytest.fixture(scope="module")
def report():
    return run_audit()


def test_d6_audit_passes_all_checks(report):
    assert report["status"] == "passed"
    assert report["real_ship_validation"] is False
    assert report["summary"] == {
        "passed_checks": 5,
        "total_checks": 5,
        "d5_case_count": 40,
        "d5_verified_case_count": 40,
        "manifest_file_count": 4,
        "boundary_count": 5,
        "document_count": 3,
    }


def test_deterministic_projection_ignores_only_environment_and_timing():
    source = {
        "environment": {"platform": "one"},
        "aggregate": {"case_count": 1, "mean_case_elapsed_ms": 3.2,
                      "max_case_elapsed_ms": 4.5},
        "results": [{"case_id": "x", "elapsed_ms": 2.1, "energy": 1.2345678912}],
    }
    changed = json.loads(json.dumps(source))
    changed["environment"]["platform"] = "two"
    changed["aggregate"]["mean_case_elapsed_ms"] = 99
    changed["results"][0]["elapsed_ms"] = 88
    assert deterministic_projection(source) == deterministic_projection(changed)
    changed["results"][0]["energy"] = 2
    assert deterministic_projection(source) != deterministic_projection(changed)


def test_manifest_detects_tampering(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "data.json").write_text("{}\n", encoding="utf-8")
    (evidence / "evidence_manifest.json").write_text(
        json.dumps({
            "algorithm": "SHA-256",
            "real_ship_validation": False,
            "files": {"data.json": "0" * 64},
        }),
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="哈希不一致"):
        verify_manifest(evidence)


def test_audit_writer_refuses_overwrite(tmp_path, report):
    output = tmp_path / "audit.json"
    write_report(report, output)
    restored = json.loads(output.read_text(encoding="utf-8"))
    assert restored["status"] == "passed"
    with pytest.raises(FileExistsError):
        write_report(report, output)
