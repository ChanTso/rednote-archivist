from __future__ import annotations

import re
import time
from difflib import SequenceMatcher
from pathlib import Path

from PIL import Image, ImageDraw

from xhs_archive.config import OCRConfig
from xhs_archive.models import OCREngineEvidence, OCRAuditImageResult, OCRImageResult, OCRUncertainSpan
from xhs_archive.ocr.base import OCREngine
from xhs_archive.utils import ensure_dir

TECH_TERMS = {
    "Java",
    "JVM",
    "Redis",
    "RDB",
    "AOF",
    "MySQL",
    "PostgreSQL",
    "Spring",
    "Spring Boot",
    "Kafka",
    "RPC",
    "HTTP",
    "HTTPS",
    "TCP",
    "UDP",
    "Linux",
    "Docker",
    "Kubernetes",
    "LeetCode",
    "字节跳动",
    "阿里",
    "腾讯",
    "美团",
    "一面",
    "二面",
    "三面",
    "HR面",
}

REGION_CROP_SUFFIX = ".png"
REGION_RERUN_MAX_SIDE = 24_000

PUNCT_TRANSLATION = str.maketrans(
    {
        "，": ",",
        "。": ".",
        "：": ":",
        "；": ";",
        "（": "(",
        "）": ")",
        "？": "?",
        "！": "!",
        "｜": "|",
        "—": "-",
        "－": "-",
    }
)


def audit_image(
    image_path: Path,
    image_index: int,
    *,
    primary: OCREngine,
    secondary: OCREngine | None,
    vl_engine: OCREngine | None,
    cfg: OCRConfig,
    thumbnail_suspected: bool = False,
) -> OCRAuditImageResult:
    start = time.monotonic()
    native_width, native_height = _image_size(image_path)
    primary_result = primary.recognize(image_path, image_index)
    secondary_result = secondary.recognize(image_path, image_index) if secondary else _not_available(image_path, image_index, "apple-vision", "not configured")

    metrics = compare_results(primary_result, secondary_result, cfg)
    trigger_vl = should_trigger_vl(primary_result, secondary_result, metrics, cfg, thumbnail_suspected)
    metrics["vl_requested"] = trigger_vl
    vl_result = _not_triggered(image_path, image_index, "paddleocr-vl")
    if trigger_vl and vl_engine:
        vl_result = vl_engine.recognize(image_path, image_index)
    elif trigger_vl:
        vl_result = _not_available(image_path, image_index, "paddleocr-vl", "VL adjudication requested but engine is unavailable")

    final_text, selected_source, decision_reason, needs_human = adjudicate(primary_result, secondary_result, vl_result, metrics, cfg)
    uncertain_spans: list[OCRUncertainSpan] = []
    conflict = selected_source == "uncertain" or needs_human or thumbnail_suspected
    if conflict:
        span = build_uncertain_span(
            image_path,
            primary_result,
            secondary_result,
            vl_result,
            final_text,
            decision_reason,
            needs_human=needs_human or thumbnail_suspected,
        )
        span.candidates.extend(
            rerun_uncertain_region(
                image_path,
                span,
                image_index,
                primary=primary,
                secondary=secondary,
                vl_engine=vl_engine if trigger_vl else None,
                cfg=cfg,
            )
        )
        uncertain_spans.append(span)
        if cfg.generate_overlay_on_conflict:
            overlay_path = create_overlay(image_path, primary_result, secondary_result, uncertain_spans)
            metrics["overlay_path"] = overlay_path

    engines = {
        "ppocr": evidence_from_result(primary_result),
        "apple_vision": evidence_from_result(secondary_result),
        "paddleocr_vl": evidence_from_result(vl_result),
    }
    status = "success"
    if primary_result.status != "success" and secondary_result.status != "success":
        status = "failed"
        needs_human = True
    elif needs_human or thumbnail_suspected:
        status = "needs_review"

    sources = [source for source, result in (("ppocr", primary_result), ("apple_vision", secondary_result), ("paddleocr_vl", vl_result)) if result.status == "success"]
    return OCRAuditImageResult(
        image_index=image_index,
        image_path=image_path.name,
        native_width=native_width,
        native_height=native_height,
        original_download_verified=not thumbnail_suspected,
        engines=engines,
        agreement_score=metrics.get("agreement_score"),
        uncertain_spans=uncertain_spans,
        final_text=final_text,
        needs_human_review=needs_human or thumbnail_suspected,
        status=status,
        sources=sources,
        audit={**metrics, "elapsed_seconds": time.monotonic() - start, "selected_source": selected_source, "decision_reason": decision_reason},
    )


