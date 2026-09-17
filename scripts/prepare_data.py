"""D2 conservative quality preparation. Sources are read-only; no model is fitted.

Run from the repository root with python -m scripts.prepare_data.
Only explicitly unit-mapped numeric channels are exported, never raw rows/GPS.
"""

import argparse
import csv
import hashlib
import json
import math
import sys
import xml.etree.ElementTree as ET
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from scripts.inspect_data import (
    DEFAULT_FILES, MISSING, ROOT, csv_rows, discover_files, inspect_rows, parse_time,
    select_column, xlsx_rows, xlsx_sheets,
)

PRIORITY = (
    "blank_record", "ragged_record", "missing_time", "invalid_time",
    "mixed_timezone", "out_of_order", "duplicate_timestamp",
    "duplicate_record", "missing_numeric", "invalid_numeric", "out_of_range",
    "unconfirmed_units", "negative_power", "stopped", "candidate",
)
UNITS = {"speed": "km/h", "power": "kW", "current": "A",
         "voltage": "V", "soc": "fraction"}


def prepare_rows(rows, mapping, *, epoch_1904=False):
    """Return one audit record per source logical record, with exhaustive counts.

    Repeated timestamps quarantine *all* members, not just subsequent records.
    Candidate means structurally usable, NOT stable cruising or approved training.
    """
    iterator = iter(rows)
    header_row = mapping.get("header_row", 1)
    if type(header_row) is not int or header_row < 1:
        raise ValueError("header_row must be a positive integer")
    headers = []
    for _ in range(header_row):
        headers = [str(v).strip() if v is not None else "" for v in next(iterator, [])]
    # Reuse D1 role/unit/selector validation before processing observations.
    inspect_rows([headers], dict(mapping, header_row=1), epoch_1904=epoch_1904)
    if mapping.get("time_column") is None:
        raise ValueError("Explicit time_column required")
    time_index = select_column(mapping["time_column"], headers)
    specs = [(name, spec, select_column(spec["column"], headers))
             for name, spec in mapping.get("fields", {}).items()
             if spec["role"] != "position"]
    if not specs:
        raise ValueError("At least one non-position channel required")
    # Unknown auxiliary/SOC candidates do not erase known speed/power evidence.
    # A speed-power pair is still required for structural sample candidacy.
    unresolved = not {"speed", "power"}.issubset({spec["role"] for _, spec, _ in specs})
    records, stamps, fingerprints = [], Counter(), Counter()
    previous = awareness = None
    for number, row in enumerate(iterator, header_row + 1):
        flags, values = set(), {}
        text = [str(v).strip() if v is not None else "" for v in row]
        if not any(text):
            flags.add("blank_record")
        if len(row) != len(headers):
            flags.add("ragged_record")
        fingerprint = hashlib.sha256(json.dumps(text, ensure_ascii=False).encode()).hexdigest()
        fingerprints[fingerprint] += 1
        raw_time = text[time_index] if time_index < len(text) else ""
        stamp = None
        if raw_time.lower() in MISSING:
            flags.add("missing_time")
        else:
            try:
                stamp = parse_time(raw_time, mapping, epoch_1904)
                aware = isinstance(stamp, datetime) and stamp.tzinfo is not None
                if awareness is None:
                    awareness = aware
                if aware != awareness:
                    flags.add("mixed_timezone")
                    stamp = None
                else:
                    if previous is not None and stamp < previous:
                        flags.add("out_of_order")
                    stamps[stamp] += 1
            except (ValueError, OverflowError):
                flags.add("invalid_time")
        previous = None if "blank_record" in flags else stamp
        if unresolved:
            flags.add("unconfirmed_units")
        for name, spec, index in specs:
            role = spec["role"]
            if role == "other":
                continue  # Unknown meaning: do not export numbers as engineering inputs.
            raw = text[index] if index < len(text) else ""
            values[name] = None
            if raw.lower() in MISSING:
                flags.add("missing_numeric")
                continue
            try:
                value = float(raw) * {"knots": 1.852, "W": .001, "percent": .01}.get(spec.get("unit"), 1)
                if not math.isfinite(value):
                    raise ValueError("Nonfinite")
            except ValueError:
                flags.add("invalid_numeric")
                continue
            values[name] = value
            if (role in ("speed", "voltage") and value < 0) or (role == "soc" and not 0 <= value <= 1):
                flags.add("out_of_range")
            if role == "power" and value < 0:
                flags.add("negative_power")
            if role == "speed" and value == 0:
                flags.add("stopped")
        records.append({"source_logical_record": number, "_stamp": stamp,
                        "_fingerprint": fingerprint, "_flags": flags, "values": values})
    primary, flag_counts = Counter({key: 0 for key in PRIORITY}), Counter()
    for record in records:
        stamp = record.pop("_stamp")
        fingerprint = record.pop("_fingerprint")
        flags = record.pop("_flags")
        if stamp is not None and stamps[stamp] > 1:
            flags.add("duplicate_timestamp")
        if fingerprints[fingerprint] > 1 and "blank_record" not in flags:
            flags.add("duplicate_record")
        record["time"] = (stamp.astimezone(timezone.utc).isoformat()
                          if isinstance(stamp, datetime) and stamp.tzinfo else
                          stamp.isoformat() if isinstance(stamp, datetime) else stamp)
        record["flags"] = sorted(flags)
        record["disposition"] = next((key for key in PRIORITY if key in flags), "candidate")
        primary[record["disposition"]] += 1
        flag_counts.update(flags)
    summary = {"records_after_header": len(records), "primary_counts": dict(primary),
               "flag_counts_overlapping": dict(sorted(flag_counts.items())),
               "counts_reconciled": sum(primary.values()) == len(records),
               "duplicate_policy": "quarantine_all_members; no first/last/mean selection",
               "time_kind": mapping.get("time_format", "iso"),
               "numeric_channels": {name: {"role": spec["role"], "unit": UNITS[spec["role"]]}
                                    for name, spec, _ in specs if spec["role"] in UNITS},
               "unresolved_channels": [name for name, spec, _ in specs if spec["role"] == "other"],
               "training_ready": False,
               "notes": ["No fill, interpolation, sorting, row deletion or power-channel summation.",
                         "Source record numbers refer to logical reader records, not Excel row addresses.",
                         "Candidate is not proof of stable cruising, sensor boundary or independent trip."]}
    return records, summary


