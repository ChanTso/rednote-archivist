from __future__ import annotations

from pathlib import Path

from PIL import Image

from .config import AppConfig
from .db import ArchiveDB
from .models import ImageMeta
from .ocr import create_ocr_engine
from .ocr.audit import audit_image, evidence_from_result
from .ocr.base import NullOCREngine, OCREngine
from .utils import file_sha256, now_iso, write_json


def _safe_engine(name: str) -> OCREngine:
    try:
        return create_ocr_engine(name)
    except Exception as exc:
        engine = NullOCREngine(f"{name} unavailable: {type(exc).__name__}: {exc}")
        engine.name = "paddleocr-vl" if name == "vl" else name
        return engine


def _backfill_image_file_meta(db: ArchiveDB, note_id: str, image, output_dir: Path) -> bool:
    if image["download_status"] != "success" or not image["local_path"]:
        return False
    path = output_dir / image["local_path"]
    if not path.exists():
        return False
    with Image.open(path) as img:
        width, height = img.size
        image_format = img.format
    db.upsert_image(
        note_id,
        ImageMeta(
            position=int(image["position"]),
            source_url=image["source_url"],
            source_variant=image["source_variant"],
            display_width=image["display_width"],
            display_height=image["display_height"],
            declared_width=image["declared_width"],
            declared_height=image["declared_height"],
            local_path=image["local_path"],
            sha256=file_sha256(path),
            width=width,
            height=height,
            downloaded_width=width,
            downloaded_height=height,
            file_size=path.stat().st_size,
            format=image_format,
            is_probable_thumbnail=bool(image["is_probable_thumbnail"]),
            thumbnail_reason=image["thumbnail_reason"],
            download_status="success",
            ocr_status=image["ocr_status"] or "pending",
            ocr_engine=image["ocr_engine"],
            ocr_confidence=image["ocr_confidence"],
            last_error=image["last_error"],
        ),
    )
    return True


def ocr_pending(db: ArchiveDB, cfg: AppConfig, *, engine_name: str = "auto", limit: int | None = None, force: bool = False) -> dict:
    rows = db.list_notes(["fetched", "ocr_done", "rendered", "verified"] if force else ["fetched"], limit=limit)
    primary_name = cfg.ocr.primary_engine if engine_name == "auto" else engine_name
    primary = _safe_engine(primary_name)
    secondary = _safe_engine(cfg.ocr.secondary_engine) if cfg.ocr.secondary_engine else None
    vl_engine = _safe_engine("vl") if cfg.ocr.enable_vl_adjudication else None
    completed: list[str] = []
    failed: list[dict] = []
    for row in rows:
        note_id = row["note_id"]
        output_dir = Path(row["output_dir"] or "")
        try:
            if not output_dir.exists():
                raise FileNotFoundError(f"output_dir missing: {output_dir}")
            db.update_note_fields(note_id, stage="ocr_running")
            images = db.list_images(note_id)
            results = []
            for image in images:
                if not image["file_size"] or not image["downloaded_width"] or not image["downloaded_height"]:
                    _backfill_image_file_meta(db, note_id, image, output_dir)
                    image = db.conn.execute(
                        "SELECT * FROM images WHERE note_id=? AND position=?",
                        (note_id, image["position"]),
                    ).fetchone()
                if image["download_status"] != "success" or not image["local_path"]:
                    failed_result = NullOCREngine(image["last_error"] or "image not downloaded").recognize(
                        output_dir / (image["local_path"] or f"{int(image['position']):02d}.missing"),
                        int(image["position"]),
                    )
                    results.append(
                        {
                            "image_index": int(image["position"]),
                            "image_path": image["local_path"] or "",
                            "status": "failed",
                            "final_text": "",
                            "needs_human_review": True,
                            "engines": {"ppocr": evidence_from_result(failed_result).model_dump()},
                            "agreement_score": None,
                            "uncertain_spans": [],
                            "audit": {"error": image["last_error"] or "image not downloaded"},
                        }
                    )
                    db.update_image_ocr(
                        note_id,
                        image["position"],
                        status="failed",
                        engine=primary.name,
                        confidence=None,
                        error=image["last_error"] or "image not downloaded",
                    )
                    continue
                image_path = output_dir / image["local_path"]
                result = audit_image(
                    image_path,
                    int(image["position"]),
                    primary=primary,
                    secondary=secondary,
                    vl_engine=vl_engine,
                    cfg=cfg.ocr,
                    thumbnail_suspected=bool(image["is_probable_thumbnail"]),
                )
                results.append(result.model_dump())
                status = "needs_review" if result.needs_human_review and result.status == "success" else result.status
                db.update_image_ocr(
                    note_id,
                    image["position"],
                    status=status,
                    engine="+".join(result.sources) if result.sources else primary.name,
                    confidence=result.agreement_score,
                    error=None if not result.needs_human_review else "ocr_audit_needs_review",
                )
            write_json(output_dir / "ocr.json", results)
            db.update_note_fields(note_id, stage="ocr_done", ocr_completed_at=now_iso())
            completed.append(note_id)
        except Exception as exc:
            db.mark_failed(note_id, "ocr", f"{type(exc).__name__}: {exc}", "ocr_failed")
            failed.append({"note_id": note_id, "error": f"{type(exc).__name__}: {exc}"})
    return {"ok": not failed, "engine": primary.name, "secondary_engine": secondary.name if secondary else None, "completed": completed, "failed": failed}
