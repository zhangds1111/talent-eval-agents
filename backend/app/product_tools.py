from __future__ import annotations

import logging
import time
from contextvars import copy_context
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from dataclasses import dataclass
from threading import Lock
from typing import Annotated, Any, Callable
from uuid import uuid4

from langchain.tools import ToolRuntime, tool
from pydantic import Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import EmployeeProfile, ToolCallAudit
from app.query_plan import FilterCondition, QueryPlan, TaskType, select_candidate_ids

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ProductToolContext:
    tenant_id: str
    permission_scopes: tuple[str, ...]
    actor_id: str = "unknown"
    run_id: str = "unknown"


@dataclass(frozen=True)
class ToolPolicy:
    max_attempts: int = 2
    timeout_seconds: float | None = 3.0
    backoff_seconds: float = 0.05


class TransientToolError(RuntimeError):
    pass


class InMemoryAuditSink:
    def __init__(self) -> None:
        self.records: list[dict[str, Any]] = []

    def write(self, record: dict[str, Any]) -> None:
        self.records.append(record)


class SqlAuditSink:
    def __init__(self, session_factory: Callable[[], Session]) -> None:
        self.session_factory = session_factory

    def write(self, record: dict[str, Any]) -> None:
        with self.session_factory() as db:
            db.add(ToolCallAudit(**record))
            db.commit()


class CircuitBreaker:
    def __init__(self, *, failure_threshold: int = 3, recovery_seconds: float = 30) -> None:
        self.failure_threshold = failure_threshold
        self.recovery_seconds = recovery_seconds
        self._failures: dict[str, int] = {}
        self._opened_at: dict[str, float] = {}
        self._lock = Lock()

    def allow(self, name: str) -> bool:
        with self._lock:
            opened_at = self._opened_at.get(name)
            if opened_at is None:
                return True
            if time.monotonic() - opened_at >= self.recovery_seconds:
                self._failures[name] = 0
                self._opened_at.pop(name, None)
                return True
            return False

    def succeed(self, name: str) -> None:
        with self._lock:
            self._failures[name] = 0
            self._opened_at.pop(name, None)

    def fail(self, name: str) -> None:
        with self._lock:
            failures = self._failures.get(name, 0) + 1
            self._failures[name] = failures
            if failures >= self.failure_threshold:
                self._opened_at[name] = time.monotonic()


