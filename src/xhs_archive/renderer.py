from __future__ import annotations

from pathlib import Path
import re

import yaml

from .db import ArchiveDB
from .ocr_flags import is_user_discarded_ocr, manual_review_decision
from .title import extract_light_structured
from .utils import now_iso, read_json


def _load_ocr(path: Path) -> list[dict]:
    if not path.exists():
        return []
    data = read_json(path)
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def _ocr_text(result: dict) -> str:
    return str(result.get("final_text") if result.get("final_text") is not None else result.get("text") or "")


def _ocr_index(result: dict) -> int | str:
    return result.get("image_index") or result.get("index") or "?"


def _ocr_needs_review(result: dict) -> bool:
    if manual_review_decision(result):
        return False
    if "needs_human_review" in result:
        return bool(result.get("needs_human_review"))
    return bool(result.get("needs_review") or result.get("status") not in {"success", None})


def _ocr_engines(result: dict) -> list[str]:
    if isinstance(result.get("engines"), dict):
        return [str(value.get("engine") or key) for key, value in result["engines"].items() if isinstance(value, dict)]
    engine = result.get("engine")
    return [str(engine)] if engine else []


def _ocr_confidence(result: dict) -> float | None:
    value = result.get("agreement_score")
    if value is None:
        value = result.get("mean_confidence")
    return float(value) if isinstance(value, (int, float)) else None


def render_note(output_dir: Path) -> Path:
    raw_path = output_dir / "raw.json"
    raw = read_json(raw_path)
    assert isinstance(raw, dict)
    ocr_results = _load_ocr(output_dir / "ocr.json")
    ocr_text = "\n".join(_ocr_text(result) for result in ocr_results if not is_user_discarded_ocr(result) and _ocr_text(result))
    structured = extract_light_structured((raw.get("body_text") or "") + "\n" + ocr_text)
    needs_review = any(_ocr_needs_review(result) for result in ocr_results)
    engines = sorted({engine for result in ocr_results for engine in _ocr_engines(result)})
    image_count = int(raw.get("image_count") or len(raw.get("image_urls") or []))

    front_matter = {
        "note_id": raw.get("note_id"),
        "source_url": raw.get("canonical_url") or raw.get("url"),
        "original_title": raw.get("title"),
        "summary_title": raw.get("summary_title"),
        "author_name": raw.get("author_name"),
        "author_display_id": raw.get("author_display_id"),
        "author_profile_id": raw.get("author_profile_id"),
        "publish_time_raw": raw.get("publish_time_raw"),
        "publish_time_iso": raw.get("publish_time_iso"),
        "collected_at": raw.get("collected_at"),
        "image_count": image_count,
        "ocr_engines": engines,
        "needs_review": needs_review,
    }
    lines = ["---", yaml.safe_dump(front_matter, allow_unicode=True, sort_keys=False).strip(), "---", ""]
    lines.extend(
        [
            f"# {raw.get('summary_title') or raw.get('title') or raw.get('note_id')}",
            "",
            "## 原帖信息",
            "",
            f"- 原标题：{raw.get('title') or ''}",
            f"- 作者昵称：{raw.get('author_name') or ''}",
            f"- 小红书号：{raw.get('author_display_id') or ''}",
            f"- 作者 Profile ID：{raw.get('author_profile_id') or ''}",
            f"- 发布时间：{raw.get('publish_time_raw') or ''}",
            f"- 原帖链接：{raw.get('canonical_url') or raw.get('url') or ''}",
            "",
            "## 原文",
            "",
            raw.get("body_text") or "",
            "",
            "## 图片文字",
            "",
        ]
    )
    if ocr_results:
        for result in ocr_results:
            if is_user_discarded_ocr(result):
                lines.extend(
                    [
                        f"### 图片 {_ocr_index(result)}",
                        "",
                        "用户已确认该图无归档价值；OCR 文本已从正文、索引和人工复核清单中排除。",
                        "",
                    ]
                )
                continue
            confidence = _ocr_confidence(result)
            uncertain_spans = result.get("uncertain_spans") if isinstance(result.get("uncertain_spans"), list) else []
            lines.extend(
                [
                    f"### 图片 {_ocr_index(result)}",
                    "",
                    _ocr_text(result),
                    "",
                    f"一致性/置信度：{'' if confidence is None else round(confidence, 4)}  ",
                    f"识别引擎：{', '.join(_ocr_engines(result))}  ",
                    f"需要复核：{'是' if _ocr_needs_review(result) else '否'}",
                    "",
                ]
            )
            if uncertain_spans:
                lines.extend(["争议片段：", ""])
                for span in uncertain_spans:
                    if not isinstance(span, dict):
                        continue
                    candidates = span.get("candidates") if isinstance(span.get("candidates"), list) else []
                    candidate_texts = [str(item.get("text")) for item in candidates if isinstance(item, dict) and item.get("text")]
                    marker = "｜".join(candidate_texts[:3])
                    selected = span.get("selected") or ""
                    lines.append(f"- [{marker}] → {selected}；原因：{span.get('reason') or ''}；局部图：{span.get('crop_path') or ''}")
                lines.append("")
    else:
        lines.extend(["未执行 OCR。", ""])
    lines.extend(
        [
            "## 轻量结构化信息",
            "",
            f"- 公司：{structured['company'] or '未确认'}",
            f"- 岗位：{structured['position'] or '未确认'}",
            f"- 城市：{structured['city'] or '未确认'}",
            f"- 面试轮次：{structured['interview_round'] or '未确认'}",
            f"- 题型：{structured['question_type'] or '未确认'}",
            f"- 面试结果：{structured['result'] or '未确认'}",
            "- 识别置信度：未确认",
            "",
            "## 原图",
            "",
        ]
    )
    image_files = sorted(
        path.name
        for path in output_dir.iterdir()
        if path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"} and re.match(r"^\d{2}\.", path.name)
    )
    for index, name in enumerate(image_files, start=1):
        lines.extend([f"![图片 {index}]({name})", ""])
    note_path = output_dir / "note.md"
    note_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return note_path


def render_pending(db: ArchiveDB, *, limit: int | None = None) -> dict:
    rows = db.list_notes(["ocr_done", "fetched"], limit=limit)
    rendered: list[str] = []
    failed: list[dict] = []
    for row in rows:
        output_dir = Path(row["output_dir"] or "")
        try:
            if not output_dir.exists():
                raise FileNotFoundError(f"output_dir missing: {output_dir}")
            render_note(output_dir)
            db.update_note_fields(row["note_id"], stage="rendered", rendered_at=now_iso())
            rendered.append(row["note_id"])
        except Exception as exc:
            db.mark_failed(row["note_id"], "render", f"{type(exc).__name__}: {exc}", "render_failed")
            failed.append({"note_id": row["note_id"], "error": f"{type(exc).__name__}: {exc}"})
    return {"ok": not failed, "rendered": rendered, "failed": failed}
