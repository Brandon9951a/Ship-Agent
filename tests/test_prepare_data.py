"""D2 fixtures are artificial, without real telemetry or private coordinates."""

import json
from zipfile import ZipFile

import pytest

from scripts.prepare_data import main, prepare_file, prepare_rows

MAPPING = {"time_column": 0, "time_format": "elapsed_seconds", "fields": {
    "speed": {"column": 1, "role": "speed", "unit": "knots"},
    "power": {"column": 2, "role": "power", "unit": "W"},
}}


def test_units_missing_zero_and_negative_preserved():
    rows, stats = prepare_rows([["t", "v", "p"], [0, 2, 3000], [5, "", 3000],
                                [10, 0, 3000], [15, 2, -3000]], MAPPING)
    assert rows[0]["values"] == {"speed": 3.704, "power": 3}
    assert rows[1]["values"]["speed"] is None
    assert [r["disposition"] for r in rows] == ["candidate", "missing_numeric", "stopped", "negative_power"]
    assert rows[3]["values"]["power"] == -3
    assert stats["counts_reconciled"] and not stats["training_ready"]


def test_all_duplicate_timestamp_members_quarantined_without_first_selection():
    rows, stats = prepare_rows([["t", "v", "p"], [0, 2, 3000], [0, 2, 4000],
                                [5, 2, 3000], [0, 2, 3000]], MAPPING)
    assert all("duplicate_timestamp" in rows[i]["flags"] for i in (0, 1, 3))
    assert stats["flag_counts_overlapping"]["duplicate_timestamp"] == 3
    assert stats["flag_counts_overlapping"]["duplicate_record"] == 2
    assert rows[0]["values"]["power"] == 3 and rows[1]["values"]["power"] == 4
    assert len(rows) == 4


def test_ragged_blank_invalid_missing_accounting_is_exclusive():
    rows, stats = prepare_rows([["t", "v", "p"], [], [1, 2], ["bad", 2, 3],
                                [None, 2, 3], [3, "[]", 3], [4, -1, 3]], MAPPING)
    assert sum(stats["primary_counts"].values()) == stats["records_after_header"] == 6
    assert [r["disposition"] for r in rows] == ["blank_record", "ragged_record", "invalid_time",
                                               "missing_time", "invalid_numeric", "out_of_range"]
    assert stats["flag_counts_overlapping"]["missing_time"] == 2


@pytest.mark.parametrize("value", ["inf", "-inf", "1e309", "[]", "text"])
def test_bad_numeric_not_zero(value):
    rows, _ = prepare_rows([["t", "v", "p"], [0, value, 3]], MAPPING)
    assert rows[0]["values"]["speed"] is None
    assert rows[0]["disposition"] == "invalid_numeric"


def test_unknown_units_and_position_never_exported():
    mapping = {"time_column": 0, "time_format": "elapsed_seconds", "fields": {
        "power_raw": {"column": 1, "role": "other", "unit": None},
        "latitude": {"column": 2, "role": "position"},
    }}
    rows, stats = prepare_rows([["t", "p", "gps"], [0, "private-sensor-text", "116.123456"]], mapping)
    rendered = json.dumps([rows, stats])
    assert "116.123456" not in rendered and "private-sensor-text" not in rendered
    assert rows[0]["values"] == {} and rows[0]["disposition"] == "unconfirmed_units"


def test_unknown_soc_does_not_erase_known_power_speed_pair():
    mapping = dict(MAPPING, fields=dict(MAPPING["fields"], soc_candidate={"column": 3, "role": "other"}))
    rows, stats = prepare_rows([["t", "v", "p", "soc"], [0, 2, 3000, 75]], mapping)
    assert rows[0]["disposition"] == "candidate"
    assert "soc_candidate" not in rows[0]["values"]
    assert not stats["training_ready"]


def test_mixed_timezone_and_invalid_gap_breaks_order():
    mapping = dict(MAPPING, time_format="iso")
    rows, _ = prepare_rows([["t", "v", "p"], ["2025-01-01T00:00:00", 2, 3],
                            ["2025-01-01T00:00:05Z", 2, 3], ["bad", 2, 3],
                            ["2024-01-01T00:00:00", 2, 3]], mapping)
    assert rows[1]["disposition"] == "mixed_timezone"
    assert rows[2]["disposition"] == "invalid_time"
    assert "out_of_order" not in rows[3]["flags"]


def test_equivalent_aware_instants_are_duplicates():
    rows, _ = prepare_rows([["t", "v", "p"], ["2025-01-01T08:00:00+08:00", 2, 3],
                            ["2025-01-01T00:00:00Z", 2, 4]], dict(MAPPING, time_format="iso"))
    assert all("duplicate_timestamp" in row["flags"] for row in rows)


