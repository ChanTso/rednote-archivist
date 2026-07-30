from pathlib import Path

from PIL import Image

from xhs_archive.collector import collect_from_html
from xhs_archive.db import ArchiveDB
from xhs_archive.models import ImageMeta, OCRImageResult
from xhs_archive.parser import parse_note_html
from xhs_archive.renderer import render_pending
from xhs_archive.utils import file_sha256, note_dir_name, write_json
from xhs_archive.verifier import verify

FIXTURES = Path(__file__).parent / "fixtures"


def test_fixture_collect_to_verify_pipeline(tmp_path: Path) -> None:
    db = ArchiveDB(tmp_path / "state.db")
    board_html = (FIXTURES / "board.html").read_text(encoding="utf-8")
    collect_result = collect_from_html(db, "https://www.xiaohongshu.com/board/demo", board_html, limit=1)
    assert collect_result["unique_note_count"] == 1

    note_html = (FIXTURES / "note.html").read_text(encoding="utf-8")
    parsed = parse_note_html(note_html, "https://www.xiaohongshu.com/explore/111111111111111111111111")
    output_dir = tmp_path / note_dir_name(
        author_display_id=parsed.author_display_id,
        author_profile_id=parsed.author_profile_id,
        publish_time_iso=parsed.publish_time_iso,
        summary_title=parsed.summary_title,
        note_id=parsed.note_id,
    )
    output_dir.mkdir()
    write_json(output_dir / "raw.json", parsed.model_dump())
    (output_dir / "page.html").write_text(note_html, encoding="utf-8")
    Image.new("RGB", (120, 80), "white").save(output_dir / "01.webp")
    Image.new("RGB", (120, 80), "white").save(output_dir / "02.webp")
    write_json(
        output_dir / "ocr.json",
        [
            OCRImageResult(image_index=1, image_path="01.webp", engine="mock", status="success", text="Redis", mean_confidence=0.9).model_dump(),
            OCRImageResult(image_index=2, image_path="02.webp", engine="mock", status="success", text="MySQL", mean_confidence=0.9).model_dump(),
        ],
    )
    db.update_note_from_parsed(parsed, board_id=collect_result["board_id"], output_dir=str(output_dir))
    db.upsert_image(
        parsed.note_id,
        ImageMeta(
            position=1,
            source_url=parsed.image_urls[0],
            local_path="01.webp",
            sha256=file_sha256(output_dir / "01.webp"),
            width=120,
            height=80,
            downloaded_width=120,
            downloaded_height=80,
            file_size=(output_dir / "01.webp").stat().st_size,
            format="WEBP",
            download_status="success",
            ocr_status="success",
        ),
    )
    db.upsert_image(
        parsed.note_id,
        ImageMeta(
            position=2,
            source_url=parsed.image_urls[1],
            local_path="02.webp",
            sha256=file_sha256(output_dir / "02.webp"),
            width=120,
            height=80,
            downloaded_width=120,
            downloaded_height=80,
            file_size=(output_dir / "02.webp").stat().st_size,
            format="WEBP",
            is_probable_thumbnail=True,
            thumbnail_reason="low_resolution_fixture",
            download_status="success",
            ocr_status="success",
        ),
    )
    db.update_note_fields(parsed.note_id, stage="ocr_done")

    render_result = render_pending(db)
    assert render_result["ok"] is True
    report = verify(db)
    assert report.ok is True
    assert len(report.warnings) == 1
    assert report.warnings[0].code == "thumbnail_suspected"
    assert db.get_note(parsed.note_id)["stage"] == "verified"
    db.close()
