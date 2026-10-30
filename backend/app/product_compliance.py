"""选品的程序计算部分：单位毛利与「市场×品类→必需认证」缺口。

这两项不交给 LLM：毛利由数值列直接计算；认证缺口由规则表与已登记的认证文件比对。
报告只写「有证据 / 缺证据」，不下合规结论。
"""
from __future__ import annotations

from datetime import date
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import CertificationRule, EmployeeProfile, ProductAttributes, ProductCertification

GapStatus = Literal["evidenced", "missing", "expired", "model_mismatch"]
_FLAG_BY_CONDITION = {
    "electrical": "is_electrical",
    "wireless": "is_wireless",
    "lithium_battery": "has_lithium_battery",
    "children": "for_children",
    "food_contact": "food_contact",
}


class UnitEconomics(BaseModel):
    currency: str
    target_price: float
    landed_cost: float
    gross_profit: float
    gross_margin_percent: float


class CertificationCheck(BaseModel):
    certification: str
    rationale: str
    status: GapStatus
    document_no: str | None = None
    detail: str


class ProductFacts(BaseModel):
    sku_id: str
    model_no: str | None = None
    unit_economics: UnitEconomics | None = None
    certification_checks: list[CertificationCheck] = []

    @property
    def certification_gaps(self) -> list[CertificationCheck]:
        return [item for item in self.certification_checks if item.status != "evidenced"]


def compute_unit_economics(*, currency: str, target_price: float, landed_cost: float) -> UnitEconomics:
    if target_price <= 0:
        raise ValueError("target_price 必须大于 0")
    profit = target_price - landed_cost
    return UnitEconomics(
        currency=currency,
        target_price=round(target_price, 2),
        landed_cost=round(landed_cost, 2),
        gross_profit=round(profit, 2),
        gross_margin_percent=round(profit / target_price * 100, 1),
    )


def rule_applies(rule: CertificationRule, *, market: str | None, category: str | None, attrs: ProductAttributes) -> bool:
    if rule.market not in ("*", market) or rule.category not in ("*", category):
        return False
    if rule.applies_when == "always":
        return True
    return bool(getattr(attrs, _FLAG_BY_CONDITION[rule.applies_when]))


def check_certifications(
    *,
    rules: list[CertificationRule],
    records: list[ProductCertification],
    market: str | None,
    category: str | None,
    attrs: ProductAttributes,
    today: date,
) -> list[CertificationCheck]:
    checks: list[CertificationCheck] = []
    seen: set[str] = set()
    for rule in rules:
        if rule.certification in seen or not rule_applies(rule, market=market, category=category, attrs=attrs):
            continue
        seen.add(rule.certification)
        skus = [item for item in records if item.certification == rule.certification]
        checks.append(_best_match(rule, skus, model_no=attrs.model_no, today=today))
    return checks


def _best_match(rule: CertificationRule, records: list[ProductCertification], *, model_no: str, today: date) -> CertificationCheck:
    base = {"certification": rule.certification, "rationale": rule.rationale}
    if not records:
        return CertificationCheck(**base, status="missing", detail="未登记任何对应的检测报告或证书")
    problems: list[CertificationCheck] = []
    for item in records:
        if item.valid_until is not None and item.valid_until < today:
            problems.append(CertificationCheck(**base, status="expired", document_no=item.document_no,
                                               detail=f"{item.document_no} 有效期至 {item.valid_until.isoformat()}，已过期"))
        elif item.applicable_model and item.applicable_model != model_no:
            problems.append(CertificationCheck(**base, status="model_mismatch", document_no=item.document_no,
                                               detail=f"{item.document_no} 适用型号 {item.applicable_model}，与商品型号 {model_no} 不符"))
        elif not item.applicable_model:
            problems.append(CertificationCheck(**base, status="model_mismatch", document_no=item.document_no,
                                               detail=f"{item.document_no} 未载明适用整机型号，无法对应 {model_no}"))
        else:
            return CertificationCheck(**base, status="evidenced", document_no=item.document_no,
                                      detail=f"{item.document_no} 适用型号 {model_no}，在有效期内")
    return problems[0]


def load_product_facts(db: Session, sku_ids: list[str], *, today: date | None = None) -> dict[str, ProductFacts]:
    current = today or date.today()
    rules = list(db.scalars(select(CertificationRule).order_by(CertificationRule.id)).all())
    rows = db.execute(
        select(EmployeeProfile, ProductAttributes)
        .outerjoin(ProductAttributes, ProductAttributes.product_id == EmployeeProfile.id)
        .where(EmployeeProfile.employee_no.in_(sku_ids))
    ).all()
    facts: dict[str, ProductFacts] = {}
    for product, attrs in rows:
        if attrs is None:
            facts[product.employee_no] = ProductFacts(sku_id=product.employee_no)
            continue
        records = list(db.scalars(select(ProductCertification).where(ProductCertification.product_id == product.id)).all())
        facts[product.employee_no] = ProductFacts(
            sku_id=product.employee_no,
            model_no=attrs.model_no,
            unit_economics=compute_unit_economics(
                currency=attrs.currency, target_price=float(attrs.target_price), landed_cost=float(attrs.landed_cost)
            ),
            certification_checks=check_certifications(
                rules=rules, records=records, market=product.region, category=product.department, attrs=attrs, today=current
            ),
        )
    return facts
