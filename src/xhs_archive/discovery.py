from __future__ import annotations

import json
import re
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from bs4 import BeautifulSoup

from .browser import is_auth_required_text, launch_persistent_chromium
from .config import AppConfig
from .db import ArchiveDB
from .utils import board_id_from_url, canonical_note_url, extract_note_id, now_iso, utc_compact_timestamp, write_json

DISCOVERY_SOURCES = ("network", "dom", "embedded_state", "redbook")
SOURCE_ALIASES = {
    "network": "network",
    "dom": "dom",
    "embedded-state": "embedded_state",
    "embedded_state": "embedded_state",
    "redbook": "redbook",
}

SENSITIVE_QUERY_RE = re.compile(r"(token|cookie|session|auth|sign|sig|secret|key|ticket)", re.I)
CURSOR_KEYS = {"cursor", "nextcursor", "next_cursor", "lastcursor", "last_cursor", "searchid", "search_id"}
HAS_MORE_KEYS = {"hasmore", "has_more", "ismore", "is_more", "more"}
NOTE_ID_KEYS = {"noteid", "note_id", "noteidstr", "note_id_str", "noteids", "note_ids"}
XSEC_KEYS = {"xsectoken", "xsec_token"}
NOTE_CONTEXT_HINTS = {
    "cover",
    "coverurl",
    "displaytitle",
    "display_title",
    "interactinfo",
    "interact_info",
    "imagelist",
    "image_list",
    "notecard",
    "note_card",
    "notetype",
    "note_type",
    "xsectoken",
    "xsec_token",
}


@dataclass(frozen=True)
class DiscoveryRef:
    note_id: str
    url: str
    canonical_url: str
    cursor: str | None = None
    sequence_number: int | None = None
    raw_snapshot_path: str | None = None


@dataclass(frozen=True)
class ExtractedDiscovery:
    refs: list[DiscoveryRef]
    has_more: bool | None = None
    cursor: str | None = None


def normalize_sources(value: str | list[str] | tuple[str, ...] | None) -> list[str]:
    if value is None:
        return list(DISCOVERY_SOURCES)
    raw = value if isinstance(value, (list, tuple)) else str(value).split(",")
    normalized: list[str] = []
    for source in raw:
        key = str(source).strip()
        if not key:
            continue
        mapped = SOURCE_ALIASES.get(key)
        if not mapped:
            raise ValueError(f"unsupported discovery source: {source}")
        if mapped not in normalized:
            normalized.append(mapped)
    return normalized


def board_slug_from_url(board_url: str) -> str | None:
    parsed = urlparse(board_url)
    parts = [part for part in parsed.path.split("/") if part]
    if "board" in parts:
        idx = parts.index("board")
        if idx + 1 < len(parts):
            return parts[idx + 1]
    return None


def sanitize_url(url: str) -> str:
    parsed = urlparse(url)
    kept: list[tuple[str, str]] = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        if SENSITIVE_QUERY_RE.search(key):
            kept.append((key, "REDACTED"))
        else:
            kept.append((key, value))
    return urlunparse(parsed._replace(query=urlencode(kept, doseq=True)))


def extract_note_refs_from_json(
    data: Any,
    *,
    raw_snapshot_path: str | None = None,
    board_slug: str | None = None,
) -> ExtractedDiscovery:
    refs: list[DiscoveryRef] = []
    seen: set[str] = set()
    has_more_values: list[bool] = []
    cursors: list[str] = []

    def add_ref(note_id: str | None, cursor: str | None, path: tuple[str, ...]) -> None:
        if not note_id or note_id == board_slug or note_id in seen:
            return
        try:
            canonical = canonical_note_url(note_id)
        except ValueError:
            return
        seen.add(note_id)
        refs.append(
            DiscoveryRef(
                note_id=note_id,
                url=canonical,
                canonical_url=canonical,
                cursor=cursor,
                sequence_number=len(refs) + 1,
                raw_snapshot_path=raw_snapshot_path,
            )
        )

    def walk(value: Any, path: tuple[str, ...], inherited_cursor: str | None = None) -> None:
        cursor = inherited_cursor
        if isinstance(value, dict):
            cursor = _cursor_from_dict(value) or cursor
            if cursor:
                cursors.append(cursor)
            more = _has_more_from_dict(value)
            if more is not None:
                has_more_values.append(more)

            context = _looks_like_note_context(path, value)
            for key, item in value.items():
                key_norm = _normalize_key(key)
                if isinstance(item, (str, int)):
                    text = str(item)
                    if key_norm in NOTE_ID_KEYS or ("note" in key_norm and "id" in key_norm):
                        add_ref(extract_note_id(text), cursor, path + (str(key),))
                    elif key_norm in {"id", "idstr", "id_str"} and context:
                        add_ref(extract_note_id(text), cursor, path + (str(key),))
                    elif _looks_like_note_url(text):
                        add_ref(extract_note_id(text), cursor, path + (str(key),))
                walk(item, path + (str(key),), cursor)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                walk(item, path + (str(index),), inherited_cursor)
        elif isinstance(value, str) and _looks_like_note_url(value):
            add_ref(extract_note_id(value), inherited_cursor, path)

    walk(data, ())
    has_more = True if any(has_more_values) else False if has_more_values else None
    cursor = cursors[0] if cursors else None
    return ExtractedDiscovery(refs=refs, has_more=has_more, cursor=cursor)


