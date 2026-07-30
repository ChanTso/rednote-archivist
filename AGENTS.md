# Contributor Instructions

## Environment

- Use `uv` and Python 3.11.
- Keep the virtual environment and cache inside the repository.
- Do not rely on Conda or install CUDA dependencies.
- Treat Apple Silicon macOS as the primary runtime; keep non-macOS imports optional.

## Quality checks

Run before submitting changes:

```bash
UV_CACHE_DIR=.uv-cache uv sync --locked --group dev
UV_CACHE_DIR=.uv-cache uv run ruff check src tests
UV_CACHE_DIR=.uv-cache uv run pytest
```

Update synthetic fixtures and tests before changing live-page selectors.

## Data safety

- Never commit `data/`, browser profiles, cookies, databases, logs, raw pages, archived media, or user exports.
- Never print cookies, tokens, sensitive headers, or browser-profile contents.
- Keep machine-readable command output on stdout and operational logs on stderr or under `data/logs/`.
- Preserve resumability: SQLite is the state source of truth, and commands must be idempotent.

## Platform boundaries

- Do not bypass login, CAPTCHA, SMS verification, risk controls, or access restrictions.
- Do not forge or reverse engineer private API signatures.
- Keep browser collection concurrency at one with conservative delays.
- When authentication is required, preserve state and ask the user to complete it.

## Verification

- A successful run requires both SQLite state and file-integrity checks.
- Preserve raw evidence during retries.
- Keep OCR candidates and uncertainty metadata when engines disagree.
- Do not use real account IDs, note IDs, names, URLs, screenshots, or interview content in tests.
