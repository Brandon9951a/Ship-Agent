"""Read-only, standard-library D1 CSV/XLSX inspection; reports stay in artifacts/.

No unit, timezone, sensor identity or battery topology is inferred from values.
XLSX uses cached formula values; this reader never recalculates formulas.
"""

import argparse
import codecs
import csv
import hashlib
import json
import math
import posixpath
import re
import statistics
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator
from zipfile import BadZipFile, ZipFile

ROOT = Path(__file__).resolve().parents[1]
NS = {"m": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
      "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships"}
DEFAULT_FILES = ("data.xlsx", "log4p-10-29(1).csv", "log4p-10-31(1).csv")
MISSING = {"", "na", "n/a", "null", "none", "nan"}


def detect_encoding(path: Path) -> str:
    with path.open("rb") as stream:
        prefix = stream.read(4)
    if prefix.startswith(codecs.BOM_UTF16_LE) or prefix.startswith(codecs.BOM_UTF16_BE):
        candidates = ["utf-16"]
    else:
        candidates = ["utf-8-sig", "gb18030"]
    for encoding in candidates:
        try:
            with path.open(encoding=encoding, errors="strict") as stream:
                while stream.read(65536):
                    pass
            return encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("Unsupported text encoding; decoding failed without replacing characters")


def csv_rows(path: Path) -> tuple[str, Iterator[list[Any]], Any]:
    encoding = detect_encoding(path)
    stream = path.open(encoding=encoding, newline="")
    sample = stream.read(8192)
    stream.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel_tab if path.suffix.lower() == ".tsv" else csv.excel
    return encoding, csv.reader(stream, dialect, strict=True), stream


def column_index(reference: str) -> int:
    index = 0
    for letter in re.match(r"[A-Za-z]+", reference).group(0).upper():
        index = index * 26 + ord(letter) - ord("A") + 1
    return index - 1


def xlsx_rows(archive: ZipFile, target: str, strings: list[str]) -> Iterator[list[Any]]:
    with archive.open(target) as stream:
        for event, node in ET.iterparse(stream, events=("end",)):
            if node.tag != f"{{{NS['m']}}}row":
                continue
            cells: dict[int, Any] = {}
            for fallback, cell in enumerate(node.findall("m:c", NS)):
                index = column_index(cell.attrib["r"]) if "r" in cell.attrib else fallback
                value = cell.findtext("m:v", default="", namespaces=NS)
                kind = cell.attrib.get("t", "n")
                if kind == "s":
                    value = strings[int(value)] if value else ""
                elif kind == "inlineStr":
                    value = "".join(t.text or "" for t in cell.findall("m:is//m:t", NS))
                elif kind == "b":
                    value = "true" if value == "1" else "false"
                # Error cells remain nonnumeric strings; formulas without cache stay missing.
                cells[index] = value
            row = [cells.get(i, "") for i in range(max(cells, default=-1) + 1)]
            node.clear()
            yield row


def xlsx_sheets(archive: ZipFile) -> tuple[list[tuple[str, str]], list[str], bool]:
    workbook = ET.fromstring(archive.read("xl/workbook.xml"))
    properties = workbook.find("m:workbookPr", NS)
    epoch_1904 = properties is not None and properties.attrib.get("date1904") in ("1", "true")
    relationships = ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))
    targets = {r.attrib["Id"]: r.attrib["Target"] for r in relationships
               if r.attrib.get("TargetMode") != "External"}
    strings: list[str] = []
    if "xl/sharedStrings.xml" in archive.namelist():
        with archive.open("xl/sharedStrings.xml") as stream:
            for event, node in ET.iterparse(stream, events=("end",)):
                if node.tag == f"{{{NS['m']}}}si":
                    strings.append("".join(t.text or "" for t in node.findall(".//m:t", NS)))
                    node.clear()
    sheets = []
    for sheet in workbook.findall("m:sheets/m:sheet", NS):
        target = targets[sheet.attrib[f"{{{NS['r']}}}id"]]
        path = target.lstrip("/") if target.startswith("/") else posixpath.normpath("xl/" + target)
        sheets.append((sheet.attrib["name"], path))
    return sheets, strings, epoch_1904


