# OCR Accuracy Architecture

This project keeps the Playwright + SQLite archive pipeline as the primary architecture. OpenCLI is optional only for future collection/media-download comparison and must not replace SQLite state, OCR, Markdown rendering, verification, or retry logic.

## Image Provenance

OCR input must come from downloaded native image files, not page screenshots or CSS display sizes.

For every carousel image, the fetch stage now preserves candidate provenance and download evidence:

- candidate source variant, such as `structured-original`, `structured-wb-dft`, `largest-srcset`, or `current-src`
- display and declared dimensions when available
- downloaded native width and height
- file size, image format, and SHA-256
- thumbnail suspicion flag and reason

If all candidates look like thumbnails, the file is still preserved but verification reports `thumbnail_suspected`.

## OCR Cascade

The main OCR path is:

1. Download the best available native image.
2. Run PP-OCRv6 medium on the original pixels.
3. Run Apple Vision OCR in accurate mode as an independent second opinion.
4. Align and compare results by normalized text, OCR block counts, critical numbers/dates/technical terms, and character agreement.
5. Trigger PaddleOCR-VL / MLX-VLM adjudication only for conflicts or complex cases. Current local VL availability is detected by `doctor --json`; failure does not block archiving.
6. Save `uncertain_spans` with crops and all candidates for unresolved regions.

RapidOCR + ONNX Runtime ARM64 remains a deployment fallback if PaddleOCR cannot run; it is not treated as an independent second opinion.

## Apple Vision

`tools/vision-ocr/vision_ocr.swift` is compiled locally into `.build/vision-ocr`. The CLI uses:

- `recognitionLevel = .accurate`
- Chinese simplified/traditional and English languages, filtered by what the system reports as supported
- language correction off by default

Some sandboxed environments may return `nilError` from Vision. Use `doctor --json` in the
actual run environment for the authoritative status, then validate the backend with data you are
authorized to process.

## Review Semantics

The project no longer relies on a single `needs_review` boolean. New OCR JSON items include:

- raw evidence from PP-OCR, Apple Vision, and conditional VL
- `agreement_score`
- `final_text`
- `uncertain_spans`
- `needs_human_review`

High character agreement is not enough when numbers, dates, company/role words, or technical terms disagree. If VL is unavailable, those critical conflicts are kept in the review queue.

## Evaluation Set

Before trusting a full run, build a small real-image evaluation set:

```bash
PYTHONPATH=src UV_NO_SYNC=1 UV_CACHE_DIR=.uv-cache uv run python -m xhs_archive.cli prepare-eval-set --limit 50
```

Fill `data/eval/manifest.json` with `ground_truth_text`, then score:

```bash
PYTHONPATH=src UV_NO_SYNC=1 UV_CACHE_DIR=.uv-cache uv run python -m xhs_archive.cli score-eval-set
```

The scorer writes `data/eval/score_report.json`.
