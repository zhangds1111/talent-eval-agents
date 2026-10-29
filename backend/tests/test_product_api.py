"""商品库 API：创建 SKU 时写入数值属性，列表返回程序计算的毛利率。"""
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import get_db
from app.main import create_app
from app.models import Base


def _client():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    app = create_app()

    def db():
        with Session(engine) as session:
            yield session

    app.dependency_overrides[get_db] = db
    return TestClient(app)


def test_create_product_with_attributes_and_list_margin():
    client = _client()
    created = client.post("/api/employees", json={
        "employee_no": "SKU-T-1", "name": "测试露营灯", "region": "美国", "department": "户外照明",
        "model_no": "T-1", "currency": "USD", "target_price": 20, "landed_cost": 12, "is_electrical": True,
    })
    assert created.status_code == 201, created.text
    assert created.json()["gross_margin_percent"] == 40.0
    listed = client.get("/api/employees").json()
    assert listed[0]["employee_no"] == "SKU-T-1"
    assert listed[0]["gross_margin_percent"] == 40.0


def test_partial_numeric_attributes_are_rejected():
    client = _client()
    response = client.post("/api/employees", json={"employee_no": "SKU-T-2", "name": "x", "target_price": 10})
    assert response.status_code == 422
