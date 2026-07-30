from __future__ import annotations

import csv
import html
import json
import os
import re
import unicodedata
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from PIL import Image

from .config import AppConfig, OCRConfig
from .db import ArchiveDB
from .ocr.audit import TECH_TERMS, block_bbox
from .ocr_flags import DISCARDED_MANUAL_STATUSES, manual_review_decision
from .utils import ensure_dir, read_json, write_json

HIGH_RISK_LEVELS = {
    "critical_token_conflict",
    "sentence_or_line_missing",
    "reading_order_conflict",
    "image_quality_risk",
    "three_way_conflict",
    "human_review_required",
}

SUMMARY_FIELDS = [
    "total_images",
    "previous_needs_review",
    "auto_accepted",
    "formatting_only",
    "minor_noncritical",
    "manual_accepted",
    "discarded_by_user",
    "sentence_or_line_missing",
    "critical_token_conflict",
    "reading_order_conflict",
    "image_quality_risk",
    "three_way_conflict",
    "human_review_required",
    "human_review_regions",
    "auto_cleared_from_previous_needs_review",
    "supported_term_corrections",
]

REVIEW_PRIORITIES = [
    "sentence_or_line_missing",
    "critical_token_conflict",
    "reading_order_conflict",
    "three_way_conflict",
    "image_quality_risk",
]

PUNCT_OR_SPACE_RE = re.compile(r"[\s`*_#>\-\[\](){}。，、！？；：：“”‘’「」『』《》〈〉（）【】,.!?;:\"'|/\\]+")
MARKDOWN_RE = re.compile(r"(```.*?```|`[^`]*`|\*\*|__|#+\s*|>\s*)", re.S)
URL_RE = re.compile(r"https?://[^\s)）]+", re.I)
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
ID_RE = re.compile(r"\b[A-Za-z]{1,8}[-_][A-Za-z0-9_-]{3,}\b")
ARABIC_NUMBER_RE = re.compile(r"(?<![A-Za-z0-9])\d+(?:\.\d+)?%?(?![A-Za-z0-9])")
DATE_RE = re.compile(r"(?:20\d{2}|19\d{2})(?:[年./-]\d{1,2})?(?:[月./-]\d{1,2}日?)?")
MONEY_RE = re.compile(r"(?:(?:￥|\$)\d+(?:\.\d+)?|\d+(?:\.\d+)?(?:万|千|元|块))")
COMPLEXITY_RE = re.compile(r"O\s*\([^)]{1,24}\)", re.I)
CHINESE_NUMERAL_RE = re.compile(r"[零一二三四五六七八九十百千万两]+(?:次|轮|面|年|月|日|个|道|小时|分钟)")
INTERVIEW_ROUND_RE = re.compile(r"(?:[一二三四五六七八九十123456789]\s*面|HR\s*面|hr\s*面|终面|群面)")

NEGATION_TERMS = {
    "不",
    "未",
    "无",
    "非",
    "不能",
    "没有",
    "没",
    "失败",
    "拒绝",
    "不可以",
    "未通过",
}

RESULT_TERMS = {
    "通过",
    "未通过",
    "不通过",
    "挂",
    "已挂",
    "凉",
    "oc",
    "OC",
    "offer",
    "拒",
    "拒绝",
}

COMPANY_TERMS = {
    "阿里",
    "阿里巴巴",
    "淘天",
    "腾讯",
    "字节",
    "字节跳动",
    "美团",
    "百度",
    "京东",
    "拼多多",
    "快手",
    "网易",
    "华为",
    "米哈游",
    "携程",
    "美图",
    "滴滴",
    "蚂蚁",
    "飞书",
    "腾讯音乐",
    "科大讯飞",
}

ROLE_TERMS = {
    "后端开发",
    "后端研发",
    "前端开发",
    "客户端开发",
    "服务端开发",
    "服务端研发",
    "全栈开发",
    "算法工程师",
    "测试开发",
    "数据开发",
    "AI应用开发",
    "Agent开发",
    "Java研发",
    "Java开发",
    "Go开发",
}

CITY_TERMS = {
    "北京",
    "上海",
    "深圳",
    "广州",
    "杭州",
    "南京",
    "成都",
    "武汉",
    "西安",
    "苏州",
    "重庆",
}

TECH_EXTRA_TERMS = {
    "缓存穿透",
    "缓存击穿",
    "缓存雪崩",
    "一致性哈希",
    "分布式锁",
    "线程池",
    "事务",
    "索引",
    "B+树",
    "红黑树",
    "Raft",
    "Paxos",
    "CAP",
    "BASE",
    "MVCC",
    "GC",
    "NIO",
    "Netty",
    "Dubbo",
    "gRPC",
    "REST",
    "SQL",
    "NoSQL",
    "LRU",
    "LFU",
    "BFS",
    "DFS",
    "DP",
}

