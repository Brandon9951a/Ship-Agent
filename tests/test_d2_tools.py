import json
from pathlib import Path

from schemas.types import DataContext, Status, VoyageRequest
from tools.tdata import load_config, tdata
from tools.tseg import segment
from ui.app import run


ROOT = Path(__file__).resolve().parents[1]


def configs():
    return {
        "route_config": load_config(ROOT / "configs/route_facts.yaml"),
        "aliases_config": load_config(ROOT / "configs/aliases.yaml"),
        "vessel_config": load_config(ROOT / "configs/vessel_facts.yaml"),
        "limits_config": load_config(ROOT / "configs/limits.yaml"),
        "demo_policy_config": load_config(ROOT / "configs/demo_policy.yaml"),
    }


def request(origin="平顶山港", destination="军李船闸"):
    return VoyageRequest(origin=origin, destination=destination, soc_initial=.85,
                         max_duration_h=6, load_state="半载")


def test_tdata_applies_explicit_synthetic_demo_constraints_and_sources():
    response = tdata(request(), **configs())
    assert response.status == Status.OK
    assert "route.max_speed_kmh" not in response.missing_fields
    assert "route.waiting_h" not in response.missing_fields
    assert response.payload["route_id"] == "yj001_pingdingshan_zhoukou_forward"
    assert response.payload["vessel"]["capacity_kwh"] == 1411.065
    assert response.payload["parameters"]["nominal_capacity_kwh"]["value"] == 1567.85
    assert response.payload["parameters"]["max_speed_kmh"]["value"] == 11.112
    assert response.payload["vessel"]["soc_alarm"] == .35
    assert response.payload["parameters"]["battery_group_capacity_kwh"]["value"] == 783.925
    assert response.payload["parameters"]["soh_initial"]["value"] == .9
    assert response.payload["parameters"]["default_queue_wait_h"]["value"] == 0


def test_tdata_unknown_place_is_a_question():
    response = tdata(request(origin="未知码头"), **configs())
    assert response.status == Status.NEED_CLARIFICATION
    assert "origin" in response.missing_fields


def test_tseg_selects_contiguous_subroute_and_reverse():
    route = configs()["route_config"]
    aliases = configs()["aliases_config"]
    data = tdata(request(), **configs()).payload
    context = DataContext.from_dict(data)
    sub = segment(request("平顶山港", "漯河港"), context,
                  route_config=route, aliases_config=aliases,
                  demo_policy_config=configs()["demo_policy_config"])
    assert sub.status == Status.OK
    assert all(item["max_speed_kmh"] == 11.112 for item in sub.payload["segments"])
    assert not any(field.endswith(".waiting_h") for field in sub.missing_fields)
    assert all(item["waiting_h"] == 0 for item in sub.payload["segments"])
    reverse = segment(request("周口港", "平顶山港"), context,
                      route_config=route, aliases_config=aliases,
                      demo_policy_config=configs()["demo_policy_config"])
    assert reverse.status == Status.OK
    assert "segments.s08_zhoukou_lock_to_zhoukou_port_reverse.waiting_h" not in reverse.missing_fields


def test_tseg_keeps_waiting_unknown_outside_approved_route_scope():
    cfg = configs()
    context = DataContext.from_dict(tdata(request(), **cfg).payload)
    context = DataContext(
        vessel=context.vessel,
        route_id="outside_henan_demo_route",
        parameters=context.parameters,
    )
    response = segment(
        request(), context,
        route_config=cfg["route_config"],
        aliases_config=cfg["aliases_config"],
        demo_policy_config=cfg["demo_policy_config"],
    )
    assert any(field.endswith(".waiting_h") for field in response.missing_fields)


def test_ui_loads_configs_independently_of_current_directory(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    response = run(request().to_dict())
    assert response["tdata"]["payload"]["vessel"]["soc_alarm"] == .35
    assert response["tseg"]["status"] == Status.OK.value


def test_cli_example_is_valid_json():
    json.loads((ROOT / "configs/examples/voyage_request.json").read_text(encoding="utf-8"))
