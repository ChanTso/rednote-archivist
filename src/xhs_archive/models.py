from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

Stage = Literal[
    "discovered",
    "fetching",
    "fetched",
    "ocr_running",
    "ocr_done",
    "rendered",
    "verified",
    "failed",
    "auth_required",
    "excluded",
]


class ImageMeta(BaseModel):
    position: int
    source_url: str | None = None
    source_variant: str | None = None
    display_width: int | None = None
    display_height: int | None = None
    declared_width: int | None = None
    declared_height: int | None = None
    local_path: str | None = None
    sha256: str | None = None
    width: int | None = None
    height: int | None = None
    downloaded_width: int | None = None
    downloaded_height: int | None = None
    file_size: int | None = None
    format: str | None = None
    is_probable_thumbnail: bool = False
    thumbnail_reason: str | None = None
    download_status: str = "pending"
    ocr_status: str = "pending"
    ocr_engine: str | None = None
    ocr_confidence: float | None = None
    last_error: str | None = None


class ImageCandidate(BaseModel):
    position: int
    url: str
    source_variant: str = "unknown"
    display_width: int | None = None
    display_height: int | None = None
    declared_width: int | None = None
    declared_height: int | None = None
    score: int = 0


class ParsedNote(BaseModel):
    note_id: str
    url: str
    canonical_url: str
    title: str | None = None
    summary_title: str | None = None
    author_name: str | None = None
    author_display_id: str | None = None
    author_profile_id: str | None = None
    publish_time_raw: str | None = None
    publish_time_iso: str | None = None
    body_text: str | None = None
    tags: list[str] = Field(default_factory=list)
    image_urls: list[str] = Field(default_factory=list)
    image_candidates: list[ImageCandidate] = Field(default_factory=list)
    image_count: int = 0
    collected_at: str | None = None
    raw_extras: dict[str, Any] = Field(default_factory=dict)


class OCRImageResult(BaseModel):
    image_index: int
    image_path: str
    engine: str
    engine_version: str | None = None
    model: str | None = None
    status: str
    text: str = ""
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    mean_confidence: float | None = None
    elapsed_seconds: float = 0.0
    needs_review: bool = False
    error: str | None = None


class OCREngineEvidence(BaseModel):
    status: str
    engine: str
    engine_version: str | None = None
    model: str | None = None
    text: str = ""
    blocks: list[dict[str, Any]] = Field(default_factory=list)
    mean_confidence: float | None = None
    elapsed_seconds: float | None = None
    error: str | None = None


class OCRUncertainSpan(BaseModel):
    bbox: list[float] | None = None
    crop_path: str | None = None
    candidates: list[dict[str, Any]] = Field(default_factory=list)
    selected: str | None = None
    reason: str | None = None
    needs_human_review: bool = False


class OCRAuditImageResult(BaseModel):
    image_index: int
    image_path: str
    native_width: int | None = None
    native_height: int | None = None
    original_download_verified: bool = False
    engines: dict[str, OCREngineEvidence] = Field(default_factory=dict)
    agreement_score: float | None = None
    uncertain_spans: list[OCRUncertainSpan] = Field(default_factory=list)
    final_text: str = ""
    needs_human_review: bool = False
    status: str = "success"
    sources: list[str] = Field(default_factory=list)
    audit: dict[str, Any] = Field(default_factory=dict)


class VerifyIssue(BaseModel):
    note_id: str | None = None
    path: str | None = None
    code: str
    message: str


class VerifyReport(BaseModel):
    ok: bool
    checked_notes: int
    issues: list[VerifyIssue] = Field(default_factory=list)
    warnings: list[VerifyIssue] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)


class NotePaths(BaseModel):
    output_dir: Path
    raw_json: Path
    page_html: Path
    page_png: Path
    ocr_json: Path
    note_md: Path
