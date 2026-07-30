from __future__ import annotations

import shutil

from .browser import (
    collect_image_candidates_from_page,
    collect_image_urls_from_page,
    human_delay,
    is_auth_required_text,
    launch_persistent_chromium,
    note_state_from_page,
    request_fetcher_from_context,
)
from .config import AppConfig
from .db import ArchiveDB
from .downloader import download_image_candidates
from .models import ImageCandidate, ParsedNote
from .parser import parse_note_html, parse_note_state
from .title import generate_summary_title
from .utils import atomic_move_dir, ensure_dir, note_dir_name, utc_compact_timestamp, write_json


GENERIC_XHS_TITLES = {"小红书 - 你的生活兴趣社区", "小红书"}


def _is_metadata_incomplete(parsed: ParsedNote) -> bool:
    stable_author = bool(parsed.author_display_id or parsed.author_profile_id)
    title_generic = (parsed.title or "").strip() in GENERIC_XHS_TITLES
    body = parsed.body_text or ""
    looks_like_home = "沪ICP备" in body and "你的生活兴趣社区" in (parsed.title or body)
    looks_like_board_or_profile = (
        ("我的收藏" in body and "创建专辑" in body)
        or ("专辑・" in body and "笔记・" in body and not stable_author)
        or ((parsed.title or "").strip().endswith("-") and "笔记・" in body and not stable_author)
    )
    return (not stable_author and title_generic) or looks_like_home or looks_like_board_or_profile


def _merge_image_urls(parsed: ParsedNote, page_urls: list[str]) -> ParsedNote:
    seen: set[str] = set()
    merged: list[str] = []
    for url in [*page_urls, *parsed.image_urls]:
        if url and url not in seen:
            seen.add(url)
            merged.append(url)
    parsed.image_urls = merged
    parsed.image_count = len(merged)
    return parsed


def _page_groups_to_candidates(groups: list[dict]) -> list[ImageCandidate]:
    candidates: list[ImageCandidate] = []
    for position, group in enumerate(groups, start=1):
        if not isinstance(group, dict):
            continue
        display_width = group.get("display_width")
        display_height = group.get("display_height")
        for item in group.get("candidates") or []:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            candidates.append(
                ImageCandidate(
                    position=position,
                    url=str(item["url"]),
                    source_variant=str(item.get("source_variant") or "page-candidate"),
                    display_width=int(display_width) if isinstance(display_width, (int, float)) else None,
                    display_height=int(display_height) if isinstance(display_height, (int, float)) else None,
                    score=int(item.get("score") or 0),
                )
            )
    return candidates


def _ensure_image_candidates(parsed: ParsedNote) -> ParsedNote:
    if parsed.image_candidates:
        return parsed
    parsed.image_candidates = [
        ImageCandidate(position=index, url=url, source_variant="legacy-url", score=0)
        for index, url in enumerate(parsed.image_urls, start=1)
    ]
    return parsed


def _preferred_urls(candidates: list[ImageCandidate]) -> list[str]:
    grouped: dict[int, list[ImageCandidate]] = {}
    for candidate in candidates:
        grouped.setdefault(candidate.position, []).append(candidate)
    urls: list[str] = []
    for position in sorted(grouped):
        urls.append(sorted(grouped[position], key=lambda item: item.score, reverse=True)[0].url)
    return urls


