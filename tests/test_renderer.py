from pathlib import Path

import yaml
from PIL import Image

from xhs_archive.models import OCRImageResult
from xhs_archive.renderer import render_note
from xhs_archive.utils import write_json


def test_render_markdown_keeps_front_matter_body_and_images(tmp_path: Path) -> None:
    raw = {
        "note_id": "111111111111111111111111",
        "canonical_url": "https://www.xiaohongshu.com/explore/111111111111111111111111",
        "title": "示例后端面试复盘",
        "summary_title": "示例后端面试复盘",
        "author_name": "example-user",
        "author_display_id": "0000000000",
        "author_profile_id": "000000000000000000000000",
        "publish_time_raw": "2025-03-28",
        "publish_time_iso": "2025-03-28",
        "body_text": "完整网页正文，不做摘要，不省略。",
        "image_count": 1,
        "image_urls": ["https://sns-img.example.com/notes/01.webp"],
    }
    write_json(tmp_path / "raw.json", raw)
    write_json(
        tmp_path / "ocr.json",
        [
            OCRImageResult(
                image_index=1,
                image_path="01.webp",
                engine="mock",
                status="success",
                text="图片中的中文文字",
                mean_confidence=0.91,
            ).model_dump()
        ],
    )
    Image.new("RGB", (100, 50), color="white").save(tmp_path / "01.webp")
    note_path = render_note(tmp_path)
    text = note_path.read_text(encoding="utf-8")
    front = text.split("---", 2)[1]
    data = yaml.safe_load(front)
    assert data["note_id"] == "111111111111111111111111"
    assert "完整网页正文，不做摘要，不省略。" in text
    assert "![图片 1](01.webp)" in text
