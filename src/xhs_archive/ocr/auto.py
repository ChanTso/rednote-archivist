from __future__ import annotations

from .base import NullOCREngine, OCREngine


def create_ocr_engine(name: str = "auto") -> OCREngine:
    normalized = (name or "auto").lower()
    if normalized in {"none", "off", "disabled"}:
        return NullOCREngine("OCR disabled by command line")
    if normalized in {"paddle", "ppocr", "pp-ocrv6", "paddleocr"}:
        from .paddle_standard import PaddleStandardOCREngine

        return PaddleStandardOCREngine()
    if normalized in {"pp-ocrv6-medium", "paddle-medium", "ppocr-medium"}:
        from .paddle_standard import PaddleStandardOCREngine

        return PaddleStandardOCREngine(model_size="medium")
    if normalized in {"apple", "apple-vision", "vision"}:
        from .apple_vision import AppleVisionOCREngine

        return AppleVisionOCREngine()
    if normalized in {"rapid", "rapidocr", "onnx"}:
        from .rapidocr import RapidOCREngine

        return RapidOCREngine()
    if normalized in {"vl", "paddleocr-vl", "paddle-vl"}:
        from .paddle_vl import PaddleVLOCREngine

        return PaddleVLOCREngine()
    errors: list[str] = []
    for engine_name, factory in (
        ("paddle", lambda: __import__("xhs_archive.ocr.paddle_standard", fromlist=["PaddleStandardOCREngine"]).PaddleStandardOCREngine()),
        ("rapidocr", lambda: __import__("xhs_archive.ocr.rapidocr", fromlist=["RapidOCREngine"]).RapidOCREngine()),
    ):
        try:
            return factory()
        except Exception as exc:
            errors.append(f"{engine_name}: {type(exc).__name__}: {exc}")
    return NullOCREngine("; ".join(errors) or "No OCR backend available")
