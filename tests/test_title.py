from xhs_archive.title import extract_light_structured, generate_summary_title


def test_title_prefers_meaningful_original_title() -> None:
    assert generate_summary_title("字节后端一面复盘", "", "", "abcdef123456") == "字节后端一面复盘"


def test_title_falls_back_to_keywords() -> None:
    title = generate_summary_title("", "腾讯 后端 二面 面经", "", "abcdef123456")
    assert "腾讯" in title
    assert "后端" in title


def test_extract_light_structured_is_conservative() -> None:
    data = extract_light_structured("美团后端一面，最后拿到 offer")
    assert data["company"] == "美团"
    assert data["position"] == "后端"
    assert data["interview_round"] == "一面"
    assert data["result"] == "疑似通过"
