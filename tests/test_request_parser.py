import pytest

from core.request_parser import parse_voyage_request
from schemas.types import Status


def test_complete_duration_request_is_ready():
    result = parse_voyage_request("从平顶山港到周口港，初始SOC 80%，半载，20小时内到达")
    assert result.status == Status.OK
    assert result.request.origin == "平顶山港"
    assert result.request.destination == "周口港"
    assert result.request.soc_initial == .8
    assert result.request.load_state == "半载"
    assert result.request.max_duration_h == 20


@pytest.mark.parametrize("connector", ["到", "至", "前往"])
def test_route_connectors(connector):
    result = parse_voyage_request(f"平顶山港{connector}漯河港，SOC90%，空载，最多8小时")
    assert result.status == Status.OK
    assert (result.request.origin, result.request.destination) == ("平顶山港", "漯河港")


@pytest.mark.parametrize("soc_text,expected", [
    ("SOC 80%", .8), ("soc：80％", .8), ("电量0.8", .8), ("初始电量为80", .8),
])
def test_soc_forms(soc_text, expected):
    result = parse_voyage_request(f"从漯河港到周口港，{soc_text}，半载，6小时内")
    assert result.status == Status.OK
    assert result.request.soc_initial == expected


@pytest.mark.parametrize("load", ["空载", "半载", "满载"])
def test_load_states(load):
    result = parse_voyage_request(f"从漯河港到周口港，SOC70%，{load}，最长6小时")
    assert result.status == Status.OK
    assert result.request.load_state == load


def test_draft_can_replace_load_state():
    result = parse_voyage_request("从漯河港到周口港，SOC70%，吃水2.4米，最长6小时")
    assert result.status == Status.OK
    assert result.request.load_state is None
    assert result.request.draft_m == 2.4


def test_missing_fields_are_questions_not_defaults():
    result = parse_voyage_request("从漯河港到周口港")
    assert result.status == Status.NEED_CLARIFICATION
    assert set(result.missing_fields) == {
        "soc_initial", "load_state_or_draft_m", "arrival_deadline_or_max_duration_h"
    }
    assert len(result.questions) == 3


def test_unknown_place_is_not_silently_mapped():
    result = parse_voyage_request("从郑州港到周口港，SOC80%，半载，6小时内")
    assert result.status == Status.NEED_CLARIFICATION
    assert "origin" in result.missing_fields and "destination" in result.missing_fields


@pytest.mark.parametrize("soc", ["SOC101%", "SOC150", "SOC1.1"])
def test_invalid_soc_is_rejected(soc):
    result = parse_voyage_request(f"从漯河港到周口港，{soc}，半载，6小时内")
    assert result.status == Status.INVALID_INPUT
    assert result.questions


def test_full_labeled_datetimes_are_parsed_with_china_timezone():
    result = parse_voyage_request(
        "从平顶山港到漯河港，2026-09-20 08:00出发，"
        "2026-09-20 18:00前到达，SOC85%，半载"
    )
    assert result.status == Status.OK
    assert result.request.departure_at == "2026-09-20T08:00:00+08:00"
    assert result.request.arrival_deadline == "2026-09-20T18:00:00+08:00"


def test_chinese_datetime_is_parsed():
    result = parse_voyage_request(
        "从平顶山港到漯河港，2026年9月20日8点出发，"
        "2026年9月20日18点前到达，SOC85%，半载"
    )
    assert result.status == Status.OK
    assert result.request.departure_at.endswith("+08:00")
    assert result.request.arrival_deadline.endswith("+08:00")


def test_deadline_without_departure_requests_departure():
    result = parse_voyage_request(
        "从平顶山港到漯河港，2026-09-20 18:00前到达，SOC85%，半载"
    )
    assert result.status == Status.NEED_CLARIFICATION
    assert result.missing_fields == ["departure_at"]


def test_relative_time_is_not_guessed():
    result = parse_voyage_request("明天上午从平顶山港到漯河港，SOC85%，半载")
    assert result.status == Status.NEED_CLARIFICATION
    assert any("相对时间" in item for item in result.warnings)


def test_same_origin_destination_is_invalid():
    result = parse_voyage_request("从漯河港到漯河港，SOC80%，半载，2小时内")
    assert result.status == Status.INVALID_INPUT


def test_empty_text_is_invalid():
    result = parse_voyage_request("  ")
    assert result.status == Status.INVALID_INPUT
    assert result.questions == ["请输入非空的航行任务。"]