class ToolExecutor:
    def __init__(
        self,
        *,
        policy: ToolPolicy | None = None,
        tool_policies: dict[str, ToolPolicy] | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        audit_sink: Any | None = None,
    ) -> None:
        self.policy = policy or ToolPolicy()
        self.tool_policies = tool_policies or {}
        self.circuit_breaker = circuit_breaker or CircuitBreaker()
        self.audit_sink = audit_sink or InMemoryAuditSink()

    def execute(
        self,
        name: str,
        operation: Callable[[], Any],
        *,
        context: ProductToolContext,
        arguments: dict[str, Any],
    ) -> dict[str, Any]:
        call_id = str(uuid4())
        started = time.monotonic()
        policy = self.tool_policies.get(name, self.policy)
        logger.info(
            "event=tool_call_started function=ToolExecutor.execute "
            "tool_name=%s call_id=%s run_id=%s actor_id=%s "
            "argument_keys=%s max_attempts=%s timeout_seconds=%s",
            name, call_id, context.run_id, context.actor_id,
            sorted(arguments), policy.max_attempts, policy.timeout_seconds,
        )
        if not self.circuit_breaker.allow(name):
            logger.error(
                "event=tool_call_failed function=ToolExecutor.execute "
                "failure_type=circuit_open tool_name=%s call_id=%s run_id=%s "
                "error_code=circuit_open argument_keys=%s",
                name, call_id, context.run_id, sorted(arguments),
            )
            return self._finish(
                name, call_id, context, arguments, started, 0, "blocked",
                error={"code": "circuit_open", "message": "依赖服务暂时不可用", "retryable": True},
                degraded=True,
            )

        last_error: Exception | None = None
        for attempt in range(1, policy.max_attempts + 1):
            pool = ThreadPoolExecutor(max_workers=1)
            try:
                future = pool.submit(copy_context().run, operation)
                data = future.result(timeout=policy.timeout_seconds)
                self.circuit_breaker.succeed(name)
                logger.info(
                    "event=tool_call_completed function=ToolExecutor.execute "
                    "tool_name=%s call_id=%s run_id=%s attempts=%s duration_ms=%s",
                    name, call_id, context.run_id, attempt,
                    round((time.monotonic() - started) * 1000, 2),
                )
                return self._finish(name, call_id, context, arguments, started, attempt, "succeeded", data=data)
            except (TransientToolError, FutureTimeoutError) as exc:
                last_error = exc
                if attempt < policy.max_attempts:
                    logger.warning(
                        "event=tool_call_retry function=ToolExecutor.execute "
                        "failure_type=%s tool_name=%s call_id=%s run_id=%s "
                        "attempt=%s max_attempts=%s",
                        "timeout" if isinstance(exc, FutureTimeoutError) else "transient",
                        name, call_id, context.run_id, attempt, policy.max_attempts,
                    )
                if attempt < policy.max_attempts and policy.backoff_seconds:
                    time.sleep(policy.backoff_seconds * (2 ** (attempt - 1)))
            except (ValueError, PermissionError) as exc:
                code = "permission_denied" if isinstance(exc, PermissionError) else "invalid_argument"
                logger.error(
                    "event=tool_call_failed function=ToolExecutor.execute "
                    "failure_type=%s tool_name=%s call_id=%s run_id=%s "
                    "attempt=%s error_code=%s argument_keys=%s",
                    type(exc).__name__, name, call_id, context.run_id,
                    attempt, code, sorted(arguments),
                )
                return self._finish(
                    name, call_id, context, arguments, started, attempt, "failed",
                    error={"code": code, "message": str(exc), "retryable": False},
                )
            except Exception as exc:
                logger.exception(
                    "event=tool_call_crashed function=ToolExecutor.execute "
                    "failure_type=%s tool_name=%s call_id=%s run_id=%s "
                    "attempt=%s argument_keys=%s",
                    type(exc).__name__, name, call_id, context.run_id,
                    attempt, sorted(arguments),
                )
                raise
            finally:
                pool.shutdown(wait=False, cancel_futures=True)

        self.circuit_breaker.fail(name)
        message = "依赖服务调用超时" if isinstance(last_error, FutureTimeoutError) else "依赖服务暂时不可用"
        logger.error(
            "event=tool_call_failed function=ToolExecutor.execute "
            "failure_type=%s tool_name=%s call_id=%s run_id=%s "
            "attempts=%s error_code=dependency_unavailable argument_keys=%s "
            "duration_ms=%s",
            "timeout" if isinstance(last_error, FutureTimeoutError) else "transient",
            name, call_id, context.run_id, policy.max_attempts,
            sorted(arguments), round((time.monotonic() - started) * 1000, 2),
        )
        return self._finish(
            name, call_id, context, arguments, started, policy.max_attempts, "failed",
            error={"code": "dependency_unavailable", "message": message, "retryable": True},
            degraded=True,
        )

    def _finish(
        self, name, call_id, context, arguments, started, attempts, status,
        *, data=None, error=None, degraded=False,
    ) -> dict[str, Any]:
        meta = {
            "tool_name": name,
            "call_id": call_id,
            "attempts": attempts,
            "degraded": degraded,
        }
        self.audit_sink.write(
            {
                "call_id": call_id,
                "tool_name": name,
                "tenant_id": context.tenant_id,
                "actor_id": context.actor_id,
                "run_id": context.run_id,
                "argument_keys": sorted(arguments),
                "status": status,
                "attempts": attempts,
                "duration_ms": round((time.monotonic() - started) * 1000, 2),
                "error_code": error["code"] if error else None,
            }
        )
        return {"ok": error is None, "data": data, "error": error, "meta": meta}


