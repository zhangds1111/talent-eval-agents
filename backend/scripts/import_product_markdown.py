"""把选品样例 Markdown 材料导入系统（上传 → 解析 → 切分 → 向量化）。

数据源：backend/samples/product/markdown/ 下的虚构 SKU 材料。
前置条件：后端 API、Postgres、Redis、MinIO、Milvus 已启动，.env 已配置 DASHSCOPE_API_KEY。

用法（在 backend/ 目录下执行，Windows / Linux 通用）：
    uv run --no-sync python scripts/import_product_markdown.py
可选环境变量 PRODUCT_API（默认 http://127.0.0.1:18080/api）。
"""
import re
import sys
import time
from pathlib import Path

import os

import httpx


BACKEND_DIR = Path(__file__).resolve().parents[1]
DATA = BACKEND_DIR / "samples" / "product" / "markdown"
API = os.getenv("PRODUCT_API", "http://127.0.0.1:18080/api")
HEADERS = {"X-Tenant-ID": "course-demo", "X-Permission-Scopes": "hr_private"}
# 文件名后缀到 document_type 的映射（kind 为 SKU 材料类型）
KIND_TO_DOC_TYPE = {
    "quotation": "product_quotation",
    "test_report": "test_report",
    "certificate": "certificate",
    "product_page": "product_page",
    "reviews": "reviews",
}


def wait_for(condition, timeout=120, interval=1.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        result = condition()
        if result is not None:
            return result
        time.sleep(interval)
    return None


def main() -> None:
    files = sorted(DATA.glob("*.md"))
    if not files:
        print(f"未找到样例材料：{DATA}", file=sys.stderr)
        sys.exit(1)
    client = httpx.Client(timeout=300)
    employees = {item["employee_no"]: item["id"] for item in client.get(f"{API}/employees", headers=HEADERS).json()}
    knowledge_bases = client.get(f"{API}/knowledge-bases", headers=HEADERS).json()
    knowledge_base_id = knowledge_bases[0]["id"] if knowledge_bases else None
    succeeded = 0
    for path in files:
        match = re.match(r"(SKU-[A-Z]{2}-\d{3})_(\w+)\.md$", path.name)
        if not match:
            print(f"跳过无法识别的文件名：{path.name}", file=sys.stderr)
            continue
        sku_id, kind = match.group(1), match.group(2)
        document_type = KIND_TO_DOC_TYPE.get(kind)
        if document_type is None:
            print(f"跳过未知材料类型：{path.name}", file=sys.stderr)
            continue
        form = {"sku_id": sku_id, "title": path.stem, "document_type": document_type}
        if employees.get(sku_id):
            form["employee_id"] = employees[sku_id]
        if knowledge_base_id:
            form["knowledge_base_id"] = knowledge_base_id
        upload = client.post(
            f"{API}/documents",
            files={"file": (path.name, path.read_bytes(), "text/markdown")},
            data=form,
            headers=HEADERS,
        )
        upload.raise_for_status()
        document_id = upload.json()["id"]

        def parse_done():
            detail = client.get(f"{API}/documents/{document_id}", headers=HEADERS).json()
            status = detail["parse_status"]
            return detail if status in {"succeeded", "failed"} else None

        detail = wait_for(parse_done)
        if not detail or detail["parse_status"] != "succeeded":
            print(f"{path.name}: parse {detail['parse_status'] if detail else 'timeout'}", file=sys.stderr)
            continue

        chunk = client.post(
            f"{API}/documents/{document_id}/chunks",
            json={"chunk_size": 800, "chunk_overlap": 100},
            headers=HEADERS,
        )
        chunk.raise_for_status()

        index = client.post(f"{API}/documents/{document_id}/evidence-index", headers=HEADERS)
        index.raise_for_status()

        def index_done():
            item = client.get(f"{API}/documents/{document_id}", headers=HEADERS).json()
            status = item["index_status"]
            return item if status in {"succeeded", "failed"} else None

        final = wait_for(index_done)
        if not final or final["index_status"] != "succeeded":
            print(f"{path.name}: index {final['index_status'] if final else 'timeout'}", file=sys.stderr)
            continue
        succeeded += 1
        print(
            f"{sku_id}  {path.stem}  chunk={chunk.json()['strategy']}  index={final['index_status']}",
            flush=True,
        )
    print(f"导入完成：{succeeded}/{len(files)} 份材料")
    if succeeded != len(files):
        sys.exit(1)


if __name__ == "__main__":
    main()
