from xhs_archive.utils import canonical_note_url, extract_note_id, note_dir_name, safe_filename


def test_extract_note_id_and_canonical_url() -> None:
    url = "https://www.xiaohongshu.com/explore/111111111111111111111111?xsec_token=synthetic"
    assert extract_note_id(url) == "111111111111111111111111"
    assert canonical_note_url(url) == "https://www.xiaohongshu.com/explore/111111111111111111111111"


def test_safe_filename_removes_cross_platform_bad_chars() -> None:
    assert safe_filename(' a/b:c*? "x" ') == "a_b_c_x"


def test_same_nickname_different_author_ids_do_not_collide() -> None:
    one = note_dir_name(
        author_display_id="0000000001",
        author_profile_id=None,
        publish_time_iso="2025-03-28",
        summary_title="后端一面",
        note_id="noteaaa111",
    )
    two = note_dir_name(
        author_display_id="0000000002",
        author_profile_id=None,
        publish_time_iso="2025-03-28",
        summary_title="后端一面",
        note_id="notebbb222",
    )
    assert one != two
    assert "__noteaaa111" in one
    assert "__notebbb222" in two
