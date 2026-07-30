from __future__ import annotations

import re
from pathlib import Path

import yaml
from PIL import Image

from .db import ArchiveDB
from .models import VerifyIssue, VerifyReport
from .utils import read_json


def _issue(note_id: str | None, path: Path | None, code: str, message: str) -> VerifyIssue:
    return VerifyIssue(note_id=note_id, path=str(path) if path else None, code=code, message=message)


def verify(db: ArchiveDB, *, limit: int | None = None) -> VerifyReport:
    rows = db.list_notes(None, limit=limit)
    issues: list[VerifyIssue] = []
    warnings: list[VerifyIssue] = []
    checked = 0
    for row in rows:
        checked += 1
        note_id = row["note_id"]
        output_dir = Path(row["output_dir"] or "")
        if not row["canonical_url"]:
            issues.append(_issue(note_id, None, "missing_canonical_url", "canonical_url is empty"))
        if not output_dir.exists():
            issues.append(_issue(note_id, output_dir, "missing_output_dir", "output directory does not exist"))
            continue
        raw_path = output_dir / "raw.json"
        html_path = output_dir / "page.html"
        ocr_path = output_dir / "ocr.json"
        md_path = output_dir / "note.md"
        raw = None
        if not raw_path.exists():
            issues.append(_issue(note_id, raw_path, "missing_raw_json", "raw.json is missing"))
        else:
            try:
                raw = read_json(raw_path)
            except Exception as exc:
                issues.append(_issue(note_id, raw_path, "invalid_raw_json", str(exc)))
        if not html_path.exists():
            issues.append(_issue(note_id, html_path, "missing_page_html", "page.html is missing"))
        image_files = sorted(
            path
            for path in output_dir.iterdir()
            if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and re.match(r"^\d{2}\.", path.name)
        )
        image_rows = db.list_images(note_id)
        expected_images = int(raw.get("image_count") or len(raw.get("image_urls") or [])) if isinstance(raw, dict) else 0
        if expected_images != len(image_files):
            issues.append(
                _issue(note_id, output_dir, "image_count_mismatch", f"expected {expected_images}, found {len(image_files)}")
            )
        if image_rows and len(image_rows) != len(image_files):
            issues.append(_issue(note_id, output_dir, "sqlite_image_count_mismatch", f"SQLite has {len(image_rows)} images, files have {len(image_files)}"))
        for image in image_files:
            try:
                with Image.open(image) as img:
                    img.verify()
                with Image.open(image) as img:
                    width, height = img.size
                image_row = next((item for item in image_rows if item["local_path"] == image.name), None)
                if image_row:
                    if image_row["sha256"] and len(str(image_row["sha256"])) < 32:
                        issues.append(_issue(note_id, image, "weak_image_sha256", "SQLite sha256 value is missing or invalid"))
                    if image_row["downloaded_width"] and int(image_row["downloaded_width"]) != width:
                        issues.append(_issue(note_id, image, "image_width_mismatch", f"SQLite width {image_row['downloaded_width']} != file width {width}"))
                    if image_row["downloaded_height"] and int(image_row["downloaded_height"]) != height:
                        issues.append(_issue(note_id, image, "image_height_mismatch", f"SQLite height {image_row['downloaded_height']} != file height {height}"))
                    if not image_row["file_size"]:
                        issues.append(_issue(note_id, image, "missing_image_file_size", "SQLite file_size is missing"))
                    if image_row["is_probable_thumbnail"]:
                        warnings.append(_issue(note_id, image, "thumbnail_suspected", image_row["thumbnail_reason"] or "image is flagged as probable thumbnail"))
            except Exception as exc:
                issues.append(_issue(note_id, image, "invalid_image", str(exc)))
        if not ocr_path.exists():
            issues.append(_issue(note_id, ocr_path, "missing_ocr_json", "ocr.json is missing"))
        else:
            try:
                ocr_data = read_json(ocr_path)
                if not isinstance(ocr_data, list):
                    issues.append(_issue(note_id, ocr_path, "invalid_ocr_json", "ocr.json is not a list"))
                else:
                    for item in ocr_data:
                        if not isinstance(item, dict):
                            issues.append(_issue(note_id, ocr_path, "invalid_ocr_item", "OCR item is not an object"))
                            continue
                        if "final_text" in item:
                            if not isinstance(item.get("engines"), dict):
                                issues.append(_issue(note_id, ocr_path, "missing_ocr_evidence", "audit OCR item has no engines evidence"))
                            for span in item.get("uncertain_spans") or []:
                                if isinstance(span, dict) and span.get("crop_path") and not (output_dir / str(span["crop_path"])).exists():
                                    issues.append(_issue(note_id, ocr_path, "missing_uncertain_crop", f"crop missing: {span['crop_path']}"))
                        elif "text" not in item:
                            issues.append(_issue(note_id, ocr_path, "invalid_legacy_ocr_item", "legacy OCR item has no text"))
            except Exception as exc:
                issues.append(_issue(note_id, ocr_path, "invalid_ocr_json", str(exc)))
        if not md_path.exists():
            issues.append(_issue(note_id, md_path, "missing_note_md", "note.md is missing"))
        else:
            text = md_path.read_text(encoding="utf-8")
            match = re.match(r"^---\n(.*?)\n---\n", text, re.S)
            if not match:
                issues.append(_issue(note_id, md_path, "missing_front_matter", "Markdown front matter is missing"))
            else:
                try:
                    yaml.safe_load(match.group(1))
                except Exception as exc:
                    issues.append(_issue(note_id, md_path, "invalid_front_matter", str(exc)))
            if isinstance(raw, dict) and raw.get("body_text") and raw["body_text"] not in text:
                issues.append(_issue(note_id, md_path, "markdown_body_missing", "Markdown does not contain full body_text"))
            md_image_refs = len(re.findall(r"!\[图片\s+\d+\]\(", text))
            if md_image_refs != len(image_files):
                issues.append(_issue(note_id, md_path, "markdown_image_ref_mismatch", f"expected {len(image_files)}, found {md_image_refs}"))
        if row["author_display_id"] in {None, "", row["author_name"]} and row["author_profile_id"] in {None, "", row["author_name"]}:
            issues.append(_issue(note_id, output_dir, "missing_author_stable_id", "No stable author display/profile id"))
    issue_note_ids = {issue.note_id for issue in issues if issue.note_id}
    for row in rows:
        if row["note_id"] not in issue_note_ids and row["stage"] in {"rendered", "ocr_done"}:
            db.update_note_fields(row["note_id"], stage="verified")
    return VerifyReport(ok=not issues, checked_notes=checked, issues=issues, warnings=warnings, stats=db.status_counts())
