"""导入选品评估虚构样例：候选商品（SKU）、数值属性、必需认证规则与已登记认证。

用法（在 backend/ 目录下执行，Windows / Linux 通用）：
    uv run --no-sync python -m scripts.seed_product_samples
"""
from __future__ import annotations

import json
from pathlib import Path

from app.product_demo_data import (
    seed_certification_rules,
    seed_product_attributes,
    seed_product_certifications,
    seed_product_skus,
)


SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples" / "product"


def main() -> None:
    skus = json.loads((SAMPLES_DIR / "skus.json").read_text(encoding="utf-8"))
    def load(name: str):
        return json.loads((SAMPLES_DIR / name).read_text(encoding="utf-8"))

    result = {
        "skus": {**seed_product_skus(skus), "total": len(skus)},
        "attributes": seed_product_attributes(load("product_attributes.json")),
        "rules": seed_certification_rules(load("certification_rules.json")),
        "certifications": seed_product_certifications(load("product_certifications.json")),
    }
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
