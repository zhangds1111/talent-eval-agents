"""使用真实选品数据、检索服务和模型验证 Run 与 SSE 投影链路。"""

from __future__ import annotations

import argparse
import asyncio
import json
from time import perf_counter

from sqlalchemy import func, select

from app.database import SessionLocal
from app.models import Document, DocumentChunk, EmployeeProfile, ProductAttributes
from app.product_run_stream import RunStatus, ProductRunManager


DEFAULT_QUESTION = (
    "我要为美国 Amazon 站选一批户外照明产品（露营灯、头灯），带电产品需要提供 FCC 和 UN38.3 检测报告，"
    "样品报价毛利率不低于 40%。请基于报价单、检测报告和用户评价给出候选商品的证据、风险与推荐结论"
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="验证真实选品评估 Run 链路")
    parser.add_argument("--question", default=DEFAULT_QUESTION)
    parser.add_argument("--max-concurrency", type=int, default=6)
    return parser


def _data_inventory() -> dict[str, int]:
    with SessionLocal() as db:
        return {
            "products": db.scalar(select(func.count()).select_from(EmployeeProfile)) or 0,
            "product_attributes": db.scalar(select(func.count()).select_from(ProductAttributes)) or 0,
            "documents": db.scalar(select(func.count()).select_from(Document)) or 0,
            "document_chunks": db.scalar(select(func.count()).select_from(DocumentChunk)) or 0,
        }


async def verify(
    question: str,
    max_concurrency: int,
) -> dict[str, object]:
    manager = ProductRunManager(max_concurrency=max_concurrency)
    started = perf_counter()
    run = await manager.create_run(question)
    cursor = 0
    while manager.get_run(run.run_id).status == RunStatus.RUNNING:
        await manager.wait_for_change(run.run_id, cursor, timeout=10)
        events = manager.events_since(run.run_id, cursor)
        if events:
            cursor = events[-1].sequence

    current = manager.get_run(run.run_id)
    events = manager.events_since(run.run_id, 0)
    report_event = next(
        (event for event in events if event.type == "report.completed"),
        None,
    )
    event_types = [event.type for event in events]
    if current.status != RunStatus.COMPLETED:
        failure = next(
            (event.data.get("message") for event in reversed(events) if event.type == "run.failed"),
            None,
        )
        raise AssertionError(f"Run 未完成: {current.status}; {failure}")
    if current.branch_total < 1 or current.branch_completed != current.branch_total:
        raise AssertionError(
            "评估分支未全部完成: "
            f"total={current.branch_total}, completed={current.branch_completed}, "
            f"graph_status={current.graph_status}, events={event_types}"
        )
    if current.extraction_failures:
        raise AssertionError(f"存在 {current.extraction_failures} 个取证失败")
    if report_event is None or not report_event.data.get("citations"):
        raise AssertionError("报告或可回溯引用缺失")
    if [event.sequence for event in events] != list(range(1, len(events) + 1)):
        raise AssertionError("事件顺序号不连续")
    required = {
        "run.started",
        "step.started",
        "step.progress",
        "step.completed",
        "report.completed",
        "run.completed",
    }
    if missing := required.difference(event_types):
        raise AssertionError(f"事件链路不完整: {sorted(missing)}")
    return {
        "evaluation_run_id": run.run_id,
        "status": current.status,
        "elapsed_seconds": round(perf_counter() - started, 2),
        "data_inventory": _data_inventory(),
        "event_count": len(events),
        "branch_total": current.branch_total,
        "branch_status_counts": current.branch_status_counts,
        "extraction_failures": current.extraction_failures,
        "citation_count": len(report_event.data["citations"]),
        "report_chars": len(str(report_event.data["markdown"])),
    }


def main() -> None:
    args = _parser().parse_args()
    result = asyncio.run(
        verify(
            args.question,
            args.max_concurrency,
        )
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, default=str))


if __name__ == "__main__":
    main()
