"""选品样例 SKU 数据 Seed 的幂等性测试（第 1 阶段：暂借 employee_profiles 承载 SKU）。"""
from __future__ import annotations

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base, EmployeeProfile
from app.product_demo_data import seed_product_skus


def _session_factory(engine):
    return lambda: Session(engine)


def _sku_records():
    return [
        {
            "employee_no": "SKU-CL-001",
            "tenant_id": "course-demo",
            "name": "星野 Lumina-1 户外露营灯",
            "region": "美国",
            "current_position": "充电式露营灯",
            "department": "户外照明",
            "job_level": None,
            "employment_status": "active",
        },
        {
            "employee_no": "SKU-KT-001",
            "tenant_id": "course-demo",
            "name": "小贝比硅胶折叠儿童餐具",
            "region": "欧盟",
            "current_position": "儿童硅胶餐具",
            "department": "儿童厨房用品",
            "job_level": None,
            "employment_status": "active",
        },
    ]


def test_seed_product_skus_is_idempotent_and_updates_existing_rows():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    session_factory = _session_factory(engine)
    records = _sku_records()

    assert seed_product_skus(records, session_factory=session_factory) == {
        "inserted": 2,
        "updated": 0,
    }
    records[0]["region"] = "日本"
    assert seed_product_skus(records, session_factory=session_factory) == {
        "inserted": 0,
        "updated": 2,
    }

    with Session(engine) as db:
        skus = db.scalars(
            select(EmployeeProfile).order_by(EmployeeProfile.employee_no)
        ).all()
        assert len(skus) == 2
        assert skus[0].region == "日本"


def test_sample_skus_json_loads_and_covers_required_fields():
    """samples/product/skus.json 必须可被 seed 直接使用，且包含带电品类样例。"""
    import json
    from pathlib import Path

    path = Path(__file__).resolve().parents[1] / "samples" / "product" / "skus.json"
    records = json.loads(path.read_text(encoding="utf-8"))
    assert len(records) >= 3
    required = {
        "employee_no",
        "tenant_id",
        "name",
        "region",
        "department",
        "current_position",
        "employment_status",
    }
    for record in records:
        assert required <= set(record), record
    # 覆盖带电（锂电池）品类与不同目标市场，供埋坑材料演示
    departments = {record["department"] for record in records}
    assert "户外照明" in departments
    assert len({record["region"] for record in records}) >= 2
    assert len({record["tenant_id"] for record in records}) >= 2
