"""选品评估样例数据 Seed。

第 2 阶段新增：``product_attributes``（售价、到岸成本、带电/锂电/儿童/食品接触等属性）、
``certification_rules``（市场×品类→必需认证）与 ``product_certifications``（从材料登记的认证文件）。

以下为第 1 阶段说明：

SKU 基础信息沿用 employee_profiles 表：
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
from datetime import date

from app.models import CertificationRule, EmployeeProfile, ProductAttributes, ProductCertification

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


ATTRIBUTE_FIELDS = (
    "model_no", "currency", "target_price", "landed_cost", "is_electrical",
    "is_wireless", "has_lithium_battery", "for_children", "food_contact",
)


def _product_ids(db: Session, skus: set[str]) -> dict[str, Any]:
    rows = db.execute(select(EmployeeProfile.employee_no, EmployeeProfile.id).where(EmployeeProfile.employee_no.in_(skus))).all()
    found = {sku: product_id for sku, product_id in rows}
    missing = skus - set(found)
    if missing:
        raise ValueError(f"请先导入 SKU：{sorted(missing)}")
    return found


def seed_product_attributes(records: Sequence[dict[str, Any]], *, session_factory: Callable[[], Session] = SessionLocal) -> dict[str, int]:
    """按 SKU 幂等写入数值与属性；同一 SKU 的售价与成本必须同币种。"""
    upserted = 0
    with session_factory() as db:
        ids = _product_ids(db, {item["sku"] for item in records})
        for record in records:
            values = {field: record[field] for field in ATTRIBUTE_FIELDS}
            row = db.get(ProductAttributes, ids[record["sku"]])
            if row is None:
                db.add(ProductAttributes(product_id=ids[record["sku"]], **values))
            else:
                for field, value in values.items():
                    setattr(row, field, value)
            upserted += 1
        db.commit()
    return {"upserted": upserted}


def seed_certification_rules(records: Sequence[dict[str, Any]], *, session_factory: Callable[[], Session] = SessionLocal) -> dict[str, int]:
    """规则表整体替换为样例内容（规则以文件为准，便于审阅与回滚）。"""
    with session_factory() as db:
        db.query(CertificationRule).delete()
        db.add_all(CertificationRule(**record) for record in records)
        db.commit()
    return {"rules": len(records)}


def seed_product_certifications(records: Sequence[dict[str, Any]], *, session_factory: Callable[[], Session] = SessionLocal) -> dict[str, int]:
    """按 SKU 整体替换已登记认证文件。"""
    with session_factory() as db:
        ids = _product_ids(db, {item["sku"] for item in records})
        db.query(ProductCertification).filter(ProductCertification.product_id.in_(ids.values())).delete(synchronize_session=False)
        for record in records:
            db.add(ProductCertification(
                product_id=ids[record["sku"]],
                certification=record["certification"],
                document_no=record["document_no"],
                applicable_model=record.get("applicable_model"),
                issued_on=date.fromisoformat(record["issued_on"]) if record.get("issued_on") else None,
                valid_until=date.fromisoformat(record["valid_until"]) if record.get("valid_until") else None,
                source_material=record["source_material"],
            ))
        db.commit()
    return {"certifications": len(records)}
