import json
import mimetypes
import re
import sys
from collections import Counter
from pathlib import Path

import httpx


ROOT = Path(__file__).resolve().parents[2]
DATA = ROOT / "sample-data" / "generated"
API = "http://127.0.0.1:18080/api"
MAIN_SUFFIXES = {".pdf", ".docx", ".xlsx", ".pptx", ".png", ".wav", ".csv", ".json", ".html", ".md"}


def main() -> None:
    skus = {item["sku_id"]: item for item in json.loads((ROOT / "sample-data/source/skus.json").read_text())}
    files = sorted(path for path in DATA.glob("*/*") if path.suffix.lower() in MAIN_SUFFIXES)
    results = []
    with httpx.Client(timeout=900) as client:
        for path in files:
            sku_id = re.match(r"(C\d{3})_", path.name).group(1)
            sku = skus[sku_id]
            response = client.post(
                f"{API}/documents",
                files={"file": (path.name, path.read_bytes(), mimetypes.guess_type(path)[0] or "application/octet-stream")},
                data={"sku_id": sku_id, "title": path.stem, "document_type": path.parent.name},
            )
            response.raise_for_status()
            document = response.json()
            parse_response = client.post(f"{API}/documents/{document['id']}/parse?async_mode=false")
            parse_response.raise_for_status()
            job = parse_response.json()
            results.append({"file": path.name, "document_id": document["id"], **job})
            print(f"{len(results):02d}/{len(files)} {path.parent.name:8s} {job['status']:16s} {job['parser_name']} {path.name}", flush=True)
    report = {"total": len(results), "statuses": Counter(row["status"] for row in results), "parsers": Counter(row["parser_name"] for row in results), "items": results}
    report["statuses"] = dict(report["statuses"])
    report["parsers"] = dict(report["parsers"])
    output = ROOT / "sample-data/qa/e2e-report.json"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("total", "statuses", "parsers")}, ensure_ascii=False, indent=2))
    if report["statuses"].get("failed"):
        sys.exit(1)


if __name__ == "__main__":
    main()
