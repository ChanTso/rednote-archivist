from pathlib import Path

from PIL import Image

from xhs_archive.config import OCRConfig
from xhs_archive.ocr_review import classify_ocr_image, correct_supported_terms


def _image(tmp_path: Path) -> Path:
    path = tmp_path / "01.webp"
    Image.new("RGB", (1080, 1440), "white").save(path)
    return path


def _row(**overrides):
    row = {
        "position": 1,
        "local_path": "01.webp",
        "download_status": "success",
        "ocr_status": "needs_review",
        "ocr_engine": "ppocr+apple",
        "ocr_confidence": 0.9,
        "downloaded_width": 1080,
        "downloaded_height": 1440,
        "width": 1080,
        "height": 1440,
        "is_probable_thumbnail": 0,
        "thumbnail_reason": None,
    }
    row.update(overrides)
    return row


def _ocr_item(ppocr_text: str, apple_text: str, ppocr_blocks: list[dict] | None = None, apple_blocks: list[dict] | None = None) -> dict:
    return {
        "image_index": 1,
        "image_path": "01.webp",
        "native_width": 1080,
        "native_height": 1440,
        "engines": {
            "ppocr": {"status": "success", "text": ppocr_text, "blocks": ppocr_blocks or [{"text": ppocr_text, "box": [10, 10, 500, 60]}]},
            "apple_vision": {"status": "success", "text": apple_text, "blocks": apple_blocks or [{"text": apple_text, "box": [12, 12, 502, 62]}]},
            "paddleocr_vl": {"status": "failed", "text": "", "blocks": []},
        },
        "final_text": ppocr_text,
        "needs_human_review": True,
        "status": "needs_review",
    }


def _classify(tmp_path: Path, item: dict, row: dict | None = None):
    image = _image(tmp_path)
    return classify_ocr_image(
        note_id="note1",
        title="demo",
        source_url="https://example.com/note1",
        output_dir=tmp_path,
        image_path=image,
        ocr_item=item,
        image_row=row or _row(),
        cfg=OCRConfig(),
        crop_dir=tmp_path / "crops",
        exports_dir=tmp_path,
    )


def test_reclassify_ignores_formatting_only_difference(tmp_path: Path) -> None:
    record = _classify(tmp_path, _ocr_item("Redis，RDB\n缓存", "redis, rdb 缓存"))
    assert record["human_review_required"] is False
    assert record["review_level"] == "formatting_only"


def test_reclassify_flags_critical_number_difference(tmp_path: Path) -> None:
    record = _classify(tmp_path, _ocr_item("2025 年一面通过", "2026 年一面通过"))
    assert record["human_review_required"] is True
    assert "critical_token_conflict" in record["review_reasons"]


def test_manual_review_decision_clears_machine_high_risk(tmp_path: Path) -> None:
    item = _ocr_item("2025 年一面通过", "2026 年一面通过")
    item["manual_review"] = {
        "status": "corrected",
        "human_review_required": False,
        "reviewer": "codex",
        "decision": "manual_image_verified",
        "reason": "final text was checked against the source image",
    }
    record = _classify(tmp_path, item)
    assert record["human_review_required"] is False
    assert record["review_level"] == "manual_accepted"
    assert "critical_token_conflict" in record["machine_review_reasons"]
    assert record["manual_cleared_regions"]


def test_user_discarded_image_clears_review_with_discard_level(tmp_path: Path) -> None:
    item = _ocr_item("2025 年一面通过", "2026 年一面通过")
    item["manual_review"] = {
        "status": "discarded_by_user",
        "human_review_required": False,
        "reviewer": "user",
        "decision": "discard_irrelevant_image",
        "reason": "user confirmed this image is not useful for the archive",
    }
    record = _classify(tmp_path, item)
    assert record["human_review_required"] is False
    assert record["review_level"] == "discarded_by_user"
    assert "critical_token_conflict" in record["machine_review_reasons"]
    assert record["manual_cleared_regions"]


def test_reclassify_corrects_supported_mysql_variants(tmp_path: Path) -> None:
    item = _ocr_item("7. MysqI 的主从复制", "7. Mysql 的主从复制")
    record = _classify(tmp_path, item)
    assert record["human_review_required"] is False
    assert "MySQL" in record["final_text"]
    assert record["postprocess_corrections"]


def test_supported_sql_variant_correction() -> None:
    corrected, corrections = correct_supported_terms("慢5q排查，MNySQL事务")
    assert corrected == "慢SQL排查，MySQL事务"
    assert len(corrections) == 2


def test_supported_common_tech_term_corrections() -> None:
    corrected, corrections = correct_supported_terms("Al coding, Htps, guihub, python, java, bm25, mcp, LangOhain, websocket, mabatis")
    assert corrected == "AI coding, HTTPS, GitHub, Python, Java, BM25, MCP, LangChain, WebSocket, MyBatis"
    assert len(corrections) == 10


def test_reclassify_ignores_list_number_marker_difference(tmp_path: Path) -> None:
    record = _classify(tmp_path, _ocr_item("14. 了解 Spring AI 吗？", "了解 Spring Al 吗？"))
    assert record["human_review_required"] is False
    assert record["review_level"] in {"formatting_only", "minor_noncritical"}


def test_reclassify_flags_missing_whole_line(tmp_path: Path) -> None:
    ppocr_blocks = [
        {"text": "第一行说明", "box": [10, 10, 400, 50]},
        {"text": "这里是一整句很长的面试重点总结", "box": [10, 80, 800, 130]},
    ]
    apple_blocks = [{"text": "第一行说明", "box": [12, 12, 402, 52]}]
    record = _classify(tmp_path, _ocr_item("第一行说明\n这里是一整句很长的面试重点总结", "第一行说明", ppocr_blocks, apple_blocks))
    assert record["human_review_required"] is True
    assert "sentence_or_line_missing" in record["review_reasons"]


def test_reclassify_flags_severe_image_quality_risk(tmp_path: Path) -> None:
    record = _classify(
        tmp_path,
        _ocr_item("普通文本", "普通文本"),
        _row(
            downloaded_width=360,
            downloaded_height=240,
            width=360,
            height=240,
            is_probable_thumbnail=1,
            thumbnail_reason="native_resolution_too_small_for_text_ocr;file_size_small_for_text_image",
        ),
    )
    assert record["human_review_required"] is True
    assert "image_quality_risk" in record["review_reasons"]
