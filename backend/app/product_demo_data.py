"""选品评估样例数据 Seed（第 1 阶段：暂借现有表承载选品数据）。

表结构沿用人才评估阶段，本阶段不新增表、也不使用 job_descriptions：
- ``employee_profiles`` 暂存候选商品（employee_no → SKU、region → 目标市场、
  department → 品类、current_position → 子类），内部键名保持不变。
- 选品模板概念已移除：需求理解阶段直接把用户自然语言需求编译成 QueryPlan 筛选 SKU。
"""
from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import EmployeeProfile

# 候选商品可更新字段（与 employee_profiles 表字段一致）
SKU_FIELDS = (
    "tenant_id",
    "name",
    "region",
    "current_position",
    "department",
    "job_level",
    "employment_status",
)


def seed_product_skus(
    records: Sequence[dict[str, Any]],
    *,
    session_factory: Callable[[], Session] = SessionLocal,
) -> dict[str, int]:
    """按 SKU 编号（存于 employee_no 字段）幂等导入候选商品。"""
    inserted = 0
    updated = 0
    with session_factory() as db:
        for record in records:
            sku = db.scalar(
                select(EmployeeProfile).where(EmployeeProfile.employee_no == record["employee_no"])
            )
            if sku is None:
                db.add(EmployeeProfile(**record))
                inserted += 1
                continue
            for field in SKU_FIELDS:
                setattr(sku, field, record[field])
            updated += 1
        db.commit()
    return {"inserted": inserted, "updated": updated}