SUPPORTED_TERM_REPLACEMENTS = [
    ("HTTPS", re.compile(r"(?<![A-Za-z0-9])(?:[Hh][Tt][Tt][Pp][Ss]|[Hh][Tt][Pp][Ss]|[Hh][Tt][Tt][Ee][Ss])(?![A-Za-z0-9])")),
    ("HTTP", re.compile(r"(?<![A-Za-z0-9])[Hh][Tt][Tt][Pp](?![A-Za-z0-9])")),
    ("API", re.compile(r"(?<![A-Za-z0-9])A[Pp][IiLl](?![A-Za-z0-9])")),
    ("AI", re.compile(r"(?<![A-Za-z0-9])[Aa][IiIl](?![A-Za-z0-9])")),
    ("MCP", re.compile(r"(?<![A-Za-z0-9])[Mm][Cc][Pp](?![A-Za-z0-9])")),
    ("RAG", re.compile(r"(?<![A-Za-z0-9])[Rr][Aa][Gg](?![A-Za-z0-9])")),
    ("BM25", re.compile(r"(?<![A-Za-z0-9])[Bb][Mm]25(?![A-Za-z0-9])")),
    ("JD", re.compile(r"(?<![A-Za-z0-9])[Jj][Dd](?![A-Za-z0-9])")),
    ("Python", re.compile(r"(?<![A-Za-z0-9])[Pp]ython(?![A-Za-z0-9])")),
    ("Java", re.compile(r"(?<![A-Za-z0-9])[Jj]ava(?![A-Za-z0-9])")),
    ("Redis", re.compile(r"(?<![A-Za-z0-9])[Rr]edis(?![A-Za-z0-9])")),
    ("Nginx", re.compile(r"(?<![A-Za-z0-9])[Nn]ginx(?![A-Za-z0-9])")),
    ("Docker", re.compile(r"(?<![A-Za-z0-9])[Dd]ocker(?![A-Za-z0-9])")),
    ("GitHub", re.compile(r"(?<![A-Za-z0-9])(?:[Gg]it[Hh]ub|[Gg]uihub)(?![A-Za-z0-9])")),
    ("LangChain", re.compile(r"(?<![A-Za-z0-9])[Ll]ang(?:[Cc]|[Oo0])hain(?![A-Za-z0-9])")),
    ("LangGraph", re.compile(r"(?<![A-Za-z0-9])[Ll]ang[Gg]raph(?![A-Za-z0-9])")),
    ("AutoGen", re.compile(r"(?<![A-Za-z0-9])[Aa]uto[Gg]en(?![A-Za-z0-9])")),
    ("WebSocket", re.compile(r"(?<![A-Za-z0-9])[Ww]eb[Ss]ocket(?![A-Za-z0-9])")),
    ("SpringBoot", re.compile(r"(?<![A-Za-z0-9])[Ss]pring[Bb]oot(?![A-Za-z0-9])")),
    ("MyBatis", re.compile(r"(?<![A-Za-z0-9])(?:[Mm]y[Bb]atis|[Mm]a[Bb]atis)(?![A-Za-z0-9])")),
    ("QPS", re.compile(r"(?<![A-Za-z0-9])[Qq][Pp][Ss](?![A-Za-z0-9])")),
    ("SSE", re.compile(r"(?<![A-Za-z0-9])[Ss][Ss][Ee](?![A-Za-z0-9])")),
    ("JWT", re.compile(r"(?<![A-Za-z0-9])[Jj][Ww][Tt](?![A-Za-z0-9])")),
    ("LLM", re.compile(r"(?<![A-Za-z0-9])[Ll][Ll][Mm](?![A-Za-z0-9])")),
    ("MySQL", re.compile(r"(?<![A-Za-z0-9])[Mm][Nn]?[Yy][Ss][Qq][LlIi1/](?![A-Za-z0-9])")),
    ("MySQL", re.compile(r"(?<![A-Za-z0-9])[Mm][Yy][Ss][Aa][LlIi1](?![A-Za-z0-9])")),
    ("MySQL", re.compile(r"(?<![A-Za-z0-9])[Mm][Yy][Ss][Qq](?![A-Za-z0-9])")),
    ("SQL", re.compile(r"(?<![A-Za-z0-9])5[Qq][LlIi1]?(?![A-Za-z0-9])")),
    ("SQL", re.compile(r"(?<![A-Za-z0-9])[Ss][Qq][Ll](?![A-Za-z0-9])")),
]

PLATFORM_NOISE_TERMS = ("小红书",)


@dataclass
class OCRBlock:
    source: str
    text: str
    norm: str
    bbox: list[float] | None
    score: float | None = None


def reclassify_ocr_reviews(db: ArchiveDB, cfg: AppConfig, *, dry_run: bool = True) -> dict:
    exports_dir = ensure_dir(cfg.exports_dir)
    review_dir = ensure_dir(exports_dir / "ocr_review")
    crop_dir = ensure_dir(review_dir / "crops")
    records: list[dict] = []
    high_risk_rows: list[dict] = []
    auto_cleared_rows: list[dict] = []
    supported_term_corrections = 0

    for note in db.list_notes(None):
        output_dir = Path(note["output_dir"] or "")
        if not output_dir.exists():
            continue
        ocr_path = output_dir / "ocr.json"
        if not ocr_path.exists():
            continue
        raw = _load_raw(output_dir / "raw.json")
        ocr_items = _load_ocr_items(ocr_path)
        ocr_modified = False
        images_by_position = {int(image["position"]): image for image in db.list_images(note["note_id"])}
        for item in ocr_items:
            corrections = _apply_supported_term_corrections(item)
            if corrections:
                supported_term_corrections += len(corrections)
                ocr_modified = True
            index = int(item.get("image_index") or item.get("index") or 0)
            if index <= 0:
                continue
            image_row = images_by_position.get(index)
            image_path = output_dir / str(item.get("image_path") or (image_row["local_path"] if image_row else ""))
            record = classify_ocr_image(
                note_id=str(note["note_id"]),
                title=str(note["summary_title"] or note["title"] or raw.get("summary_title") or raw.get("title") or ""),
                source_url=str(note["canonical_url"] or note["url"] or raw.get("canonical_url") or raw.get("url") or ""),
                output_dir=output_dir,
                image_path=image_path,
                ocr_item=item,
                image_row=image_row,
                cfg=cfg.ocr,
                crop_dir=crop_dir,
                exports_dir=exports_dir,
            )
            records.append(record)
            if record["human_review_required"]:
                high_risk_rows.extend(_high_risk_rows(record))
            if record["previous_needs_review"] and not record["human_review_required"]:
                auto_cleared_rows.append(_auto_cleared_row(record))
            if not dry_run and image_row:
                status = "needs_review" if record["human_review_required"] else "success"
                db.update_image_ocr(
                    record["note_id"],
                    record["image_index"],
                    status=status,
                    engine=image_row["ocr_engine"],
                    confidence=image_row["ocr_confidence"],
                    error="ocr_review:" + ",".join(record["review_reasons"]) if record["human_review_required"] else None,
                )
        if not dry_run and ocr_modified:
            write_json(ocr_path, ocr_items)

    summary = _summary(records, dry_run=dry_run)
    summary["supported_term_corrections"] = supported_term_corrections
    summary["paths"] = {
        "summary": str(exports_dir / "ocr_review_summary.json"),
        "high_risk_csv": str(exports_dir / "ocr_review_high_risk.csv"),
        "auto_cleared_csv": str(exports_dir / "ocr_review_auto_cleared.csv"),
        "review_index": str(review_dir / "index.html"),
        "records_jsonl": str(review_dir / "reclassification.jsonl"),
    }
    write_json(exports_dir / "ocr_review_summary.json", summary)
    _write_csv(exports_dir / "ocr_review_high_risk.csv", high_risk_rows, HIGH_RISK_CSV_FIELDS)
    _write_csv(exports_dir / "ocr_review_auto_cleared.csv", auto_cleared_rows, AUTO_CLEARED_CSV_FIELDS)
    _write_jsonl(review_dir / "reclassification.jsonl", records)
    _write_review_html(review_dir / "index.html", summary, records, exports_dir)
    return summary


