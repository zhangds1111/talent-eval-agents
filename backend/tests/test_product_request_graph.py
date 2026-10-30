from __future__ import annotations

from langchain.messages import HumanMessage
from langgraph.checkpoint.memory import InMemorySaver

from app.query_plan import FilterCondition, SemanticRequirement
from app.product_decision_graph import DecisionContext
from app.evaluation_case import EvaluationCaseDraft
from app.product_request_graph import ProductRequestDraft, build_product_request_graph


def _context() -> DecisionContext:
    return DecisionContext(tenant_id="course-demo", permission_scopes=("hr_private",))


class FixtureInterpreter:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, request_text: str) -> ProductRequestDraft:
        self.calls.append(request_text)
        if "美国" in request_text:
            return ProductRequestDraft(
                filters=[FilterCondition(field="region", operator="eq", value="美国")],
                semantic_requirements=[
                    SemanticRequirement(requirement_id="S1", query="FCC 检测报告", required=True)
                ],
                evaluation_preferences=["交期短优先"],
            )
        return ProductRequestDraft(
            clarifications=[{"expression": request_text, "reason": "缺少目标市场"}],
        )


def test_detailed_requirement_builds_plan_directly():
    interpreter = FixtureInterpreter()

    graph = build_product_request_graph(
        request_interpreter=interpreter,
        checkpointer=InMemorySaver(),
    )
    result = graph.invoke(
        {"request_text": "美国站露营灯，需要 FCC 检测报告，交期短优先"},
        {"configurable": {"thread_id": "lesson14-detailed"}},
        context=_context(),
    )

    assert result["status"] == "plan_ready"
    assert result["input_mode"] == "detailed_requirement"
    assert result["query_plan"]["filters"] == [
        {"field": "region", "operator": "eq", "value": "美国"}
    ]
    assert result["product_request"]["evaluation_preferences"] == ["交期短优先"]
    assert "target_job" not in result["product_request"]
    assert interpreter.calls == ["美国站露营灯，需要 FCC 检测报告，交期短优先"]


def test_request_understanding_is_automatically_captured_from_graph_execution():
    captured: list[EvaluationCaseDraft] = []
    graph = build_product_request_graph(
        request_interpreter=FixtureInterpreter(),
        case_recorder=captured.append,
    )
    context = DecisionContext(
        tenant_id="course-demo",
        permission_scopes=("hr_private",),
        run_id="run-22",
        conversation_id="conversation-22",
        turn_no=3,
    )

    result = graph.invoke(
        {"request_text": "美国站露营灯，需要 FCC 检测报告"},
        context=context,
    )

    assert result["status"] == "plan_ready"
    assert len(captured) == 1
    case = captured[0]
    assert (case.source_run_id, case.conversation_id, case.turn_no) == (
        "run-22",
        "conversation-22",
        3,
    )
    assert case.input_payload["messages"][0]["content"] == "美国站露营灯，需要 FCC 检测报告"
    assert "selected_job" not in case.input_payload
    assert case.output_payload["draft"]["filters"][0]["value"] == "美国"
    assert case.status == "succeeded"


def test_ambiguous_request_reports_clarification_without_job_branch():
    graph = build_product_request_graph(
        request_interpreter=FixtureInterpreter(),
        checkpointer=InMemorySaver(),
    )

    result = graph.invoke(
        {"request_text": "随便选点好卖的"},
        {"configurable": {"thread_id": "product-clarify"}},
        context=_context(),
    )

    assert result["status"] == "clarification_required"
    assert result["clarifications"] == [{"expression": "随便选点好卖的", "reason": "缺少目标市场"}]


def test_model_interpreter_prompt_distinguishes_region_from_current_position(monkeypatch):
    from app import product_request_graph as module

    captured: dict[str, object] = {}

    class CapturingModel:
        def with_structured_output(self, schema, **kwargs):
            captured["schema"] = schema
            return self

        def invoke(self, messages):
            captured["messages"] = messages
            return {
                "draft": {
                    "input_mode": "detailed_requirement",
                    "filters": [{"field": "region", "operator": "eq", "value": "上海"}],
                    "semantic_requirements": [],
                    "evaluation_preferences": [],
                    "clarifications": [],
                },
                "revision_action": "initial",
                "revision_summary": "建立初始要求",
                "conversation_title": "上海人才筛选",
            }

    monkeypatch.setattr(module, "get_chat_model", lambda temperature=0: CapturingModel())

    result = module.model_request_interpreter(
        [HumanMessage(content="筛选地区在上海的候选人")],
        {},
    )

    system_prompt = captured["messages"][0][1]
    assert "结合最近消息窗口和 previous_request" in system_prompt
    assert "region: 目标市场（国家或地区）" in system_prompt
    assert "current_position: 商品子类" in system_prompt
    assert "国家或地区只映射为 region" in system_prompt
    assert "input_mode 固定为 detailed_requirement" in system_prompt
    assert result.draft.filters[0].field.value == "region"