def compare_results(primary: OCRImageResult, secondary: OCRImageResult, cfg: OCRConfig) -> dict:
    a = normalize_text(primary.text)
    b = normalize_text(secondary.text)
    agreement = SequenceMatcher(None, a, b).ratio() if a or b else 1.0
    exclusive = _exclusive_character_count(a, b)
    metrics = {
        "agreement_score": agreement,
        "exclusive_character_count": exclusive,
        "primary_block_count": len(primary.blocks),
        "secondary_block_count": len(secondary.blocks),
        "block_count_delta": abs(len(primary.blocks) - len(secondary.blocks)),
        "numeric_tokens_match": extract_numbers(a) == extract_numbers(b),
        "date_tokens_match": extract_dates(a) == extract_dates(b),
        "technical_terms_match": extract_terms(a) == extract_terms(b),
        "primary_text_length": len(a),
        "secondary_text_length": len(b),
    }
    return metrics


def should_trigger_vl(
    primary: OCRImageResult,
    secondary: OCRImageResult,
    metrics: dict,
    cfg: OCRConfig,
    thumbnail_suspected: bool,
) -> bool:
    if not cfg.enable_vl_adjudication:
        return False
    if thumbnail_suspected:
        return True
    if primary.status != "success" or secondary.status != "success":
        return True
    if primary.text.strip() or secondary.text.strip():
        return True
    if float(metrics.get("agreement_score") or 0.0) < cfg.agreement_threshold:
        return True
    if int(metrics.get("exclusive_character_count") or 0) > cfg.exclusive_character_threshold:
        return True
    if not metrics.get("numeric_tokens_match"):
        return True
    if not metrics.get("date_tokens_match"):
        return True
    if not metrics.get("technical_terms_match"):
        return True
    if int(metrics.get("block_count_delta") or 0) >= 3:
        return True
    if primary.mean_confidence is not None and primary.mean_confidence < cfg.standard_confidence_threshold:
        return True
    return False


def adjudicate(
    primary: OCRImageResult,
    secondary: OCRImageResult,
    vl_result: OCRImageResult,
    metrics: dict,
    cfg: OCRConfig,
) -> tuple[str, str, str, bool]:
    p_text = primary.text.strip()
    s_text = secondary.text.strip()
    v_text = vl_result.text.strip()
    p_norm = normalize_text(p_text)
    s_norm = normalize_text(s_text)
    v_norm = normalize_text(v_text)

    if primary.status == "success" and secondary.status == "success" and p_norm == s_norm:
        return p_text or s_text, "ppocr+apple_vision", "primary-secondary-agreement", False
    if vl_result.status == "success":
        if p_norm and p_norm == v_norm:
            return p_text, "ppocr", "primary-vl-agreement", False
        if s_norm and s_norm == v_norm:
            return s_text, "apple_vision", "secondary-vl-agreement", False
    if not metrics.get("numeric_tokens_match") or not metrics.get("date_tokens_match") or not metrics.get("technical_terms_match"):
        if vl_result.status != "success":
            return p_text or s_text, "uncertain", "critical-token-conflict-without-vl", True
    if primary.status == "success" and float(metrics.get("agreement_score") or 0.0) >= cfg.agreement_threshold:
        return p_text, "ppocr", "high-primary-secondary-agreement", False
    if primary.status == "success" and secondary.status != "success":
        return p_text, "ppocr", "secondary-unavailable", True
    if secondary.status == "success":
        return s_text, "apple_vision", "primary-unavailable", True
    return p_text or s_text or v_text, "uncertain", "no-successful-traditional-ocr", True


