from __future__ import annotations

import importlib.util
import json
import os
import platform
import re
import resource
import shutil
import subprocess
import sys
from pathlib import Path
from difflib import SequenceMatcher
from typing import Annotated
from urllib.parse import urlsplit

import typer
from PIL import Image, ImageStat

from .browser import chrome_executable_path, login as browser_login
from .collector import collect_board
from .config import load_config
from .db import ArchiveDB
from .discovery import discovery_report_from_db, run_discovery
from .fetcher import fetch_pending
from .indexer import export_index as export_index_impl
from .ocr.audit import audit_image, extract_numbers, extract_terms
from .ocr_runner import _safe_engine
from .ocr_runner import ocr_pending
from .ocr_review import reclassify_ocr_reviews
from .renderer import render_note, render_pending
from .verifier import verify as verify_impl
from .utils import read_json
from .utils import write_json

app = typer.Typer(no_args_is_help=True)
_ALLOWED_BOARD_HOSTS = frozenset({"xiaohongshu.com", "www.xiaohongshu.com"})


def _json(data: object) -> None:
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _db() -> tuple[ArchiveDB, object]:
    cfg = load_config()
    db = ArchiveDB(cfg.db_path)
    return db, cfg


def _run_command(args: list[str]) -> str | None:
    try:
        completed = subprocess.run(args, check=False, capture_output=True, text=True, timeout=10)
        if completed.returncode == 0:
            return completed.stdout.strip() or completed.stderr.strip()
    except Exception:
        return None
    return None