def classify_ocr_image(
    *,
    note_id: str,
    title: str,
    source_url: str,
    output_dir: Path,
    image_path: Path,
    ocr_item: dict,
    image_row: Any | None,
    cfg: OCRConfig,
    crop_dir: Path,
    exports_dir: Path,
) -> dict:
    final_text = str(ocr_item.get("final_text") if ocr_item.get("final_text") is not None else ocr_item.get("text") or "")
    corrected_final_text, potential_corrections = correct_supported_terms(final_text)
    final_text = corrected_final_text
    previous_needs_review = _previous_needs_review(ocr_item, image_row)
    engines = _engine_evidence(ocr_item)
    engine_blocks = {source: _blocks_from_evidence(source, evidence) for source, evidence in engines.items()}
    all_regions: list[dict] = []
    ignored_formatting: list[dict] = []
    ignored_minor: list[dict] = []
    critical_diffs: list[dict] = []
    sentence_diffs: list[dict] = []
    reading_diffs: list[dict] = []
    three_way_diffs: list[dict] = []
    quality_diffs = _image_quality_differences(image_row, image_path, cfg)

    for left, right in (("ppocr", "apple_vision"), ("ppocr", "paddleocr_vl"), ("apple_vision", "paddleocr_vl")):
        if left not in engine_blocks or right not in engine_blocks:
            continue
        if not engine_blocks[left] or not engine_blocks[right]:
            continue
        pair = _compare_block_lists(engine_blocks[left], engine_blocks[right], cfg)
        ignored_formatting.extend(pair["formatting_only"])
        ignored_minor.extend(pair["minor_noncritical"])
        critical_diffs.extend(pair["critical_token_conflict"])
        sentence_diffs.extend(pair["sentence_or_line_missing"])

    for left, right in (("ppocr", "apple_vision"), ("ppocr", "paddleocr_vl"), ("apple_vision", "paddleocr_vl")):
        if left in engine_blocks and right in engine_blocks:
            diff = _reading_order_difference(engine_blocks[left], engine_blocks[right], cfg)
            if diff:
                reading_diffs.append(diff)

    three_way = _three_way_text_conflict(engines, cfg)
    if three_way:
        three_way_diffs.append(three_way)

    high_groups = [
        ("sentence_or_line_missing", sentence_diffs),
        ("critical_token_conflict", critical_diffs),
        ("reading_order_conflict", reading_diffs),
        ("three_way_conflict", three_way_diffs),
        ("image_quality_risk", quality_diffs),
    ]
    review_reasons = [name for name, values in high_groups if values]
    machine_review_reasons = list(review_reasons)
    review_level = _review_level(review_reasons, ignored_formatting, ignored_minor)
    human_required = bool(review_reasons)

    for risk_type, values in high_groups:
        for value in values:
            region = {
                "risk_type": risk_type,
                "reason": value.get("reason") or risk_type,
                "bbox": value.get("bbox"),
                "left_source": value.get("left_source"),
                "right_source": value.get("right_source"),
                "left_text": value.get("left_text", ""),
                "right_text": value.get("right_text", ""),
                "tokens": value.get("tokens", []),
            }
            region["crop_path"] = _crop_review_region(note_id, int(ocr_item.get("image_index") or 0), image_path, region["bbox"], crop_dir, exports_dir)
            all_regions.append(region)

    manual_review = manual_review_decision(ocr_item)
    manual_cleared_regions: list[dict] = []
    if manual_review and str(manual_review.get("status") or "") in DISCARDED_MANUAL_STATUSES:
        manual_cleared_regions = all_regions
        all_regions = []
        review_reasons = []
        review_level = "discarded_by_user"
        human_required = False
    elif manual_review and human_required:
        manual_cleared_regions = all_regions
        all_regions = []
        review_reasons = []
        review_level = "manual_accepted"
        human_required = False

    return {
        "note_id": note_id,
        "title": title,
        "source_url": source_url,
        "output_dir": str(output_dir),
        "image_index": int(ocr_item.get("image_index") or 0),
        "image_path": str(image_path),
        "image_path_relative_to_exports": _relative_for_html(image_path, exports_dir / "ocr_review"),
        "native_width": ocr_item.get("native_width") or (image_row["downloaded_width"] if image_row else None),
        "native_height": ocr_item.get("native_height") or (image_row["downloaded_height"] if image_row else None),
        "previous_needs_review": previous_needs_review,
        "review_level": review_level,
        "review_reasons": review_reasons,
        "machine_review_reasons": machine_review_reasons,
        "sentence_level_differences": sentence_diffs,
        "critical_token_differences": critical_diffs,
        "ignored_minor_differences": ignored_minor,
        "formatting_only_differences": ignored_formatting,
        "reading_order_differences": reading_diffs,
        "three_way_differences": three_way_diffs,
        "image_quality_differences": quality_diffs,
        "human_review_required": human_required,
        "high_risk_regions": all_regions,
        "manual_cleared_regions": manual_cleared_regions,
        "manual_review": manual_review,
        "final_text": final_text,
        "postprocess_corrections": ocr_item.get("postprocess_corrections") or potential_corrections,
        "engine_texts": {source: str(evidence.get("text") or "") for source, evidence in engines.items()},
        "uncertain_spans": ocr_item.get("uncertain_spans") if isinstance(ocr_item.get("uncertain_spans"), list) else [],
    }