@dataclass
class ColumnStats:
    index: int
    name: str
    missing: int = 0
    numeric: int = 0
    text: int = 0
    non_finite: int = 0
    negative: int = 0

    def add(self, value: Any) -> None:
        text = str(value).strip() if value is not None else ""
        if text.lower() in MISSING:
            self.missing += 1
            return
        try:
            number = float(text)
        except ValueError:
            self.text += 1
            return
        if not math.isfinite(number):
            self.non_finite += 1
            return
        self.numeric += 1
        self.negative += int(number < 0)

    def report(self, count: int) -> dict[str, Any]:
        return {"index": self.index, "name": self.name,
                "type": "mixed" if self.numeric and self.text else ("numeric" if self.numeric else ("text" if self.text else "unknown")),
                "missing": self.missing, "missing_rate": self.missing / count if count else None,
                "numeric": self.numeric, "text": self.text, "non_finite": self.non_finite,
                "negative": self.negative, "unit": "unconfirmed"}


def select_column(selector: Any, headers: list[str]) -> int:
    if type(selector) is int and 0 <= selector < len(headers):
        return selector
    if isinstance(selector, str):
        matches = [i for i, value in enumerate(headers) if value == selector]
        if len(matches) == 1:
            return matches[0]
    raise ValueError(f"Unknown or ambiguous column selector: {selector!r}; use a zero-based index")


def parse_time(value: Any, mapping: dict[str, Any], epoch_1904: bool) -> datetime:
    text = str(value).strip()
    fmt = mapping.get("time_format", "iso")
    if fmt == "excel_serial":
        serial = float(text)
        if not math.isfinite(serial) or serial < 0:
            raise ValueError("Invalid Excel date serial")
        # Excel's fictitious 1900-02-29 cannot be represented as a real date.
        if not epoch_1904 and 60 <= serial < 61:
            raise ValueError("Fictitious Excel leap day")
        base = datetime(1904, 1, 1) if epoch_1904 else datetime(1899, 12, 30)
        if not epoch_1904 and serial < 60:
            base = datetime(1899, 12, 31)
        result = base + timedelta(days=serial)
    elif fmt == "iso":
        result = datetime.fromisoformat(text.replace("Z", "+00:00"))
    else:
        result = datetime.strptime(text, fmt)
    offset = mapping.get("timezone_offset")
    if result.tzinfo is None and offset:
        match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", offset)
        if not match or int(match[2]) > 23 or int(match[3]) > 59:
            raise ValueError("timezone_offset must be +/-HH:MM")
        minutes = (int(match[2]) * 60 + int(match[3])) * (1 if match[1] == "+" else -1)
        result = result.replace(tzinfo=timezone(timedelta(minutes=minutes)))
    return result