def discovery_report_from_db(db: ArchiveDB, board_url: str | None = None) -> dict:
    board_id = board_id_from_url(board_url) if board_url else None
    run = db.latest_discovery_run(board_id)
    if not run:
        return {"ok": False, "reason": "no_discovery_run"}
    stored_report: dict[str, Any] = {}
    if run["report_json"]:
        try:
            loaded = json.loads(run["report_json"])
            if isinstance(loaded, dict):
                stored_report = loaded
        except Exception:
            stored_report = {}
    evidence = db.list_discovery_evidence(run["run_id"])
    by_source = _sets_by_source(
        {
            source: [row["note_id"] for row in evidence if row["source"] == source]
            for source in DISCOVERY_SOURCES
        }
    )
    expected = stored_report.get("expected_ui_count", run["expected_ui_count"])
    union_count = len(set().union(*by_source.values()))
    differences = _source_differences(by_source)
    raw_dir_value = stored_report.get("raw_dir")
    report = _build_report(
        run_id=run["run_id"],
        board_id=run["board_id"],
        expected_count=expected,
        source_sets=by_source,
        existing_database_count=db.count_notes(run["board_id"]),
        existing_before=db.existing_note_ids(run["board_id"]),
        network_has_more=bool(run["network_has_more"]) if run["network_has_more"] is not None else None,
        raw_dir=Path(raw_dir_value) if raw_dir_value else None,
        redbook_status=stored_report.get("redbook_status"),
        status=run["status"] or _status_for_counts(expected, union_count, None),
        source_differences=differences,
        user_expected_count=stored_report.get("user_expected_count"),
        current_ui_count=stored_report.get("current_ui_count"),
        expected_count_source=stored_report.get("expected_count_source", "unknown"),
        count_observed_at=stored_report.get("count_observed_at"),
        count_resolution=stored_report.get("count_resolution"),
    )
    for key in ("report_path", "historical_ui_count", "invalid_notes_removed"):
        if key in stored_report and key not in report:
            report[key] = stored_report[key]
    return report


