from pathlib import Path

from PIL import Image

from xhs_archive.downloader import download_image_candidates
from xhs_archive.models import ImageCandidate


def _image_bytes(path: Path, size: tuple[int, int]) -> bytes:
    Image.new("RGB", size, "white").save(path, format="WEBP")
    return path.read_bytes()


def test_download_image_candidates_prefers_non_thumbnail_candidate(tmp_path: Path) -> None:
    tiny = _image_bytes(tmp_path / "tiny.webp", (200, 120))
    original = _image_bytes(tmp_path / "original.webp", (1200, 900))

    def fetch(url: str) -> bytes:
        return tiny if "prv" in url else original

    results = download_image_candidates(
        [
            ImageCandidate(position=1, url="https://example.com/image!prv.webp", source_variant="structured-wb-prv", score=90),
            ImageCandidate(position=1, url="https://example.com/image.webp", source_variant="structured-wb-dft", score=80),
        ],
        tmp_path / "out",
        fetch_bytes=fetch,
    )

    assert len(results) == 1
    assert results[0].source_variant == "structured-wb-dft"
    assert results[0].downloaded_width == 1200
    assert results[0].is_probable_thumbnail is False


def test_download_image_candidates_marks_thumbnail_when_no_better_candidate(tmp_path: Path) -> None:
    tiny = _image_bytes(tmp_path / "tiny.webp", (200, 120))
    results = download_image_candidates(
        [ImageCandidate(position=1, url="https://example.com/image!prv.webp", source_variant="structured-wb-prv", score=90)],
        tmp_path / "out",
        fetch_bytes=lambda _url: tiny,
    )

    assert results[0].download_status == "success"
    assert results[0].is_probable_thumbnail is True
    assert "thumbnail" in (results[0].thumbnail_reason or "")