def build_uncertain_span(
    image_path: Path,
    primary: OCRImageResult,
    secondary: OCRImageResult,
    vl_result: OCRImageResult,
    selected: str,
    reason: str,
    *,
    needs_human: bool,
) -> OCRUncertainSpan:
    bbox = union_bbox([*primary.blocks, *secondary.blocks]) or _whole_image_bbox(image_path)
    crop_path = create_region_crop(image_path, bbox)
    candidates = []
    for source, result in (("ppocr", primary), ("apple_vision", secondary), ("paddleocr_vl", vl_result)):
        if result.status == "success":
            candidates.append({"text": result.text, "source": source})
        elif result.status not in {"not_triggered"}:
            candidates.append({"text": "", "source": source, "error": result.error})
    return OCRUncertainSpan(
        bbox=[round(float(value), 2) for value in bbox],
        crop_path=crop_path,
        candidates=candidates,
        selected=selected,
        reason=reason,
        needs_human_review=needs_human,
    )


def rerun_uncertain_region(
    image_path: Path,
    span: OCRUncertainSpan,
    image_index: int,
    *,
    primary: OCREngine,
    secondary: OCREngine | None,
    vl_engine: OCREngine | None,
    cfg: OCRConfig,
) -> list[dict]:
    if not span.crop_path:
        return []
    crop_path = image_path.parent / span.crop_path
    if not crop_path.exists():
        return []
    scaled_path = create_scaled_region(crop_path, cfg.local_rerun_scale or cfg.rerun_scale or 2.0)
    candidates: list[dict] = []
    for source, engine in (("ppocr_rerun", primary), ("apple_vision_rerun", secondary), ("paddleocr_vl_rerun", vl_engine)):
        if engine is None:
            continue
        try:
            result = engine.recognize(scaled_path, image_index)
            candidates.append(
                {
                    "text": result.text,
                    "source": source,
                    "status": result.status,
                    "confidence": result.mean_confidence,
                    "error": result.error,
                    "scaled_crop_path": str(scaled_path.relative_to(image_path.parent)),
                }
            )
        except Exception as exc:
            candidates.append(
                {
                    "text": "",
                    "source": source,
                    "status": "failed",
                    "confidence": None,
                    "error": f"{type(exc).__name__}: {exc}",
                    "scaled_crop_path": str(scaled_path.relative_to(image_path.parent)),
                }
            )
    return candidates


def create_region_crop(image_path: Path, bbox: list[float]) -> str:
    review_dir = ensure_dir(image_path.parent / "review")
    path = _next_region_crop_path(review_dir)
    with Image.open(image_path) as img:
        width, height = img.size
        x1, y1, x2, y2 = _clamp_bbox(bbox, width, height, pad=8)
        img.crop((x1, y1, x2, y2)).save(path, format="PNG")
    return str(path.relative_to(image_path.parent))


def create_scaled_region(crop_path: Path, scale: float) -> Path:
    scale = max(1.0, min(float(scale), 3.0))
    scaled_path = crop_path.with_name(f"{crop_path.stem}_x{scale:g}{REGION_CROP_SUFFIX}")
    if scaled_path.exists():
        return scaled_path
    with Image.open(crop_path) as img:
        width, height = img.size
        target_size = _capped_scaled_size(width, height, scale, REGION_RERUN_MAX_SIDE)
        resized = img.resize(target_size, Image.Resampling.LANCZOS)
        resized.save(scaled_path, format="PNG")
    return scaled_path


def _next_region_crop_path(review_dir: Path) -> Path:
    index = 1
    while True:
        candidate = review_dir / f"region_{index:03d}{REGION_CROP_SUFFIX}"
        legacy_webp = review_dir / f"region_{index:03d}.webp"
        if not candidate.exists() and not legacy_webp.exists():
            return candidate
        index += 1


def _capped_scaled_size(width: int, height: int, scale: float, max_side: int) -> tuple[int, int]:
    scaled_width = max(1, int(width * scale))
    scaled_height = max(1, int(height * scale))
    longest = max(scaled_width, scaled_height)
    if longest <= max_side:
        return scaled_width, scaled_height
    factor = max_side / longest
    return max(1, int(scaled_width * factor)), max(1, int(scaled_height * factor))


