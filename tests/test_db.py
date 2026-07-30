from xhs_archive.db import ArchiveDB
from xhs_archive.models import ImageMeta


def test_upsert_discovered_note_is_idempotent(tmp_path) -> None:
    db = ArchiveDB(tmp_path / "state.db")
    db.upsert_discovered_note(
        note_id="abc123456789",
        board_id="board",
        url="https://www.xiaohongshu.com/explore/abc123456789",
        canonical_url="https://www.xiaohongshu.com/explore/abc123456789",
    )
    db.upsert_discovered_note(
        note_id="abc123456789",
        board_id="board",
        url="https://www.xiaohongshu.com/explore/abc123456789",
        canonical_url="https://www.xiaohongshu.com/explore/abc123456789",
    )
    assert db.status_counts()["total"] == 1
    db.close()


def test_recover_incomplete_states(tmp_path) -> None:
    db = ArchiveDB(tmp_path / "state.db")
    db.upsert_discovered_note(
        note_id="abc123456789",
        board_id="board",
        url="https://www.xiaohongshu.com/explore/abc123456789",
        canonical_url="https://www.xiaohongshu.com/explore/abc123456789",
    )
    db.update_note_fields("abc123456789", stage="fetching")
    db.recover_incomplete()
    assert db.get_note("abc123456789")["stage"] == "discovered"
    db.close()


def test_image_upsert_and_ocr_update(tmp_path) -> None:
    db = ArchiveDB(tmp_path / "state.db")
    db.upsert_discovered_note(note_id="abc123456789", board_id="board", url="u", canonical_url="u")
    db.upsert_image("abc123456789", ImageMeta(position=1, local_path="01.webp", download_status="success"))
    db.upsert_image("abc123456789", ImageMeta(position=1, local_path="01.webp", download_status="success"))
    db.update_image_ocr("abc123456789", 1, status="success", engine="mock", confidence=0.9)
    images = db.list_images("abc123456789")
    assert len(images) == 1
    assert images[0]["ocr_status"] == "success"
    db.close()


def test_excluded_note_is_not_counted_as_active(tmp_path) -> None:
    db = ArchiveDB(tmp_path / "state.db")
    db.upsert_discovered_note(note_id="abc123456789", board_id="board", url="u", canonical_url="u")
    db.upsert_discovered_note(note_id="def123456789", board_id="board", url="u2", canonical_url="u2")
    db.upsert_image("def123456789", ImageMeta(position=1, local_path="01.webp", download_status="success"))
    db.exclude_note("def123456789", reason="accidental browser click", source="user")

    counts = db.status_counts()
    assert counts["total"] == 1
    assert counts["total_including_excluded"] == 2
    assert counts["excluded_notes"] == 1
    assert counts["images_total"] == 0
    assert counts["images_total_including_excluded"] == 1
    assert db.count_notes("board") == 1
    assert db.existing_note_ids("board") == {"abc123456789"}
    assert [row["note_id"] for row in db.list_notes(None)] == ["abc123456789"]
    assert db.get_note("def123456789")["stage"] == "excluded"
    db.close()
