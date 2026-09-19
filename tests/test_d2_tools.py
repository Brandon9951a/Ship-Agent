import json
from pathlib import Path

from schemas.types import Status, VoyageRequest
from tools.tdata import load_config, tdata
from tools.tseg import segment


ROOT = Path(__file__).resolve().parents[1]


def configs():
    return {
        "route_config": load_config(ROOT / "configs/route_facts.yaml"),
        "aliases_config": load_config(ROOT / "configs/aliases.yaml"),
        "vessel_config": load_config(ROOT / "configs/vessel_facts.yaml"),
        "limits_config": load_config(ROOT / "configs/limits.yaml"),
    }


def request(origin="平顶山港", destination="军李船闸"):
    return VoyageRequest(origin=origin, destination=destination, soc_initial=.85,
                         max_duration_h=6, load_state="半载")


def test_tdata_preserves_unknown_constraints_and_sources():
    response = tdata(request(), **configs())
    assert response.status == Status.NEED_CLARIFICATION
    assert "route.max_speed_kmh" in response.missing_fields
    assert "route.waiting_h" in response.missing_fields
    assert response.payload["route_id"] == "yj001_pingdingshan_zhoukou_forward"


def test_tdata_unknown_place_is_a_question():
    response = tdata(request(origin="未知码头"), **configs())
    assert response.status == Status.NEED_CLARIFICATION
    assert "origin" in response.missing_fields


def test_tseg_selects_contiguous_subroute_and_reverse():
    route = configs()["route_config"]
    aliases = configs()["aliases_config"]
    data = tdata(request(), **configs()).payload
    from schemas.types import DataContext
    context = DataContext.from_dict(data)
    sub = segment(request("平顶山港", "漯河港"), context,
                  route_config=route, aliases_config=aliases)
    assert sub.status == Status.NEED_CLARIFICATION
    assert "segments.s01_pingdingshan_gang_to_junli.max_speed_kmh" in sub.missing_fields
    reverse = segment(request("周口港", "平顶山港"), context,
                      route_config=route, aliases_config=aliases)
    assert reverse.status == Status.NEED_CLARIFICATION
    assert "segments.s08_zhoukou_lock_to_zhoukou_port_reverse.waiting_h" in reverse.missing_fields


def test_cli_example_is_valid_json():
    json.loads((ROOT / "configs/examples/voyage_request.json").read_text(encoding="utf-8"))

