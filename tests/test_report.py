from core.llm_layer import LLMCallResult
from core.orchestrator import run_workflow


TASK = (
    "从平顶山港到军李船闸，2026-09-21 08:00出发，"
    "2026-09-21 11:00到达，SOC85%，半载"
)


class FakeClient:
    def __init__(self, result):
        self.result = result
        self.calls = 0

    def complete_text(self, *args, **kwargs):
        self.calls += 1
        return self.result


def test_template_report_keeps_units_sources_and_tool_values():
    state = run_workflow(TASK)
    report = state["report"]
    understanding = state["task_understanding"]
    assert understanding["mode"] == "template_fallback"
    assert understanding["authoritative_source"] == "deterministic_parser_and_schema_validation"
    assert "soc_initial" in understanding["recognised_fields"]
    assert report["explanation_mode"] == "template_fallback"
    assert report["summary"]["required_energy"]["unit"] == "kWh"
    assert report["summary"]["required_energy"]["value"] == state["plan"]["management"][
        "required_energy_kwh"
    ]
    assert report["segments"][0]["distance_source"]["source_id"]
    assert report["scope"] == "synthetic_demo"


def test_llm_can_only_add_number_free_qualitative_wording():
    client = FakeClient(LLMCallResult(
        "ok", "deepseek", "test-model",
        text="本次估算反映输入任务条件下的能量变化。",
    ))
    state = run_workflow(TASK, llm_client=client)
    assert state["task_understanding"]["mode"] == "llm_qualitative"
    assert state["report"]["explanation_mode"] == "llm_qualitative"


def test_llm_failure_or_new_number_uses_template_fallback():
    for result in (
        LLMCallResult("failed", "deepseek", "test-model", error_code="timeout"),
        LLMCallResult("ok", "deepseek", "test-model", text="建议保持速度为十点五公里每小时 10.5"),
        LLMCallResult("ok", "deepseek", "test-model", text="建议保持速度为十点五公里每小时"),
    ):
        state = run_workflow(TASK, llm_client=FakeClient(result))
        assert state["task_understanding"]["mode"] == "template_fallback"
        assert state["report"]["explanation_mode"] == "template_fallback"
        assert "10.5" not in state["report"]["explanation"]


def test_internal_validation_wording_is_not_exposed_to_operator():
    client = FakeClient(LLMCallResult(
        "ok", "deepseek", "test-model",
        text="软件演示结论仅到当前终点为止，需要说明结论边界。",
    ))
    state = run_workflow(TASK, llm_client=client)
    assert state["report"]["explanation_mode"] == "template_fallback"
    explanation = state["report"]["explanation"]
    assert "软件仿真" in explanation
    assert "不构成航行或设备操作指令" in explanation


def test_management_advice_changes_with_voyage_energy_margin():
    high = run_workflow(
        "从平顶山港到军李船闸，2026-09-21 08:00出发，SOC85%，半载，6小时内到达"
    )
    low = run_workflow(
        "从平顶山港到军李船闸，2026-09-21 08:00出发，SOC47%，半载，6小时内到达"
    )
    assert high["status"] == low["status"] == "ok"
    high_advice = high["report"]["management_advice"]
    low_advice = low["report"]["management_advice"]
    assert high_advice != low_advice
    assert "演示模型估算" in high_advice[0]
    assert "低于模型规划线" in low_advice[0]
    assert "现场完成充电并确认实测 SOC" in low_advice[1]
    assumptions = high["report"]["assumptions"]
    assert all("建议保持常规角色分工" not in item for item in assumptions)
    assert any("不代表实船供电状态或操作要求" in item for item in assumptions)


def test_missing_fields_stop_before_any_llm_call():
    client = FakeClient(LLMCallResult(
        "ok", "deepseek", "test-model", text="仅确认语义边界。"
    ))
    state = run_workflow("从平顶山港到军李船闸", llm_client=client)
    assert state["status"] == "need_clarification"
    assert state["questions"]
    assert state["task_understanding"]["mode"] == "template_fallback"
    assert client.calls == 0
