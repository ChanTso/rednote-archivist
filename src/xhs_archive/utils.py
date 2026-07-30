from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse

NOTE_ID_RE = re.compile(r"^[0-9A-Za-z_-]{8,80}$")
BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
SPACE_RE = re.compile(r"\s+")


def now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def utc_compact_timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def safe_filename(value: str | None, max_chars: int = 80, fallback: str = "untitled") -> str:
    text = unicodedata.normalize("NFKC", value or "").strip()
    text = BAD_FILENAME_CHARS.sub("_", text)
    text = SPACE_RE.sub("_", text)
    text = re.sub(r"_+", "_", text)
    text = text.strip("._ ")
    if not text:
        text = fallback
    return text[:max_chars].strip("._ ") or fallback


def extract_note_id(url_or_id: str) -> str | None:
    candidate = (url_or_id or "").strip()
    if NOTE_ID_RE.match(candidate) and "/" not in candidate:
        return candidate
    parsed = urlparse(candidate)
    path_parts = [part for part in parsed.path.split("/") if part]
    for marker in ("explore", "discovery", "item"):
        if marker in path_parts:
            idx = path_parts.index(marker)
            if idx + 1 < len(path_parts) and NOTE_ID_RE.match(path_parts[idx + 1]):
                return path_parts[idx + 1]
    for key in ("note_id", "noteId", "id"):
        value = parse_qs(parsed.query).get(key, [None])[0]
        if value and NOTE_ID_RE.match(value):
            return value
    for part in reversed(path_parts):
        if NOTE_ID_RE.match(part):
            return part
    return None


def canonical_note_url(url_or_id: str) -> str:
    note_id = extract_note_id(url_or_id)
    if not note_id:
        raise ValueError(f"Cannot extract note_id from {url_or_id!r}")
    return f"https://www.xiaohongshu.com/explore/{note_id}"


def board_id_from_url(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:16]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_json(path: Path, data: object) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def atomic_move_dir(src: Path, dest: Path) -> Path:
    ensure_dir(dest.parent)
    if dest.exists():
        return dest
    os.replace(src, dest)
    return dest


def normalize_publish_time(raw: str | None) -> str | None:
    if not raw:
        return None
    text = raw.strip()
    patterns = [
        "%Y-%m-%d",
        "%Y/%m/%d",
        "%Y.%m.%d",
        "%Y年%m月%d日",
    ]
    for pattern in patterns:
        try:
            return datetime.strptime(text, pattern).date().isoformat()
        except ValueError:
            pass
    match = re.search(r"(20\d{2})[./年-](\d{1,2})[./月-](\d{1,2})", text)
    if match:
        year, month, day = match.groups()
        return f"{int(year):04d}-{int(month):02d}-{int(day):02d}"
    return None


def note_dir_name(
    *,
    author_display_id: str | None,
    author_profile_id: str | None,
    publish_time_iso: str | None,
    summary_title: str | None,
    note_id: str,
) -> str:
    author_id = author_display_id or author_profile_id or "unknown-author"
    date_part = publish_time_iso or "unknown-date"
    title_part = safe_filename(summary_title, max_chars=40, fallback=f"无标题面经_{note_id[:8]}")
    return f"{safe_filename(author_id, 48)}_{safe_filename(date_part, 24)}_{title_part}__{safe_filename(note_id, 80)}"


def reset_dir(path: Path) -> None:
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True, exist_ok=True)