def create_overlay(image_path: Path, primary: OCRImageResult, secondary: OCRImageResult, spans: list[OCRUncertainSpan]) -> str:
    path = image_path.parent / "review" / f"{image_path.stem}_ocr_overlay.png"
    ensure_dir(path.parent)
    with Image.open(image_path).convert("RGB") as img:
        draw = ImageDraw.Draw(img)
        for block in primary.blocks:
            box = block_bbox(block)
            if box:
                draw.rectangle(box, outline=(255, 0, 0), width=3)
        for block in secondary.blocks:
            box = block_bbox(block)
            if box:
                draw.rectangle(box, outline=(0, 90, 255), width=3)
        for span in spans:
            if span.bbox:
                draw.rectangle(span.bbox, outline=(255, 180, 0), width=5)
        draw.rectangle([8, 8, 390, 94], fill=(255, 255, 255))
        draw.text((18, 18), "red: PP-OCR | blue: Apple Vision | yellow: conflict", fill=(0, 0, 0))
        img.save(path)
    return str(path.relative_to(image_path.parent))


def evidence_from_result(result: OCRImageResult) -> OCREngineEvidence:
    return OCREngineEvidence(
        status=result.status,
        engine=result.engine,
        engine_version=result.engine_version,
        model=result.model,
        text=result.text,
        blocks=result.blocks,
        mean_confidence=result.mean_confidence,
        elapsed_seconds=result.elapsed_seconds,
        error=result.error,
    )


def normalize_text(text: str) -> str:
    return re.sub(r"\s+", "", text.translate(PUNCT_TRANSLATION)).strip()


def extract_numbers(text: str) -> list[str]:
    return re.findall(r"\d+(?:\.\d+)?", text)


def extract_dates(text: str) -> list[str]:
    return re.findall(r"(?:20\d{2})[-/.年]?\d{1,2}[-/.月]?\d{1,2}日?", text)


def extract_terms(text: str) -> list[str]:
    normalized = text.lower()
    found = []
    for term in TECH_TERMS:
        if term.lower() in normalized:
            found.append(term)
    return sorted(found)


def union_bbox(blocks: list[dict]) -> list[float] | None:
    boxes = [box for block in blocks if (box := block_bbox(block))]
    if not boxes:
        return None
    return [min(box[0] for box in boxes), min(box[1] for box in boxes), max(box[2] for box in boxes), max(box[3] for box in boxes)]


def block_bbox(block: dict) -> list[float] | None:
    box = block.get("box")
    if not box:
        return None
    if isinstance(box, list) and len(box) == 4 and all(isinstance(value, (int, float)) for value in box):
        x1, y1, x2, y2 = [float(value) for value in box]
        return [x1, y1, x2, y2]
    if isinstance(box, list) and len(box) >= 4 and all(isinstance(point, list) and len(point) >= 2 for point in box):
        xs = [float(point[0]) for point in box]
        ys = [float(point[1]) for point in box]
        return [min(xs), min(ys), max(xs), max(ys)]
    return None


def _exclusive_character_count(a: str, b: str) -> int:
    matcher = SequenceMatcher(None, a, b)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return max(len(a), len(b)) - matched


def _image_size(image_path: Path) -> tuple[int | None, int | None]:
    try:
        with Image.open(image_path) as img:
            return img.size
    except Exception:
        return None, None


def _whole_image_bbox(image_path: Path) -> list[float]:
    width, height = _image_size(image_path)
    return [0.0, 0.0, float(width or 0), float(height or 0)]


def _clamp_bbox(bbox: list[float], width: int, height: int, *, pad: int) -> tuple[int, int, int, int]:
    x1, y1, x2, y2 = bbox
    return (
        max(0, int(x1) - pad),
        max(0, int(y1) - pad),
        min(width, int(x2) + pad),
        min(height, int(y2) + pad),
    )


def _not_available(image_path: Path, image_index: int, engine: str, reason: str) -> OCRImageResult:
    return OCRImageResult(
        image_index=image_index,
        image_path=image_path.name,
        engine=engine,
        status="failed",
        text="",
        elapsed_seconds=0.0,
        needs_review=True,
        error=reason,
    )


def _not_triggered(image_path: Path, image_index: int, engine: str) -> OCRImageResult:
    return OCRImageResult(
        image_index=image_index,
        image_path=image_path.name,
        engine=engine,
        status="not_triggered",
        text="",
        elapsed_seconds=0.0,
        needs_review=False,
    )
