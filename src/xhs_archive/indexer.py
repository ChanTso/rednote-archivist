from __future__ import annotations

import csv
import json
from pathlib import Path

from .db import ArchiveDB
from .ocr_flags import is_user_discarded_ocr
from .title import extract_light_structured
from .utils import read_json, write_json


CATALOG_FIELDS = [
    "note_id",
    "summary_title",
    "original_title",
    "author_name",
    "author_display_id",
    "author_profile_id",
    "publish_time_raw",
    "publish_time_iso",
    "company",
    "position",
    "interview_round",
    "result",
    "image_count",
    "ocr_status",
    "needs_review",
    "source_url",
    "output_dir",
]


def export_index(db: ArchiveDB, exports_dir: Path) -> dict:
    exports_dir.mkdir(parents=True, exist_ok=True)
    rows = db.list_notes(None)
    catalog: list[dict] = []
    failures: list[dict] = []
    reviews: list[dict] = []
    for row in rows:
        output_dir = Path(row["output_dir"] or "")
        raw = {}
        if output_dir.exists() and (output_dir / "raw.json").exists():
            loaded = read_json(output_dir / "raw.json")
            raw = loaded if isinstance(loaded, dict) else {}
        text = (row["body_text"] or "") + "\n"
        if output_dir.exists() and (output_dir / "ocr.json").exists():
            try:
                ocr_data = read_json(output_dir / "ocr.json")
                if isinstance(ocr_data, list):
                    text += "\n".join(
                        str(item.get("final_text") if item.get("final_text") is not None else item.get("text", ""))
                        for item in ocr_data
                        if isinstance(item, dict) and not is_user_discarded_ocr(item)
                    )
            except Exception:
                pass
        structured = extract_light_structured(text)
        images = db.list_images(row["note_id"])
        needs_review = any(image["ocr_status"] in {"failed", "needs_review", "vl_pending"} for image in images)
        item = {
            "note_id": row["note_id"],
            "summary_title": row["summary_title"] or raw.get("summary_title"),
            "original_title": row["title"] or raw.get("title"),
            "author_name": row["author_name"],
            "author_display_id": row["author_display_id"],
            "author_profile_id": row["author_profile_id"],
            "publish_time_raw": row["publish_time_raw"],
            "publish_time_iso": row["publish_time_iso"],
            "company": structured["company"],
            "position": structured["position"],
            "interview_round": structured["interview_round"],
            "result": structured["result"],
            "image_count": len(images),
            "ocr_status": "success" if images and all(image["ocr_status"] == "success" for image in images) else "pending",
            "needs_review": needs_review,
            "source_url": row["canonical_url"] or row["url"],
            "output_dir": row["output_dir"],
        }
        catalog.append(item)
        if row["stage"] == "failed":
            failures.append({**item, "last_error": row["last_error"]})
        if needs_review:
            reviews.append(item)

    catalog_csv = exports_dir / "catalog.csv"
    with catalog_csv.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=CATALOG_FIELDS)
        writer.writeheader()
        writer.writerows(catalog)
    catalog_jsonl = exports_dir / "catalog.jsonl"
    with catalog_jsonl.open("w", encoding="utf-8") as fh:
        for item in catalog:
            fh.write(json.dumps(item, ensure_ascii=False) + "\n")
    _write_rows(exports_dir / "failures.csv", failures)
    _write_rows(exports_dir / "needs_review.csv", reviews)
    index_md = exports_dir / "INDEX.md"
    index_md.write_text(_render_index_md(catalog), encoding="utf-8")
    report = {
        "stats": db.status_counts(),
        "catalog_count": len(catalog),
        "failure_count": len(failures),
        "needs_review_count": len(reviews),
        "paths": {
            "catalog_csv": str(catalog_csv),
            "catalog_jsonl": str(catalog_jsonl),
            "index_md": str(index_md),
            "failures_csv": str(exports_dir / "failures.csv"),
            "needs_review_csv": str(exports_dir / "needs_review.csv"),
            "run_report": str(exports_dir / "run_report.json"),
        },
    }
    write_json(exports_dir / "run_report.json", report)
    return report


def _write_rows(path: Path, rows: list[dict]) -> None:
    fields = list(rows[0].keys()) if rows else CATALOG_FIELDS
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _render_index_md(catalog: list[dict]) -> str:
    lines = ["# 小红书面经归档索引", ""]
    for field, title in [("company", "公司"), ("position", "岗位"), ("interview_round", "面试轮次"), ("publish_time_iso", "发布时间")]:
        lines.extend([f"## {title}", ""])
        groups: dict[str, list[dict]] = {}
        for item in catalog:
            groups.setdefault(item.get(field) or "未分类", []).append(item)
        for group, items in sorted(groups.items()):
            lines.extend([f"### {group}", ""])
            for item in items:
                lines.append(f"- [{item.get('summary_title') or item['note_id']}]({item.get('output_dir')}/note.md)")
            lines.append("")
    lines.extend(["## 待复核", ""])
    for item in catalog:
        if item.get("needs_review"):
            lines.append(f"- [{item.get('summary_title') or item['note_id']}]({item.get('output_dir')}/note.md)")
    return "\n".join(lines).rstrip() + "\n"
