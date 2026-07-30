from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field


class DataConfig(BaseModel):
    root: Path = Path("data")


class BrowserConfig(BaseModel):
    headless: bool = False
    slow_mo_ms: int = 100
    min_delay_seconds: float = 3
    max_delay_seconds: float = 7
    concurrency: int = 1
    save_page_screenshot: bool = True


class CollectorConfig(BaseModel):
    stable_rounds: int = 5
    scroll_step_px: int = 900
    max_scroll_rounds: int = 500
    recovery_scroll_attempts: int = 8
    recovery_delay_seconds: float = 5.0
    fine_scan_step_px: int = 320
    fine_scan_delay_ms: int = 450


class FetchConfig(BaseModel):
    max_retries: int = 3
    timeout_seconds: int = 60


class OCRConfig(BaseModel):
    engine: str = "auto"
    device: str = "cpu"
    primary_engine: str = "pp-ocrv6-medium"
    secondary_engine: str = "apple-vision"
    vision_recognition_level: str = "accurate"
    standard_confidence_threshold: float = 0.82
    max_workers: int = 2
    ppocr_workers: int = 2
    vision_workers: int = 2
    vl_workers: int = 1
    enable_vl_fallback: bool = True
    enable_vl_adjudication: bool = True
    vl_engine: str = "paddleocr-vl-1.6"
    vl_backend: str = "mlx-vlm"
    vl_benchmark_required: bool = True
    vl_max_seconds_per_image: int = 60
    preserve_all_candidates: bool = True
    preserve_raw_boxes: bool = True
    preserve_region_crops: bool = True
    agreement_threshold: float = 0.97
    exclusive_character_threshold: int = 5
    review_missing_abs_chars: int = 12
    review_missing_relative_threshold: float = 0.25
    review_minor_char_diff: int = 2
    review_minor_similarity_threshold: float = 0.92
    review_reading_order_inversion_threshold: float = 0.25
    review_low_resolution_long_side: int = 1000
    review_low_resolution_short_side: int = 300
    rerun_scale: float = 2.0
    local_rerun_scale: float = 3.0
    tile_overlap_ratio: float = 0.15
    detect_thumbnail: bool = True
    require_original_pixel_ocr_first: bool = True
    generate_overlay_on_conflict: bool = True


class RenderConfig(BaseModel):
    include_original_images: bool = True
    include_raw_ocr_confidence: bool = True


class AppConfig(BaseModel):
    data: DataConfig = Field(default_factory=DataConfig)
    browser: BrowserConfig = Field(default_factory=BrowserConfig)
    collector: CollectorConfig = Field(default_factory=CollectorConfig)
    fetch: FetchConfig = Field(default_factory=FetchConfig)
    ocr: OCRConfig = Field(default_factory=OCRConfig)
    render: RenderConfig = Field(default_factory=RenderConfig)

    @property
    def root(self) -> Path:
        return self.data.root

    @property
    def db_path(self) -> Path:
        return self.root / "state.db"

    @property
    def browser_profile_dir(self) -> Path:
        return self.root / "browser-profile"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    @property
    def exports_dir(self) -> Path:
        return self.root / "exports"

    @property
    def notes_dir(self) -> Path:
        return self.root / "notes"


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | Path | None = None) -> AppConfig:
    config_path = Path(path) if path else Path("config.toml")
    data: dict[str, Any] = {}
    if config_path.exists():
        with config_path.open("rb") as fh:
            data = tomllib.load(fh)
    cfg = AppConfig.model_validate(_deep_merge(AppConfig().model_dump(), data))
    for directory in [cfg.root, cfg.logs_dir, cfg.exports_dir, cfg.notes_dir, cfg.browser_profile_dir]:
        directory.mkdir(parents=True, exist_ok=True)
    return cfg
