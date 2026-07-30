from __future__ import annotations

import importlib.metadata
import time
from pathlib import Path
from typing import Any

from xhs_archive.models import OCRImageResult
from xhs_archive.ocr.base import OCREngine


class PaddleStandardOCREngine(OCREngine):
    name = "pp-ocrv6"
    model = "PaddleOCR"

    def __init__(self, *, model_size: str = "standard") -> None:
        from paddleocr import PaddleOCR

        self.model_size = model_size
        if model_size == "medium":
            self.name = "pp-ocrv6-medium"
            self.model = "PP-OCRv6 medium"
        try:
            self._engine = PaddleOCR(
                lang="ch",
                ocr_version="PP-OCRv6",
                use_doc_orientation_classify=False,
                use_doc_unwarping=False,
                use_textline_orientation=False,
            )
        except TypeError:
            self._engine = PaddleOCR(lang="ch")
        try:
            self._version = importlib.metadata.version("paddleocr")
        except importlib.metadata.PackageNotFoundError:
            self._version = None

    def recognize(self, image_path: Path, image_index: int) -> OCRImageResult:
        start = time.monotonic()
        try:
            raw = self._predict(image_path)
            text, blocks, confidence = _normalize_paddle_result(raw)
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

    def _predict(self, image_path: Path) -> Any:
        if hasattr(self._engine, "predict"):
            return self._engine.predict(str(image_path))
        if hasattr(self._engine, "ocr"):
            return self._engine.ocr(str(image_path), cls=True)
        raise RuntimeError("Unsupported PaddleOCR API")


def _normalize_paddle_result(raw: Any) -> tuple[str, list[dict], float | None]:
    blocks: list[dict] = []
    texts: list[str] = []
    confidences: list[float] = []

    def visit(item: Any) -> None:
        if item is None:
            return
        if hasattr(item, "keys") and not isinstance(item, dict):
            try:
                item = {key: item[key] for key in item.keys()}
            except Exception:
                pass
        if isinstance(item, dict):
            if isinstance(item.get("rec_texts"), list):
                rec_texts = item.get("rec_texts") or []
                rec_scores = item.get("rec_scores") or []
                rec_boxes = item.get("rec_polys") or item.get("rec_boxes") or []
                for idx, text in enumerate(rec_texts):
                    if not isinstance(text, str):
                        continue
                    score = rec_scores[idx] if idx < len(rec_scores) else None
                    box = rec_boxes[idx] if idx < len(rec_boxes) else None
                    if hasattr(box, "tolist"):
                        box = box.tolist()
                    texts.append(text)
                    if isinstance(score, (int, float)):
                        confidences.append(float(score))
                    blocks.append({"text": text, "score": float(score) if isinstance(score, (int, float)) else None, "box": box})
                return
            text = item.get("text") or item.get("rec_text") or item.get("transcription")
            score = item.get("confidence") or item.get("score") or item.get("rec_score")
            if isinstance(text, str):
                texts.append(text)
                blocks.append({"text": text, "score": float(score) if isinstance(score, (int, float)) else None})
            if isinstance(score, (int, float)):
                confidences.append(float(score))
            for value in item.values():
                if isinstance(value, (list, tuple, dict)):
                    visit(value)
        elif isinstance(item, (list, tuple)):
            if len(item) >= 2 and isinstance(item[1], (list, tuple)) and item[1] and isinstance(item[1][0], str):
                texts.append(item[1][0])
                if len(item[1]) > 1 and isinstance(item[1][1], (int, float)):
                    confidences.append(float(item[1][1]))
                blocks.append({"raw": repr(item)[:1000]})
            else:
                for child in item:
                    visit(child)

    visit(raw)
    mean = sum(confidences) / len(confidences) if confidences else None
    return "\n".join(texts).strip(), blocks, mean