def run_discovery(
    db: ArchiveDB,
    cfg: AppConfig,
    board_url: str,
    *,
    expected_count: int | None = None,
    sources: list[str] | tuple[str, ...] | str | None = None,
    resume: bool = False,
) -> dict:
    selected_sources = normalize_sources(sources)
    board_id = board_id_from_url(board_url)
    board_slug = board_slug_from_url(board_url)
    previous_run = db.latest_discovery_run(board_id)
    run_id = _run_id(board_id, resume=resume, db=db)
    raw_dir = cfg.exports_dir / "discovery" / "raw" / run_id
    raw_dir.mkdir(parents=True, exist_ok=True)

    db.start_discovery_run(run_id=run_id, board_id=board_id, expected_ui_count=expected_count)
    existing_before = db.existing_note_ids(board_id)
    source_refs: dict[str, dict[str, DiscoveryRef]] = {source: {} for source in DISCOVERY_SOURCES}
    network_has_more_values: list[bool] = []
    redbook_status: dict | None = None
    user_expected_count = expected_count
    current_ui_count: int | None = None
    count_observed_at: str | None = None
    effective_expected_count = expected_count
    expected_count_source = "user_argument" if user_expected_count is not None else "unknown"

    browser_sources = {"network", "dom", "embedded_state"} & set(selected_sources)
    if browser_sources:
        from playwright.sync_api import sync_playwright

        with sync_playwright() as p:
            context = launch_persistent_chromium(p, cfg, headed=True)
            page = context.pages[0] if context.pages else context.new_page()
            response_sequence = {"value": 0}

            if "network" in selected_sources:

                def handle_response(response) -> None:
                    _handle_network_response(
                        response=response,
                        raw_dir=raw_dir,
                        board_slug=board_slug,
                        source_refs=source_refs["network"],
                        has_more_values=network_has_more_values,
                        sequence_state=response_sequence,
                    )

                page.on("response", handle_response)

            page.goto(board_url, wait_until="domcontentloaded", timeout=60_000)
            page.wait_for_timeout(3000)
            body_text = page.locator("body").inner_text(timeout=5000)
            current_ui_count = parse_ui_count(body_text)
            if current_ui_count is not None:
                count_observed_at = now_iso()
            if effective_expected_count is None and current_ui_count is not None:
                effective_expected_count = current_ui_count
                expected_count_source = "current_page_ui"
            if effective_expected_count is None:
                latest_confirmed = latest_confirmed_count(db, board_id)
                if latest_confirmed is not None:
                    effective_expected_count = latest_confirmed
                    expected_count_source = "latest_confirmed_effective_count"
            if is_auth_required_text(body_text) and not _page_has_board_note_links(page, board_slug):
                screenshot = cfg.logs_dir / f"auth_required_discovery_{utc_compact_timestamp()}.png"
                page.screenshot(path=str(screenshot), full_page=True)
                context.close()
                report = _auth_required_report(run_id, board_id, effective_expected_count, screenshot)
                report["existing_database_count"] = len(existing_before)
                report.update(
                    _count_context_fields(
                        user_expected_count=user_expected_count,
                        current_ui_count=current_ui_count,
                        effective_expected_count=effective_expected_count,
                        expected_count_source=expected_count_source,
                        count_observed_at=count_observed_at,
                    )
                )
                db.complete_discovery_run(
                    run_id=run_id,
                    expected_ui_count=effective_expected_count,
                    dom_count=0,
                    network_count=len(source_refs["network"]),
                    embedded_state_count=0,
                    redbook_count=0,
                    union_count=len(source_refs["network"]),
                    unresolved_gap=effective_expected_count,
                    network_has_more=_merge_has_more(network_has_more_values),
                    possibly_incomplete=True,
                    status="auth_required",
                    new_note_ids=[],
                    source_differences={},
                    report=report,
                )
                return report

            if "embedded_state" in selected_sources:
                for ref in _collect_embedded_state(page, raw_dir, board_slug=board_slug):
                    source_refs["embedded_state"].setdefault(ref.note_id, ref)

            if "dom" in selected_sources:
                dom_refs, dom_rounds = _collect_dom_source(page, cfg, board_url, board_slug=board_slug)
                dom_snapshot = raw_dir / "dom_rounds.json"
                write_json(dom_snapshot, {"rounds": dom_rounds})
                for ref in dom_refs:
                    source_refs["dom"].setdefault(
                        ref.note_id,
                        DiscoveryRef(
                            note_id=ref.note_id,
                            url=ref.url,
                            canonical_url=ref.canonical_url,
                            cursor=ref.cursor,
                            sequence_number=ref.sequence_number,
                            raw_snapshot_path=str(dom_snapshot),
                        ),
                    )
            page.wait_for_timeout(2000)
            context.close()

    if "redbook" in selected_sources:
        redbook_status, redbook_refs = _collect_redbook(board_url, raw_dir, board_slug=board_slug)
        for ref in redbook_refs:
            source_refs["redbook"].setdefault(ref.note_id, ref)

    source_sets = {source: set(refs) for source, refs in source_refs.items()}
    union_refs = _union_refs(source_refs)
    union_ids = sorted(union_refs)
    new_note_ids = [note_id for note_id in union_ids if note_id not in existing_before]
    if effective_expected_count is None:
        effective_expected_count = latest_confirmed_count(db, board_id)
        if effective_expected_count is not None:
            expected_count_source = "latest_confirmed_effective_count"
    unresolved_gap = max(0, (effective_expected_count or 0) - len(union_ids)) if effective_expected_count else None
    network_has_more = _merge_has_more(network_has_more_values)
    differences = _source_differences(source_sets)
    count_resolution = cleanup_resolution_from_previous(
        previous_run,
        current_expected_count=effective_expected_count,
        current_discovered_count=len(union_ids),
    )
    status = _status_for_counts(effective_expected_count, len(union_ids), network_has_more)
    if status == "complete" and count_resolution:
        status = "complete_after_invalid_notes_cleanup"
    possibly_incomplete = bool(effective_expected_count and len(union_ids) < effective_expected_count)

    for source, refs in source_refs.items():
        for ref in refs.values():
            db.add_discovery_evidence(
                run_id=run_id,
                note_id=ref.note_id,
                source=source,
                cursor=ref.cursor,
                sequence_number=ref.sequence_number,
                raw_snapshot_path=ref.raw_snapshot_path,
            )

    for note_id in new_note_ids:
        ref = union_refs[note_id]
        db.upsert_discovered_note(
            note_id=note_id,
            board_id=board_id,
            url=ref.url,
            canonical_url=ref.canonical_url,
        )
    db.upsert_board(board_id=board_id, url=board_url, expected_count=effective_expected_count, discovered_count=len(union_ids))

    report = _build_report(
        run_id=run_id,
        board_id=board_id,
        expected_count=effective_expected_count,
        source_sets=source_sets,
        existing_database_count=len(existing_before),
        existing_before=existing_before,
        network_has_more=network_has_more,
        raw_dir=raw_dir,
        redbook_status=redbook_status,
        status=status,
        source_differences=differences,
        user_expected_count=user_expected_count,
        current_ui_count=current_ui_count,
        expected_count_source=expected_count_source,
        count_observed_at=count_observed_at,
        count_resolution=count_resolution,
    )
    report_path = cfg.exports_dir / "discovery" / f"discovery_report_{run_id}.json"
    report["report_path"] = str(report_path)
    if count_resolution:
        resolution_path = cfg.exports_dir / "discovery" / f"count_resolution_{run_id}.json"
        write_json(resolution_path, count_resolution)
        report["count_resolution_path"] = str(resolution_path)
        write_json(report_path, report)
    else:
        write_json(report_path, report)
    write_json(cfg.exports_dir / "discovery" / "latest_discovery_report.json", report)

    db.complete_discovery_run(
        run_id=run_id,
        expected_ui_count=effective_expected_count,
        dom_count=len(source_sets["dom"]),
        network_count=len(source_sets["network"]),
        embedded_state_count=len(source_sets["embedded_state"]),
        redbook_count=len(source_sets["redbook"]),
        union_count=len(union_ids),
        unresolved_gap=unresolved_gap,
        network_has_more=network_has_more,
        possibly_incomplete=possibly_incomplete,
        status=status,
        new_note_ids=new_note_ids,
        source_differences=differences,
        report=report,
    )
    return report