class ProductToolService:
    def __init__(
        self,
        *,
        session_factory: Callable[[], Session],
        evidence_provider: Callable[..., dict[str, Any]] | None = None,
    ) -> None:
        self.session_factory = session_factory
        self.evidence_provider = evidence_provider

    @staticmethod
    def _check_context(context: ProductToolContext) -> None:
        if not context.tenant_id.strip():
            raise PermissionError("缺少可信租户")
        if not context.permission_scopes:
            raise PermissionError("缺少可用权限范围")

    def filter_candidates(self, filters: list[FilterCondition], *, context: ProductToolContext) -> list[str]:
        self._check_context(context)
        plan = QueryPlan(task_type=TaskType.FIND_PRODUCT, filters=filters)
        with self.session_factory() as db:
            return select_candidate_ids(db, plan, tenant_id=context.tenant_id)

    def get_candidate_profiles(self, candidate_ids: list[str], *, context: ProductToolContext) -> list[dict[str, Any]]:
        self._check_context(context)
        if not candidate_ids:
            return []
        with self.session_factory() as db:
            rows = db.scalars(
                select(EmployeeProfile).where(
                    EmployeeProfile.tenant_id == context.tenant_id,
                    EmployeeProfile.employee_no.in_(candidate_ids),
                    EmployeeProfile.employment_status == "active",
                ).order_by(EmployeeProfile.employee_no)
            ).all()
        return [
            {"candidate_id": row.employee_no, "name": row.name, "region": row.region,
             "current_position": row.current_position, "job_level": row.job_level,
             "years_of_experience": row.years_of_experience, "department": row.department}
            for row in rows
        ]

    def check_product_compliance(self, candidate_ids: list[str], *, context: ProductToolContext) -> list[dict[str, Any]]:
        """程序计算毛利与必需认证缺口；只返回当前租户内的在售商品。"""
        from app.product_compliance import load_product_facts

        allowed = [item["candidate_id"] for item in self.get_candidate_profiles(candidate_ids, context=context)]
        if not allowed:
            return []
        with self.session_factory() as db:
            facts = load_product_facts(db, allowed)
        return [
            {**facts[candidate_id].model_dump(mode="json"),
             "certification_gap_count": len(facts[candidate_id].certification_gaps)}
            for candidate_id in allowed if candidate_id in facts
        ]

    def list_certification_rules(self, *, context: ProductToolContext) -> list[dict[str, Any]]:
        from app.models import CertificationRule

        self._check_context(context)
        with self.session_factory() as db:
            rows = db.scalars(select(CertificationRule).order_by(CertificationRule.id)).all()
        return [
            {"market": row.market, "category": row.category, "applies_when": row.applies_when,
             "certification": row.certification, "rationale": row.rationale}
            for row in rows
        ]

    def search_candidate_evidence(
        self, query: str, candidate_ids: list[str], *, context: ProductToolContext,
    ) -> dict[str, Any]:
        logger.info(
            "event=product_evidence_search_started "
            "function=ProductToolService.search_candidate_evidence "
            "run_id=%s candidate_ids=%s query_chars=%s permission_scope_count=%s",
            context.run_id, candidate_ids, len(query), len(context.permission_scopes),
        )
        self._check_context(context)
        if self.evidence_provider is None:
            raise TransientToolError("evidence_provider_not_configured")
        result = self.evidence_provider(
            query=query,
            candidate_ids=candidate_ids,
            tenant_id=context.tenant_id,
            permission_scopes=list(context.permission_scopes),
            include_evidence_pack=True,
        )
        for pack in result.get("evidence_packs", []):
            if pack.get("schema_version") != "2.0":
                raise ValueError("不支持的证据协议版本")
        logger.info(
            "event=product_evidence_search_completed "
            "function=ProductToolService.search_candidate_evidence "
            "run_id=%s candidate_ids=%s chunk_count=%s evidence_pack_count=%s",
            context.run_id, candidate_ids, len(result.get("chunks", [])),
            len(result.get("evidence_packs", [])),
        )
        return result


def build_product_tools(service: ProductToolService, executor: ToolExecutor):
    @tool
    def filter_candidates(
        filters: Annotated[list[FilterCondition], Field(max_length=20)],
        runtime: ToolRuntime[ProductToolContext],
    ):
        """按目标市场、品类、售价、毛利率等结构化条件筛选授权租户内的在售 SKU。"""
        return executor.execute(
            "filter_candidates", lambda: service.filter_candidates(filters, context=runtime.context),
            context=runtime.context, arguments={"filters": filters},
        )

    @tool
    def search_candidate_evidence(
        query: Annotated[str, Field(min_length=1, max_length=500)],
        candidate_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
        runtime: ToolRuntime[ProductToolContext],
    ):
        """在给定 SKU 范围内检索商品材料证据并返回 Evidence Pack 2.0。"""
        return executor.execute(
            "search_candidate_evidence",
            lambda: service.search_candidate_evidence(query, candidate_ids, context=runtime.context),
            context=runtime.context, arguments={"query": query, "candidate_ids": candidate_ids},
        )

    @tool
    def get_candidate_profiles(
        candidate_ids: Annotated[list[str], Field(min_length=1, max_length=100)],
        runtime: ToolRuntime[ProductToolContext],
    ):
        """批量读取授权租户内 SKU 的结构化基础信息，用于评估取证。"""
        return executor.execute(
            "get_candidate_profiles",
            lambda: service.get_candidate_profiles(candidate_ids, context=runtime.context),
            context=runtime.context, arguments={"candidate_ids": candidate_ids},
        )

    return [filter_candidates, search_candidate_evidence, get_candidate_profiles]


def build_candidate_provider(
    service: ProductToolService,
    *,
    compile_filters: Callable[[str], list[FilterCondition]],
):
    """Adapt the lesson 12 structured filter service to the lesson 11 graph contract."""
    def provider(request: dict[str, Any], decision_context: Any) -> list[str]:
        context = ProductToolContext(
            tenant_id=decision_context.tenant_id,
            permission_scopes=tuple(decision_context.permission_scopes),
            actor_id="langgraph",
        )
        return service.filter_candidates(compile_filters(request["original_text"]), context=context)

    return provider
