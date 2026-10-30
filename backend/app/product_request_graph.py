from __future__ import annotations

from app.evaluation_case import capture_call, EvaluationModule, safe_record

from collections.abc import Callable
import inspect
from operator import add
from time import perf_counter
from typing import Annotated, Any, Literal

from langchain.messages import AnyMessage, HumanMessage
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages
from langgraph.runtime import Runtime
from langgraph.types import interrupt
from pydantic import BaseModel, Field, ValidationError
from typing_extensions import TypedDict

from app.database import SessionLocal
from app.config import get_settings
from app.evaluation_case import (
    EvaluationCaseDraft,
    SqlEvaluationCaseStore,
    build_request_understanding_case,
    recent_request_messages,
)
from app.model_provider import get_chat_model
from app.query_plan import (
    Clarification,
    FilterCondition,
    QueryPlan,
    SemanticRequirement,
    TaskType,
    filter_dsl_catalog,
)
from app.product_decision_graph import DecisionContext
from app.product_tools import ProductToolContext, ProductToolService


RequestInputMode = Literal["detailed_requirement"]
RequestStatus = Literal[
    "received",
    "request_understood",
    "plan_ready",
    "clarification_required",
    "skus_empty",
    "skus_ready",
    "plan_revised",
    "request_revised",
    "cancelled",
]


class ProductRequestDraft(BaseModel):
    input_mode: RequestInputMode = "detailed_requirement"
    filters: list[FilterCondition] = Field(default_factory=list)
    semantic_requirements: list[SemanticRequirement] = Field(default_factory=list)
    evaluation_preferences: list[str] = Field(default_factory=list)
    clarifications: list[Clarification] = Field(default_factory=list)


class ProductRequestRevision(BaseModel):
    draft: ProductRequestDraft
    revision_action: Literal["initial", "add", "update", "remove"] = "initial"
    revision_summary: str = "建立初始选品要求"


class ProductRequestUnderstanding(ProductRequestRevision):
    conversation_title: str = Field(min_length=1, max_length=80)


