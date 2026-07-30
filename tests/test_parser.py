from pathlib import Path

from xhs_archive.parser import parse_board_links, parse_note_html, parse_note_state

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_board_links_dedupes_and_preserves_order() -> None:
    html = (FIXTURES / "board.html").read_text(encoding="utf-8")
    links = parse_board_links(html, "https://www.xiaohongshu.com/board/demo")
    assert [note_id for note_id, _ in links] == [
        "111111111111111111111111",
        "222222222222222222222222",
        "333333333333333333333333",
    ]
    assert links[1][1] == "https://www.xiaohongshu.com/explore/222222222222222222222222"


def test_parse_note_fixture() -> None:
    html = (FIXTURES / "note.html").read_text(encoding="utf-8")
    note = parse_note_html(html, "https://www.xiaohongshu.com/explore/111111111111111111111111")
    assert note.note_id == "111111111111111111111111"
    assert note.title == "示例后端面试复盘"
    assert note.author_name == "example-user"
    assert note.author_display_id == "0000000000"
    assert note.author_profile_id == "000000000000000000000000"
    assert note.publish_time_iso == "2025-03-28"
    assert "完整网页正文" in (note.body_text or "")
    assert note.image_urls == [
        "https://sns-img.example.com/notes/01.webp",
        "https://sns-img.example.com/notes/02.webp",
    ]


def test_parse_note_state_prefers_carousel_images_only() -> None:
    note = parse_note_state(
        "444444444444444444444444",
        "https://www.xiaohongshu.com/board/b/444444444444444444444444?xsec_token=t",
        {
            "title": "示例春招后端面经",
            "desc": "合成正文 #示例[话题]#",
            "time": 1777389694000,
            "user": {"userId": "555555555555555555555555", "nickname": "example-user"},
            "tagList": [{"name": "示例"}],
            "imageList": [
                {
                    "infoList": [
                        {"imageScene": "WB_PRV", "url": "http://example.com/prv.webp"},
                        {"imageScene": "WB_DFT", "url": "http://example.com/dft.webp"},
                    ]
                }
            ],
            "xsecToken": "t",
        },
    )
    assert note.title == "示例春招后端面经"
    assert note.author_profile_id == "555555555555555555555555"
    assert note.publish_time_iso == "2026-04-28"
    assert note.image_urls == ["https://example.com/dft.webp"]
