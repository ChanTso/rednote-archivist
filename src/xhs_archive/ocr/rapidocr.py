from __future__ import annotations

import importlib.metadata
import time
from pathlib import Path
from typing import Any

from xhs_archive.models import OCRImageResult
from xhs_archive.ocr.base import OCREngine


class RapidOCREngine(OCREngine):
    name = "rapidocr"
    model = "RapidOCR_ONNXRuntime"

    def __init__(self) -> None:
        try:
            from rapidocr import RapidOCR
        except ImportError:
            from rapidocr_onnxruntime import RapidOCR  # type: ignore

        self._engine = RapidOCR()
        for package in ("rapidocr", "rapidocr-onnxruntime"):
            try:
                self._version = importlib.metadata.version(package)
                break
            except importlib.metadata.PackageNotFoundError:
                self._version = None

    def recognize(self, image_path: Path, image_index: int) -> OCRImageResult:
        start = time.monotonic()
        try:
            raw = self._engine(str(image_path))
            text, blocks, confidence = _normalize_rapid_result(raw)
            needs_review = confidence is not None and confidence < 0.82
            return OCRImageResult(
                image_index=image_index,
                image_path=image_path.name,
                engine=self.name,
                engine_version=self._version,
                model=self.model,
                status="success",
                text=text,
                blocks=blocks,
                mean_confidence=confidence,
                elapsed_seconds=time.monotonic() - start,
                needs_review=needs_review,
            )
        except Exception as exc:
            return OCRImageResult(
                image_index=image_index,
                image_path=image_path.name,
                engine=self.name,
                engine_version=self._version,
                model=self.model,
                status="failed",
                elapsed_seconds=time.monotonic() - start,
                needs_review=True,
                error=f"{type(exc).__name__}: {exc}",
            )


def _normalize_rapid_result(raw: Any) -> tuple[str, list[dict], float | None]:
    if hasattr(raw, "txts"):
        txts = getattr(raw, "txts", None)
        raw_scores = getattr(raw, "scores", None)
        raw_boxes = getattr(raw, "boxes", None)
        texts = list(txts) if txts is not None else []
        scores = [float(score) for score in list(raw_scores) if isinstance(score, (int, float))] if raw_scores is not None else []
        blocks = []
        boxes = list(raw_boxes) if raw_boxes is not None else []
        for idx, text in enumerate(texts):
            box = boxes[idx] if idx < len(boxes) else None
            if hasattr(box, "tolist"):
                box = box.tolist()
            blocks.append({"text": str(text), "score": scores[idx] if idx < len(scores) else None, "box": box})
        mean = sum(scores) / len(scores) if scores else None
        return "\n".join(texts).strip(), blocks, mean
    if isinstance(raw, tuple):
        result = raw[0]
    else:
        result = raw
    texts: list[str] = []
    scores: list[float] = []
    blocks: list[dict] = []
    if isinstance(result, list):
        for item in result:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                text = item[1]
                score = item[2] if len(item) > 2 else None
                if isinstance(text, str):
                    texts.append(text)
                    blocks.append({"text": text, "score": score, "raw": repr(item)[:1000]})
                if isinstance(score, (int, float)):
                    scores.append(float(score))
    mean = sum(scores) / len(scores) if scores else None
    return "\n".join(texts).strip(), blocks, mean
