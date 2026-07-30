import json
from pathlib import Path

from xhs_archive.db import ArchiveDB
from xhs_archive.discovery import (
    discovery_report_from_db,
    extract_note_refs_from_json,
    parse_ui_count,
    sanitize_url,
)
from xhs_archive.utils import read_json


def test_extract_note_refs_from_nested_board_json() -> None:
    data = read_json(Path("tests/fixtures/discovery_response.json"))
    extracted = extract_note_refs_from_json(data, raw_snapshot_path="raw/network_0001.json", board_slug="aaaaaaaaaaaaaaaaaaaaaaaa")

    assert extracted.has_more is False
    assert extracted.cursor == "cursor_001"
    assert [ref.note_id for ref in extracted.refs] == [
        "bbbbbbbbbbbbbbbbbbbbbbbb",
        "cccccccccccccccccccccccc",
    ]
    assert extracted.refs[0].canonical_url == "https://www.xiaohongshu.com/explore/bbbbbbbbbbbbbbbbbbbbbbbb"
    assert extracted.refs[0].raw_snapshot_path == "raw/network_0001.json"


def test_sanitize_url_redacts_sensitive_query_values() -> None:
    sanitized = sanitize_url("https://edith.xiaohongshu.com/api/sns/web/v1?cursor=abc&xsec_token=secret&sign=bad")

    assert "cursor=abc" in sanitized
    assert "xsec_token=REDACTED" in sanitized
    assert "sign=REDACTED" in sanitized
    assert "secret" not in sanitized
    assert "bad" not in sanitized


def test_parse_ui_count_supports_board_count_separator() -> None:
    assert parse_ui_count("面经\n暂无简介\n笔记・573\n粉丝・0") == 573


def test_discovery_report_round_trip_from_sqlite(tmp_path) -> None:
    db = ArchiveDB(tmp_path / "state.db")
    run_id = "run-test"
    board_id = "board-test"
    report = {
        "ok": False,
        "run_id": run_id,
        "board_id": board_id,
        "expected_ui_count": 3,
        "dom_unique_count": 1,
        "network_unique_count": 2,
        "embedded_state_unique_count": 0,
        "redbook_unique_count": 0,
        "union_unique_count": 2,
        "existing_database_count": 1,
        "new_note_ids": ["cccccccccccccccccccccccc"],
        "unresolved_gap": 1,
        "possibly_incomplete": True,
        "source_differences": {"network_not_dom": ["cccccccccccccccccccccccc"]},
    }

    db.start_discovery_run(run_id=run_id, board_id=board_id, expected_ui_count=3)
    db.add_discovery_evidence(run_id=run_id, note_id="bbbbbbbbbbbbbbbbbbbbbbbb", source="dom", sequence_number=1)
    db.add_discovery_evidence(run_id=run_id, note_id="bbbbbbbbbbbbbbbbbbbbbbbb", source="network", sequence_number=1)
    db.add_discovery_evidence(run_id=run_id, note_id="cccccccccccccccccccccccc", source="network", sequence_number=2)
    db.complete_discovery_run(
        run_id=run_id,
        dom_count=1,
        network_count=2,
        embedded_state_count=0,
        redbook_count=0,
        union_count=2,
        unresolved_gap=1,
        network_has_more=False,
        possibly_incomplete=True,
        status="sources_exhausted_but_count_mismatch",
        new_note_ids=["cccccccccccccccccccccccc"],
        source_differences={"network_not_dom": ["cccccccccccccccccccccccc"]},
        report=report,
    )

    loaded = discovery_report_from_db(db)
    assert loaded["run_id"] == run_id
    assert loaded["union_unique_count"] == 2
    assert json.loads(db.latest_discovery_run()["new_note_ids_json"]) == ["cccccccccccccccccccccccc"]
    assert len(db.list_discovery_evidence(run_id)) == 3
    db.close()
