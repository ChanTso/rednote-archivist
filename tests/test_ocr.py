from pathlib import Path

from PIL import Image

from xhs_archive.config import OCRConfig
from xhs_archive.models import OCRImageResult
from xhs_archive.ocr.audit import REGION_RERUN_MAX_SIDE, audit_image, compare_results, create_region_crop, create_scaled_region
from xhs_archive.ocr.base import NullOCREngine
from xhs_archive.ocr.base import OCREngine


def test_null_ocr_engine_marks_review(tmp_path: Path) -> None:
    image = tmp_path / "01.webp"
    image.write_bytes(b"not really an image")
    result = NullOCREngine("disabled").recognize(image, 1)
    assert result.status == "failed"
    assert result.needs_review is True
    assert result.error == "disabled"


class StaticEngine(OCREngine):
    def __init__(self, name: str, text: str, blocks: list[dict] | None = None):
        self.name = name
        self.model = name
        self.text = text
        self.blocks = blocks or [{"text": text, "box": [10, 10, 100, 40], "score": 0.99}]

    def recognize(self, image_path: Path, image_index: int) -> OCRImageResult:
        return OCRImageResult(
            image_index=image_index,
            image_path=image_path.name,
            engine=self.name,
            model=self.model,
            status="success",
            text=self.text,
            blocks=self.blocks,
            mean_confidence=0.99,
            elapsed_seconds=0.01,
        )


def test_ocr_audit_accepts_independent_agreement(tmp_path: Path) -> None:
    image = tmp_path / "01.webp"
    Image.new("RGB", (400, 200), "white").save(image)
    result = audit_image(
        image,
        1,
        primary=StaticEngine("pp-ocrv6-medium", "Redis RDB"),
        secondary=StaticEngine("apple-vision", "Redis RDB"),
        vl_engine=None,
        cfg=OCRConfig(enable_vl_adjudication=True),
    )
    assert result.needs_human_review is False
    assert result.final_text == "Redis RDB"
    assert result.uncertain_spans == []
    assert result.audit["vl_requested"] is True
    assert result.engines["paddleocr_vl"].status == "failed"


def test_ocr_audit_records_uncertain_span_on_conflict(tmp_path: Path) -> None:
    image = tmp_path / "01.webp"
    Image.new("RGB", (400, 200), "white").save(image)
    result = audit_image(
        image,
        1,
        primary=StaticEngine("pp-ocrv6-medium", "Redis RDB"),
        secondary=StaticEngine("apple-vision", "Redis ROB"),
        vl_engine=None,
        cfg=OCRConfig(enable_vl_adjudication=True),
    )
    assert result.needs_human_review is True
    assert result.uncertain_spans
    assert Path(tmp_path / result.uncertain_spans[0].crop_path).exists()
    assert any(candidate["source"].endswith("_rerun") for candidate in result.uncertain_spans[0].candidates)


def test_compare_results_checks_technical_terms() -> None:
    primary = OCRImageResult(image_index=1, image_path="01.webp", engine="ppocr", status="success", text="MySQL Redis")
    secondary = OCRImageResult(image_index=1, image_path="01.webp", engine="vision", status="success", text="MysqI Redis")
    metrics = compare_results(primary, secondary, OCRConfig())
    assert metrics["technical_terms_match"] is False


def test_region_rerun_uses_png_and_caps_long_images(tmp_path: Path) -> None:
    image = tmp_path / "01.webp"
    Image.new("RGB", (120, 9000), "white").save(image)

    crop_relative = create_region_crop(image, [0, 0, 120, 9000])
    crop_path = tmp_path / crop_relative
    assert crop_path.suffix == ".png"
    assert crop_path.exists()

    scaled_path = create_scaled_region(crop_path, 3.0)
    assert scaled_path.suffix == ".png"
    with Image.open(scaled_path) as scaled:
        assert max(scaled.size) <= REGION_RERUN_MAX_SIDE
