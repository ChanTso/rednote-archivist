from __future__ import annotations

import mimetypes
import re
from pathlib import Path
from typing import Callable

import httpx
from PIL import Image

from .models import ImageCandidate, ImageMeta
from .utils import ensure_dir, file_sha256

BytesFetcher = Callable[[str], bytes]


def _extension_from_content_type(content_type: str | None) -> str:
    if not content_type:
        return ".webp"
    ext = mimetypes.guess_extension(content_type.split(";")[0].strip())
    if ext == ".jpe":
        return ".jpg"
    return ext or ".webp"


def _verify_image(path: Path) -> tuple[int, int, str | None]:
    with Image.open(path) as img:
        img.verify()
    with Image.open(path) as img:
        return img.size[0], img.size[1], img.format


def _extension_from_format(image_format: str | None, fallback: str = ".webp") -> str:
    normalized = (image_format or "").upper()
    return {
        "JPEG": ".jpg",
        "JPG": ".jpg",
        "PNG": ".png",
        "WEBP": ".webp",
        "GIF": ".gif",
    }.get(normalized, fallback)


def _candidate_from_url(index: int, url: str) -> ImageCandidate:
    return ImageCandidate(position=index, url=url, source_variant="legacy-url", score=0)


def _group_candidates(candidates: list[ImageCandidate]) -> list[list[ImageCandidate]]:
    grouped: dict[int, list[ImageCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.position, []).append(candidate)
    groups: list[list[ImageCandidate]] = []
    for position in sorted(grouped):
        seen: set[str] = set()
        unique: list[ImageCandidate] = []
        for candidate in sorted(grouped[position], key=lambda item: item.score, reverse=True):
            if candidate.url in seen:
                continue
            seen.add(candidate.url)
            unique.append(candidate)
        groups.append(unique)
    return groups


THUMBNAIL_URL_RE = re.compile(
    r"(thumb|thumbnail|small|avatar|icon|resize|crop|quality|q_\d+|w_\d+|h_\d+|/s\d+|prv|preview|!.*prv)",
    re.I,
)


def _thumbnail_reasons(candidate: ImageCandidate, width: int, height: int, file_size: int, *, has_better_candidate: bool) -> list[str]:
    reasons: list[str] = []
    if THUMBNAIL_URL_RE.search(candidate.url):
        reasons.append("url_has_thumbnail_or_transform_marker")
    if candidate.display_width and width < int(candidate.display_width * 0.9):
        reasons.append("downloaded_width_smaller_than_display_width")
    if candidate.display_height and height < int(candidate.display_height * 0.9):
        reasons.append("downloaded_height_smaller_than_display_height")
    if candidate.declared_width and width < int(candidate.declared_width * 0.9):
        reasons.append("downloaded_width_smaller_than_declared_width")
    if candidate.declared_height and height < int(candidate.declared_height * 0.9):
        reasons.append("downloaded_height_smaller_than_declared_height")
    if max(width, height) < 720:
        reasons.append("native_resolution_too_small_for_text_ocr")
    if file_size < 30_000 and max(width, height) < 1000:
        reasons.append("file_size_small_for_text_image")
    if has_better_candidate and candidate.source_variant in {"current-src", "legacy-url"}:
        reasons.append("higher_priority_candidate_available")
    return reasons


def download_image_candidates(
    image_candidates: list[ImageCandidate],
    output_dir: Path,
    *,
    fetch_bytes: BytesFetcher | None = None,
    timeout_seconds: int = 60,
) -> list[ImageMeta]:
    ensure_dir(output_dir)
    results: list[ImageMeta] = []
    client = None if fetch_bytes else httpx.Client(follow_redirects=True, timeout=timeout_seconds)
    try:
        for group in _group_candidates(image_candidates):
            if not group:
                continue
            position = group[0].position
            fallback_thumbnail: ImageMeta | None = None
            last_error: str | None = None
            selected: ImageMeta | None = None
            for candidate_index, candidate in enumerate(group):
                try:
                    content_type = None
                    if fetch_bytes:
                        content = fetch_bytes(candidate.url)
                    else:
                        assert client is not None
                        response = client.get(candidate.url)
                        response.raise_for_status()
                        content = response.content
                        content_type = response.headers.get("content-type")
                    tmp_path = output_dir / f"{position:02d}.download"
                    tmp_path.write_bytes(content)
                    width, height, image_format = _verify_image(tmp_path)
                    suffix = _extension_from_format(image_format, _extension_from_content_type(content_type))
                    path = output_dir / f"{position:02d}{suffix}"
                    if path.exists():
                        path.unlink()
                    tmp_path.replace(path)
                    file_size = path.stat().st_size
                    reasons = _thumbnail_reasons(
                        candidate,
                        width,
                        height,
                        file_size,
                        has_better_candidate=candidate_index > 0 or any(item.score > candidate.score for item in group),
                    )
                    meta = ImageMeta(
                        position=position,
                        source_url=candidate.url,
                        source_variant=candidate.source_variant,
                        display_width=candidate.display_width,
                        display_height=candidate.display_height,
                        declared_width=candidate.declared_width,
                        declared_height=candidate.declared_height,
                        local_path=path.name,
                        sha256=file_sha256(path),
                        width=width,
                        height=height,
                        downloaded_width=width,
                        downloaded_height=height,
                        file_size=file_size,
                        format=image_format,
                        is_probable_thumbnail=bool(reasons),
                        thumbnail_reason=";".join(reasons) if reasons else None,
                        download_status="success",
                    )
                    if reasons:
                        fallback_thumbnail = meta
                        continue
                    selected = meta
                    break
                except Exception as exc:  # pragma: no cover - exercised in integration/live paths
                    last_error = f"{type(exc).__name__}: {exc}"
            if selected:
                results.append(selected)
            elif fallback_thumbnail:
                results.append(fallback_thumbnail)
            else:
                first = group[0]
                results.append(
                    ImageMeta(
                        position=position,
                        source_url=first.url,
                        source_variant=first.source_variant,
                        display_width=first.display_width,
                        display_height=first.display_height,
                        declared_width=first.declared_width,
                        declared_height=first.declared_height,
                        download_status="failed",
                        last_error=last_error or "no image candidate could be downloaded",
                    )
                )
    finally:
        if client:
            client.close()
    return results


def download_images(
    image_urls: list[str],
    output_dir: Path,
    *,
    fetch_bytes: BytesFetcher | None = None,
    timeout_seconds: int = 60,
) -> list[ImageMeta]:
    candidates = [_candidate_from_url(index, url) for index, url in enumerate(image_urls, start=1)]
    return download_image_candidates(candidates, output_dir, fetch_bytes=fetch_bytes, timeout_seconds=timeout_seconds)