def _compare_block_lists(left_blocks: list[OCRBlock], right_blocks: list[OCRBlock], cfg: OCRConfig) -> dict[str, list[dict]]:
    result = {
        "formatting_only": [],
        "minor_noncritical": [],
        "critical_token_conflict": [],
        "sentence_or_line_missing": [],
    }
    sorted_left = sorted(left_blocks, key=_block_sort_key)
    sorted_right = sorted(right_blocks, key=_block_sort_key)
    left_joined = "\n".join(block.text for block in sorted_left)
    right_joined = "\n".join(block.text for block in sorted_right)
    left_joined_norm = normalize_review_text(left_joined)
    right_joined_norm = normalize_review_text(right_joined)
    if left_joined != right_joined and left_joined_norm == right_joined_norm:
        result["formatting_only"].append(
            {
                "level": "formatting_only",
                "reason": "same_text_with_line_wrap_or_block_split",
                "left_source": sorted_left[0].source if sorted_left else None,
                "right_source": sorted_right[0].source if sorted_right else None,
                "left_text": left_joined,
                "right_text": right_joined,
                "bbox": _union_bbox([block.bbox for block in sorted_left + sorted_right]),
            }
        )
        return result
    pairs, unmatched_left, unmatched_right = _match_blocks(left_blocks, right_blocks)
    for left, right in pairs:
        diff = _classify_text_difference(left.text, right.text, cfg)
        if not diff:
            continue
        diff.update(
            {
                "left_source": left.source,
                "right_source": right.source,
                "left_text": left.text,
                "right_text": right.text,
                "bbox": _union_bbox([left.bbox, right.bbox]),
            }
        )
        result[diff["level"]].append(diff)
    for side, blocks in (("left", unmatched_left), ("right", unmatched_right)):
        other_norm = right_joined_norm if side == "left" else left_joined_norm
        for block in blocks:
            norm_len = len(block.norm)
            tokens = sorted(_critical_tokens(block.text))
            if block.norm and block.norm in other_norm:
                result["formatting_only"].append(
                    {
                        "level": "formatting_only",
                        "reason": f"{side}_line_already_present_after_split_or_merge",
                        "left_source": block.source if side == "left" else None,
                        "right_source": block.source if side == "right" else None,
                        "left_text": block.text if side == "left" else "",
                        "right_text": block.text if side == "right" else "",
                        "bbox": block.bbox,
                    }
                )
                continue
            if _is_missing_high_risk(block.text, cfg):
                result["sentence_or_line_missing"].append(
                    {
                        "reason": f"{side}_line_missing",
                        "left_source": block.source if side == "left" else None,
                        "right_source": block.source if side == "right" else None,
                        "left_text": block.text if side == "left" else "",
                        "right_text": block.text if side == "right" else "",
                        "bbox": block.bbox,
                        "tokens": tokens,
                        "normalized_length": norm_len,
                    }
                )
            elif norm_len:
                result["minor_noncritical"].append(
                    {
                        "level": "minor_noncritical",
                        "reason": f"{side}_small_unmatched_text",
                        "left_source": block.source if side == "left" else None,
                        "right_source": block.source if side == "right" else None,
                        "left_text": block.text if side == "left" else "",
                        "right_text": block.text if side == "right" else "",
                        "bbox": block.bbox,
                    }
                )
    return result


def _classify_text_difference(left: str, right: str, cfg: OCRConfig) -> dict | None:
    if left == right:
        return None
    left_norm = normalize_review_text(left)
    right_norm = normalize_review_text(right)
    if left_norm == right_norm:
        return {"level": "formatting_only", "reason": "formatting_or_punctuation_only"}
    left_tokens = _critical_tokens(left)
    right_tokens = _critical_tokens(right)
    token_diff_ignorable = _ignorable_token_difference(left_tokens, right_tokens)
    if (left_tokens != right_tokens and not token_diff_ignorable) or _critical_edit(left, right):
        return {
            "level": "critical_token_conflict",
            "reason": "critical_token_set_differs",
            "tokens": sorted(left_tokens.symmetric_difference(right_tokens)),
        }
    diff_chars = _exclusive_character_count(left_norm, right_norm)
    ratio = diff_chars / max(len(left_norm), len(right_norm), 1)
    similarity = SequenceMatcher(None, left_norm, right_norm).ratio() if left_norm or right_norm else 1.0
    if diff_chars <= cfg.review_minor_char_diff or similarity >= cfg.review_minor_similarity_threshold:
        return {
            "level": "minor_noncritical",
            "reason": "small_noncritical_text_difference",
            "diff_chars": diff_chars,
            "diff_ratio": ratio,
            "similarity": similarity,
        }
    if max(len(left_norm), len(right_norm)) >= cfg.review_missing_abs_chars and (
        diff_chars >= cfg.review_missing_abs_chars or ratio >= cfg.review_missing_relative_threshold
    ):
        return {
            "level": "sentence_or_line_missing",
            "reason": "line_or_sentence_text_diverges",
            "diff_chars": diff_chars,
            "diff_ratio": ratio,
            "similarity": similarity,
        }
    return {
        "level": "minor_noncritical",
        "reason": "noncritical_text_difference_below_high_risk_threshold",
        "diff_chars": diff_chars,
        "diff_ratio": ratio,
        "similarity": similarity,
    }


def _match_blocks(left: list[OCRBlock], right: list[OCRBlock]) -> tuple[list[tuple[OCRBlock, OCRBlock]], list[OCRBlock], list[OCRBlock]]:
    pairs: list[tuple[OCRBlock, OCRBlock]] = []
    used_right: set[int] = set()
    for left_block in sorted(left, key=_block_sort_key):
        best_index = None
        best_score = 0.0
        for index, right_block in enumerate(right):
            if index in used_right:
                continue
            score = _block_match_score(left_block, right_block)
            if score > best_score:
                best_score = score
                best_index = index
        if best_index is not None and best_score >= 0.45:
            used_right.add(best_index)
            pairs.append((left_block, right[best_index]))
    used_left = {id(left_block) for left_block, _ in pairs}
    unmatched_left = [block for block in left if id(block) not in used_left]
    unmatched_right = [block for index, block in enumerate(right) if index not in used_right]
    return pairs, unmatched_left, unmatched_right


def _block_match_score(left: OCRBlock, right: OCRBlock) -> float:
    text_similarity = SequenceMatcher(None, left.norm, right.norm).ratio() if left.norm or right.norm else 1.0
    if left.bbox and right.bbox:
        vertical = _vertical_overlap_ratio(left.bbox, right.bbox)
        iou = _bbox_iou(left.bbox, right.bbox)
        center_distance = abs(_center_y(left.bbox) - _center_y(right.bbox)) / max(_height(left.bbox), _height(right.bbox), 1.0)
        spatial = max(iou * 2.0, vertical - min(center_distance * 0.15, 0.4))
        return max(text_similarity * 0.6, spatial) + (0.25 if text_similarity >= 0.85 else 0.0)
    return text_similarity


def _reading_order_difference(left: list[OCRBlock], right: list[OCRBlock], cfg: OCRConfig) -> dict | None:
    left_seq = [block.norm for block in sorted(left, key=_block_sort_key) if len(block.norm) >= 3]
    right_seq = [block.norm for block in sorted(right, key=_block_sort_key) if len(block.norm) >= 3]
    if len(left_seq) < 3 or len(right_seq) < 3:
        return None
    matched_positions: list[int] = []
    used: set[int] = set()
    for line in left_seq:
        best_index = None
        best_score = 0.0
        for index, other in enumerate(right_seq):
            if index in used:
                continue
            score = SequenceMatcher(None, line, other).ratio()
            if score > best_score:
                best_index = index
                best_score = score
        if best_index is not None and best_score >= 0.9:
            used.add(best_index)
            matched_positions.append(best_index)
    if len(matched_positions) < 3:
        return None
    inversions = 0
    comparisons = 0
    for i, left_pos in enumerate(matched_positions):
        for right_pos in matched_positions[i + 1 :]:
            comparisons += 1
            if left_pos > right_pos:
                inversions += 1
    ratio = inversions / max(comparisons, 1)
    if ratio >= cfg.review_reading_order_inversion_threshold:
        return {
            "reason": "matched_lines_appear_in_different_order",
            "left_source": left[0].source,
            "right_source": right[0].source,
            "left_text": "\n".join(left_seq),
            "right_text": "\n".join(right_seq),
            "inversion_ratio": ratio,
            "bbox": _union_bbox([block.bbox for block in left + right]),
        }
    return None