def _module_importable(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def _validated_board_url(value: str) -> str:
    candidate = value.strip()
    try:
        parsed = urlsplit(candidate)
        hostname = (parsed.hostname or "").rstrip(".").lower()
        port = parsed.port
    except ValueError as exc:
        raise typer.BadParameter("收藏专辑 URL 无效。") from exc
    if (
        parsed.scheme != "https"
        or hostname not in _ALLOWED_BOARD_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in {None, 443}
        or not parsed.path.startswith("/board/")
    ):
        raise typer.BadParameter("收藏专辑 URL 必须是 https://www.xiaohongshu.com/board/...。")
    return candidate


def _resolve_board_url(board_url: str | None) -> str:
    if board_url:
        return _validated_board_url(board_url)
    env_url = os.environ.get("XHS_BOARD_URL")
    if env_url:
        return _validated_board_url(env_url)
    paste = _run_command(["/usr/bin/pbpaste"])
    if paste:
        try:
            return _validated_board_url(paste)
        except typer.BadParameter:
            pass
    raise typer.BadParameter("缺少收藏专辑 URL：请设置 XHS_BOARD_URL 或传入 --board-url。")


def _path_is_within(candidate: str, root: str) -> bool:
    normalized_candidate = os.path.normcase(os.path.normpath(candidate))
    normalized_root = os.path.normcase(os.path.normpath(root))
    try:
        return os.path.commonpath([normalized_candidate, normalized_root]) == normalized_root
    except ValueError:
        return False


@app.command()
def doctor(json_output: Annotated[bool, typer.Option("--json", help="Output JSON.")] = False) -> None:
    cfg = load_config()
    conda_prefix = os.environ.get("CONDA_PREFIX")
    conda_active = bool(conda_prefix and _path_is_within(sys.prefix, conda_prefix))
    paddle_importable = _module_importable("paddle")
    paddle_device = None
    if paddle_importable:
        try:
            import paddle  # type: ignore

            paddle_device = paddle.get_device()
        except Exception as exc:
            paddle_device = f"error: {type(exc).__name__}: {exc}"
    onnx_providers = []
    if _module_importable("onnxruntime"):
        try:
            import onnxruntime as ort  # type: ignore

            onnx_providers = list(ort.get_available_providers())
        except Exception:
            onnx_providers = []
    vl_importable = False
    try:
        from paddleocr import PaddleOCRVL as _PaddleOCRVL  # type: ignore  # noqa: F401

        vl_importable = True
    except Exception:
        vl_importable = False
    vl_benchmark = None
    vl_benchmark_path = cfg.exports_dir / "vl_benchmark.json"
    if vl_benchmark_path.exists():
        try:
            vl_loaded = read_json(vl_benchmark_path)
            vl_benchmark = vl_loaded if isinstance(vl_loaded, dict) else None
        except Exception:
            vl_benchmark = None
    apple_vision_cli = {"available": False, "error": None}
    try:
        from .ocr.apple_vision import _ensure_vision_binary

        apple_vision_cli["binary"] = str(_ensure_vision_binary())
        apple_vision_cli["available"] = platform.system() == "Darwin"
    except Exception as exc:
        apple_vision_cli["error"] = f"{type(exc).__name__}: {exc}"
    mps_detected = "not_available"
    if _module_importable("torch"):
        try:
            import torch  # type: ignore

            mps_detected = bool(getattr(torch.backends, "mps", None) and torch.backends.mps.is_available())
        except Exception as exc:
            mps_detected = f"error: {type(exc).__name__}: {exc}"
    disk = shutil.disk_usage(cfg.root)
    result = {
        "platform": platform.system(),
        "platform_release": platform.release(),
        "machine": platform.machine(),
        "apple_silicon": platform.system() == "Darwin" and platform.machine() == "arm64",
        "python_version": platform.python_version(),
        "python_executable": sys.executable,
        "uv_version": _run_command(["uv", "--version"]) or _run_command(["/opt/homebrew/bin/uv", "--version"]),
        "venv_path": os.environ.get("VIRTUAL_ENV") or sys.prefix,
        "conda_active": conda_active,
        "conda_prefix_inherited": conda_prefix,
        "cuda_available": bool(os.environ.get("CUDA_HOME") or _run_command(["which", "nvcc"])),
        "mps_detected": mps_detected,
        "paddlepaddle_importable": paddle_importable,
        "paddle_device": paddle_device or "unavailable",
        "paddleocr_standard_available": _module_importable("paddleocr"),
        "rapidocr_available": _module_importable("rapidocr") or _module_importable("rapidocr_onnxruntime"),
        "onnxruntime_providers": onnx_providers,
        "paddleocr_vl_importable": vl_importable,
        "paddleocr_vl_available": bool(vl_benchmark and vl_benchmark.get("ok")),
        "paddleocr_vl_benchmark": vl_benchmark,
        "apple_vision_ocr": apple_vision_cli,
        "playwright_importable": _module_importable("playwright"),
        "system_chrome": chrome_executable_path(),
        "opencli_available": bool(_run_command(["opencli", "--version"])),
        "data_root": str(cfg.root),
        "disk_free_bytes": disk.free,
    }
    if json_output:
        _json(result)
    else:
        typer.echo(result)


@app.command()
def login() -> None:
    cfg = load_config()
    _json(browser_login(cfg))


@app.command()
def collect(
    board_url: Annotated[str | None, typer.Option("--board-url")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    expected_count: Annotated[int | None, typer.Option("--expected-count")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    db, cfg = _db()
    try:
        _json(collect_board(db, cfg, _resolve_board_url(board_url), limit=limit, expected_count=expected_count, dry_run=dry_run))
    finally:
        db.close()


@app.command()
def discover(
    board_url: Annotated[str | None, typer.Option("--board-url")] = None,
    expected_count: Annotated[int | None, typer.Option("--expected-count")] = None,
    sources: Annotated[str, typer.Option("--sources")] = "network,dom,embedded-state,redbook",
    resume: Annotated[bool, typer.Option("--resume")] = False,
) -> None:
    db, cfg = _db()
    try:
        _json(
            run_discovery(
                db,
                cfg,
                _resolve_board_url(board_url),
                expected_count=expected_count,
                sources=sources,
                resume=resume,
            )
        )
    finally:
        db.close()


@app.command("discovery-report")
def discovery_report(
    board_url: Annotated[str | None, typer.Option("--board-url")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    db, _cfg = _db()
    try:
        report = discovery_report_from_db(db, _resolve_board_url(board_url) if board_url else None)
        if json_output:
            _json(report)
        else:
            typer.echo(report)
    finally:
        db.close()


@app.command()
def fetch(
    pending: Annotated[bool, typer.Option("--pending")] = False,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    dry_run: Annotated[bool, typer.Option("--dry-run")] = False,
) -> None:
    db, cfg = _db()
    try:
        db.recover_incomplete()
        _json(fetch_pending(db, cfg, limit=limit, dry_run=dry_run))
    finally:
        db.close()


@app.command(name="ocr")
def ocr_cmd(
    pending: Annotated[bool, typer.Option("--pending")] = False,
    engine: Annotated[str, typer.Option("--engine")] = "auto",
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    force: Annotated[bool, typer.Option("--force", help="Re-run OCR for fetched/ocr_done/rendered/verified notes.")] = False,
) -> None:
    db, cfg = _db()
    try:
        _json(ocr_pending(db, cfg, engine_name=engine, limit=limit, force=force))
    finally:
        db.close()


@app.command("reclassify-ocr-review")
def reclassify_ocr_review(
    dry_run: Annotated[bool, typer.Option("--dry-run", help="Analyze and write reports without changing SQLite review status.")] = False,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    db, cfg = _db()
    try:
        report = reclassify_ocr_reviews(db, cfg, dry_run=dry_run)
        if not dry_run:
            rerendered = 0
            for row in db.list_notes(None):
                output_dir = Path(row["output_dir"] or "")
                if output_dir.exists() and (output_dir / "ocr.json").exists() and (output_dir / "raw.json").exists():
                    render_note(output_dir)
                    rerendered += 1
            report["rerendered_notes"] = rerendered
            report["index_report"] = export_index_impl(db, cfg.exports_dir)
            write_json(cfg.exports_dir / "ocr_review_summary.json", report)
        if json_output:
            _json(report)
        else:
            typer.echo(report)
    finally:
        db.close()


@app.command()
def render(
    pending: Annotated[bool, typer.Option("--pending")] = False,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
) -> None:
    db, _cfg = _db()
    try:
        _json(render_pending(db, limit=limit))
    finally:
        db.close()


@app.command()
def verify(
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    json_output: Annotated[bool, typer.Option("--json")] = False,
) -> None:
    db, _cfg = _db()
    try:
        report = verify_impl(db, limit=limit)
        data = report.model_dump()
        if json_output:
            _json(data)
        else:
            typer.echo(data)
        if not report.ok:
            raise typer.Exit(1)
    finally:
        db.close()


@app.command("export-index")
def export_index() -> None:
    db, cfg = _db()
    try:
        _json(export_index_impl(db, cfg.exports_dir))
    finally:
        db.close()


@app.command()
def status(json_output: Annotated[bool, typer.Option("--json")] = False) -> None:
    db, _cfg = _db()
    try:
        data = db.status_counts()
        if json_output:
            _json(data)
        else:
            typer.echo(data)
    finally:
        db.close()


@app.command("exclude-note")
def exclude_note(
    note_id: Annotated[str, typer.Argument(help="Note ID to exclude from the active archive.")],
    reason: Annotated[str, typer.Option("--reason", help="Traceable reason for excluding this note.")] = "excluded_by_user",
) -> None:
    db, _cfg = _db()
    try:
        row = db.get_note(note_id)
        if row is None:
            raise typer.BadParameter(f"note not found: {note_id}")
        db.exclude_note(note_id, reason=reason, source="user")
        _json({"ok": True, "note_id": note_id, "stage": "excluded", "reason": reason})
    finally:
        db.close()


@app.command("prepare-eval-set")
def prepare_eval_set(limit: Annotated[int, typer.Option("--limit")] = 50) -> None:
    db, cfg = _db()
    try:
        eval_dir = cfg.root / "eval"
        image_dir = eval_dir / "images"
        image_dir.mkdir(parents=True, exist_ok=True)
        candidates: list[dict] = []
        for note in db.list_notes(None):
            output_dir = Path(note["output_dir"] or "")
            if not output_dir.exists():
                continue
            for image in db.list_images(note["note_id"]):
                if image["download_status"] != "success" or not image["local_path"]:
                    continue
                src = output_dir / image["local_path"]
                if not src.exists():
                    continue
                tags = _eval_tags_for_image(note, image, src)
                candidates.append(
                    {
                        "note_id": note["note_id"],
                        "image_position": int(image["position"]),
                        "source_path": str(src),
                        "width": image["downloaded_width"] or image["width"],
                        "height": image["downloaded_height"] or image["height"],
                        "source_variant": image["source_variant"],
                        "is_probable_thumbnail": bool(image["is_probable_thumbnail"]),
                        "tags": tags,
                        "ground_truth_text": "",
                    }
                )
        entries = _select_representative_eval_samples(candidates, limit)
        for index, entry in enumerate(entries, start=1):
            src = Path(entry["source_path"])
            dest_name = f"{index:03d}_{entry['note_id']}_{int(entry['image_position']):02d}{src.suffix}"
            dest = image_dir / dest_name
            if not dest.exists():
                shutil.copy2(src, dest)
            entry["sample_id"] = f"{index:03d}"
            entry["eval_image_path"] = str(dest)
        manifest = {"schema": "xhs-archive-eval-v1", "count": len(entries), "requires_manual_ground_truth": True, "samples": entries}
        write_json(eval_dir / "manifest.json", manifest)
        _json({"ok": True, "count": len(entries), "manifest": str(eval_dir / "manifest.json"), "images_dir": str(image_dir)})
    finally:
        db.close()


@app.command("benchmark-eval-set")
def benchmark_eval_set(
    manifest_path: Annotated[str | None, typer.Option("--manifest")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
) -> None:
    cfg = load_config()
    path = Path(manifest_path) if manifest_path else cfg.root / "eval" / "manifest.json"
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
        raise typer.BadParameter("Invalid eval manifest.")
    samples = [sample for sample in data["samples"] if isinstance(sample, dict) and str(sample.get("ground_truth_text") or "").strip()]
    if limit:
        samples = samples[:limit]
    if not samples:
        report = {
            "ok": False,
            "reason": "no_ground_truth_text",
            "manifest": str(path),
            "message": "Fill ground_truth_text for sampled images before running a real OCR benchmark.",
        }
        write_json(path.parent / "benchmark_report.json", report)
        _json(report)
        return

    primary = _safe_engine(cfg.ocr.primary_engine)
    secondary = _safe_engine(cfg.ocr.secondary_engine) if cfg.ocr.secondary_engine else None
    vl_engine = _safe_engine("vl") if cfg.ocr.enable_vl_adjudication else None
    rows: list[dict] = []
    for sample in samples:
        image_path = Path(str(sample.get("eval_image_path") or sample.get("source_path") or ""))
        truth = str(sample.get("ground_truth_text") or "")
        result = audit_image(
            image_path,
            int(sample.get("image_position") or 1),
            primary=primary,
            secondary=secondary,
            vl_engine=vl_engine,
            cfg=cfg.ocr,
            thumbnail_suspected=bool(sample.get("is_probable_thumbnail")),
        )
        variants = {
            "ppocr": result.engines.get("ppocr"),
            "apple_vision": result.engines.get("apple_vision"),
            "paddleocr_vl": result.engines.get("paddleocr_vl"),
            "adjudicated": None,
        }
        metrics = {}
        for name, evidence in variants.items():
            prediction = result.final_text if name == "adjudicated" else (evidence.text if evidence else "")
            item_metrics = _ocr_benchmark_metrics(truth, prediction)
            if name == "adjudicated":
                item_metrics["uncertain_span_count"] = len(result.uncertain_spans)
                item_metrics["auto_pass"] = _benchmark_auto_pass(item_metrics, result.needs_human_review)
                item_metrics["elapsed_seconds"] = result.audit.get("elapsed_seconds")
            else:
                item_metrics["uncertain_span_count"] = None
                item_metrics["auto_pass"] = _benchmark_auto_pass(item_metrics, not evidence or evidence.status != "success")
                item_metrics["elapsed_seconds"] = evidence.elapsed_seconds if evidence else None
            metrics[name] = item_metrics
        rows.append(
            {
                "sample_id": sample.get("sample_id"),
                "note_id": sample.get("note_id"),
                "image_position": sample.get("image_position"),
                "tags": sample.get("tags") or [],
                "metrics": metrics,
                "ocr_status": result.status,
                "needs_human_review": result.needs_human_review,
                "peak_memory_bytes": _peak_memory_bytes(),
            }
        )
    report = {
        "ok": True,
        "sample_count": len(rows),
        "engines": {
            "ppocr": primary.name,
            "apple_vision": secondary.name if secondary else None,
            "paddleocr_vl": vl_engine.name if vl_engine else None,
        },
        "aggregate": _aggregate_benchmark_rows(rows),
        "samples": rows,
    }
    write_json(path.parent / "benchmark_report.json", report)
    _json(report)


def _eval_tags_for_image(note, image, image_path: Path) -> list[str]:
    tags: set[str] = set()
    width = int(image["downloaded_width"] or image["width"] or 0)
    height = int(image["downloaded_height"] or image["height"] or 0)
    if width and height:
        ratio = height / max(width, 1)
        if ratio >= 1.8:
            tags.add("long_image")
        elif width / max(height, 1) >= 1.2:
            tags.add("multi_column_or_table_candidate")
        else:
            tags.add("ordinary_text")
        if max(width, height) >= 1000 and min(width, height) >= 700:
            tags.add("small_font_clear_candidate")
    if _image_luminance(image_path) >= 180:
        tags.add("light_background")
    note_text = " ".join(str(note[key] or "") for key in ("title", "summary_title", "body_text") if key in note.keys())
    if re.search(r"\d|20\d{2}|[一二三四五六七八九十]面", note_text):
        tags.add("numbers_dates_candidate")
    if extract_terms(note_text):
        tags.add("technical_terms_candidate")
    if _has_cjk(note_text) and _has_ascii_word(note_text):
        tags.add("mixed_cn_en_candidate")
    if bool(image["is_probable_thumbnail"]):
        tags.add("thumbnail_suspected")
    return sorted(tags or {"ordinary_text"})


def _select_representative_eval_samples(candidates: list[dict], limit: int) -> list[dict]:
    required = [
        "ordinary_text",
        "technical_terms_candidate",
        "numbers_dates_candidate",
        "long_image",
        "multi_column_or_table_candidate",
        "light_background",
        "small_font_clear_candidate",
        "mixed_cn_en_candidate",
    ]
    selected: list[dict] = []
    used: set[tuple[str, int]] = set()
    for tag in required:
        for candidate in candidates:
            key = (str(candidate["note_id"]), int(candidate["image_position"]))
            if key in used:
                continue
            if tag in candidate.get("tags", []):
                selected.append(candidate)
                used.add(key)
                break
    for candidate in candidates:
        if len(selected) >= limit:
            break
        key = (str(candidate["note_id"]), int(candidate["image_position"]))
        if key in used:
            continue
        selected.append(candidate)
        used.add(key)
    return selected[:limit]


def _ocr_benchmark_metrics(truth: str, prediction: str) -> dict:
    numbers_error = _token_error_rate(extract_numbers(truth), extract_numbers(prediction))
    terms_error = _token_error_rate(extract_terms(truth), extract_terms(prediction))
    return {
        "cer": _cer(truth, prediction),
        "missing_line_rate": _line_miss_rate(truth, prediction),
        "numeric_error_rate": numbers_error,
        "technical_term_error_rate": terms_error,
        "read_order_error_rate": _read_order_error_rate(truth, prediction),
    }


def _benchmark_auto_pass(metrics: dict, needs_review: bool) -> bool:
    return bool(
        not needs_review
        and float(metrics.get("cer") or 0.0) <= 0.01
        and float(metrics.get("missing_line_rate") or 0.0) == 0.0
        and float(metrics.get("numeric_error_rate") or 0.0) == 0.0
        and float(metrics.get("technical_term_error_rate") or 0.0) == 0.0
        and float(metrics.get("read_order_error_rate") or 0.0) == 0.0
        and int(metrics.get("uncertain_span_count") or 0) == 0
    )


def _line_miss_rate(truth: str, prediction: str) -> float:
    truth_lines = [_compact(line) for line in truth.splitlines() if _compact(line)]
    if not truth_lines:
        return 0.0
    pred_lines = [_compact(line) for line in prediction.splitlines() if _compact(line)]
    pred_joined = _compact(prediction)
    missed = 0
    for line in truth_lines:
        if line in pred_joined:
            continue
        best = max((SequenceMatcher(None, line, pred).ratio() for pred in pred_lines), default=0.0)
        if best < 0.82:
            missed += 1
    return missed / len(truth_lines)


def _read_order_error_rate(truth: str, prediction: str) -> float:
    truth_lines = [_compact(line) for line in truth.splitlines() if _compact(line)]
    if len(truth_lines) <= 1:
        return 0.0
    pred_joined = _compact(prediction)
    positions: list[int] = []
    missing = 0
    for line in truth_lines:
        pos = pred_joined.find(line)
        if pos < 0:
            missing += 1
        else:
            positions.append(pos)
    inversions = sum(1 for prev, current in zip(positions, positions[1:]) if current < prev)
    return (missing + inversions) / len(truth_lines)


def _token_error_rate(expected: list[str], actual: list[str]) -> float | None:
    if not expected:
        return None
    if expected == actual:
        return 0.0
    matched = sum(block.size for block in SequenceMatcher(None, expected, actual).get_matching_blocks())
    return (max(len(expected), len(actual)) - matched) / len(expected)


def _aggregate_benchmark_rows(rows: list[dict]) -> dict:
    aggregate: dict[str, dict] = {}
    for engine in ("ppocr", "apple_vision", "paddleocr_vl", "adjudicated"):
        metrics = [row["metrics"][engine] for row in rows if engine in row.get("metrics", {})]
        aggregate[engine] = {
            "cer": _avg(metric.get("cer") for metric in metrics),
            "missing_line_rate": _avg(metric.get("missing_line_rate") for metric in metrics),
            "numeric_error_rate": _avg(metric.get("numeric_error_rate") for metric in metrics),
            "technical_term_error_rate": _avg(metric.get("technical_term_error_rate") for metric in metrics),
            "read_order_error_rate": _avg(metric.get("read_order_error_rate") for metric in metrics),
            "auto_pass_rate": _avg(1.0 if metric.get("auto_pass") else 0.0 for metric in metrics),
            "uncertain_span_count": _avg(metric.get("uncertain_span_count") for metric in metrics),
            "single_image_seconds": _avg(metric.get("elapsed_seconds") for metric in metrics),
        }
    aggregate["peak_memory_bytes"] = max((int(row.get("peak_memory_bytes") or 0) for row in rows), default=0)
    return aggregate


def _avg(values) -> float | None:
    clean = [float(value) for value in values if value is not None]
    return sum(clean) / len(clean) if clean else None


def _image_luminance(image_path: Path) -> float:
    try:
        with Image.open(image_path).convert("L") as img:
            img.thumbnail((64, 64))
            return float(ImageStat.Stat(img).mean[0])
    except Exception:
        return 0.0


def _has_cjk(text: str) -> bool:
    return bool(re.search(r"[\u4e00-\u9fff]", text))


def _has_ascii_word(text: str) -> bool:
    return bool(re.search(r"[A-Za-z]{2,}", text))


def _peak_memory_bytes() -> int:
    value = int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)
    return value if platform.system() == "Darwin" else value * 1024


@app.command("score-eval-set")
def score_eval_set(manifest_path: Annotated[str | None, typer.Option("--manifest")] = None) -> None:
    cfg = load_config()
    path = Path(manifest_path) if manifest_path else cfg.root / "eval" / "manifest.json"
    data = read_json(path)
    if not isinstance(data, dict) or not isinstance(data.get("samples"), list):
        raise typer.BadParameter("Invalid eval manifest.")
    rows = []
    for sample in data["samples"]:
        if not isinstance(sample, dict):
            continue
        truth = str(sample.get("ground_truth_text") or "")
        if not truth.strip():
            continue
        prediction = _prediction_for_sample(sample)
        rows.append(
            {
                "sample_id": sample.get("sample_id"),
                "note_id": sample.get("note_id"),
                "image_position": sample.get("image_position"),
                "cer": _cer(truth, prediction),
                "truth_length": len(_compact(truth)),
                "prediction_length": len(_compact(prediction)),
            }
        )
    avg_cer = sum(row["cer"] for row in rows) / len(rows) if rows else None
    report = {"ok": True, "scored_count": len(rows), "average_cer": avg_cer, "samples": rows}
    write_json(path.parent / "score_report.json", report)
    _json(report)


def _prediction_for_sample(sample: dict) -> str:
    source_path = Path(str(sample.get("source_path") or ""))
    ocr_path = source_path.parent / "ocr.json"
    if not ocr_path.exists():
        return ""
    data = read_json(ocr_path)
    if not isinstance(data, list):
        return ""
    wanted = int(sample.get("image_position") or 0)
    for item in data:
        if not isinstance(item, dict):
            continue
        if int(item.get("image_index") or 0) == wanted:
            return str(item.get("final_text") if item.get("final_text") is not None else item.get("text") or "")
    return ""


def _compact(text: str) -> str:
    return "".join(text.split())


def _cer(truth: str, prediction: str) -> float:
    a = _compact(truth)
    b = _compact(prediction)
    if not a:
        return 0.0 if not b else 1.0
    matched = sum(block.size for block in SequenceMatcher(None, a, b).get_matching_blocks())
    return (max(len(a), len(b)) - matched) / len(a)


@app.command()
def retry(failed: Annotated[bool, typer.Option("--failed")] = False) -> None:
    db, _cfg = _db()
    try:
        if not failed:
            raise typer.BadParameter("Only --failed is currently supported.")
        rows = db.list_notes(["failed"])
        for row in rows:
            db.update_note_fields(row["note_id"], stage="discovered", last_error=None)
        _json({"ok": True, "reset_failed_count": len(rows)})
    finally:
        db.close()


@app.command()
def run(
    board_url: Annotated[str | None, typer.Option("--board-url")] = None,
    limit: Annotated[int | None, typer.Option("--limit")] = None,
    expected_count: Annotated[int | None, typer.Option("--expected-count")] = None,
    resume: Annotated[bool, typer.Option("--resume")] = False,
    retry_failed: Annotated[bool, typer.Option("--retry-failed")] = False,
) -> None:
    resolved_url = _resolve_board_url(board_url)
    db, cfg = _db()
    try:
        db.recover_incomplete()
        if retry_failed:
            for row in db.list_notes(["failed"]):
                db.update_note_fields(row["note_id"], stage="discovered", last_error=None)
        collect_result = collect_board(db, cfg, resolved_url, limit=limit if not resume else None, expected_count=expected_count)
        if collect_result.get("stage") == "auth_required":
            _json({"ok": False, "collect": collect_result})
            raise typer.Exit(2)
        if collect_result.get("possibly_incomplete") and expected_count:
            _json({"ok": False, "collect": collect_result, "reason": "collection_incomplete"})
            raise typer.Exit(1)
        fetch_result = fetch_pending(db, cfg, limit=limit if not resume else None)
        if any(item.get("stage") == "auth_required" for item in fetch_result.get("results", [])):
            _json({"ok": False, "collect": collect_result, "fetch": fetch_result})
            raise typer.Exit(2)
        ocr_result = ocr_pending(db, cfg, engine_name=cfg.ocr.engine, limit=limit if not resume else None)
        render_result = render_pending(db, limit=limit if not resume else None)
        verify_report = verify_impl(db, limit=limit if not resume else None)
        index_result = export_index_impl(db, cfg.exports_dir)
        _json(
            {
                "ok": verify_report.ok,
                "collect": collect_result,
                "fetch": fetch_result,
                "ocr": ocr_result,
                "render": render_result,
                "verify": verify_report.model_dump(),
                "index": index_result,
            }
        )
        if not verify_report.ok:
            raise typer.Exit(1)
    finally:
        db.close()


if __name__ == "__main__":
    app()
