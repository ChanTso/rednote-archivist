from __future__ import annotations

import html
import json
import re
from collections.abc import Iterable
from datetime import datetime, timezone
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .models import ImageCandidate, ParsedNote
from .title import generate_summary_title
from .utils import canonical_note_url, extract_note_id, normalize_publish_time

IMAGE_URL_RE = re.compile(r"https?:\\?/\\?/[^\"'\s<>]+?\.(?:jpg|jpeg|png|webp)(?:\?[^\"'\s<>]*)?", re.I)


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    cleaned = re.sub(r"\s+", " ", html.unescape(text)).strip()
    return cleaned or None


def _dedupe(values: Iterable[str | None]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for value in values:
        if not value:
            continue
        item = html.unescape(value).replace("\\/", "/")
        if item.startswith("//"):
            item = "https:" + item
        if item not in seen:
            seen.add(item)
            out.append(item)
    return out


def _normalize_image_url(value: str | None) -> str | None:
    if not value:
        return None
    item = html.unescape(str(value)).replace("\\/", "/")
    if item.startswith("//"):
        item = "https:" + item
    if item.startswith("http://"):
        item = "https://" + item.removeprefix("http://")
    return item


def _dedupe_candidates(values: Iterable[ImageCandidate]) -> list[ImageCandidate]:
    seen: set[tuple[int, str]] = set()
    out: list[ImageCandidate] = []
    for candidate in sorted(values, key=lambda item: (item.position, -item.score)):
        url = _normalize_image_url(candidate.url)
        if not url:
            continue
        key = (candidate.position, url)
        if key in seen:
            continue
        seen.add(key)
        candidate.url = url
        out.append(candidate)
    return out


def _preferred_candidate_urls(candidates: list[ImageCandidate]) -> list[str]:
    urls: list[str] = []
    grouped: dict[int, list[ImageCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.position, []).append(candidate)
    for position in sorted(grouped):
        best = sorted(grouped[position], key=lambda item: item.score, reverse=True)[0]
        urls.append(best.url)
    return urls


def _meta(soup: BeautifulSoup, *names: str) -> str | None:
    for name in names:
        node = soup.find("meta", attrs={"property": name}) or soup.find("meta", attrs={"name": name})
        if node and node.get("content"):
            return _clean(str(node["content"]))
    return None


def _json_candidates(soup: BeautifulSoup) -> list[dict]:
    candidates: list[dict] = []
    for script in soup.find_all("script"):
        script_type = (script.get("type") or "").lower()
        text = script.string or script.get_text() or ""
        if not text.strip():
            continue
        if "json" in script_type:
            try:
                data = json.loads(text)
                if isinstance(data, list):
                    candidates.extend(item for item in data if isinstance(item, dict))
                elif isinstance(data, dict):
                    candidates.append(data)
            except json.JSONDecodeError:
                pass
        for marker in ("__INITIAL_STATE__", "window.__INITIAL_STATE__"):
            if marker in text:
                match = re.search(r"__INITIAL_STATE__\s*=\s*(\{.*?\})\s*(?:</script>|;)", text, re.S)
                if match:
                    try:
                        candidates.append(json.loads(match.group(1)))
                    except json.JSONDecodeError:
                        pass
    return candidates


def _walk_json_images(value: object) -> list[str]:
    found: list[str] = []
    if isinstance(value, dict):
        for key, child in value.items():
            if key.lower() in {"url", "src", "image", "imageurl", "originalurl"} and isinstance(child, str):
                if re.search(r"\.(jpg|jpeg|png|webp)(\?|$)", child, re.I):
                    found.append(child)
            found.extend(_walk_json_images(child))
    elif isinstance(value, list):
        for child in value:
            found.extend(_walk_json_images(child))
    return found


def _walk_first(value: object, keys: set[str]) -> str | None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key in keys and isinstance(child, str):
                return child
            nested = _walk_first(child, keys)
            if nested:
                return nested
    elif isinstance(value, list):
        for child in value:
            nested = _walk_first(child, keys)
            if nested:
                return nested
    return None


def parse_note_html(source_html: str, url: str) -> ParsedNote:
    note_id = extract_note_id(url)
    if not note_id:
        raise ValueError(f"Cannot parse note_id from URL: {url}")
    soup = BeautifulSoup(source_html, "lxml")
    json_data = _json_candidates(soup)

    title = _meta(soup, "og:title", "twitter:title") or _clean(soup.title.string if soup.title else None)
    if title:
        title = re.sub(r"[-_ ]?小红书.*$", "", title).strip() or title
    article = soup.find("article") or soup.find(attrs={"role": "article"})
    body = _clean(article.get_text("\n")) if article else None
    if not body:
        body = _meta(soup, "description", "og:description")
    if not body:
        body = _clean(soup.get_text("\n"))

    author_name = None
    author_display_id = None
    author_profile_id = None
    publish_raw = None
    for item in json_data:
        author = item.get("author") if isinstance(item, dict) else None
        if isinstance(author, dict):
            author_name = author_name or _clean(author.get("name") or author.get("nickname"))
            author_profile_id = author_profile_id or _clean(author.get("url") or author.get("identifier"))
        author_name = author_name or _clean(_walk_first(item, {"nickname", "authorName", "name"}))
        author_display_id = author_display_id or _clean(_walk_first(item, {"redId", "red_id", "displayId", "xhsId"}))
        author_profile_id = author_profile_id or _clean(_walk_first(item, {"userId", "user_id", "profileId"}))
        publish_raw = publish_raw or _clean(_walk_first(item, {"datePublished", "time", "publishTime", "publish_time"}))

    text = soup.get_text("\n")
    display_match = re.search(r"(?:小红书号|RED\s*ID)[:：\s]*([0-9A-Za-z_.-]{3,80})", text, re.I)
    if display_match:
        author_display_id = author_display_id or display_match.group(1)
    date_match = re.search(r"(20\d{2}[./年-]\d{1,2}[./月-]\d{1,2}日?|\d{1,2}[./-]\d{1,2})", text)
    if date_match:
        publish_raw = publish_raw or date_match.group(1)

    meta_images = [_meta(soup, "og:image", "twitter:image")]
    dom_images = [img.get("src") or img.get("data-src") or img.get("currentSrc") for img in soup.find_all("img")]
    script_images = IMAGE_URL_RE.findall(source_html)
    json_images: list[str] = []
    for item in json_data:
        json_images.extend(_walk_json_images(item))
    images = _dedupe([*meta_images, *json_images, *script_images, *dom_images])
    image_candidates = [
        ImageCandidate(position=index, url=url, source_variant="html-discovered", score=10)
        for index, url in enumerate(images, start=1)
    ]

    tags = _dedupe(match.group(1) for match in re.finditer(r"#([\w\u4e00-\u9fff-]{1,40})", text))
    summary = generate_summary_title(title, body, None, note_id)
    return ParsedNote(
        note_id=note_id,
        url=url,
        canonical_url=canonical_note_url(note_id),
        title=title,
        summary_title=summary,
        author_name=author_name,
        author_display_id=author_display_id,
        author_profile_id=author_profile_id,
        publish_time_raw=publish_raw,
        publish_time_iso=normalize_publish_time(publish_raw),
        body_text=body,
        tags=tags,
        image_urls=images,
        image_candidates=image_candidates,
        image_count=len(images),
    )


def _int_or_none(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _state_image_candidates(image: dict, position: int) -> list[ImageCandidate]:
    declared_width = _int_or_none(image.get("width") or image.get("originalWidth") or image.get("original_width"))
    declared_height = _int_or_none(image.get("height") or image.get("originalHeight") or image.get("original_height"))
    candidates: list[ImageCandidate] = []

    for key, variant, score in (
        ("urlOriginal", "structured-original", 120),
        ("url", "structured-url", 105),
        ("urlDefault", "structured-default", 95),
        ("urlPre", "structured-preview", 45),
    ):
        url = _normalize_image_url(image.get(key))
        if url:
            candidates.append(
                ImageCandidate(
                    position=position,
                    url=url,
                    source_variant=variant,
                    declared_width=declared_width,
                    declared_height=declared_height,
                    score=score,
                )
            )

    scene_scores = {
        "WB_ORI": ("structured-original", 115),
        "WB_DFT": ("structured-wb-dft", 100),
        "WB_PRV": ("structured-wb-prv", 55),
        "CRD_WM_WEBP": ("structured-card-webp", 40),
    }
    for info in image.get("infoList") or []:
        if not isinstance(info, dict):
            continue
        url = _normalize_image_url(info.get("url"))
        if not url:
            continue
        scene = str(info.get("imageScene") or "UNKNOWN")
        variant, score = scene_scores.get(scene, (f"structured-{scene.lower()}", 60))
        width = _int_or_none(info.get("width")) or declared_width
        height = _int_or_none(info.get("height")) or declared_height
        candidates.append(
            ImageCandidate(
                position=position,
                url=url,
                source_variant=variant,
                declared_width=width,
                declared_height=height,
                score=score,
            )
        )
    return candidates


def parse_note_state(note_id: str, url: str, state_note: dict) -> ParsedNote:
    title = _clean(state_note.get("title"))
    body = _clean(state_note.get("desc"))
    user = state_note.get("user") if isinstance(state_note.get("user"), dict) else {}
    tags = []
    for tag in state_note.get("tagList") or []:
        if isinstance(tag, dict) and tag.get("name"):
            tags.append(str(tag["name"]))
    image_candidates: list[ImageCandidate] = []
    for position, image in enumerate(state_note.get("imageList") or [], start=1):
        if not isinstance(image, dict):
            continue
        image_candidates.extend(_state_image_candidates(image, position))
    image_candidates = _dedupe_candidates(image_candidates)
    image_urls = _preferred_candidate_urls(image_candidates)
    publish_raw = None
    publish_iso = None
    time_value = state_note.get("time") or state_note.get("lastUpdateTime")
    if isinstance(time_value, (int, float)):
        publish_raw = str(int(time_value))
        try:
            publish_iso = datetime.fromtimestamp(float(time_value) / 1000, tz=timezone.utc).date().isoformat()
        except Exception:
            publish_iso = None
    summary = generate_summary_title(title, body, None, note_id)
    return ParsedNote(
        note_id=note_id,
        url=url,
        canonical_url=canonical_note_url(note_id),
        title=title,
        summary_title=summary,
        author_name=_clean(user.get("nickname") if isinstance(user, dict) else None),
        author_display_id=_clean((user.get("redId") or user.get("red_id")) if isinstance(user, dict) else None),
        author_profile_id=_clean((user.get("userId") or user.get("user_id")) if isinstance(user, dict) else None),
        publish_time_raw=publish_raw,
        publish_time_iso=publish_iso,
        body_text=body,
        tags=tags,
        image_urls=image_urls,
        image_candidates=image_candidates,
        image_count=len(image_urls),
        raw_extras={"source": "initial_state", "xsec_token": state_note.get("xsecToken")},
    )


def parse_board_links(source_html: str, base_url: str) -> list[tuple[str, str]]:
    soup = BeautifulSoup(source_html, "lxml")
    found: list[tuple[str, str]] = []
    for anchor in soup.find_all("a", href=True):
        href = urljoin(base_url, str(anchor["href"]))
        if "xiaohongshu.com" not in href and "xhslink.com" not in href:
            continue
        note_id = extract_note_id(href)
        if note_id:
            found.append((note_id, href))
    return _dedupe_pairs(found)


def _dedupe_pairs(values: Iterable[tuple[str, str]]) -> list[tuple[str, str]]:
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    for note_id, url in values:
        if note_id not in seen:
            seen.add(note_id)
            out.append((note_id, url))
    return out
