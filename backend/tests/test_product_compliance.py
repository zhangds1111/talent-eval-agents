"""选品程序计算：毛利与必需认证缺口（不依赖 LLM）。"""
from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.models import Base
from app.product_compliance import compute_unit_economics, load_product_facts
from app.product_demo_data import (
    seed_certification_rules,
    seed_product_attributes,
    seed_product_certifications,
    seed_product_skus,
)

SAMPLES = Path(__file__).resolve().parents[1] / "samples" / "product"


def _load(name):
    return json.loads((SAMPLES / name).read_text(encoding="utf-8"))


@pytest.fixture()
def session_factory():
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    factory = lambda: Session(engine)  # noqa: E731
    seed_product_skus(_load("skus.json"), session_factory=factory)
    seed_product_attributes(_load("product_attributes.json"), session_factory=factory)
    seed_certification_rules(_load("certification_rules.json"), session_factory=factory)
    seed_product_certifications(_load("product_certifications.json"), session_factory=factory)
    return factory


def test_unit_economics_is_computed_not_guessed():
    result = compute_unit_economics(currency="USD", target_price=20.0, landed_cost=12.0)
    assert result.gross_profit == 8.0
    assert result.gross_margin_percent == 40.0
    with pytest.raises(ValueError):
        compute_unit_economics(currency="USD", target_price=0, landed_cost=1)


def test_sample_certification_gaps_match_planted_traps(session_factory):
    skus = ["SKU-CL-001", "SKU-CL-002", "SKU-PT-001", "SKU-KT-001", "SKU-AU-001"]
    with session_factory() as db:
        facts = load_product_facts(db, skus, today=date(2026, 10, 30))
    status = {sku: {c.certification: c.status for c in item.certification_checks} for sku, item in facts.items()}
    assert status["SKU-CL-001"] == {"FCC Part 15": "model_mismatch", "UN38.3": "expired"}
    assert status["SKU-CL-002"] == {"FCC Part 15": "missing", "UN38.3": "evidenced"}
    assert status["SKU-PT-001"] == {"FCC Part 15": "missing", "UN38.3": "evidenced"}
    assert status["SKU-KT-001"] == {"EU 1935/2004": "missing", "EN 14372": "missing"}
    assert status["SKU-AU-001"] == {"UN38.3": "evidenced", "CE-RED": "evidenced"}
    assert facts["SKU-AU-001"].certification_gaps == []
    assert facts["SKU-CL-002"].unit_economics.gross_margin_percent == 33.8


def test_product_without_attributes_has_no_fabricated_facts(session_factory):
    with session_factory() as db:
        facts = load_product_facts(db, ["SKU-CL-001"], today=date(2026, 10, 30))
        assert facts["SKU-CL-001"].unit_economics is not None
    seed_product_skus([{**_load("skus.json")[0], "employee_no": "SKU-NEW-1"}], session_factory=session_factory)
    with session_factory() as db:
        facts = load_product_facts(db, ["SKU-NEW-1"])
    assert facts["SKU-NEW-1"].unit_economics is None
    assert facts["SKU-NEW-1"].certification_checks == []