def _handle_network_response(
    *,
    response,
    raw_dir: Path,
    board_slug: str | None,
    source_refs: dict[str, DiscoveryRef],
    has_more_values: list[bool],
    sequence_state: dict[str, int],
) -> None:
    try:
        url = response.url
        if "xiaohongshu.com" not in url and "xhscdn.com" not in url:
            return
        content_type = (response.headers or {}).get("content-type", "")
        if "json" not in content_type.lower() and "/api/" not in url:
            return
        text = response.text()
        stripped = text.lstrip()
        if not stripped.startswith("{") and not stripped.startswith("["):
            return
        data = json.loads(text)
        sequence_state["value"] += 1
        seq = sequence_state["value"]
        raw_path = raw_dir / f"network_{seq:04d}.json"
        extracted = extract_note_refs_from_json(data, raw_snapshot_path=str(raw_path), board_slug=board_slug)
        if not extracted.refs:
            return
        raw_path.write_text(text, encoding="utf-8")
        write_json(
            raw_dir / f"network_{seq:04d}.meta.json",
            {
                "sanitized_url": sanitize_url(url),
                "status": response.status,
                "sequence_number": seq,
                "note_ref_count": len(extracted.refs),
                "cursor": extracted.cursor,
                "has_more": extracted.has_more,
            },
        )
        if extracted.has_more is not None:
            has_more_values.append(extracted.has_more)
        for ref in extracted.refs:
            source_refs.setdefault(
                ref.note_id,
                DiscoveryRef(
                    note_id=ref.note_id,
                    url=ref.url,
                    canonical_url=ref.canonical_url,
                    cursor=ref.cursor or extracted.cursor,
                    sequence_number=ref.sequence_number,
                    raw_snapshot_path=str(raw_path),
                ),
            )
    except Exception:
        return