def _three_way_text_conflict(engines: dict[str, dict], cfg: OCRConfig) -> dict | None:
    success = {source: str(evidence.get("text") or "") for source, evidence in engines.items() if evidence.get("status") == "success" and str(evidence.get("text") or "").strip()}
    if len(success) < 3:
        return None
    norms = {source: normalize_review_text(text) for source, text in success.items()}
    if len(set(norms.values())) < 3:
        return None
    pair_scores = [
        SequenceMatcher(None, a, b).ratio()
        for index, a in enumerate(norms.values())
        for b in list(norms.values())[index + 1 :]
    ]
    token_sets = {source: _critical_tokens(text) for source, text in success.items()}
    critical_disagreement = len({tuple(sorted(tokens)) for tokens in token_sets.values()}) > 1
    if critical_disagreement or all(score < cfg.review_minor_similarity_threshold for score in pair_scores):
        return {
            "reason": "three_successful_engines_without_two_vote_agreement",
            "left_text": "\n---\n".join(f"{source}: {text}" for source, text in success.items()),
            "right_text": "",
            "tokens": sorted(set().union(*token_sets.values())),
            "bbox": None,
        }
    return None


def _image_quality_differences(image_row: Any | None, image_path: Path, cfg: OCRConfig) -> list[dict]:
    if not image_row:
        return [{"reason": "missing_image_sqlite_row", "bbox": None}]
    risks: list[dict] = []
    width = int(image_row["downloaded_width"] or image_row["width"] or 0)
    height = int(image_row["downloaded_height"] or image_row["height"] or 0)
    reasons = str(image_row["thumbnail_reason"] or "")
    severe_reason_markers = {
        "native_resolution_too_small_for_text_ocr",
        "file_size_small_for_text_image",
        "higher_priority_candidate_available",
    }
    if image_row["download_status"] != "success":
        risks.append({"reason": "image_download_not_success", "bbox": None})
    if not image_path.exists():
        risks.append({"reason": "image_file_missing", "bbox": None})
    if width and height:
        if max(width, height) < cfg.review_low_resolution_long_side or min(width, height) < cfg.review_low_resolution_short_side:
            risks.append({"reason": "download_resolution_below_review_threshold", "bbox": [0, 0, width, height]})
    if image_row["is_probable_thumbnail"] and any(marker in reasons for marker in severe_reason_markers):
        risks.append({"reason": reasons or "probable_thumbnail_with_quality_risk", "bbox": [0, 0, width, height] if width and height else None})
    return risks


def _engine_evidence(ocr_item: dict) -> dict[str, dict]:
    engines = ocr_item.get("engines")
    if isinstance(engines, dict):
        return {str(source): evidence for source, evidence in engines.items() if isinstance(evidence, dict) and evidence.get("status") == "success"}
    if ocr_item.get("status") == "success":
        return {"legacy": ocr_item}
    return {}


def _blocks_from_evidence(source: str, evidence: dict) -> list[OCRBlock]:
    blocks: list[OCRBlock] = []
    for block in evidence.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        text = str(block.get("text") or "").strip()
        if not text:
            continue
        score = block.get("score")
        blocks.append(
            OCRBlock(
                source=source,
                text=text,
                norm=normalize_review_text(text),
                bbox=block_bbox(block),
                score=float(score) if isinstance(score, (int, float)) else None,
            )
        )
    if not blocks:
        for line in str(evidence.get("text") or "").splitlines():
            text = line.strip()
            if text:
                blocks.append(OCRBlock(source=source, text=text, norm=normalize_review_text(text), bbox=None))
    return blocks


def normalize_review_text(text: str) -> str:
    normalized = unicodedata.normalize("NFKC", MARKDOWN_RE.sub("", text or ""))
    normalized = correct_supported_terms(normalized)[0]
    for term in PLATFORM_NOISE_TERMS:
        normalized = normalized.replace(term, "")
    normalized = normalized.casefold()
    return PUNCT_OR_SPACE_RE.sub("", normalized)


def _critical_tokens(text: str) -> set[str]:
    normalized = correct_supported_terms(unicodedata.normalize("NFKC", text or ""))[0]
    tokens: set[str] = set()
    patterns = [
        ("url", URL_RE),
        ("email", EMAIL_RE),
        ("id", ID_RE),
        ("date", DATE_RE),
        ("amount", MONEY_RE),
        ("complexity", COMPLEXITY_RE),
        ("chinese_number", CHINESE_NUMERAL_RE),
        ("round", INTERVIEW_ROUND_RE),
        ("number", ARABIC_NUMBER_RE),
    ]
    for category, pattern in patterns:
        for match in pattern.finditer(normalized):
            value = match.group(0)
            if not value.strip():
                continue
            if category == "number" and _is_low_risk_list_marker_number(normalized, match):
                continue
            tokens.add(_token(f"{category}:{value}"))
    for category, terms in (
        ("tech", set(TECH_TERMS) | TECH_EXTRA_TERMS),
        ("company", COMPANY_TERMS),
        ("role", ROLE_TERMS),
        ("city", CITY_TERMS),
        ("result", RESULT_TERMS),
        ("negation", NEGATION_TERMS),
    ):
        for term in terms:
            if term and term.casefold() in normalized.casefold():
                tokens.add(_token(f"{category}:{term}"))
    return tokens


def _critical_edit(left: str, right: str) -> bool:
    if normalize_review_text(left) == normalize_review_text(right):
        return False
    matcher = SequenceMatcher(None, left, right)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            continue
        changed = left[i1:i2] + right[j1:j2]
        if _critical_tokens(changed):
            return True
    return False


def _ignorable_token_difference(left_tokens: set[str], right_tokens: set[str]) -> bool:
    if left_tokens == right_tokens:
        return True
    diff = left_tokens.symmetric_difference(right_tokens)
    return bool(diff) and all(token in {"techmysql", "techsql"} for token in diff)


def _is_low_risk_list_marker_number(text: str, match: re.Match) -> bool:
    start, end = match.span()
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start].strip()
    if prefix:
        return False
    suffix = text[end : end + 2]
    if not suffix or suffix[0] not in ".．、)）":
        return False
    previous_line = text[text.rfind("\n", 0, max(line_start - 1, 0)) + 1 : line_start].strip()
    if previous_line.endswith(("月", "年", "/", "-", ":")):
        return False
    return True


