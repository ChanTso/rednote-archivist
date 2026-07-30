from __future__ import annotations

import json
import os
import platform
import subprocess
import time
from pathlib import Path
from typing import Any

from xhs_archive.models import OCRImageResult
from xhs_archive.ocr.base import OCREngine


class AppleVisionOCREngine(OCREngine):
    name = "apple-vision"
    model = "VNRecognizeTextRequest-accurate"

    def __init__(self, *, language_correction: bool = False) -> None:
        self.language_correction = language_correction
        self._version = platform.mac_ver()[0] or None
        self._binary = _ensure_vision_binary()

    def recognize(self, image_path: Path, image_index: int) -> OCRImageResult:
        start = time.monotonic()
        if platform.system() != "Darwin":
            return OCRImageResult(
                image_index=image_index,
                image_path=image_path.name,
                engine=self.name,
                engine_version=self._version,
                model=self.model,
                status="failed",
                elapsed_seconds=time.monotonic() - start,
                needs_review=True,
                error="Apple Vision OCR is only available on macOS",
            )
        try:
            args = [str(self._binary), str(image_path)]
            if self.language_correction:
                args.append("--language-correction")
            completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=120)
            if completed.returncode != 0:
                raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}")
            raw = json.loads(completed.stdout)
            if raw.get("status") != "success":
                raise RuntimeError(str(raw.get("error") or "Vision OCR failed"))
            text, blocks, confidence = _normalize_vision_result(raw)
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


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _ensure_vision_binary() -> Path:
    root = _repo_root()
    source = root / "tools" / "vision-ocr" / "vision_ocr.swift"
    binary = root / ".build" / "vision-ocr"
    if binary.exists() and binary.stat().st_mtime >= source.stat().st_mtime:
        return binary
    binary.parent.mkdir(parents=True, exist_ok=True)
    module_cache = binary.parent / "swift-module-cache"
    module_cache.mkdir(parents=True, exist_ok=True)
    env = {
        **os.environ,
        "CLANG_MODULE_CACHE_PATH": str(module_cache),
        "SWIFT_MODULE_CACHE_PATH": str(module_cache),
    }
    completed = subprocess.run(
        ["xcrun", "swiftc", "-O", "-module-cache-path", str(module_cache), str(source), "-o", str(binary)],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    if completed.returncode != 0:
        raise RuntimeError(completed.stderr.strip() or completed.stdout.strip() or "swiftc failed")
    return binary


def _normalize_vision_result(raw: dict[str, Any]) -> tuple[str, list[dict], float | None]:
    blocks: list[dict] = []
    scores: list[float] = []
    for block in raw.get("blocks") or []:
        if not isinstance(block, dict):
            continue
        score = block.get("score")
        if isinstance(score, (int, float)):
            scores.append(float(score))
        blocks.append(block)
    text = str(raw.get("text") or "").strip()
    mean = sum(scores) / len(scores) if scores else None
    return text, blocks, mean