def inspect_rows(rows: Iterable[list[Any]], mapping: dict[str, Any] | None = None,
                 *, epoch_1904: bool = False, gap_seconds: float = 300) -> dict[str, Any]:
    mapping = {} if mapping is None else mapping
    if not isinstance(mapping, dict) or not isinstance(mapping.get("fields", {}), dict):
        raise ValueError("mapping and fields must be objects")
    if not isinstance(mapping.get("time_format", "iso"), str):
        raise ValueError("time_format must be a string")
    if mapping.get("timezone_offset") is not None and not isinstance(mapping["timezone_offset"], str):
        raise ValueError("timezone_offset must be a string")
    iterator = iter(rows)
    header_row = mapping.get("header_row", 1)
    if type(header_row) is not int or header_row < 1:
        raise ValueError("header_row must be a positive logical record index")
    headers: list[str] = []
    for _ in range(header_row):
        headers = [str(v).strip() if v is not None else "" for v in next(iterator, [])]
    if not headers:
        raise ValueError("Header record is absent or empty")
    columns = [ColumnStats(i, name) for i, name in enumerate(headers)]
    time_index = select_column(mapping["time_column"], headers) if mapping.get("time_column") is not None else None
    mapped = []
    for name, spec in mapping.get("fields", {}).items():
        if not isinstance(spec, dict) or spec.get("role") not in ("speed", "soc", "power", "current", "voltage", "position", "other"):
            raise ValueError(f"{name}: explicit supported role required")
        index = select_column(spec.get("column"), headers)
        mapped.append((name, spec, index, Counter()))
        role, unit = spec["role"], spec.get("unit")
        allowed = {"speed": {"km/h", "knots"}, "soc": {"fraction", "percent"},
                   "power": {"kW", "W"}, "current": {"A"}, "voltage": {"V"}}
        if role in allowed and unit not in allowed[role]:
            raise ValueError(f"{name}: unit must be confirmed explicitly")
    count = blank_rows = duplicate_rows = ragged_rows = 0
    row_hashes: set[bytes] = set()
    times: set[datetime] = set()
    intervals: list[float] = []
    time_errors = Counter()
    first = last = previous = None
    timezone_known = None
    for row in iterator:
        if not any(str(v).strip() for v in row if v is not None):
            blank_rows += 1
            continue
        if len(row) != len(headers):
            ragged_rows += 1
        while len(columns) < len(row):
            columns.append(ColumnStats(len(columns), "", missing=count))
        normalized = [str(v).strip() if v is not None else "" for v in row]
        fingerprint = hashlib.sha256(json.dumps(normalized, ensure_ascii=False).encode("utf-8")).digest()
        duplicate_rows += int(fingerprint in row_hashes)
        row_hashes.add(fingerprint)
        count += 1
        for column in columns:
            column.add(row[column.index] if column.index < len(row) else None)
        for name, spec, index, stats in mapped:
            text = normalized[index] if index < len(normalized) else ""
            if text.lower() in MISSING:
                stats["missing"] += 1
                continue
            if spec["role"] in ("position", "other"):
                stats["present"] += 1
                continue
            try:
                value = float(text)
                if not math.isfinite(value):
                    raise ValueError("Nonfinite")
            except ValueError:
                stats["invalid_numeric"] += 1
                continue
            factor = {"knots": 1.852, "percent": 0.01, "W": 0.001}.get(spec.get("unit"), 1)
            value *= factor
            if not math.isfinite(value):
                stats["invalid_numeric"] += 1
                continue
            stats["valid_numeric"] += 1
            stats["negative"] += int(value < 0)
            if spec["role"] == "soc" and not 0 <= value <= 1:
                stats["out_of_range"] += 1
            if spec["role"] in ("speed", "voltage") and value < 0:
                stats["out_of_range"] += 1
        if time_index is None:
            continue
        value = row[time_index] if time_index < len(row) else None
        if value is None or str(value).strip().lower() in MISSING:
            time_errors["missing"] += 1
            previous = None
            continue
        try:
            stamp = parse_time(value, mapping, epoch_1904)
        except (ValueError, OverflowError):
            time_errors["invalid"] += 1
            previous = None
            continue
        aware = stamp.tzinfo is not None
        if timezone_known is None:
            timezone_known = aware
        if aware != timezone_known:
            time_errors["mixed_timezone"] += 1
            previous = None
            continue
        time_errors["duplicate"] += int(stamp in times)
        times.add(stamp)
        first = min(first, stamp) if first else stamp
        last = max(last, stamp) if last else stamp
        if previous is not None:
            dt = (stamp - previous).total_seconds()
            if dt < 0:
                time_errors["out_of_order"] += 1
            elif dt > 0:
                intervals.append(dt)
                time_errors["large_gap"] += int(dt > gap_seconds)
        previous = stamp
    counts = Counter(headers)
    return {"data_rows": count, "blank_rows": blank_rows, "header_columns": len(headers),
            "observed_columns": len(columns), "ragged_rows": ragged_rows,
            "duplicate_rows": duplicate_rows,
            "unnamed_columns": [c.index for c in columns if not c.name or c.name.lower().startswith("unnamed")],
            "duplicate_headers": [h for h, n in counts.items() if n > 1],
            "columns": [c.report(count) for c in columns],
            "time": {"status": "inspected" if time_index is not None else "unmapped",
                     "min": first.isoformat() if first else None, "max": last.isoformat() if last else None,
                     "timezone_known": timezone_known,
                     "counts": dict(time_errors), "gap_threshold_seconds": gap_seconds,
                     "median_interval_seconds": statistics.median(intervals) if intervals else None,
                     "max_interval_seconds": max(intervals) if intervals else None},
            "mapped_fields": {name: {"role": spec["role"], "input_unit": spec.get("unit"),
                                    "counts": dict(stats)} for name, spec, index, stats in mapped},
            "notes": ["All records preserved; negative power/current counted, not deleted.",
                      "No raw rows, text samples or coordinate values exported.",
                      "Units and sensor channels require evidence; unmapped columns remain unconfirmed."]}