def correct_supported_terms(text: str) -> tuple[str, list[dict]]:
    corrected = text or ""
    corrections: list[dict] = []
    for canonical, pattern in SUPPORTED_TERM_REPLACEMENTS:
        def repl(match: re.Match) -> str:
            original = match.group(0)
            if original == canonical:
                return original
            corrections.append({"original": original, "corrected": canonical, "reason": "supported_ocr_tech_term_variant"})
            return canonical

        corrected = pattern.sub(repl, corrected)
    return corrected, corrections


def _apply_supported_term_corrections(ocr_item: dict) -> list[dict]:
    if "final_text" not in ocr_item:
        return []
    original = str(ocr_item.get("final_text") or "")
    corrected, corrections = correct_supported_terms(original)
    if not corrections or corrected == original:
        return []
    if "original_final_text" not in ocr_item:
        ocr_item["original_final_text"] = original
    ocr_item["final_text"] = corrected
    existing = ocr_item.get("postprocess_corrections")
    if not isinstance(existing, list):
        existing = []
    existing.append(
        {
            "type": "supported_ocr_term_correction",
            "original_final_text": original,
            "corrected_final_text": corrected,
            "replacements": corrections,
        }
    )
    ocr_item["postprocess_corrections"] = existing
    audit = ocr_item.get("audit")
    if not isinstance(audit, dict):
        audit = {}
    audit["supported_term_correction_count"] = int(audit.get("supported_term_correction_count") or 0) + len(corrections)
    ocr_item["audit"] = audit
    return corrections


def _is_missing_high_risk(text: str, cfg: OCRConfig) -> bool:
    norm = normalize_review_text(text)
    if len(norm) >= cfg.review_missing_abs_chars:
        return True
    tokens = _critical_tokens(text)
    if any(
        token.startswith(prefix)
        for token in tokens
        for prefix in ("date", "amount", "complexity", "round", "company", "role", "city", "result", "negation", "tech")
    ):
        return True
    return False


def _review_level(high_reasons: list[str], formatting: list[dict], minor: list[dict]) -> str:
    for level in REVIEW_PRIORITIES:
        if level in high_reasons:
            return level
    if high_reasons:
        return "human_review_required"
    if minor:
        return "minor_noncritical"
    if formatting:
        return "formatting_only"
    return "auto_accepted"


def _previous_needs_review(ocr_item: dict, image_row: Any | None) -> bool:
    if image_row:
        return image_row["ocr_status"] in {"needs_review", "failed", "vl_pending"}
    if bool(ocr_item.get("needs_human_review") or ocr_item.get("needs_review")):
        return True
    return str(ocr_item.get("status") or "") in {"needs_review", "failed", "vl_pending"}


def _summary(records: list[dict], *, dry_run: bool) -> dict:
    summary = {field: 0 for field in SUMMARY_FIELDS}
    summary["dry_run"] = dry_run
    summary["total_images"] = len(records)
    for record in records:
        level = record["review_level"]
        if level in summary:
            summary[level] += 1
        if record["previous_needs_review"]:
            summary["previous_needs_review"] += 1
        if record["human_review_required"]:
            summary["human_review_required"] += 1
            summary["human_review_regions"] += len(record.get("high_risk_regions") or []) or 1
        elif record["previous_needs_review"]:
            summary["auto_cleared_from_previous_needs_review"] += 1
        for reason in record.get("review_reasons") or []:
            if reason in summary and reason != level:
                summary[reason] += 1
    return summary


HIGH_RISK_CSV_FIELDS = [
    "note_id",
    "image_index",
    "review_level",
    "risk_type",
    "reason",
    "title",
    "source_url",
    "image_path",
    "crop_path",
    "bbox",
    "ppocr_text",
    "apple_vision_text",
    "paddleocr_vl_text",
    "final_text",
]

AUTO_CLEARED_CSV_FIELDS = [
    "note_id",
    "image_index",
    "review_level",
    "title",
    "source_url",
    "image_path",
    "clear_reason",
]


def _high_risk_rows(record: dict) -> list[dict]:
    rows = []
    for region in record.get("high_risk_regions") or [{"risk_type": record["review_level"], "reason": ",".join(record["review_reasons"])}]:
        rows.append(
            {
                "note_id": record["note_id"],
                "image_index": record["image_index"],
                "review_level": record["review_level"],
                "risk_type": region.get("risk_type"),
                "reason": region.get("reason"),
                "title": record["title"],
                "source_url": record["source_url"],
                "image_path": record["image_path"],
                "crop_path": region.get("crop_path"),
                "bbox": json.dumps(region.get("bbox"), ensure_ascii=False),
                "ppocr_text": record["engine_texts"].get("ppocr", ""),
                "apple_vision_text": record["engine_texts"].get("apple_vision", ""),
                "paddleocr_vl_text": record["engine_texts"].get("paddleocr_vl", ""),
                "final_text": record["final_text"],
            }
        )
    return rows


def _auto_cleared_row(record: dict) -> dict:
    reasons = []
    if record.get("manual_review"):
        reasons.append("manual_review_" + str(record["manual_review"].get("status") or "accepted"))
    if record.get("formatting_only_differences"):
        reasons.append("formatting_only")
    if record.get("ignored_minor_differences"):
        reasons.append("minor_noncritical")
    if not reasons:
        reasons.append("no_high_risk_difference")
    return {
        "note_id": record["note_id"],
        "image_index": record["image_index"],
        "review_level": record["review_level"],
        "title": record["title"],
        "source_url": record["source_url"],
        "image_path": record["image_path"],
        "clear_reason": ";".join(reasons),
    }


def _write_csv(path: Path, rows: list[dict], fields: list[str]) -> None:
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def _write_jsonl(path: Path, records: list[dict]) -> None:
    with path.open("w", encoding="utf-8") as fh:
        for record in records:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")


