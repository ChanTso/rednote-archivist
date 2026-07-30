from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any, Iterable

from .models import ImageMeta, ParsedNote
from .utils import now_iso

SCHEMA_VERSION = 3
EXCLUDED_STAGE = "excluded"


class ArchiveDB:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(self.path)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.init_schema()

    def close(self) -> None:
        self.conn.close()

    def init_schema(self) -> None:
        self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS boards (
                board_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                title TEXT,
                expected_count INTEGER,
                discovered_count INTEGER NOT NULL DEFAULT 0,
                last_collected_at TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS notes (
                note_id TEXT PRIMARY KEY,
                board_id TEXT,
                url TEXT NOT NULL,
                canonical_url TEXT,
                title TEXT,
                summary_title TEXT,
                author_name TEXT,
                author_display_id TEXT,
                author_profile_id TEXT,
                publish_time_raw TEXT,
                publish_time_iso TEXT,
                body_text TEXT,
                stage TEXT NOT NULL DEFAULT 'discovered',
                retry_count INTEGER NOT NULL DEFAULT 0,
                last_error TEXT,
                output_dir TEXT,
                discovered_at TEXT NOT NULL,
                fetched_at TEXT,
                ocr_completed_at TEXT,
                rendered_at TEXT,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS images (
                image_id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id TEXT NOT NULL,
                position INTEGER NOT NULL,
                source_url TEXT,
                source_variant TEXT,
                display_width INTEGER,
                display_height INTEGER,
                declared_width INTEGER,
                declared_height INTEGER,
                local_path TEXT,
                sha256 TEXT,
                width INTEGER,
                height INTEGER,
                downloaded_width INTEGER,
                downloaded_height INTEGER,
                file_size INTEGER,
                format TEXT,
                is_probable_thumbnail INTEGER NOT NULL DEFAULT 0,
                thumbnail_reason TEXT,
                download_status TEXT NOT NULL DEFAULT 'pending',
                ocr_status TEXT NOT NULL DEFAULT 'pending',
                ocr_engine TEXT,
                ocr_confidence REAL,
                last_error TEXT,
                UNIQUE(note_id, position),
                FOREIGN KEY(note_id) REFERENCES notes(note_id)
            );

            CREATE TABLE IF NOT EXISTS events (
                event_id INTEGER PRIMARY KEY AUTOINCREMENT,
                note_id TEXT,
                stage TEXT NOT NULL,
                level TEXT NOT NULL,
                message TEXT NOT NULL,
                details_json TEXT,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS discovery_runs (
                run_id TEXT PRIMARY KEY,
                board_id TEXT,
                expected_ui_count INTEGER,
                dom_count INTEGER,
                network_count INTEGER,
                embedded_state_count INTEGER,
                redbook_count INTEGER,
                union_count INTEGER,
                unresolved_gap INTEGER,
                network_has_more INTEGER,
                possibly_incomplete INTEGER NOT NULL,
                status TEXT,
                new_note_ids_json TEXT,
                source_differences_json TEXT,
                report_json TEXT,
                started_at TEXT NOT NULL,
                completed_at TEXT
            );

            CREATE TABLE IF NOT EXISTS discovery_evidence (
                run_id TEXT NOT NULL,
                note_id TEXT NOT NULL,
                source TEXT NOT NULL,
                cursor TEXT,
                sequence_number INTEGER,
                raw_snapshot_path TEXT,
                first_seen_at TEXT NOT NULL,
                PRIMARY KEY (run_id, note_id, source),
                FOREIGN KEY(run_id) REFERENCES discovery_runs(run_id)
            );

            CREATE INDEX IF NOT EXISTS idx_discovery_evidence_note
            ON discovery_evidence(note_id);
            """
        )
        self._ensure_image_columns()
        self._ensure_discovery_columns()
        self.conn.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES('schema_version', ?)",
            (str(SCHEMA_VERSION),),
        )
        self.conn.commit()

    def _ensure_image_columns(self) -> None:
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(images)").fetchall()}
        columns = {
            "source_variant": "TEXT",
            "display_width": "INTEGER",
            "display_height": "INTEGER",
            "declared_width": "INTEGER",
            "declared_height": "INTEGER",
            "downloaded_width": "INTEGER",
            "downloaded_height": "INTEGER",
            "file_size": "INTEGER",
            "format": "TEXT",
            "is_probable_thumbnail": "INTEGER NOT NULL DEFAULT 0",
            "thumbnail_reason": "TEXT",
        }
        for name, definition in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE images ADD COLUMN {name} {definition}")

    def _ensure_discovery_columns(self) -> None:
        existing = {row["name"] for row in self.conn.execute("PRAGMA table_info(discovery_runs)").fetchall()}
        columns = {
            "status": "TEXT",
            "new_note_ids_json": "TEXT",
            "source_differences_json": "TEXT",
            "report_json": "TEXT",
        }
        for name, definition in columns.items():
            if name not in existing:
                self.conn.execute(f"ALTER TABLE discovery_runs ADD COLUMN {name} {definition}")

    def upsert_board(
        self,
        *,
        board_id: str,
        url: str,
        title: str | None = None,
        expected_count: int | None = None,
        discovered_count: int | None = None,
    ) -> None:
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO boards(board_id, url, title, expected_count, discovered_count, last_collected_at, created_at, updated_at)
            VALUES(?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(board_id) DO UPDATE SET
                url=excluded.url,
                title=COALESCE(excluded.title, boards.title),
                expected_count=COALESCE(excluded.expected_count, boards.expected_count),
                discovered_count=COALESCE(excluded.discovered_count, boards.discovered_count),
                last_collected_at=excluded.last_collected_at,
                updated_at=excluded.updated_at
            """,
            (board_id, url, title, expected_count, discovered_count or 0, ts, ts, ts),
        )
        self.conn.commit()

    def upsert_discovered_note(self, *, note_id: str, board_id: str | None, url: str, canonical_url: str) -> None:
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO notes(note_id, board_id, url, canonical_url, stage, discovered_at, updated_at)
            VALUES(?, ?, ?, ?, 'discovered', ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET
                board_id=COALESCE(notes.board_id, excluded.board_id),
                url=CASE
                    WHEN notes.stage='discovered' THEN excluded.url
                    ELSE COALESCE(notes.url, excluded.url)
                END,
                canonical_url=COALESCE(notes.canonical_url, excluded.canonical_url),
                updated_at=excluded.updated_at
            """,
            (note_id, board_id, url, canonical_url, ts, ts),
        )
        self.conn.commit()

    def start_discovery_run(self, *, run_id: str, board_id: str, expected_ui_count: int | None) -> None:
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO discovery_runs(
                run_id, board_id, expected_ui_count, possibly_incomplete, started_at
            )
            VALUES(?, ?, ?, 1, ?)
            ON CONFLICT(run_id) DO UPDATE SET
                board_id=excluded.board_id,
                expected_ui_count=COALESCE(excluded.expected_ui_count, discovery_runs.expected_ui_count)
            """,
            (run_id, board_id, expected_ui_count, ts),
        )
        self.conn.commit()

    def add_discovery_evidence(
        self,
        *,
        run_id: str,
        note_id: str,
        source: str,
        cursor: str | None = None,
        sequence_number: int | None = None,
        raw_snapshot_path: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            INSERT INTO discovery_evidence(
                run_id, note_id, source, cursor, sequence_number, raw_snapshot_path, first_seen_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(run_id, note_id, source) DO UPDATE SET
                cursor=COALESCE(discovery_evidence.cursor, excluded.cursor),
                sequence_number=COALESCE(discovery_evidence.sequence_number, excluded.sequence_number),
                raw_snapshot_path=COALESCE(discovery_evidence.raw_snapshot_path, excluded.raw_snapshot_path)
            """,
            (run_id, note_id, source, cursor, sequence_number, raw_snapshot_path, now_iso()),
        )

    def complete_discovery_run(
        self,
        *,
        run_id: str,
        expected_ui_count: int | None = None,
        dom_count: int,
        network_count: int,
        embedded_state_count: int,
        redbook_count: int,
        union_count: int,
        unresolved_gap: int | None,
        network_has_more: bool | None,
        possibly_incomplete: bool,
        status: str,
        new_note_ids: list[str],
        source_differences: dict[str, list[str]],
        report: dict[str, Any],
    ) -> None:
        self.conn.execute(
            """
            UPDATE discovery_runs
            SET expected_ui_count=COALESCE(?, expected_ui_count),
                dom_count=?,
                network_count=?,
                embedded_state_count=?,
                redbook_count=?,
                union_count=?,
                unresolved_gap=?,
                network_has_more=?,
                possibly_incomplete=?,
                status=?,
                new_note_ids_json=?,
                source_differences_json=?,
                report_json=?,
                completed_at=?
            WHERE run_id=?
            """,
            (
                expected_ui_count,
                dom_count,
                network_count,
                embedded_state_count,
                redbook_count,
                union_count,
                unresolved_gap,
                None if network_has_more is None else int(network_has_more),
                int(possibly_incomplete),
                status,
                json.dumps(new_note_ids, ensure_ascii=False),
                json.dumps(source_differences, ensure_ascii=False),
                json.dumps(report, ensure_ascii=False),
                now_iso(),
                run_id,
            ),
        )
        self.conn.commit()

    def latest_discovery_run(self, board_id: str | None = None) -> sqlite3.Row | None:
        params: list[Any] = []
        sql = "SELECT * FROM discovery_runs"
        if board_id:
            sql += " WHERE board_id=?"
            params.append(board_id)
        sql += " ORDER BY started_at DESC LIMIT 1"
        return self.conn.execute(sql, params).fetchone()

    def list_discovery_evidence(self, run_id: str) -> list[sqlite3.Row]:
        return list(
            self.conn.execute(
                "SELECT * FROM discovery_evidence WHERE run_id=? ORDER BY source, sequence_number, note_id",
                (run_id,),
            ).fetchall()
        )

    def count_notes(self, board_id: str | None = None) -> int:
        if board_id:
            row = self.conn.execute(
                "SELECT COUNT(*) AS count FROM notes WHERE board_id=? AND stage!=?",
                (board_id, EXCLUDED_STAGE),
            ).fetchone()
            return int(row["count"]) if row else 0
        return int(
            self.conn.execute(
                "SELECT COUNT(*) AS count FROM notes WHERE stage!=?",
                (EXCLUDED_STAGE,),
            ).fetchone()["count"]
        )

    def existing_note_ids(self, board_id: str | None = None) -> set[str]:
        if board_id:
            rows = self.conn.execute(
                "SELECT note_id FROM notes WHERE board_id=? AND stage!=?",
                (board_id, EXCLUDED_STAGE),
            ).fetchall()
            return {str(row["note_id"]) for row in rows}
        return {
            str(row["note_id"])
            for row in self.conn.execute("SELECT note_id FROM notes WHERE stage!=?", (EXCLUDED_STAGE,)).fetchall()
        }

    def update_note_from_parsed(self, parsed: ParsedNote, *, board_id: str | None = None, output_dir: str | None = None) -> None:
        ts = now_iso()
        self.conn.execute(
            """
            INSERT INTO notes(
                note_id, board_id, url, canonical_url, title, summary_title, author_name,
                author_display_id, author_profile_id, publish_time_raw, publish_time_iso,
                body_text, stage, output_dir, discovered_at, fetched_at, updated_at
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'fetched', ?, ?, ?, ?)
            ON CONFLICT(note_id) DO UPDATE SET
                board_id=COALESCE(excluded.board_id, notes.board_id),
                url=excluded.url,
                canonical_url=excluded.canonical_url,
                title=excluded.title,
                summary_title=excluded.summary_title,
                author_name=excluded.author_name,
                author_display_id=excluded.author_display_id,
                author_profile_id=excluded.author_profile_id,
                publish_time_raw=excluded.publish_time_raw,
                publish_time_iso=excluded.publish_time_iso,
                body_text=excluded.body_text,
                stage='fetched',
                output_dir=COALESCE(excluded.output_dir, notes.output_dir),
                fetched_at=excluded.fetched_at,
                last_error=NULL,
                updated_at=excluded.updated_at
            """,
            (
                parsed.note_id,
                board_id,
                parsed.url,
                parsed.canonical_url,
                parsed.title,
                parsed.summary_title,
                parsed.author_name,
                parsed.author_display_id,
                parsed.author_profile_id,
                parsed.publish_time_raw,
                parsed.publish_time_iso,
                parsed.body_text,
                output_dir,
                now_iso(),
                ts,
                ts,
            ),
        )
        self.conn.commit()

    def upsert_image(self, note_id: str, image: ImageMeta) -> None:
        self.conn.execute(
            """
            INSERT INTO images(
                note_id, position, source_url, source_variant, display_width, display_height,
                declared_width, declared_height, local_path, sha256, width, height,
                downloaded_width, downloaded_height, file_size, format, is_probable_thumbnail,
                thumbnail_reason, download_status, ocr_status, ocr_engine, ocr_confidence, last_error
            )
            VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(note_id, position) DO UPDATE SET
                source_url=COALESCE(excluded.source_url, images.source_url),
                source_variant=COALESCE(excluded.source_variant, images.source_variant),
                display_width=COALESCE(excluded.display_width, images.display_width),
                display_height=COALESCE(excluded.display_height, images.display_height),
                declared_width=COALESCE(excluded.declared_width, images.declared_width),
                declared_height=COALESCE(excluded.declared_height, images.declared_height),
                local_path=COALESCE(excluded.local_path, images.local_path),
                sha256=COALESCE(excluded.sha256, images.sha256),
                width=COALESCE(excluded.width, images.width),
                height=COALESCE(excluded.height, images.height),
                downloaded_width=COALESCE(excluded.downloaded_width, images.downloaded_width),
                downloaded_height=COALESCE(excluded.downloaded_height, images.downloaded_height),
                file_size=COALESCE(excluded.file_size, images.file_size),
                format=COALESCE(excluded.format, images.format),
                is_probable_thumbnail=excluded.is_probable_thumbnail,
                thumbnail_reason=excluded.thumbnail_reason,
                download_status=excluded.download_status,
                ocr_status=excluded.ocr_status,
                ocr_engine=COALESCE(excluded.ocr_engine, images.ocr_engine),
                ocr_confidence=COALESCE(excluded.ocr_confidence, images.ocr_confidence),
                last_error=excluded.last_error
            """,
            (
                note_id,
                image.position,
                image.source_url,
                image.source_variant,
                image.display_width,
                image.display_height,
                image.declared_width,
                image.declared_height,
                image.local_path,
                image.sha256,
                image.width,
                image.height,
                image.downloaded_width or image.width,
                image.downloaded_height or image.height,
                image.file_size,
                image.format,
                int(image.is_probable_thumbnail),
                image.thumbnail_reason,
                image.download_status,
                image.ocr_status,
                image.ocr_engine,
                image.ocr_confidence,
                image.last_error,
            ),
        )
        self.conn.commit()

    def update_image_ocr(
        self,
        note_id: str,
        position: int,
        *,
        status: str,
        engine: str | None,
        confidence: float | None,
        error: str | None = None,
    ) -> None:
        self.conn.execute(
            """
            UPDATE images
            SET ocr_status=?, ocr_engine=?, ocr_confidence=?, last_error=?
            WHERE note_id=? AND position=?
            """,
            (status, engine, confidence, error, note_id, position),
        )
        self.conn.commit()

    def update_note_fields(self, note_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = now_iso()
        assignments = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [note_id]
        self.conn.execute(f"UPDATE notes SET {assignments} WHERE note_id=?", values)
        self.conn.commit()

    def exclude_note(self, note_id: str, *, reason: str, source: str = "user") -> None:
        row = self.get_note(note_id)
        if row is None:
            raise KeyError(f"note not found: {note_id}")
        self.update_note_fields(note_id, stage=EXCLUDED_STAGE, last_error=f"{source}: {reason}")
        self.add_event(note_id, EXCLUDED_STAGE, "info", reason, {"source": source})

    def mark_failed(self, note_id: str | None, stage: str, message: str, error_type: str = "unknown") -> None:
        if note_id:
            row = self.conn.execute("SELECT retry_count FROM notes WHERE note_id=?", (note_id,)).fetchone()
            retry_count = int(row["retry_count"]) + 1 if row else 1
            self.update_note_fields(note_id, stage="failed", retry_count=retry_count, last_error=f"{error_type}: {message}")
        self.add_event(note_id, stage, "error", message, {"error_type": error_type})

    def add_event(
        self,
        note_id: str | None,
        stage: str,
        level: str,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        self.conn.execute(
            "INSERT INTO events(note_id, stage, level, message, details_json, created_at) VALUES(?, ?, ?, ?, ?, ?)",
            (note_id, stage, level, message, json.dumps(details or {}, ensure_ascii=False), now_iso()),
        )
        self.conn.commit()

    def recover_incomplete(self) -> None:
        ts = now_iso()
        self.conn.execute("UPDATE notes SET stage='discovered', updated_at=? WHERE stage='fetching'", (ts,))
        self.conn.execute("UPDATE notes SET stage='fetched', updated_at=? WHERE stage='ocr_running'", (ts,))
        self.conn.commit()

    def list_notes(
        self,
        stages: Iterable[str] | None = None,
        limit: int | None = None,
        *,
        include_excluded: bool = False,
    ) -> list[sqlite3.Row]:
        sql = "SELECT * FROM notes"
        params: list[Any] = []
        if stages:
            stage_list = list(stages)
            sql += " WHERE stage IN (" + ",".join("?" for _ in stage_list) + ")"
            params.extend(stage_list)
        elif not include_excluded:
            sql += " WHERE stage!=?"
            params.append(EXCLUDED_STAGE)
        sql += " ORDER BY discovered_at ASC"
        if limit:
            sql += " LIMIT ?"
            params.append(limit)
        return list(self.conn.execute(sql, params).fetchall())

    def list_images(self, note_id: str) -> list[sqlite3.Row]:
        return list(self.conn.execute("SELECT * FROM images WHERE note_id=? ORDER BY position ASC", (note_id,)).fetchall())

    def get_note(self, note_id: str) -> sqlite3.Row | None:
        return self.conn.execute("SELECT * FROM notes WHERE note_id=?", (note_id,)).fetchone()

    def status_counts(self) -> dict[str, int]:
        rows = self.conn.execute("SELECT stage, COUNT(*) AS count FROM notes GROUP BY stage").fetchall()
        counts = {row["stage"]: int(row["count"]) for row in rows}
        counts["excluded_notes"] = counts.get(EXCLUDED_STAGE, 0)
        counts["total_including_excluded"] = int(self.conn.execute("SELECT COUNT(*) AS count FROM notes").fetchone()["count"])
        counts["total"] = int(
            self.conn.execute("SELECT COUNT(*) AS count FROM notes WHERE stage!=?", (EXCLUDED_STAGE,)).fetchone()["count"]
        )
        counts["images_total_including_excluded"] = int(self.conn.execute("SELECT COUNT(*) AS count FROM images").fetchone()["count"])
        counts["images_total"] = int(
            self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM images
                JOIN notes USING(note_id)
                WHERE notes.stage!=?
                """,
                (EXCLUDED_STAGE,),
            ).fetchone()["count"]
        )
        counts["images_downloaded"] = int(
            self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM images
                JOIN notes USING(note_id)
                WHERE notes.stage!=? AND images.download_status='success'
                """,
                (EXCLUDED_STAGE,),
            ).fetchone()["count"]
        )
        counts["ocr_success"] = int(
            self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM images
                JOIN notes USING(note_id)
                WHERE notes.stage!=? AND images.ocr_status='success'
                """,
                (EXCLUDED_STAGE,),
            ).fetchone()["count"]
        )
        counts["needs_review"] = int(
            self.conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM images
                JOIN notes USING(note_id)
                WHERE notes.stage!=? AND images.ocr_status IN ('needs_review', 'vl_pending', 'failed')
                """,
                (EXCLUDED_STAGE,),
            ).fetchone()["count"]
        )
        return counts
