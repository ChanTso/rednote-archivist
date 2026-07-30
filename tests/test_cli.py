import pytest
import typer

from xhs_archive.cli import _path_is_within, _validated_board_url


@pytest.mark.parametrize(
    "url",
    [
        "https://www.xiaohongshu.com/board/demo",
        "https://xiaohongshu.com/board/b/demo?tab=notes",
        "https://www.xiaohongshu.com.:443/board/demo",
    ],
)
def test_validated_board_url_accepts_expected_hosts(url: str) -> None:
    assert _validated_board_url(f"  {url}  ") == url


@pytest.mark.parametrize(
    "url",
    [
        "http://www.xiaohongshu.com/board/demo",
        "https://www.xiaohongshu.com.evil.example/board/demo",
        "https://www.xiaohongshu.com@evil.example/board/demo",
        "https://www.xiaohongshu.com/explore/demo",
        "not-a-url-containing-xiaohongshu.com",
    ],
)
def test_validated_board_url_rejects_untrusted_urls(url: str) -> None:
    with pytest.raises(typer.BadParameter):
        _validated_board_url(url)


def test_path_is_within_uses_path_boundaries() -> None:
    assert _path_is_within("/opt/conda/envs/archive", "/opt/conda")
    assert _path_is_within("/opt/conda", "/opt/conda")
    assert not _path_is_within("/opt/conda-evil/envs/archive", "/opt/conda")
