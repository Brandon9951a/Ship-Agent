"""Inspection fixtures are artificial; no real telemetry is committed."""

import json
from zipfile import ZipFile

import pytest

from scripts.inspect_data import discover_files, inspect_file, inspect_rows, main, parse_time


def test_csv_missing_negative_duplicate_and_unnamed(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_text("time,power,Unnamed: 2\n2026-09-18T09:00:00+08:00,-3,\n2026-09-18T09:00:00+08:00,-3,\n2026-09-18T09:10:00+08:00,,\n", encoding="utf-8-sig")
    before = path.read_bytes()
    result = inspect_file(path, {"time_column": "time", "fields": {"p": {"column": "power", "role": "power", "unit": "kW"}}})
    table = result["tables"][0]
    assert table["data_rows"] == 3
    assert table["duplicate_rows"] == 1
    assert table["columns"][1]["negative"] == 2
    assert table["columns"][1]["missing"] == 1
    assert table["unnamed_columns"] == [2]
    assert table["time"]["counts"]["duplicate"] == 1
    assert table["time"]["counts"]["large_gap"] == 1
    assert path.read_bytes() == before


@pytest.mark.parametrize("encoding", ["gb18030", "utf-16", "utf-8"])
def test_chinese_headers_decoded_without_replacement(tmp_path, encoding):
    path = tmp_path / "sample.csv"
    path.write_text("功率,电流\n-1,2\n", encoding=encoding)
    assert inspect_file(path)["tables"][0]["columns"][0]["name"] == "功率"


def test_duplicate_header_mapping_rejected_but_indices_work():
    rows = [["soc", "soc"], ["25", "110"]]
    with pytest.raises(ValueError):
        inspect_rows(rows, {"fields": {"soc": {"column": "soc", "role": "soc", "unit": "percent"}}})
    result = inspect_rows(rows, {"fields": {"battery2": {"column": 1, "role": "soc", "unit": "percent"}}})
    assert result["mapped_fields"]["battery2"]["counts"]["out_of_range"] == 1


def test_ragged_and_blank_rows_preserve_missing_accounting():
    result = inspect_rows([["a", "b"], ["1"], ["2", "3", "4"], [], ["5", "6"]])
    assert result["data_rows"] == 3
    assert result["blank_rows"] == 1
    assert result["ragged_rows"] == 2
    assert result["observed_columns"] == 3
    assert result["columns"][2]["missing"] == 2


def test_time_invalid_and_missing_break_intervals():
    result = inspect_rows([["t", "value"], ["2026-09-18T10:00:00", 1], ["bad", 1], ["2026-09-18T11:00:00", 1], [None, 1], ["2026-09-18T09:00:00", 1]], {"time_column": "t"})
    assert result["time"]["counts"]["invalid"] == 1
    assert result["time"]["counts"]["missing"] == 1
    assert result["time"]["max_interval_seconds"] is None
    assert result["time"]["timezone_known"] is False


def test_nonadjacent_timestamp_duplicates_and_order():
    result = inspect_rows([["t"], ["2026-09-18T09:00:00"], ["2026-09-18T09:00:01"], ["2026-09-18T09:00:00"]], {"time_column": "t"})
    assert result["time"]["counts"]["duplicate"] == 1
    assert result["time"]["counts"]["out_of_order"] == 1


def test_mixed_timezone_does_not_crash():
    result = inspect_rows([["t"], ["2026-09-18T09:00:00"], ["2026-09-18T09:00:00+08:00"]], {"time_column": "t"})
    assert result["time"]["counts"]["mixed_timezone"] == 1


@pytest.mark.parametrize("role,unit", [("soc", "unknown"), ("speed", None), ("power", "kWh")])
def test_mapping_requires_real_units(role, unit):
    with pytest.raises(ValueError):
        inspect_rows([["v"], ["10"]], {"fields": {"value": {"column": "v", "role": role, "unit": unit}}})


def test_empty_array_is_not_a_mapping():
    with pytest.raises(ValueError):
        inspect_rows([["v"], ["10"]], [])


def test_unit_conversion_overflow_is_not_a_valid_number():
    result = inspect_rows([["v"], ["1e308"]], {"fields": {"speed": {"column": "v", "role": "speed", "unit": "knots"}}})
    assert result["mapped_fields"]["speed"]["counts"] == {"invalid_numeric": 1}


def test_coordinates_and_text_values_are_not_exported():
    report = inspect_rows([["GPS", "private"], ["116.123456", "secret-test-string"]])
    text = json.dumps(report)
    assert "116.123456" not in text and "secret-test-string" not in text


def test_excel_serial_epoch_and_offset():
    mapping = {"time_format": "excel_serial", "timezone_offset": "+08:00"}
    assert parse_time("61", mapping, False).date().isoformat() == "1900-03-01"
    assert parse_time("1", mapping, False).date().isoformat() == "1900-01-01"
    assert parse_time("0", mapping, True).date().isoformat() == "1904-01-01"
    with pytest.raises(ValueError):
        parse_time("60", mapping, False)


def test_xlsx_shared_and_inline_strings_sparse_cells(tmp_path):
    path = tmp_path / "sample.xlsx"
    ns = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"
    with ZipFile(path, "w") as archive:
        archive.writestr("xl/workbook.xml", f'<workbook xmlns="{ns}" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="telemetry" sheetId="1" r:id="rId1"/></sheets></workbook>')
        archive.writestr("xl/_rels/workbook.xml.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Target="worksheets/sheet1.xml"/></Relationships>')
        archive.writestr("xl/sharedStrings.xml", f'<sst xmlns="{ns}"><si><t>time</t></si><si><t>power</t></si></sst>')
        archive.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{ns}"><sheetData><row r="1"><c r="A1" t="s"><v>0</v></c><c r="B1" t="s"><v>1</v></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>2026-09-18T09:00:00</t></is></c><c r="B2"><v>-5</v></c></row><row r="3"><c r="A3" t="inlineStr"><is><t>2026-09-18T09:01:00</t></is></c><c r="B3"><f>1+2</f></c></row></sheetData></worksheet>')
    result = inspect_file(path, {"time_column": "time"})
    assert result["status"] == "inspected"
    table = result["tables"][0]
    assert table["data_rows"] == 2
    assert table["columns"][1]["negative"] == 1
    assert table["columns"][1]["missing"] == 1
    assert table["time"]["median_interval_seconds"] == 60


def test_recursive_discovery_and_missing_input(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    path = nested / "data.xlsx"
    path.touch()
    assert discover_files(tmp_path) == [path]
    assert inspect_file(tmp_path / "absent.csv")["status"] == "missing"


def test_cli_reports_missing_real_data(tmp_path):
    output = tmp_path / "artifacts" / "report.json"
    assert main(["--search-root", str(tmp_path), "--output", str(output)]) == 2
    assert json.loads(output.read_text(encoding="utf-8"))["status"] == "missing_data"


def test_cli_refuses_to_overwrite_source(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    before = path.read_bytes()
    with pytest.raises(SystemExit):
        main([str(path), "--output", str(path)])
    assert path.read_bytes() == before


def test_broken_workbook_reports_failure(tmp_path):
    path = tmp_path / "data.xlsx"
    path.write_bytes(b"not a workbook")
    assert inspect_file(path)["status"] == "failed"


def test_elapsed_seconds_preserve_zero_and_never_invent_calendar_time():
    report = inspect_rows([["seconds"], ["0"], ["5"], ["5"], ["2"], ["400"]], {"time_column": "seconds", "time_format": "elapsed_seconds"})
    time = report["time"]
    assert time["min_seconds"] == 0 and time["max_seconds"] == 400
    assert time["min"] is None and time["max"] is None
    assert time["timezone_known"] is None
    assert time["counts"]["duplicate"] == 1
    assert time["counts"]["zero_interval"] == 1
    assert time["unique_valid_timestamps"] == 4
    assert time["counts"]["valid"] == 5
    assert time["counts"]["out_of_order"] == 1
    assert time["counts"]["large_gap"] == 1


@pytest.mark.parametrize("value", ["inf", "-1", "bad"])
def test_bad_elapsed_time_is_flagged(value):
    report = inspect_rows([["seconds"], [value]], {"time_column": "seconds", "time_format": "elapsed_seconds"})
    assert report["time"]["counts"]["invalid"] == 1


def test_elapsed_time_cannot_have_invented_timezone():
    with pytest.raises(ValueError):
        inspect_rows([["seconds"], ["0"]], {"time_column": "seconds", "time_format": "elapsed_seconds", "timezone_offset": "+08:00"})


def test_blank_record_breaks_adjacent_time_intervals():
    report = inspect_rows([["t"], ["0"], [], ["1000"]], {"time_column": "t", "time_format": "elapsed_seconds"})
    assert report["records_after_header"] == 3
    assert report["data_rows"] == 2 and report["blank_rows"] == 1
    assert report["time"]["max_interval_seconds"] is None


def test_cli_refuses_to_overwrite_mapping_file(tmp_path):
    path = tmp_path / "sample.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    mapping = tmp_path / "mapping.json"
    mapping.write_text("{}", encoding="utf-8")
    with pytest.raises(SystemExit):
        main([str(path), "--mapping", str(mapping), "--output", str(mapping)])
    assert mapping.read_text(encoding="utf-8") == "{}"
