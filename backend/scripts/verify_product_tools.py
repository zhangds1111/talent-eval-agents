from __future__ import annotations

import json
from datetime import date

from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, EmployeeProfile
from app.query_plan import FilterCondition
from app.product_tools import (
    CircuitBreaker,
    InMemoryAuditSink,
    ProductToolContext,
    ProductToolService,
    ToolExecutor,
    ToolPolicy,
    TransientToolError,
    build_product_tools,
)


if __name__ == "__main__":
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add_all(
            [
                EmployeeProfile(
                    employee_no="SKU-DEMO-001", tenant_id="course-demo", name="演示露营灯（虚构）",
                    region="美国", current_position="充电式露营灯", department="户外照明",
                ),
            ]
        )
        db.commit()

    context = ProductToolContext(
        tenant_id="course-demo",
        permission_scopes=("hr_private",),
        actor_id="lesson-12",
        run_id="verify-tools",
    )
    evidence_pack = {
        "sku_ids": ["SKU-DEMO-001"],
        "evidence_packs": [
            {"schema_version": "2.0", "sku_id": "SKU-DEMO-001", "requirements": []}
        ],
    }
    service = ProductToolService(
        session_factory=lambda: Session(engine),
        evidence_provider=lambda **_: evidence_pack,
    )
    audit = InMemoryAuditSink()
    executor = ToolExecutor(audit_sink=audit)
    tools = build_product_tools(service, executor)
    print("[tool_names]", [item.name for item in tools])
    print(
        "[sku_filter]",
        service.filter_skus(
            [FilterCondition(field="region", operator="eq", value="美国")], context=context
        ),
    )
    print(
        "[profile]",
        json.dumps(service.get_sku_profiles(["SKU-DEMO-001"], context=context), ensure_ascii=False),
    )
    print(
        "[evidence_schema]",
        service.search_sku_evidence("企业知识库经验", ["SKU-DEMO-001"], context=context)[
            "evidence_packs"
        ][0]["schema_version"],
    )

    attempts = {"count": 0}

    def unstable_operation():
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise TransientToolError("milvus_unavailable")
        return {"sku_ids": ["SKU-DEMO-001"]}

    retry_executor = ToolExecutor(
        policy=ToolPolicy(max_attempts=3, backoff_seconds=0), audit_sink=audit
    )
    print(
        "[retry]",
        json.dumps(
            retry_executor.execute(
                "search_sku_evidence",
                unstable_operation,
                context=context,
                arguments={"query": "企业知识库经验"},
            ),
            ensure_ascii=False,
        ),
    )

    breaker = CircuitBreaker(failure_threshold=2, recovery_seconds=60)
    breaker_executor = ToolExecutor(
        policy=ToolPolicy(max_attempts=1, backoff_seconds=0),
        circuit_breaker=breaker,
        audit_sink=audit,
    )
    for _ in range(2):
        breaker_executor.execute(
            "search_sku_evidence",
            lambda: (_ for _ in ()).throw(TransientToolError("milvus_unavailable")),
            context=context,
            arguments={},
        )
    print(
        "[circuit]",
        json.dumps(
            breaker_executor.execute(
                "search_sku_evidence",
                lambda: {"unexpected": True},
                context=context,
                arguments={},
            ),
            ensure_ascii=False,
        ),
    )
    print("[audit_statuses]", [row["status"] for row in audit.records])
