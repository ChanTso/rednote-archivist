from __future__ import annotations

import time
from abc import ABC, abstractmethod
from pathlib import Path

from xhs_archive.models import OCRImageResult


class OCREngine(ABC):
    name: str = "base"
    model: str | None = None

    @abstractmethod
    def recognize(self, image_path: Path, image_index: int) -> OCRImageResult:
        raise NotImplementedError


class NullOCREngine(OCREngine):
    name = "none"
    model = "none"

    def __init__(self, reason: str = "OCR backend unavailable"):
        self.reason = reason

    def recognize(self, image_path: Path, image_index: int) -> OCRImageResult:
        start = time.monotonic()
        return OCRImageResult(
            image_index=image_index,
            image_path=image_path.name,
            engine=self.name,
            model=self.model,
            status="failed",
            text="",
            mean_confidence=None,
            elapsed_seconds=time.monotonic() - start,
            needs_review=True,
            error=self.reason,
        )
