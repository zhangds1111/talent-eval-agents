"""导入选品评估虚构样例：候选商品（SKU）。

用法（在 backend/ 目录下执行，Windows / Linux 通用）：
    uv run --no-sync python -m scripts.seed_product_samples
"""
from __future__ import annotations

import json
from pathlib import Path

from app.product_demo_data import seed_product_skus


SAMPLES_DIR = Path(__file__).resolve().parents[1] / "samples" / "product"


def main() -> None:
    skus = json.loads((SAMPLES_DIR / "skus.json").read_text(encoding="utf-8"))
    sku_result = seed_product_skus(skus)
    print(
        json.dumps(
            {"skus": {**sku_result, "total": len(skus)}},
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()