class ProductRequestInput(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    request_text: str
    product_request: dict[str, Any]
    conversation_title: str


class ProductRequestOutput(TypedDict):
    messages: Annotated[list[AnyMessage], add_messages]
    status: RequestStatus
    input_mode: RequestInputMode
    product_request: dict[str, Any]
    query_plan: dict[str, Any]
    sku_ids: list[str]
    condition_revisions: list[dict[str, Any]]
    plan_version: int
    clarifications: list[dict[str, str]]
    errors: list[str]
    conversation_title: str
    revision_action: str
    revision_summary: str


class ProductRequestState(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    request_text: str
    input_mode: RequestInputMode
    draft: dict[str, Any]
    product_request: dict[str, Any]
    query_plan: dict[str, Any]
    sku_ids: list[str]
    condition_revisions: Annotated[list[dict[str, Any]], add]
    plan_version: int
    clarifications: list[dict[str, str]]
    status: RequestStatus
    errors: list[str]
    conversation_title: str
    revision_action: str
    revision_summary: str


RequestInterpreter = Callable[..., ProductRequestUnderstanding | ProductRequestDraft]
SkuFilter = Callable[[list[FilterCondition], DecisionContext], list[str]]
CaseRecorder = Callable[[EvaluationCaseDraft], Any]


def _receive_request(state: ProductRequestState, runtime: Runtime[DecisionContext]) -> dict[str, Any]:
    if not runtime.context.tenant_id.strip():
        raise ValueError("Runtime Context 中的 tenant_id 不能为空")
    if not runtime.context.permission_scopes:
        raise ValueError("Runtime Context 中的 permission_scopes 不能为空")
    messages = list(state.get("messages", []))
    request_text = str(state.get("request_text", "")).strip()
    if messages:
        request_text = str(messages[-1].content).strip()
    if not request_text:
        raise ValueError("request_text 不能为空")
    update: dict[str, Any] = {
        "request_text": request_text,
        "query_plan": {},
        "sku_ids": [],
        "condition_revisions": [],
        "plan_version": 0,
        "clarifications": [],
        "status": "received",
        "errors": [],
    }
    if not messages:
        update["messages"] = [HumanMessage(content=request_text)]
    return update


def _call_request_interpreter(
    interpreter: RequestInterpreter,
    state: ProductRequestState,
    context: DecisionContext,
    case_recorder: CaseRecorder | None,
) -> ProductRequestUnderstanding:
    started_at = perf_counter()
    try:
        parameters = inspect.signature(interpreter).parameters
        if len(parameters) <= 2:
            legacy = interpreter(state["request_text"])
            if isinstance(legacy, ProductRequestUnderstanding):
                understanding = legacy
            else:
                understanding = ProductRequestUnderstanding(
                    draft=ProductRequestDraft.model_validate(legacy),
                    conversation_title=state.get("conversation_title") or state["request_text"][:30],
                )
        else:
            result = interpreter(
                recent_request_messages(list(state.get("messages", []))),
                dict(state.get("product_request", {})),
                state.get("conversation_title", ""),
            )
            understanding = ProductRequestUnderstanding.model_validate(result)
    except Exception as exc:
        if case_recorder is not None and context.run_id:
            safe_record(case_recorder,
                build_request_understanding_case(
                    source_run_id=context.run_id,
                    conversation_id=context.conversation_id,
                    turn_no=context.turn_no,
                    messages=list(state.get("messages", [])),
                    previous_request=dict(state.get("product_request", {})),
                    conversation_title=state.get("conversation_title", ""),
                    output_payload={},
                    replay_context={
                        "tenant_id": context.tenant_id,
                        "permission_scopes": list(context.permission_scopes),
                    },
                    version_snapshot={
                        "prompt": "request-understanding-v1",
                        "model": get_settings().chat_model,
                    },
                    status="failed",
                    error_code=type(exc).__name__,
                    duration_ms=round((perf_counter() - started_at) * 1000),
                )
            )
        raise
    if case_recorder is not None and context.run_id:
        safe_record(case_recorder,
            build_request_understanding_case(
                source_run_id=context.run_id,
                conversation_id=context.conversation_id,
                turn_no=context.turn_no,
                messages=list(state.get("messages", [])),
                previous_request=dict(state.get("product_request", {})),
                conversation_title=state.get("conversation_title", ""),
                output_payload=understanding.model_dump(mode="json"),
                replay_context={
                    "tenant_id": context.tenant_id,
                    "permission_scopes": list(context.permission_scopes),
                },
                version_snapshot={
                    "prompt": "request-understanding-v1",
                    "model": get_settings().chat_model,
                },
                duration_ms=round((perf_counter() - started_at) * 1000),
            )
        )
    return understanding


def _interpret_input(
    request_interpreter: RequestInterpreter,
    case_recorder: CaseRecorder | None,
):
    def interpret_input(
        state: ProductRequestState,
        runtime: Runtime[DecisionContext],
    ) -> dict[str, Any]:
        understanding = _call_request_interpreter(
            request_interpreter,
            state,
            runtime.context,
            case_recorder,
        )
        draft = understanding.draft
        return {
            "draft": draft.model_dump(mode="json"),
            "input_mode": draft.input_mode,
            "status": "request_understood",
            "conversation_title": state.get("conversation_title") or understanding.conversation_title,
            "revision_action": understanding.revision_action,
            "revision_summary": understanding.revision_summary,
        }

    return interpret_input


def _build_plan(state: ProductRequestState) -> dict[str, Any]:
    draft = ProductRequestDraft.model_validate(state["draft"])

    product_request = {
        "original_text": state["request_text"],
        "task_type": "evaluate_and_recommend",
        "source": state["input_mode"],
        "hard_conditions": [item.model_dump(mode="json") for item in draft.filters],
        "semantic_conditions": [
            item.model_dump(mode="json") for item in draft.semantic_requirements
        ],
        "evaluation_preferences": draft.evaluation_preferences,
    }
    query_plan = QueryPlan(
        task_type=TaskType.FIND_PRODUCT,
        filters=draft.filters,
        semantic_requirements=draft.semantic_requirements,
        preferences=draft.evaluation_preferences,
        clarifications=draft.clarifications,
    )
    status: RequestStatus = "plan_ready" if query_plan.executable else "clarification_required"
    return {
        "product_request": product_request,
        "query_plan": query_plan.model_dump(mode="json"),
        "clarifications": [item.model_dump(mode="json") for item in draft.clarifications],
        "status": status,
    }


# ---------------------------------------------------------------------------
# 第 17 课：候选人检索与可恢复人机协同
# ---------------------------------------------------------------------------


def _route_after_plan(state: ProductRequestState) -> str:
    if state["status"] == "clarification_required":
        return "resolve_requirements"
    return "filter_skus"


def _route_skus(state: ProductRequestState) -> str:
    if state["status"] == "skus_empty":
        return "resolve_requirements"
    return END


def _route_resolution(state: ProductRequestState) -> str:
    if state["status"] == "request_revised":
        return "interpret_input"
    return END


def _await_decision(payload: dict[str, Any], parse: Callable[[Any], dict[str, Any]]) -> dict[str, Any]:
    """等待恢复数据并完成校验

    校验失败时不抛出异常，而是携带 resume_error 重新 interrupt，
    让 Thread 停留在暂停点等待下一次提交，避免节点失败导致线程卡死
    """
    decision = interrupt(payload)
    while True:
        try:
            return parse(decision)
        except ValueError as exc:
            decision = interrupt({**payload, "resume_error": str(exc)})


def _require_interaction(decision: Any, expected_id: str) -> dict[str, Any]:
    if not isinstance(decision, dict):
        raise ValueError("恢复数据必须是 JSON 对象")
    if decision.get("interaction_id") != expected_id:
        raise ValueError(f"恢复数据与当前暂停点不匹配，期望 interaction_id={expected_id}")
    return decision


def _filter_skus_node(sku_filter: SkuFilter):
    def filter_skus(state: ProductRequestState, runtime: Runtime[DecisionContext]) -> dict[str, Any]:
        plan = QueryPlan.model_validate(state["query_plan"])
        sku_ids = sku_filter(plan.filters, runtime.context)
        if sku_ids:
            return {"sku_ids": sku_ids, "status": "skus_ready"}
        return {"sku_ids": [], "status": "skus_empty"}

    return filter_skus


def _diagnose_filters(
    filters: list[FilterCondition],
    sku_filter: SkuFilter,
    context: DecisionContext,
) -> list[dict[str, Any]]:
    """leave-one-out 诊断：逐个移除条件，观察候选数变化，定位导致空集的条件"""
    diagnostics = []
    for index, condition in enumerate(filters):
        reduced = filters[:index] + filters[index + 1 :]
        hit_count = len(sku_filter(reduced, context))
        diagnostics.append(
            {
                "field": str(condition.field),
                "operator": condition.operator,
                "value": condition.value,
                "skus_without_condition": hit_count,
            }
        )
    return diagnostics


def _resolve_requirements():
    def resolve_requirements(
        state: ProductRequestState,
        runtime: Runtime[DecisionContext],
    ) -> dict[str, Any]:
        plan_version = state.get("plan_version", 0)
        interaction_id = f"rr-v{plan_version}"
        reason_code = "plan_blocked" if state["status"] == "clarification_required" else "empty_skus"
        filters = state.get("draft", {}).get("filters", [])
        if reason_code == "plan_blocked":
            blocking_reason = "当前要求存在歧义或冲突，无法生成可执行查询计划"
        else:
            rendered = "、".join(
                f"{item['field']} {item['operator']} {item['value']}" for item in filters
            )
            blocking_reason = f"使用以下结构化条件筛选后未命中候选商品：{rendered}"
        editable_request = state["request_text"]
        payload = {
            "type": "requirement_revision",
            "interaction_id": interaction_id,
            "reason_code": reason_code,
            "blocking_reason": blocking_reason,
            "question": "请补充或修订选品条件后重新提交，或取消本次任务",
            "request_text": editable_request,
            "request_source": "user_request",
            "filters": filters,
            "semantic_requirements": state.get("draft", {}).get("semantic_requirements", []),
            "clarifications": state.get("clarifications", []),
            "actions": ["revise", "cancel"],
        }

        def parse(decision: Any) -> dict[str, Any]:
            decision = _require_interaction(decision, interaction_id)
            action = decision.get("action")
            if action == "cancel":
                return {"action": "cancel"}
            if action != "revise":
                raise ValueError("action 必须是 revise 或 cancel")
            revision_text = decision.get("revision_text")
            if not isinstance(revision_text, str) or not revision_text.strip():
                raise ValueError("revise 动作必须携带非空 revision_text")
            return {"action": "revise", "revision_text": revision_text.strip()}

        parsed = _await_decision(payload, parse)
        if parsed["action"] == "cancel":
            return {"status": "cancelled"}

        revision_text = parsed["revision_text"]
        revision = {
            "interaction_id": interaction_id,
            "action": "revise",
            "reason_code": reason_code,
            "revision_text": revision_text,
            "plan_version": plan_version + 1,
        }
        return {
            "request_text": revision_text,
            "messages": [HumanMessage(content=revision_text)],
            "sku_ids": [],
            "condition_revisions": [revision],
            "plan_version": plan_version + 1,
            "status": "request_revised",
        }

    return resolve_requirements


def _apply_relaxations(
    current: list[FilterCondition],
    relaxations: list[dict[str, Any]],
) -> tuple[list[FilterCondition], list[dict[str, Any]]]:
    updated = list(current)
    changes: list[dict[str, Any]] = []
    for item in relaxations:
        field = item.get("field")
        op = item.get("op")
        index = next((i for i, cond in enumerate(updated) if str(cond.field) == field), None)
        if index is None:
            raise ValueError(f"放宽目标条件不存在：{field}")
        if op == "remove":
            removed = updated.pop(index)
            changes.append({"field": field, "op": "remove", "removed": removed.model_dump(mode="json")})
        elif op == "update":
            replacement = FilterCondition.model_validate(
                {"field": field, "operator": item.get("operator"), "value": item.get("value")}
            )
            previous = updated[index]
            updated[index] = replacement
            changes.append(
                {
                    "field": field,
                    "op": "update",
                    "previous": previous.model_dump(mode="json"),
                    "current": replacement.model_dump(mode="json"),
                }
            )
        else:
            raise ValueError(f"不支持的放宽操作：{op}")
    return updated, changes


def _resolve_conditions(state: ProductRequestState) -> dict[str, Any]:
    plan_version = state.get("plan_version", 0)
    interaction_id = f"cc-v{plan_version}"
    draft = dict(state["draft"])
    payload = {
        "type": "condition_clarification",
        "interaction_id": interaction_id,
        "question": "以下条件无法直接执行，请修订后重新提交，或取消本次任务",
        "clarifications": state["clarifications"],
        "current_filters": draft.get("filters", []),
        "current_semantic_requirements": draft.get("semantic_requirements", []),
        "actions": ["revise", "cancel"],
    }

    def parse(decision: Any) -> dict[str, Any]:
        decision = _require_interaction(decision, interaction_id)
        action = decision.get("action")
        if action == "cancel":
            return {"action": "cancel"}
        if action != "revise":
            raise ValueError("action 必须是 revise 或 cancel")
        parsed: dict[str, Any] = {"action": "revise"}
        if "filters" in decision:
            try:
                parsed["filters"] = [
                    FilterCondition.model_validate(item) for item in decision["filters"]
                ]
            except ValidationError as exc:
                raise ValueError(f"filters 校验失败：{exc.errors()[0]['msg']}") from exc
        if "semantic_requirements" in decision:
            items = decision["semantic_requirements"]
            if not isinstance(items, list) or any(not isinstance(item, dict) or not item.get("query") for item in items):
                raise ValueError("semantic_requirements 的每一项都必须包含 query")
            parsed["semantic_requirements"] = [
                SemanticRequirement(
                    requirement_id=f"S{index + 1}",
                    query=str(item["query"]),
                    required=bool(item.get("required", True)),
                )
                for index, item in enumerate(items)
            ]
        if "filters" not in parsed and "semantic_requirements" not in parsed:
            raise ValueError("revise 动作必须携带 filters 或 semantic_requirements")
        return parsed

    parsed = _await_decision(payload, parse)
    if parsed["action"] == "cancel":
        return {"status": "cancelled"}

    changes: list[dict[str, Any]] = []
    invalidates: set[str] = set()
    if "filters" in parsed:
        draft["filters"] = [item.model_dump(mode="json") for item in parsed["filters"]]
        changes.append({"kind": "filters", "count": len(parsed["filters"])})
        invalidates.add("sku_set")
    if "semantic_requirements" in parsed:
        draft["semantic_requirements"] = [
            item.model_dump(mode="json") for item in parsed["semantic_requirements"]
        ]
        changes.append({"kind": "semantic_requirements", "count": len(parsed["semantic_requirements"])})
        invalidates.update(["evidence_retrieval", "evaluation_dimensions", "evaluation_report"])

    resolved = list(draft.get("clarifications", []))
    draft["clarifications"] = []
    revision = {
        "interaction_id": interaction_id,
        "action": "revise",
        "changes": changes,
        "resolved_clarifications": resolved,
        "invalidates": sorted(invalidates),
        "plan_version": plan_version + 1,
    }
    return {
        "draft": draft,
        "condition_revisions": [revision],
        "plan_version": plan_version + 1,
        "status": "plan_revised",
    }


def _resolve_empty_skus(sku_filter: SkuFilter):
    def resolve_empty_skus(
        state: ProductRequestState,
        runtime: Runtime[DecisionContext],
    ) -> dict[str, Any]:
        plan = QueryPlan.model_validate(state["query_plan"])
        plan_version = state.get("plan_version", 0)
        interaction_id = f"ec-v{plan_version}"
        diagnostics = _diagnose_filters(plan.filters, sku_filter, runtime.context)
        current = [
            FilterCondition.model_validate(item) for item in state["draft"].get("filters", [])
        ]
        payload = {
            "type": "empty_sku_set",
            "interaction_id": interaction_id,
            "question": "当前条件未命中任何候选商品，可放宽部分条件后重试，或取消本次任务",
            "filters": diagnostics,
            "actions": ["relax", "cancel"],
        }

        def parse(decision: Any) -> dict[str, Any]:
            decision = _require_interaction(decision, interaction_id)
            action = decision.get("action")
            if action == "cancel":
                return {"action": "cancel"}
            if action != "relax":
                raise ValueError("action 必须是 relax 或 cancel")
            relaxations = decision.get("relaxations") or []
            if not relaxations:
                raise ValueError("relax 动作必须携带 relaxations")
            updated, changes = _apply_relaxations(current, relaxations)
            return {"action": "relax", "updated": updated, "changes": changes}

        parsed = _await_decision(payload, parse)
        if parsed["action"] == "cancel":
            return {"status": "cancelled"}

        draft = dict(state["draft"])
        draft["filters"] = [item.model_dump(mode="json") for item in parsed["updated"]]
        revision = {
            "interaction_id": interaction_id,
            "action": "relax",
            "changes": parsed["changes"],
            "resolved_clarifications": [],
            "invalidates": ["sku_set"],
            "plan_version": plan_version + 1,
        }
        return {
            "draft": draft,
            "condition_revisions": [revision],
            "plan_version": plan_version + 1,
            "status": "plan_revised",
        }

    return resolve_empty_skus


def build_product_request_graph(
    *,
    request_interpreter: RequestInterpreter,
    sku_filter: SkuFilter | None = None,
    case_recorder: CaseRecorder | None = None,
    checkpointer: Any | None = None,
):
    builder = StateGraph(
        ProductRequestState,
        context_schema=DecisionContext,
        input_schema=ProductRequestInput,
        output_schema=ProductRequestOutput,
    )
    builder.add_node("receive_request", _receive_request)
    builder.add_node("interpret_input", _interpret_input(request_interpreter, case_recorder))
    builder.add_node("build_plan", _build_plan)
    builder.add_edge(START, "receive_request")
    builder.add_edge("receive_request", "interpret_input")
    builder.add_edge("interpret_input", "build_plan")

    if sku_filter is None:
        # 第 14 课行为：编译出 QueryPlan 即结束，候选人检索与澄清节点不挂载
        builder.add_edge("build_plan", END)
    else:
        builder.add_node("filter_skus", _filter_skus_node(sku_filter))
        builder.add_node("resolve_requirements", _resolve_requirements())
        builder.add_conditional_edges(
            "build_plan",
            _route_after_plan,
            {"resolve_requirements": "resolve_requirements", "filter_skus": "filter_skus"},
        )
        builder.add_conditional_edges(
            "filter_skus",
            _route_skus,
            {"resolve_requirements": "resolve_requirements", END: END},
        )
        builder.add_conditional_edges(
            "resolve_requirements",
            _route_resolution,
            {"interpret_input": "interpret_input", END: END},
        )
    return builder.compile(checkpointer=checkpointer) if checkpointer is not None else builder.compile()


MODEL_SYSTEM_PROMPT = """你是跨境电商选品任务编译器。按照给定 Schema 输出结构化结果。
结合最近消息窗口和 previous_request，输出本轮修订后的完整 ProductRequestDraft。
首轮 revision_action 为 initial，conversation_title 为空时生成不超过 20 个中文字的标题。
已有 conversation_title 时不生成标题，后续轮根据最新消息在 add、update、remove 中选择，保留未被修改的既有条件。
用 revision_summary 简要说明变化。
input_mode 固定为 detailed_requirement：直接把用户自然语言需求（目标市场、渠道、品类、售价、成本、毛利、重量、是否带电、电池类型、认证偏好、评分或选品偏好）编译为结构化条件，不引用任何模板。
结构化硬条件只能写入 filters，字段定义与允许的操作符如下：
{filter_dsl}
国家或地区只映射为 region（目标市场），current_position 只表示商品子类，禁止将市场或国家写入 current_position。
售价、到岸成本、毛利率的量化阈值写入 filters（target_price、landed_cost、gross_margin_percent；“毛利不低于 40%”即 gross_margin_percent gte 40）；是否带电、电池类型、重量、评分等没有结构化字段的条件写入 semantic_requirements，不得塞进 filters 中语义不符的字段；必需认证的缺口由程序按规则表计算，用户点名的认证/检测报告仍写入 semantic_requirements 以检索材料证据；“需求旺盛”“质量稳定”“差评少”等无法精确过滤的要求写入 semantic_requirements；“优先低 MOQ”“优先交期短”等写入 evaluation_preferences。
只有歧义或矛盾导致无法形成可执行查询计划时才写入 clarifications。租户和权限不得从用户文本提取。
系统没有选品模板，不根据模板名称猜测条件。"""


def model_request_interpreter(
    messages: list[AnyMessage],
    previous_request: dict[str, Any],
    conversation_title: str = "",
) -> ProductRequestUnderstanding:
    model = get_chat_model(temperature=0)
    if model is None:
        raise RuntimeError("任务理解模型未配置，请设置 DASHSCOPE_API_KEY")
    messages = recent_request_messages(messages)
    history = "\n".join(
        f"{getattr(message, 'type', 'user')}: {message.content}" for message in messages
    )
    user_content = (
        f"message_history=\n{history}\n"
        f"previous_request={previous_request}\n"
        f"conversation_title={conversation_title}"
    )
    result = model.with_structured_output(
        ProductRequestRevision if conversation_title else ProductRequestUnderstanding,
        method="function_calling",
    ).invoke(
        [
            ("system", MODEL_SYSTEM_PROMPT.format(filter_dsl=filter_dsl_catalog())),
            ("user", user_content),
        ]
    )
    if conversation_title:
        revision = ProductRequestRevision.model_validate(result)
        return ProductRequestUnderstanding(**revision.model_dump(), conversation_title=conversation_title)
    return ProductRequestUnderstanding.model_validate(result)


def database_sku_filter(filters: list[FilterCondition], context: DecisionContext) -> list[str]:
    service = ProductToolService(session_factory=SessionLocal)
    tool_context = ProductToolContext(
        tenant_id=context.tenant_id,
        permission_scopes=context.permission_scopes,
        actor_id="langsmith-studio",
        run_id="lesson-17",
    )
    return service.filter_skus(filters, context=tool_context)


# Agent Server injects its managed checkpointer when this graph is loaded from langgraph.json.
graph = build_product_request_graph(
    request_interpreter=model_request_interpreter,
    sku_filter=database_sku_filter,
    case_recorder=SqlEvaluationCaseStore(SessionLocal).record,
)
