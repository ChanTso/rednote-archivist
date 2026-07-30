from xhs_archive.fetcher import _is_metadata_incomplete
from xhs_archive.models import ParsedNote


def test_metadata_incomplete_rejects_board_page_masquerading_as_note() -> None:
    parsed = ParsedNote(
        note_id="666666666666666666666666",
        url="https://www.xiaohongshu.com/explore/666666666666666666666666",
        canonical_url="https://www.xiaohongshu.com/explore/666666666666666666666666",
        title="示例收藏夹 -",
        body_text="我的收藏\n示例收藏夹\n笔记・61\n创建专辑",
        image_urls=["https://example.com/card.webp"],
        image_count=1,
    )
    assert _is_metadata_incomplete(parsed) is True


def test_metadata_incomplete_allows_authorless_normal_note_fallback() -> None:
    parsed = ParsedNote(
        note_id="note1",
        url="https://www.xiaohongshu.com/explore/note1",
        canonical_url="https://www.xiaohongshu.com/explore/note1",
        title="正常面经标题",
        body_text="一面问了 RAG 和 MySQL 索引，没有专辑列表。",
        image_urls=["https://example.com/note.webp"],
        image_count=1,
    )
    assert _is_metadata_incomplete(parsed) is False