def fetch_pending(db: ArchiveDB, cfg: AppConfig, *, limit: int | None = None, dry_run: bool = False) -> dict:
    from playwright.sync_api import sync_playwright

    rows = db.list_notes(["discovered", "failed"], limit=limit)
    results: list[dict] = []
    if dry_run:
        return {"ok": True, "dry_run": True, "selected": [row["note_id"] for row in rows]}

    with sync_playwright() as p:
        context = launch_persistent_chromium(p, cfg, headed=True)
        page = context.pages[0] if context.pages else context.new_page()
        for row in rows:
            note_id = row["note_id"]
            db.update_note_fields(note_id, stage="fetching")
            try:
                target_url = row["url"] or row["canonical_url"]
                page.goto(target_url, wait_until="domcontentloaded", timeout=cfg.fetch.timeout_seconds * 1000)
                page.wait_for_timeout(2500)
                try:
                    page.wait_for_function(
                        "(noteId) => window.__INITIAL_STATE__ && window.__INITIAL_STATE__.note && window.__INITIAL_STATE__.note.noteDetailMap && window.__INITIAL_STATE__.note.noteDetailMap[noteId] && window.__INITIAL_STATE__.note.noteDetailMap[noteId].note",
                        arg=note_id,
                        timeout=10_000,
                    )
                except Exception:
                    pass
                html = page.content()
                state_note = note_state_from_page(page, note_id)
                body_text = page.locator("body").inner_text(timeout=5000)
                if not state_note and is_auth_required_text(body_text):
                    screenshot = cfg.logs_dir / f"auth_required_fetch_{note_id}_{utc_compact_timestamp()}.png"
                    page.screenshot(path=str(screenshot), full_page=True)
                    db.update_note_fields(note_id, stage="auth_required", last_error=f"auth_required: {screenshot}")
                    results.append({"note_id": note_id, "ok": False, "stage": "auth_required", "screenshot": str(screenshot)})
                    break
                if state_note:
                    parsed = parse_note_state(note_id, target_url, state_note)
                else:
                    if note_id not in page.url:
                        db.mark_failed(
                            note_id,
                            "fetch",
                            f"metadata incomplete after opening {target_url}; current page {page.url}",
                            "metadata_incomplete",
                        )
                        results.append(
                            {
                                "note_id": note_id,
                                "ok": False,
                                "error": "metadata_incomplete",
                                "page_url": page.url,
                            }
                        )
                        human_delay(cfg)
                        continue
                    parsed = parse_note_html(html, row["canonical_url"] or row["url"])
                    page_candidates = _page_groups_to_candidates(collect_image_candidates_from_page(page))
                    if page_candidates:
                        parsed.image_candidates = page_candidates
                        parsed.image_urls = _preferred_urls(page_candidates)
                        parsed.image_count = len({candidate.position for candidate in page_candidates})
                    else:
                        parsed = _merge_image_urls(parsed, collect_image_urls_from_page(page))
                parsed = _ensure_image_candidates(parsed)
                parsed.summary_title = generate_summary_title(parsed.title, parsed.body_text, None, parsed.note_id)

                staging = cfg.notes_dir / ".staging" / parsed.note_id
                if staging.exists():
                    shutil.rmtree(staging)
                ensure_dir(staging)
                (staging / "page.html").write_text(html, encoding="utf-8")
                if cfg.browser.save_page_screenshot:
                    page.screenshot(path=str(staging / "page.png"), full_page=True)
                write_json(staging / "raw.json", parsed.model_dump())
                if _is_metadata_incomplete(parsed):
                    db.mark_failed(
                        note_id,
                        "fetch",
                        f"metadata incomplete after opening {target_url}; current page {page.url}",
                        "metadata_incomplete",
                    )
                    results.append(
                        {
                            "note_id": note_id,
                            "ok": False,
                            "error": "metadata_incomplete",
                            "staging_dir": str(staging),
                            "page_url": page.url,
                        }
                    )
                    human_delay(cfg)
                    continue
                image_metas = download_image_candidates(
                    parsed.image_candidates,
                    staging,
                    fetch_bytes=request_fetcher_from_context(context),
                    timeout_seconds=cfg.fetch.timeout_seconds,
                )
                for image in image_metas:
                    db.upsert_image(parsed.note_id, image)
                dest = cfg.notes_dir / note_dir_name(
                    author_display_id=parsed.author_display_id,
                    author_profile_id=parsed.author_profile_id,
                    publish_time_iso=parsed.publish_time_iso,
                    summary_title=parsed.summary_title,
                    note_id=parsed.note_id,
                )
                final_dir = atomic_move_dir(staging, dest)
                db.update_note_from_parsed(parsed, board_id=row["board_id"], output_dir=str(final_dir))
                for image in image_metas:
                    if image.local_path:
                        image.local_path = image.local_path
                    db.upsert_image(parsed.note_id, image)
                results.append({"note_id": parsed.note_id, "ok": True, "output_dir": str(final_dir), "images": len(image_metas)})
                human_delay(cfg)
            except Exception as exc:
                db.mark_failed(note_id, "fetch", f"{type(exc).__name__}: {exc}", "unknown")
                results.append({"note_id": note_id, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        context.close()
    return {"ok": all(item.get("ok") for item in results), "results": results}
