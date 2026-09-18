"""Static route evidence checks; not Tdata/Tseg or real-vessel acceptance."""

import hashlib
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from zipfile import ZipFile

import pytest

from schemas.types import SourceRef
from schemas.validate import require_valid

ROOT = Path(__file__).resolve().parents[1]
CONFIG = json.loads((ROOT / "configs/route_facts.yaml").read_text(encoding="utf-8"))
SEGMENTS = {item["id"]: item for item in CONFIG["segments"]}
FORWARD = CONFIG["routes"]["forward"]["segment_ids"]
SOURCE = CONFIG["sources"]["distance_km"]["source_id"]
DISTANCES = [25.5, 25, 44.1, 0.689, 22.6, 24.9, 27.4, 19.6]
RAW = ["25.5km", "25km", "44.1km", "689m", "22.6km", "24.9km", "27.4km", "19.6km"]


@pytest.fixture(scope="module")
def slide_texts():
    with ZipFile(ROOT / SOURCE) as archive:
        texts = []
        for index in range(1, 10):
            xml = ET.fromstring(archive.read(f"ppt/slides/slide{index}.xml"))
            texts.append("".join(node.text or "" for node in xml.iter(
                "{http://schemas.openxmlformats.org/drawingml/2006/main}t")))
        return texts


@pytest.mark.parametrize("index", range(8))
def test_forward_distance_matches_original_ppt(index, slide_texts):
    segment = SEGMENTS[FORWARD[index]]
    assert segment["origin"] in slide_texts[index]
    assert segment["destination"] in slide_texts[index]
    assert RAW[index] in slide_texts[index] and RAW[index] in slide_texts[8]
    assert segment["distance_km"] == DISTANCES[index]
    if index == 3:
        assert segment["distance_km"] == 689 / 1000
    source = segment["source"]
    assert source["source_id"] == SOURCE
    assert f"第{index + 1}页" in source["locator"]
    assert source["confirmed"] is True
    require_valid(SourceRef.from_dict(source))


@pytest.mark.parametrize("index", range(8))
def test_reverse_has_exact_id_and_unverified_condition_note(index):
    forward = SEGMENTS[FORWARD[index]]
    reverse = SEGMENTS[FORWARD[index] + "_reverse"]
    assert reverse["origin"] == forward["destination"]
    assert reverse["destination"] == forward["origin"]
    assert reverse["distance_km"] == forward["distance_km"]
    assert reverse["source"] == forward["source"]
    assert reverse["assumptions"] and "未确认" in reverse["assumptions"][0]


@pytest.mark.parametrize("direction", ["forward", "reverse"])
def test_route_continuity_and_total(direction):
    route = CONFIG["routes"][direction]
    segments = [SEGMENTS[key] for key in route["segment_ids"]]
    assert segments[0]["origin"] == route["origin"]
    assert segments[-1]["destination"] == route["destination"]
    assert all(a["destination"] == b["origin"] for a, b in zip(segments, segments[1:]))
    assert sum(item["distance_km"] for item in segments) == pytest.approx(189.789)
    assert len(segments) == len(set(route["segment_ids"])) == 8
    if direction == "reverse":
        assert route["segment_ids"] == [key + "_reverse" for key in reversed(FORWARD)]


@pytest.mark.parametrize("start,end,total", [(0, 4, 95.289), (4, 8, 94.5)])
def test_subroute_totals(start, end, total):
    assert sum(SEGMENTS[key]["distance_km"] for key in FORWARD[start:end]) == pytest.approx(total)


def test_source_fingerprint_and_ref():
    fingerprint = hashlib.sha256((ROOT / SOURCE).read_bytes()).hexdigest()
    assert fingerprint == CONFIG["source_fingerprints"][SOURCE]
    require_valid(SourceRef.from_dict(CONFIG["sources"]["distance_km"]))


def test_unknown_constraints_are_not_filled():
    assert CONFIG["status"] == "distance_source_checked_constraints_pending"
    assert len(SEGMENTS) == len(CONFIG["segments"]) == 16
    for item in SEGMENTS.values():
        for field in ("max_speed_kmh", "min_speed_kmh", "waiting_h"):
            assert item[field] is None
    for field in ("waiting_h", "speed_limits", "charging"):
        assert CONFIG["sources"][field]["confirmed"] is False


def test_manual_uses_full_registered_ids():
    text = (ROOT / "docs/数据/子航线与返程手工核对.md").read_text(encoding="utf-8")
    assert all(key in text for key in SEGMENTS)
    assert not re.search(r"\bs\d{2}_reverse\b", text)
    assert "689m" in text and "0.689" in text


def test_aliases_keep_ports_and_locks_distinct():
    config = json.loads((ROOT / "configs/aliases.yaml").read_text(encoding="utf-8"))
    assert config["aliases"]["漯河港"] == {"canonical": "漯河港", "kind": "port"}
    assert config["aliases"]["漯河船闸"] == {"canonical": "漯河船闸", "kind": "lock"}
    assert config["rules"]["unknown_place"] == "need_clarification"
