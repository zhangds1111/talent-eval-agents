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


def test_gross_margin_filter_is_sql_computed(session_factory):
    from app.query_plan import FilterCondition, QueryPlan, TaskType, select_candidate_ids

    plan = QueryPlan(
        task_type=TaskType.FIND_PRODUCT,
        filters=[
            FilterCondition(field="region", operator="eq", value="美国"),
            FilterCondition(field="department", operator="eq", value="户外照明"),
            FilterCondition(field="gross_margin_percent", operator="gte", value=40),
        ],
    )
    with session_factory() as db:
        ids = select_candidate_ids(db, plan, tenant_id="course-demo")
    assert ids == ["SKU-CL-001"]  # SKU-CL-002 毛利 33.8% 被 SQL 排除


def test_price_filter_rejects_non_numeric_operator():
    from app.query_plan import FilterCondition

    with pytest.raises(ValueError):
        FilterCondition(field="region", operator="gte", value=1)
    assert FilterCondition(field="target_price", operator="lte", value=20).value == 20


def test_ranking_puts_certification_gaps_after_and_report_renders_program_facts():
    from app.product_compliance import CertificationCheck, ProductFacts
    from app.product_evaluation_report import CandidateAssessment, _render_product_facts, rank_candidate_assessments

    def candidate(cid, score, statuses):
        facts = ProductFacts(
            candidate_id=cid,
            unit_economics=compute_unit_economics(currency="USD", target_price=20, landed_cost=11),
            certification_checks=[
                CertificationCheck(certification=f"C{i}", rationale="r", status=st, detail="d")
                for i, st in enumerate(statuses)
            ],
        )
        return CandidateAssessment(candidate_id=cid, confirmed_score=score, possible_score=score,
                                   evidence_coverage_percent=100, confidence="high", dimensions=[], product_facts=facts)

    high_but_gap = candidate("A", 90, ["evidenced", "expired"])
    lower_clean = candidate("B", 60, ["evidenced"])
    unknown = CandidateAssessment(candidate_id="C", confirmed_score=70, possible_score=70,
                                  evidence_coverage_percent=100, confidence="high", dimensions=[])
    assert [c.candidate_id for c in rank_candidate_assessments([high_but_gap, lower_clean, unknown])] == ["C", "B", "A"]
    text = "\n".join(_render_product_facts(high_but_gap.product_facts))
    assert "毛利率 **45.0%**" in text and "缺有效证据：已过期" in text


def test_market_aliases_are_normalized_before_sql(session_factory):
    from app.query_plan import FilterCondition, QueryPlan, TaskType, select_candidate_ids

    plan = QueryPlan(task_type=TaskType.FIND_PRODUCT, filters=[
        FilterCondition(field="region", operator="eq", value="US"),
        FilterCondition(field="gross_margin_percent", operator="gte", value=40),
    ])
    with session_factory() as db:
        assert select_candidate_ids(db, plan, tenant_id="course-demo") == ["SKU-CL-001", "SKU-PT-001"]