def _collect_dom_source(page, cfg: AppConfig, board_url: str, *, board_slug: str | None) -> tuple[list[DiscoveryRef], list[dict]]:
    seen: dict[str, DiscoveryRef] = {}
    rounds: list[dict] = []

    def scan(label: str, index: int) -> None:
        _wait_for_mutation_stable(page)
        data = _scan_dom_near_viewport(page)
        new_ids: list[str] = []
        for href in data["links"]:
            note_id = extract_note_id(href)
            if not note_id or note_id == board_slug or note_id in seen:
                continue
            canonical = canonical_note_url(note_id)
            ref = DiscoveryRef(
                note_id=note_id,
                url=canonical,
                canonical_url=canonical,
                sequence_number=len(seen) + 1,
            )
            seen[note_id] = ref
            new_ids.append(note_id)
        rounds.append(
            {
                "round": label,
                "index": index,
                "new_note_ids": new_ids,
                "new_count": len(new_ids),
                "total": len(seen),
                "scroll_top": data["scroll_top"],
                "scroll_height": data["scroll_height"],
                "card_count": data["card_count"],
            }
        )

    page.evaluate("() => window.scrollTo({top: 0, behavior: 'instant'})")
    page.wait_for_timeout(800)
    viewport = int(page.evaluate("() => window.innerHeight || 900"))
    step = max(120, int(viewport * 0.5))

    index = 0
    position = 0
    while True:
        index += 1
        page.evaluate("(y) => window.scrollTo({top: y, behavior: 'instant'})", position)
        scan("forward-initial", index)
        height = int(page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"))
        if position > height + viewport:
            break
        position += step
        page.wait_for_timeout(cfg.collector.fine_scan_delay_ms)

    height = int(page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"))
    position = height
    while position >= 0:
        index += 1
        page.evaluate("(y) => window.scrollTo({top: Math.max(0, y), behavior: 'instant'})", position)
        scan("reverse", index)
        position -= step
        page.wait_for_timeout(cfg.collector.fine_scan_delay_ms)

    position = 0
    while True:
        index += 1
        page.evaluate("(y) => window.scrollTo({top: y, behavior: 'instant'})", position)
        scan("forward-rescan", index)
        height = int(page.evaluate("() => Math.max(document.body.scrollHeight, document.documentElement.scrollHeight)"))
        if position > height + viewport:
            break
        position += step
        page.wait_for_timeout(cfg.collector.fine_scan_delay_ms)

    return list(seen.values()), rounds


def _wait_for_mutation_stable(page, quiet_ms: int = 350, timeout_ms: int = 4000) -> None:
    try:
        page.evaluate(
            """
            ({quietMs, timeoutMs}) => new Promise(resolve => {
              let done = false;
              let quietTimer = null;
              const finish = () => {
                if (done) return;
                done = true;
                observer.disconnect();
                resolve(true);
              };
              const observer = new MutationObserver(() => {
                if (quietTimer) clearTimeout(quietTimer);
                quietTimer = setTimeout(finish, quietMs);
              });
              observer.observe(document.body || document.documentElement, {
                childList: true,
                subtree: true,
                attributes: true
              });
              quietTimer = setTimeout(finish, quietMs);
              setTimeout(finish, timeoutMs);
            })
            """,
            {"quietMs": quiet_ms, "timeoutMs": timeout_ms},
        )
    except Exception:
        page.wait_for_timeout(quiet_ms)


def _scan_dom_near_viewport(page) -> dict:
    return page.evaluate(
        """
        () => {
          const vh = window.innerHeight || 900;
          const anchors = Array.from(document.querySelectorAll('a[href]'));
          const links = [];
          for (const anchor of anchors) {
            const rect = anchor.getBoundingClientRect();
            const href = anchor.href || '';
            const near = rect.bottom >= -vh && rect.top <= vh * 2;
            if (!near) continue;
            if (!/xiaohongshu\\.com\\/(explore|discovery\\/item|board)\\//.test(href)) continue;
            links.push(href);
          }
          const cardSelectors = [
            '[class*="note"]',
            '[class*="card"]',
            '[data-note-id]',
            'section',
            'article'
          ];
          const cards = new Set();
          for (const selector of cardSelectors) {
            for (const node of document.querySelectorAll(selector)) {
              const rect = node.getBoundingClientRect();
              if (rect.width > 80 && rect.height > 80 && rect.bottom >= -vh && rect.top <= vh * 2) {
                cards.add(node);
              }
            }
          }
          return {
            links: Array.from(new Set(links)),
            card_count: cards.size,
            scroll_top: Math.round(window.scrollY || document.documentElement.scrollTop || 0),
            scroll_height: Math.round(Math.max(document.body.scrollHeight, document.documentElement.scrollHeight))
          };
        }
        """
    )


def _collect_embedded_state(page, raw_dir: Path, *, board_slug: str | None) -> list[DiscoveryRef]:
    refs: dict[str, DiscoveryRef] = {}
    snapshot_index = 0

    def add_snapshot(data: Any, name: str) -> None:
        nonlocal snapshot_index
        if data in (None, {}, []):
            return
        snapshot_index += 1
        path = raw_dir / f"embedded_state_{snapshot_index:04d}_{name}.json"
        write_json(path, data)
        extracted = extract_note_refs_from_json(data, raw_snapshot_path=str(path), board_slug=board_slug)
        for ref in extracted.refs:
            refs.setdefault(ref.note_id, ref)

    try:
        states = page.evaluate(
            """
            () => {
              const names = [
                '__INITIAL_STATE__',
                '__APOLLO_STATE__',
                '__NUXT__',
                '__NEXT_DATA__',
                '__REDUX_STATE__'
              ];
              const out = {};
              for (const name of names) {
                try {
                  if (window[name] !== undefined) {
                    out[name] = JSON.parse(JSON.stringify(window[name]));
                  }
                } catch (err) {}
              }
              return out;
            }
            """
        )
        add_snapshot(states, "window")
    except Exception:
        pass

    try:
        soup = BeautifulSoup(page.content(), "lxml")
        for index, script in enumerate(soup.find_all("script"), start=1):
            script_type = (script.get("type") or "").lower()
            text = script.string or script.get_text() or ""
            if not text.strip():
                continue
            parsed: Any | None = None
            if "json" in script_type:
                parsed = _parse_json_text(text)
            elif "__INITIAL_STATE__" in text or "hydration" in text.lower():
                parsed = _parse_json_text(_first_balanced_json_object(text) or "")
            if parsed is not None:
                add_snapshot(parsed, f"script_{index:03d}")
    except Exception:
        pass
    return list(refs.values())


def _collect_redbook(board_url: str, raw_dir: Path, *, board_slug: str | None) -> tuple[dict, list[DiscoveryRef]]:
    executable = shutil.which("redbook")
    if not executable:
        return {"available": False, "ok": False, "reason": "redbook_not_installed"}, []
    try:
        completed = subprocess.run(
            [executable, "board", board_url, "--json"],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
    except Exception as exc:
        return {"available": True, "ok": False, "reason": f"{type(exc).__name__}: {exc}"}, []
    raw_path = raw_dir / "redbook_board.json"
    raw_path.write_text(completed.stdout or completed.stderr or "", encoding="utf-8")
    if completed.returncode != 0:
        return {
            "available": True,
            "ok": False,
            "returncode": completed.returncode,
            "raw_path": str(raw_path),
        }, []
    try:
        data = json.loads(completed.stdout)
    except Exception as exc:
        return {"available": True, "ok": False, "reason": f"invalid_json: {exc}", "raw_path": str(raw_path)}, []
    extracted = extract_note_refs_from_json(data, raw_snapshot_path=str(raw_path), board_slug=board_slug)
    return {"available": True, "ok": True, "raw_path": str(raw_path), "count": len(extracted.refs)}, extracted.refs


def _page_has_board_note_links(page, board_slug: str | None) -> bool:
    try:
        data = _scan_dom_near_viewport(page)
    except Exception:
        return False
    for href in data.get("links") or []:
        note_id = extract_note_id(href)
        if note_id and note_id != board_slug:
            return True
    return False


def _sets_by_source(values: dict[str, list[str]]) -> dict[str, set[str]]:
    return {source: set(values.get(source, [])) for source in DISCOVERY_SOURCES}


def _source_differences(source_sets: dict[str, set[str]]) -> dict[str, list[str]]:
    network = source_sets.get("network", set())
    dom = source_sets.get("dom", set())
    embedded = source_sets.get("embedded_state", set())
    redbook = source_sets.get("redbook", set())
    return {
        "network_not_dom": sorted(network - dom),
        "dom_not_network": sorted(dom - network),
        "redbook_not_network": sorted(redbook - network),
        "dom_not_redbook": sorted(dom - redbook),
        "network_not_redbook": sorted(network - redbook),
        "embedded_state_not_network": sorted(embedded - network),
        "network_not_embedded_state": sorted(network - embedded),
        "embedded_state_not_dom": sorted(embedded - dom),
        "dom_not_embedded_state": sorted(dom - embedded),
    }


def _build_report(
    *,
    run_id: str,
    board_id: str,
    expected_count: int | None,
    source_sets: dict[str, set[str]],
    existing_database_count: int,
    existing_before: set[str],
    network_has_more: bool | None,
    raw_dir: Path | None,
    redbook_status: dict | None,
    status: str,
    source_differences: dict[str, list[str]],
    user_expected_count: int | None = None,
    current_ui_count: int | None = None,
    expected_count_source: str = "unknown",
    count_observed_at: str | None = None,
    count_resolution: dict | None = None,
) -> dict:
    union_ids = sorted(set().union(*source_sets.values()))
    new_note_ids = [note_id for note_id in union_ids if note_id not in existing_before]
    unresolved_gap = max(0, (expected_count or 0) - len(union_ids)) if expected_count else None
    report = {
        "ok": status in {"complete", "complete_after_invalid_notes_cleanup"},
        "run_id": run_id,
        "board_id": board_id,
        "status": status,
        "expected_ui_count": expected_count,
        "effective_expected_count": expected_count,
        "user_expected_count": user_expected_count,
        "current_ui_count": current_ui_count,
        "expected_count_source": expected_count_source,
        "count_observed_at": count_observed_at,
        "expected_count_mismatch": bool(
            user_expected_count is not None and current_ui_count is not None and user_expected_count != current_ui_count
        ),
        "dom_unique_count": len(source_sets.get("dom", set())),
        "network_unique_count": len(source_sets.get("network", set())),
        "embedded_state_unique_count": len(source_sets.get("embedded_state", set())),
        "redbook_unique_count": len(source_sets.get("redbook", set())),
        "union_unique_count": len(union_ids),
        "existing_database_count": existing_database_count,
        "new_note_ids": new_note_ids,
        "new_note_count": len(new_note_ids),
        "unresolved_gap": unresolved_gap,
        "possibly_incomplete": bool(expected_count and len(union_ids) < expected_count),
        "network_has_more": network_has_more,
        "source_differences": source_differences,
        "raw_dir": str(raw_dir) if raw_dir else None,
        "redbook_status": redbook_status,
    }
    if count_resolution:
        report["count_resolution"] = count_resolution
        report["historical_ui_count"] = count_resolution.get("previous_expected_ui_count")
        report["invalid_notes_removed"] = count_resolution.get("invalid_notes_removed")
        report["current_effective_favorites_complete"] = True
    else:
        report["current_effective_favorites_complete"] = bool(expected_count and len(union_ids) >= expected_count)
    return report


def _status_for_counts(expected_count: int | None, union_count: int, network_has_more: bool | None) -> str:
    if expected_count and union_count >= expected_count:
        return "complete"
    if expected_count and union_count < expected_count:
        if network_has_more is False:
            return "sources_exhausted_but_count_mismatch"
        if network_has_more is True:
            return "sources_not_exhausted_count_mismatch"
        return "count_mismatch_unresolved"
    return "discovery_completed_without_expected_count"


def parse_ui_count(text: str) -> int | None:
    patterns = [
        r"(?:收藏专辑|收藏夹|专辑|合集|面经)?[^0-9]{0,10}(\d{1,5})\s*(?:篇|个)?\s*(?:笔记|帖子|收藏)",
        r"(?:笔记|帖子|收藏)[^0-9]{0,6}(\d{1,5})",
        r"(\d{1,5})\s*(?:篇|个)?\s*(?:笔记|帖子|收藏)",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return int(match.group(1))
    return None


def latest_confirmed_count(db: ArchiveDB, board_id: str) -> int | None:
    rows = db.conn.execute(
        """
        SELECT expected_ui_count, union_count, status, report_json
        FROM discovery_runs
        WHERE board_id=?
        ORDER BY started_at DESC
        LIMIT 20
        """,
        (board_id,),
    ).fetchall()
    for row in rows:
        status = str(row["status"] or "")
        expected = row["expected_ui_count"]
        union = row["union_count"]
        if status in {"complete", "complete_after_invalid_notes_cleanup"} and expected and union and int(expected) <= int(union):
            return int(expected)
        if row["report_json"]:
            try:
                report = json.loads(row["report_json"])
            except Exception:
                continue
            if report.get("current_effective_favorites_complete") and report.get("expected_ui_count"):
                return int(report["expected_ui_count"])
    return None


def cleanup_resolution_from_previous(
    previous_run,
    *,
    current_expected_count: int | None,
    current_discovered_count: int,
) -> dict | None:
    if not previous_run or not current_expected_count:
        return None
    previous_report = None
    if previous_run["report_json"]:
        try:
            previous_report = json.loads(previous_run["report_json"])
        except Exception:
            previous_report = None
    previous_expected = _int_or_none((previous_report or {}).get("expected_ui_count")) or _int_or_none(previous_run["expected_ui_count"])
    previous_discovered = _int_or_none((previous_report or {}).get("union_unique_count")) or _int_or_none(previous_run["union_count"])
    previous_gap = _int_or_none((previous_report or {}).get("unresolved_gap")) or _int_or_none(previous_run["unresolved_gap"])
    if not previous_expected or previous_discovered is None or not previous_gap:
        return None
    if current_expected_count != current_discovered_count:
        return None
    if previous_discovered != current_discovered_count:
        return None
    if previous_expected - current_expected_count != previous_gap:
        return None
    return {
        "previous_expected_ui_count": previous_expected,
        "previous_discovered_count": previous_discovered,
        "previous_gap": previous_gap,
        "resolution": "user_removed_invalid_notes_via_xiaohongshu_ui",
        "invalid_notes_removed": previous_gap,
        "current_expected_ui_count": current_expected_count,
        "current_discovered_count": current_discovered_count,
        "resolved": True,
        "expected_count_source": "current_user_verified_ui_after_cleanup",
        "count_resolution": "invalid_favorites_removed_from_xiaohongshu_board",
        "resolved_by_user_action": True,
        "resolved_at": now_iso(),
        "supersedes_run_id": previous_run["run_id"],
    }


def _count_context_fields(
    *,
    user_expected_count: int | None,
    current_ui_count: int | None,
    effective_expected_count: int | None,
    expected_count_source: str,
    count_observed_at: str | None,
) -> dict:
    return {
        "effective_expected_count": effective_expected_count,
        "user_expected_count": user_expected_count,
        "current_ui_count": current_ui_count,
        "expected_count_source": expected_count_source,
        "count_observed_at": count_observed_at,
        "expected_count_mismatch": bool(
            user_expected_count is not None and current_ui_count is not None and user_expected_count != current_ui_count
        ),
    }


def _int_or_none(value: Any) -> int | None:
    try:
        if value is None:
            return None
        return int(value)
    except Exception:
        return None


def _union_refs(source_refs: dict[str, dict[str, DiscoveryRef]]) -> dict[str, DiscoveryRef]:
    union: dict[str, DiscoveryRef] = {}
    for source in DISCOVERY_SOURCES:
        for note_id, ref in source_refs[source].items():
            union.setdefault(note_id, ref)
    return union


def _merge_has_more(values: list[bool]) -> bool | None:
    if not values:
        return None
    return values[-1]


def _auth_required_report(run_id: str, board_id: str, expected_count: int | None, screenshot: Path) -> dict:
    return {
        "ok": False,
        "run_id": run_id,
        "board_id": board_id,
        "status": "auth_required",
        "expected_ui_count": expected_count,
        "dom_unique_count": 0,
        "network_unique_count": 0,
        "embedded_state_unique_count": 0,
        "redbook_unique_count": 0,
        "union_unique_count": 0,
        "existing_database_count": 0,
        "new_note_ids": [],
        "new_note_count": 0,
        "unresolved_gap": expected_count,
        "possibly_incomplete": True,
        "source_differences": {},
        "screenshot": str(screenshot),
    }


def _run_id(board_id: str, *, resume: bool, db: ArchiveDB) -> str:
    if resume:
        latest = db.latest_discovery_run(board_id)
        if latest and not latest["completed_at"]:
            return str(latest["run_id"])
    return f"{utc_compact_timestamp()}_{board_id}_{uuid.uuid4().hex[:8]}"


def _normalize_key(value: object) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower())


def _cursor_from_dict(value: dict) -> str | None:
    for key, item in value.items():
        if _normalize_key(key) in CURSOR_KEYS and isinstance(item, (str, int, float)):
            text = str(item)
            if text:
                return text
    return None


def _has_more_from_dict(value: dict) -> bool | None:
    for key, item in value.items():
        if _normalize_key(key) not in HAS_MORE_KEYS:
            continue
        if isinstance(item, bool):
            return item
        if isinstance(item, (int, float)):
            return bool(item)
        if isinstance(item, str) and item.lower() in {"true", "false"}:
            return item.lower() == "true"
    return None


def _looks_like_note_context(path: tuple[str, ...], value: dict) -> bool:
    path_text = "/".join(path).lower()
    if "note" in path_text:
        return True
    keys = {_normalize_key(key) for key in value}
    if any("note" in key for key in keys):
        return True
    return bool(keys & {_normalize_key(key) for key in NOTE_CONTEXT_HINTS})


def _looks_like_note_url(value: str) -> bool:
    if "xiaohongshu.com" not in value:
        return False
    return bool(re.search(r"/(explore|discovery/item|board)/", value))


def _parse_json_text(text: str) -> Any | None:
    try:
        return json.loads(text.strip())
    except Exception:
        return None


def _first_balanced_json_object(text: str) -> str | None:
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape = False
    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
            continue
        if char == '"':
            in_string = True
        elif char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None