@pytest.mark.parametrize("mapping", [{}, dict(MAPPING, header_row=0),
    dict(MAPPING, fields={}), dict(MAPPING, time_column="absent"),
    dict(MAPPING, fields={"p": {"column": 2, "role": "power", "unit": "unknown"}})])
def test_invalid_mapping_refused(mapping):
    with pytest.raises(ValueError):
        prepare_rows([["t", "v", "p"], [0, 2, 3]], mapping)


def test_csv_source_bytes_unchanged(tmp_path):
    source = tmp_path / "sample.csv"
    source.write_text("t,v,p\n0,2,3000\n", encoding="utf-8-sig")
    before = source.read_bytes()
    meta, tables = prepare_file(source, MAPPING)
    assert meta["source_unchanged"] and source.read_bytes() == before
    assert tables[0][2]["records_after_header"] == 1


def test_xlsx_reader_and_unit_conversion(tmp_path):
    source = tmp_path / "sample.xlsx"
    with ZipFile(source, "w") as archive:
        archive.writestr("xl/workbook.xml", '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="S" r:id="r1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships><Relationship Id="r1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/worksheets/sheet1.xml", '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row><c t="inlineStr"><is><t>t</t></is></c><c t="inlineStr"><is><t>v</t></is></c><c t="inlineStr"><is><t>p</t></is></c></row><row><c><v>0</v></c><c><v>2</v></c><c><v>3000</v></c></row></sheetData></worksheet>')
    before = source.read_bytes()
    _, tables = prepare_file(source, MAPPING)
    assert tables[0][1][0]["values"]["power"] == 3
    assert source.read_bytes() == before


def test_cli_protects_non_artifact_output_and_existing_directories(tmp_path, monkeypatch):
    import scripts.prepare_data as module
    monkeypatch.setattr(module, "ROOT", tmp_path)
    # Establish the precondition explicitly, never rely on ignored local outputs.
    artifacts = tmp_path / "artifacts"
    artifacts.mkdir()
    assert main(["--output-dir", str(tmp_path / "out")]) == 1
    assert not (tmp_path / "out").exists()
    assert main(["--output-dir", str(artifacts)]) == 1
    assert list(artifacts.iterdir()) == []


def test_cli_invalid_mapping_leaves_no_outputs(tmp_path, monkeypatch):
    import scripts.prepare_data as module
    monkeypatch.setattr(module, "ROOT", tmp_path)
    mapping = tmp_path / "mapping.json"
    mapping.write_text("[]", encoding="utf-8")
    output = tmp_path / "artifacts" / "invalid"
    assert main(["--mapping", str(mapping), "--output-dir", str(output)]) == 1
    assert not output.exists()


def test_cli_success_and_existing_output_not_overwritten(tmp_path, monkeypatch):
    import scripts.prepare_data as module
    monkeypatch.setattr(module, "ROOT", tmp_path)
    source = tmp_path / "sample.csv"
    source.write_text("t,v,p\n0,2,3000\n", encoding="utf-8")
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps(MAPPING), encoding="utf-8")
    output = tmp_path / "artifacts" / "prepared"
    arguments = [str(source), "--mapping", str(mapping), "--output-dir", str(output)]
    assert main(arguments) == 0
    before = (output / "summary.json").read_bytes()
    assert main(arguments) == 1
    assert (output / "summary.json").read_bytes() == before
    report = json.loads(before)
    assert report["files"][0]["tables"][0]["counts_reconciled"]
    records = (output / "quality-1-1.jsonl").read_text().splitlines()
    assert len(records) == 1 and json.loads(records[0])["disposition"] == "candidate"


def test_cli_reports_incomplete_default_inventory(tmp_path, monkeypatch):
    import scripts.prepare_data as module
    monkeypatch.setattr(module, "ROOT", tmp_path)
    # Discovery is mocked, but source parsing uses the actual CSV fixture.
    source = tmp_path / "sample.csv"
    source.write_text("t,v,p\n0,2,3000\n", encoding="utf-8")
    monkeypatch.setattr(module, "discover_files", lambda root: [source])
    mapping = tmp_path / "mapping.json"
    mapping.write_text(json.dumps(MAPPING), encoding="utf-8")
    output = tmp_path / "artifacts" / "partial"
    assert main(["--mapping", str(mapping), "--output-dir", str(output)]) == 2
    report = json.loads((output / "summary.json").read_text())
    assert report["status"] == "prepared_partial" and len(report["missing_expected_files"]) == 3
