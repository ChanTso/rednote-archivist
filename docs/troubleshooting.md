# Troubleshooting

## uv Cannot Access Cache

Use the workspace cache:

```bash
UV_CACHE_DIR=.uv-cache uv sync --group dev
```

## Conda Appears Active

This project must not use Conda. If `doctor` reports inherited Conda variables, run commands with the project `.venv` through `uv` and avoid `conda activate`, `conda install`, or modifying Conda environments.

## Login or CAPTCHA Appears

Do not bypass it. Complete verification in the opened browser. The CLI will keep the Playwright profile under `data/browser-profile/` and resume from SQLite state.

## OCR Backend Fails

Run:

```bash
UV_CACHE_DIR=.uv-cache uv run xhs-archive doctor --json
```

If PaddleOCR CPU is unavailable, use RapidOCR + ONNX Runtime ARM64. PaddleOCR-VL is optional and should remain disabled for full runs unless a small CPU benchmark is stable.
