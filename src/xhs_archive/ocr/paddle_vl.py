from __future__ import annotations

import importlib.metadata
import time
from pathlib import Path

from xhs_archive.models import OCRImageResult
from xhs_archive.ocr.base import OCREngine


class PaddleVLOCREngine(OCREngine):
    name = "paddleocr-vl"
    model = "PaddleOCR-VL-1.6"

    def __init__(self) -> None:
        self.backend = "mlx-vlm"
        try:
            from paddleocr import PaddleOCRVL
        except ImportError as exc:
            raise RuntimeError("PaddleOCR-VL API is not importable") from exc
        self._engine = PaddleOCRVL()
        try:
            self._version = importlib.metadata.version("paddleocr")
        except importlib.metadata.PackageNotFoundError:
            self._version = None

    def recognize(self, image_path: Path, image_index: int) -> OCRImageResult:
        start = time.monotonic()
        try:
            raw = self._engine.predict(str(image_path))
            text = str(raw)
            return OCRImageResult(
                image_index=image_index,
                image_path=image_path.name,
                engine=self.name,
                engine_version=self._version,
                model=self.model,
                status="success",
                text=text,
                blocks=[{"raw": text[:4000]}],
                elapsed_seconds=time.monotonic() - start,
                needs_review=False,
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
