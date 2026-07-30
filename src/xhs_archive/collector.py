from __future__ import annotations

import re

from .browser import collect_note_links_from_page, human_delay, is_auth_required_text, launch_persistent_chromium
from .config import AppConfig
from .db import ArchiveDB
from .parser import parse_board_links
from .utils import board_id_from_url, canonical_note_url, extract_note_id, utc_compact_timestamp, write_json


def collect_from_html(db: ArchiveDB, board_url: str, html: str, *, limit: int | None = None) -> dict:
    board_id = board_id_from_url(board_url)
    links = parse_board_links(html, board_url)
    if limit:
        links = links[:limit]
    for note_id, url in links:
        db.upsert_discovered_note(note_id=note_id, board_id=board_id, url=url, canonical_url=canonical_note_url(note_id))
    db.upsert_board(board_id=board_id, url=board_url, discovered_count=len(links))
    return {"board_id": board_id, "unique_note_count": len(links), "links": [{"note_id": n, "url": u} for n, u in links]}


def _parse_expected_count(text: str) -> int | None:
    patterns = [
        r"(?:面经|收藏夹|专辑|合集)?[^0-9]{0,8}(\d{2,5})\s*(?:篇|个)?\s*(?:笔记|帖子|收藏)",
        r"(?:笔记|帖子|收藏)\s*(\d{2,5})",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def _recover_scroll(page, cfg: AppConfig, attempt: int) -> None:
    page.evaluate(
        """
        (attempt) => {
          const maxY = Math.max(document.body.scrollHeight, document.documentElement.scrollHeight);
          const jitter = (attempt % 3) * Math.floor(window.innerHeight * 0.6);
          window.scrollTo({top: Math.max(0, maxY - jitter), behavior: 'instant'});
        }
        """,
        attempt,
    )
    page.wait_for_timeout(int(cfg.collector.recovery_delay_seconds * 1000))
    page.mouse.wheel(0, max(300, cfg.collector.scroll_step_px // 2))
    page.wait_for_timeout(int(cfg.collector.recovery_delay_seconds * 1000))


def _collect_visible_links(page, seen: dict[str, str]) -> tuple[int, int]:
    new_count = 0
    duplicates = 0
    for item in collect_note_links_from_page(page):
        note_id = extract_note_id(item["href"])
        if not note_id:
            continue
        if note_id in seen:
            duplicates += 1
            continue
        seen[note_id] = item["href"]
        new_count += 1
    return new_count, duplicates


def _fine_scan(page, cfg: AppConfig, seen: dict[str, str], rounds: list[dict], *, expected_count: int | None) -> int:
    duplicates = 0
    scan_index = 0
    for direction in ("down", "up"):
        height = int(page.evaluate("() => document.documentElement.scrollHeight"))
        step = max(120, cfg.collector.fine_scan_step_px)
        positions = range(0, height + step, step)
        if direction == "up":
            positions = range(height, -step, -step)
        for position in positions:
            scan_index += 1
            page.evaluate("(y) => window.scrollTo({top: Math.max(0, y), behavior: 'instant'})", position)
            page.wait_for_timeout(cfg.collector.fine_scan_delay_ms)
            new_count, duplicate_count = _collect_visible_links(page, seen)
            duplicates += duplicate_count
            rounds.append(
                {
                    "round": f"fine-{direction}-{scan_index}",
                    "new": new_count,
                    "total": len(seen),
                    "scroll_height": height,
                    "scroll_top": max(0, position),
                }
            )
            if expected_count and len(seen) >= expected_count:
                return duplicates
    return duplicates


def collect_board(
    db: ArchiveDB,
    cfg: AppConfig,
    board_url: str,
    *,
    limit: int | None = None,
    expected_count: int | None = None,
    dry_run: bool = False,
) -> dict:
    from playwright.sync_api import sync_playwright

    board_id = board_id_from_url(board_url)
    seen: dict[str, str] = {}
    rounds: list[dict] = []
    duplicates = 0
    stable_rounds = 0
    recovery_attempts = 0
    previous_height = 0
    parsed_expected_count = expected_count

    with sync_playwright() as p:
        context = launch_persistent_chromium(p, cfg, headed=True)
        page = context.pages[0] if context.pages else context.new_page()
        page.goto(board_url, wait_until="domcontentloaded", timeout=60_000)
        page.wait_for_timeout(3000)
        for round_index in range(1, cfg.collector.max_scroll_rounds + 1):
            body_text = page.locator("body").inner_text(timeout=5000)
            if parsed_expected_count is None:
                parsed_expected_count = _parse_expected_count(body_text)
            if is_auth_required_text(body_text) and not seen:
                screenshot = cfg.logs_dir / f"auth_required_collect_{utc_compact_timestamp()}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                context.close()
                return {"ok": False, "stage": "auth_required", "screenshot": str(screenshot)}

            new_count, duplicate_count = _collect_visible_links(page, seen)
            duplicates += duplicate_count
            if limit and len(seen) > limit:
                seen = dict(list(seen.items())[:limit])
                if limit and len(seen) >= limit:
                    break
            height = int(page.evaluate("() => document.documentElement.scrollHeight"))
            rounds.append({"round": round_index, "new": new_count, "total": len(seen), "scroll_height": height})
            if limit and len(seen) >= limit:
                break
            if new_count == 0 and height == previous_height:
                stable_rounds += 1
            else:
                stable_rounds = 0
            if stable_rounds >= cfg.collector.stable_rounds:
                if (
                    parsed_expected_count
                    and len(seen) < parsed_expected_count
                    and recovery_attempts < cfg.collector.recovery_scroll_attempts
                    and not limit
                ):
                    recovery_attempts += 1
                    rounds[-1]["recovery_attempt"] = recovery_attempts
                    stable_rounds = 0
                    _recover_scroll(page, cfg, recovery_attempts)
                    previous_height = int(page.evaluate("() => document.documentElement.scrollHeight"))
                    continue
                break
            previous_height = height
            page.mouse.wheel(0, cfg.collector.scroll_step_px)
            human_delay(cfg)
        if parsed_expected_count and len(seen) < parsed_expected_count and not limit:
            duplicates += _fine_scan(page, cfg, seen, rounds, expected_count=parsed_expected_count)
        missing_count = max(0, (parsed_expected_count or 0) - len(seen)) if parsed_expected_count else None
        possibly_incomplete = bool((parsed_expected_count and len(seen) < parsed_expected_count and not limit) or (stable_rounds < cfg.collector.stable_rounds and not limit))
        snapshot = {
            "board_id": board_id,
            "board_url": board_url,
            "expected_count": parsed_expected_count,
            "unique_note_count": len(seen),
            "missing_count": missing_count,
            "duplicates": duplicates,
            "rounds": rounds,
            "possibly_incomplete": possibly_incomplete,
            "stable_rounds": stable_rounds,
            "recovery_attempts": recovery_attempts,
            "links": [{"note_id": note_id, "url": url} for note_id, url in seen.items()],
        }
        if not dry_run:
            for note_id, url in seen.items():
                db.upsert_discovered_note(note_id=note_id, board_id=board_id, url=url, canonical_url=canonical_note_url(note_id))
            db.upsert_board(board_id=board_id, url=board_url, expected_count=parsed_expected_count, discovered_count=len(seen))
            snapshot_path = cfg.exports_dir / f"board_snapshot_{utc_compact_timestamp()}.json"
            write_json(snapshot_path, snapshot)
            snapshot["snapshot_path"] = str(snapshot_path)
        context.close()
        return snapshot