def prepare_file(path, mapping):
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    tables = []
    if path.suffix.lower() in (".csv", ".tsv"):
        encoding, rows, stream = csv_rows(path)
        try:
            records, summary = prepare_rows(rows, mapping)
        finally:
            stream.close()
        tables.append(("csv", records, summary))
    elif path.suffix.lower() == ".xlsx":
        with ZipFile(path) as archive:
            sheets, strings, epoch = xlsx_sheets(archive)
            for name, target in sheets:
                selected = mapping.get("sheets", {}).get(name, mapping)
                records, summary = prepare_rows(xlsx_rows(archive, target, strings), selected, epoch_1904=epoch)
                tables.append((name, records, summary))
    else:
        raise ValueError("Supported input formats: CSV, TSV, XLSX")
    after = hashlib.sha256(path.read_bytes()).hexdigest()
    if before != after:
        raise ValueError("Source changed during preparation; output refused")
    return {"file": path.name, "sha256": before, "source_unchanged": True}, tables


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="*", type=Path)
    parser.add_argument("--search-root", type=Path, default=ROOT)
    parser.add_argument("--mapping", type=Path, default=ROOT / "configs/data_mapping.d1.json")
    parser.add_argument("--output-dir", type=Path, default=ROOT / "artifacts/d2-prepared")
    args = parser.parse_args(argv)
    try:
        # Limit automatic output to ignored local artifacts, protecting configs/source trees.
        args.output_dir.resolve().relative_to((ROOT / "artifacts").resolve())
        if args.output_dir.exists():
            raise ValueError("Output directory already exists; use a fresh artifacts subdirectory")
        mapping_bytes = args.mapping.read_bytes()
        mapping = json.loads(mapping_bytes.decode("utf-8-sig"))
        if not isinstance(mapping, dict) or not isinstance(mapping.get("files", {}), dict):
            raise ValueError("Mapping and files must be objects")
        paths = args.paths or discover_files(args.search_root)
        if not paths:
            raise ValueError("No input files found")
        prepared = [prepare_file(path, mapping.get("files", {}).get(path.name, mapping)) for path in paths]
        missing = [name for name in DEFAULT_FILES if name not in {path.name for path in paths}] if not args.paths else []
        report = {"status": "prepared_partial" if missing else "prepared_for_review", "training_ready": False,
                  "missing_expected_files": missing,
                  "scope": "D2 quality framework, not model fitting or vessel acceptance",
                  "mapping_sha256": hashlib.sha256(mapping_bytes).hexdigest(), "files": []}
        args.output_dir.mkdir(parents=True)
        for file_number, (meta, tables) in enumerate(prepared, 1):
            meta["tables"] = []
            for table_number, (name, records, summary) in enumerate(tables, 1):
                output_name = f"quality-{file_number}-{table_number}.jsonl"
                with (args.output_dir / output_name).open("x", encoding="utf-8") as stream:
                    for record in records:
                        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
                meta["tables"].append(dict(summary, name=name, audit_file=output_name))
            report["files"].append(meta)
        (args.output_dir / "summary.json").write_text(json.dumps(report, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")
        print(json.dumps({"status": report["status"], "files": len(prepared), "training_ready": False}))
        return 2 if missing else 0
    except (OSError, ValueError, TypeError, KeyError, AttributeError, IndexError,
            csv.Error, BadZipFile, ET.ParseError) as exc:
        print(f"Preparation failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