def inspect_file(path: Path, mapping: dict[str, Any] | None = None,
                 gap_seconds: float = 300) -> dict[str, Any]:
    if not path.is_file():
        return {"file": path.name, "status": "missing", "reason": "Input file does not exist"}
    # Hash file contents for local reproducibility; never modify the source.
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(65536), b""):
            digest.update(block)
    result: dict[str, Any] = {"file": path.name, "bytes": path.stat().st_size, "sha256": digest.hexdigest()}
    try:
        if path.suffix.lower() in (".csv", ".tsv"):
            encoding, rows, stream = csv_rows(path)
            try:
                table = inspect_rows(rows, mapping, gap_seconds=gap_seconds)
            finally:
                stream.close()
            result.update(encoding=encoding, tables=[dict(name="csv", **table)])
        elif path.suffix.lower() == ".xlsx":
            with ZipFile(path) as archive:
                sheets, strings, epoch_1904 = xlsx_sheets(archive)
                tables = []
                for name, target in sheets:
                    sheet_mapping = (mapping or {}).get("sheets", {}).get(name, mapping)
                    tables.append(dict(name=name, **inspect_rows(xlsx_rows(archive, target, strings), sheet_mapping,
                                                                epoch_1904=epoch_1904, gap_seconds=gap_seconds)))
            result.update(encoding="OOXML", excel_epoch="1904" if epoch_1904 else "1900", tables=tables,
                          formula_policy="cached_values_only; missing cache is missing data")
        else:
            raise ValueError("Supported formats: .csv, .tsv, .xlsx")
        result["status"] = "inspected"
    except (OSError, ValueError, csv.Error, KeyError, ET.ParseError, BadZipFile, TypeError, AttributeError, IndexError) as exc:
        result.update(status="failed", reason=str(exc))
    return result


def discover_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*") if p.is_file() and p.name in DEFAULT_FILES
                  and not any(part in (".git", ".venv", "venv") for part in p.relative_to(root).parts))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path, help="Explicit local source files, read-only")
    parser.add_argument("--search-root", type=Path, default=ROOT, help="Recursive search if no paths are supplied")
    parser.add_argument("--mapping", type=Path, help="Explicit column/role/unit/time-format JSON mapping")
    parser.add_argument("--output", type=Path, default=ROOT / "artifacts/d1-inspection.json")
    parser.add_argument("--gap-seconds", type=float, default=300)
    args = parser.parse_args(argv)
    if not math.isfinite(args.gap_seconds) or args.gap_seconds <= 0:
        parser.error("--gap-seconds must be a finite positive number")
    if not args.search_root.is_dir():
        parser.error("--search-root must be a directory")
    try:
        mapping = json.loads(args.mapping.read_text(encoding="utf-8-sig")) if args.mapping else {}
        if not isinstance(mapping, dict):
            raise ValueError("mapping must be an object")
        paths = args.paths or discover_files(args.search_root)
        if args.output.resolve() in {p.resolve() for p in paths}:
            parser.error("Output must not overwrite an input file")
        files = [inspect_file(p, mapping.get("files", {}).get(p.name, mapping), args.gap_seconds) for p in paths]
        present = {p.name for p in paths}
        missing = [name for name in DEFAULT_FILES if name not in present] if not args.paths else []
        status = "failed" if any(f["status"] == "failed" for f in files) else ("missing_data" if missing or any(f["status"] == "missing" for f in files) else "inspected")
        report = {"status": status, "files": files, "missing_expected_files": missing,
                  "generated_at": datetime.now(timezone.utc).isoformat(),
                  "scope": "D1 data inventory; not model accuracy or ship validation"}
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({"status": status, "files_inspected": len(files), "missing_expected_files": missing}, ensure_ascii=False))
        return 1 if status == "failed" else (2 if status == "missing_data" else 0)
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        print(f"Inspection failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