def _write_review_html(path: Path, summary: dict, records: list[dict], exports_dir: Path) -> None:
    high_records = [record for record in records if record.get("human_review_required")]
    cards = []
    for record in sorted(high_records, key=_record_priority):
        regions = record.get("high_risk_regions") or [{"risk_type": record["review_level"], "reason": ",".join(record["review_reasons"]), "crop_path": None}]
        for region in regions:
            cards.append(_render_card(record, region))
    body = "\n".join(cards) if cards else "<p class=\"empty\">没有检测到句子级、整行级或关键事实级冲突，无需人工复核。</p>"
    filters = "".join(f"<button data-filter=\"{level}\">{level}</button>" for level in REVIEW_PRIORITIES)
    path.write_text(
        f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <title>OCR 高风险复核</title>
  <style>
    body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; margin: 0; background: #f6f7f9; color: #17202a; }}
    header {{ position: sticky; top: 0; background: #fff; border-bottom: 1px solid #d8dde6; padding: 16px 24px; z-index: 2; }}
    h1 {{ margin: 0 0 8px; font-size: 22px; }}
    .summary {{ display: flex; flex-wrap: wrap; gap: 10px; font-size: 13px; }}
    .pill {{ background: #eef2f7; border: 1px solid #d8dde6; border-radius: 999px; padding: 4px 10px; }}
    .filters {{ margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; }}
    button {{ border: 1px solid #9aa7b5; background: #fff; border-radius: 6px; padding: 6px 10px; cursor: pointer; }}
    button.selected {{ background: #1f6feb; border-color: #1f6feb; color: #fff; }}
    main {{ max-width: 1180px; margin: 24px auto; padding: 0 18px 48px; }}
    .card {{ background: #fff; border: 1px solid #d8dde6; border-radius: 8px; margin-bottom: 18px; overflow: hidden; }}
    .card header {{ position: static; border-bottom: 1px solid #e6eaf0; padding: 14px 16px; }}
    .meta {{ color: #5d6b7a; font-size: 13px; line-height: 1.5; }}
    .grid {{ display: grid; grid-template-columns: minmax(280px, 420px) 1fr; gap: 16px; padding: 16px; }}
    .images {{ display: grid; gap: 12px; }}
    img {{ max-width: 100%; border: 1px solid #d8dde6; border-radius: 6px; background: #fff; }}
    pre {{ white-space: pre-wrap; word-break: break-word; background: #f7f9fb; border: 1px solid #e1e6ed; border-radius: 6px; padding: 10px; max-height: 220px; overflow: auto; }}
    .risk {{ color: #9b1c1c; font-weight: 700; }}
    .actions {{ display: flex; gap: 8px; flex-wrap: wrap; padding: 0 16px 16px; }}
    .decision-status {{ color: #465362; font-size: 13px; align-self: center; }}
    textarea[data-manual] {{ width: 100%; min-height: 72px; border: 1px solid #c8d1dc; border-radius: 6px; padding: 8px; font: inherit; }}
    .diff-del {{ background: #ffd9d9; text-decoration: line-through; }}
    .diff-add {{ background: #d8f5df; }}
    .empty {{ background: #fff; border: 1px solid #d8dde6; border-radius: 8px; padding: 28px; }}
    @media (max-width: 780px) {{ .grid {{ grid-template-columns: 1fr; }} }}
  </style>
</head>
<body>
<header>
  <h1>OCR 高风险复核</h1>
  <div class="summary">
    <span class="pill">图片总数 {summary.get("total_images", 0)}</span>
    <span class="pill">原 needs_review {summary.get("previous_needs_review", 0)}</span>
    <span class="pill">需人工复核 {summary.get("human_review_required", 0)}</span>
    <span class="pill">自动解除 {summary.get("auto_cleared_from_previous_needs_review", 0)}</span>
    <span class="pill">局部区域 {summary.get("human_review_regions", 0)}</span>
  </div>
  <div class="filters"><button data-filter="all">全部</button>{filters}<button id="download-decisions">下载审核结果 JSON</button><button id="clear-decisions">清空本页选择</button></div>
</header>
<main>{body}</main>
<script>
  const storageKey = 'xhs_ocr_review_decisions_v1';
  let decisions = JSON.parse(localStorage.getItem(storageKey) || '{{}}');
  function cardKey(card) {{
    return [card.dataset.note, card.dataset.image, card.dataset.risk, card.dataset.region].join(':');
  }}
  function saveDecisions() {{
    localStorage.setItem(storageKey, JSON.stringify(decisions));
  }}
  function renderDecision(card) {{
    const key = cardKey(card);
    const decision = decisions[key];
    const status = card.querySelector('.decision-status');
    if (!status) return;
    status.textContent = decision ? `已选择：${{decision.action}} · ${{decision.updated_at}}` : '未选择';
    card.querySelectorAll('button[data-action]').forEach(btn => {{
      btn.classList.toggle('selected', decision && decision.action === btn.dataset.action);
    }});
    const manual = card.querySelector('textarea[data-manual]');
    if (manual && decision && decision.manual_text && document.activeElement !== manual) {{
      manual.value = decision.manual_text;
    }}
  }}
  document.querySelectorAll('button[data-filter]').forEach(btn => btn.addEventListener('click', () => {{
    const filter = btn.dataset.filter;
    document.querySelectorAll('.card').forEach(card => {{
      card.style.display = filter === 'all' || card.dataset.risk === filter ? '' : 'none';
    }});
  }}));
  document.querySelectorAll('.card').forEach(card => {{
    renderDecision(card);
    card.querySelectorAll('button[data-action]').forEach(btn => btn.addEventListener('click', () => {{
      const manual = card.querySelector('textarea[data-manual]');
      decisions[cardKey(card)] = {{
        note_id: card.dataset.note,
        image_index: Number(card.dataset.image),
        risk_type: card.dataset.risk,
        region_id: card.dataset.region,
        action: btn.dataset.action,
        manual_text: manual ? manual.value : '',
        updated_at: new Date().toISOString()
      }};
      saveDecisions();
      renderDecision(card);
    }}));
    const manual = card.querySelector('textarea[data-manual]');
    if (manual) manual.addEventListener('input', () => {{
      const existing = decisions[cardKey(card)];
      if (existing) {{
        existing.manual_text = manual.value;
        existing.updated_at = new Date().toISOString();
        saveDecisions();
        renderDecision(card);
      }}
    }});
  }});
  document.getElementById('download-decisions').addEventListener('click', () => {{
    const payload = {{
      generated_at: new Date().toISOString(),
      source_page: location.href,
      decisions: Object.values(decisions)
    }};
    const blob = new Blob([JSON.stringify(payload, null, 2)], {{type: 'application/json'}});
    const link = document.createElement('a');
    link.href = URL.createObjectURL(blob);
    link.download = 'ocr_review_decisions.json';
    link.click();
    URL.revokeObjectURL(link.href);
  }});
  document.getElementById('clear-decisions').addEventListener('click', () => {{
    if (!confirm('清空本页已保存的审核选择？')) return;
    decisions = {{}};
    saveDecisions();
    document.querySelectorAll('.card').forEach(renderDecision);
  }});
</script>
</body>
</html>
""",
        encoding="utf-8",
    )


def _render_card(record: dict, region: dict) -> str:
    risk = html.escape(str(region.get("risk_type") or record["review_level"]))
    reason = html.escape(str(region.get("reason") or ""))
    region_id = html.escape(str(abs(hash(json.dumps(region, ensure_ascii=False, sort_keys=True))) % 1_000_000_000))
    original = html.escape(record.get("image_path_relative_to_exports") or "")
    crop = html.escape(str(region.get("crop_path") or ""))
    left_text = str(region.get("left_text") or record.get("engine_texts", {}).get("ppocr") or "")
    right_text = str(region.get("right_text") or record.get("engine_texts", {}).get("apple_vision") or "")
    diff_left, diff_right = _html_diff(left_text, right_text)
    return f"""
<section class="card" data-risk="{risk}" data-note="{html.escape(record["note_id"])}" data-image="{record["image_index"]}" data-region="{region_id}">
  <header>
    <div class="risk">{risk}: {reason}</div>
    <div>{html.escape(record.get("title") or record["note_id"])}</div>
    <div class="meta">note_id: {html.escape(record["note_id"])} · image {record["image_index"]} · <a href="{html.escape(record.get("source_url") or "#")}">原帖链接</a></div>
  </header>
  <div class="grid">
    <div class="images">
      <div><strong>原图上下文</strong><br><img src="{original}" loading="lazy"></div>
      {f'<div><strong>冲突区域</strong><br><img src="{crop}" loading="lazy"></div>' if crop else ''}
    </div>
    <div>
      <strong>差异区域</strong>
      <pre>{diff_left}</pre>
      <pre>{diff_right}</pre>
      <strong>PP-OCR</strong><pre>{html.escape(record.get("engine_texts", {}).get("ppocr", ""))}</pre>
      <strong>Apple Vision</strong><pre>{html.escape(record.get("engine_texts", {}).get("apple_vision", ""))}</pre>
      <strong>PaddleOCR-VL</strong><pre>{html.escape(record.get("engine_texts", {}).get("paddleocr_vl", ""))}</pre>
      <strong>当前最终文本</strong><pre>{html.escape(record.get("final_text", ""))}</pre>
    </div>
  </div>
  <div class="actions">
    <button data-action="use_current">采用当前结果</button><button data-action="use_ppocr">采用 PP-OCR</button><button data-action="use_apple_vision">采用 Apple Vision</button><button data-action="use_paddleocr_vl">采用 PaddleOCR-VL</button><button data-action="manual_edit">手工修改</button><button data-action="unable_to_decide">标记无法判断</button><button data-action="skip">跳过</button>
    <span class="decision-status">未选择</span>
    <textarea data-manual placeholder="手工修改内容或备注"></textarea>
  </div>
</section>
"""


def _html_diff(left: str, right: str) -> tuple[str, str]:
    matcher = SequenceMatcher(None, left, right)
    left_parts: list[str] = []
    right_parts: list[str] = []
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal":
            left_parts.append(html.escape(left[i1:i2]))
            right_parts.append(html.escape(right[j1:j2]))
        else:
            if i1 != i2:
                left_parts.append(f"<span class=\"diff-del\">{html.escape(left[i1:i2])}</span>")
            if j1 != j2:
                right_parts.append(f"<span class=\"diff-add\">{html.escape(right[j1:j2])}</span>")
    return "".join(left_parts), "".join(right_parts)


def _crop_review_region(note_id: str, image_index: int, image_path: Path, bbox: list[float] | None, crop_dir: Path, exports_dir: Path) -> str | None:
    if not image_path.exists():
        return None
    try:
        with Image.open(image_path) as img:
            width, height = img.size
            if not bbox:
                x1, y1, x2, y2 = 0, 0, width, height
            else:
                x1, y1, x2, y2 = bbox
                pad = 18
                x1, y1 = max(0, int(x1) - pad), max(0, int(y1) - pad)
                x2, y2 = min(width, int(x2) + pad), min(height, int(y2) + pad)
            if x2 <= x1 or y2 <= y1:
                return None
            crop = img.crop((x1, y1, x2, y2))
            crop.thumbnail((1200, 1200), Image.Resampling.LANCZOS)
            path = crop_dir / f"{note_id}_{image_index:02d}_{abs(hash((x1, y1, x2, y2))) % 1_000_000}.png"
            crop.save(path, format="PNG")
            return _relative_for_html(path, exports_dir / "ocr_review")
    except Exception:
        return None


def _load_raw(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        data = read_json(path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _load_ocr_items(path: Path) -> list[dict]:
    try:
        data = read_json(path)
    except Exception:
        return []
    return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []


def _record_priority(record: dict) -> tuple[int, str, int]:
    level = record.get("review_level")
    priority = REVIEW_PRIORITIES.index(level) if level in REVIEW_PRIORITIES else len(REVIEW_PRIORITIES)
    return priority, str(record.get("note_id")), int(record.get("image_index") or 0)


def _relative_for_html(path: Path, base: Path) -> str:
    try:
        return os.path.relpath(path.resolve(), start=base.resolve())
    except Exception:
        return str(path)


def _union_bbox(boxes: list[list[float] | None]) -> list[float] | None:
    valid = [box for box in boxes if box]
    if not valid:
        return None
    return [min(box[0] for box in valid), min(box[1] for box in valid), max(box[2] for box in valid), max(box[3] for box in valid)]


def _bbox_iou(left: list[float], right: list[float]) -> float:
    x1, y1 = max(left[0], right[0]), max(left[1], right[1])
    x2, y2 = min(left[2], right[2]), min(left[3], right[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    intersection = (x2 - x1) * (y2 - y1)
    left_area = max(0.0, (left[2] - left[0]) * (left[3] - left[1]))
    right_area = max(0.0, (right[2] - right[0]) * (right[3] - right[1]))
    return intersection / max(left_area + right_area - intersection, 1.0)


def _vertical_overlap_ratio(left: list[float], right: list[float]) -> float:
    top = max(left[1], right[1])
    bottom = min(left[3], right[3])
    if bottom <= top:
        return 0.0
    return (bottom - top) / max(min(_height(left), _height(right)), 1.0)


def _height(box: list[float]) -> float:
    return max(1.0, float(box[3]) - float(box[1]))


def _center_y(box: list[float]) -> float:
    return (float(box[1]) + float(box[3])) / 2.0


def _block_sort_key(block: OCRBlock) -> tuple[float, float, str]:
    if block.bbox:
        return float(block.bbox[1]), float(block.bbox[0]), block.text
    return 0.0, 0.0, block.text


def _exclusive_character_count(a: str, b: str) -> int:
    matcher = SequenceMatcher(None, a, b)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return max(len(a), len(b)) - matched


def _token(value: str) -> str:
    return normalize_review_text(value)
