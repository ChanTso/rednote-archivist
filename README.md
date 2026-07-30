# Rednote Archivist

[![CI](https://github.com/ChanTso/rednote-archivist/actions/workflows/ci.yml/badge.svg)](https://github.com/ChanTso/rednote-archivist/actions/workflows/ci.yml)
[![Python 3.11](https://img.shields.io/badge/python-3.11-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green.svg)](LICENSE)

A local-first archival agent for your own RedNote (Xiaohongshu) collections. It turns saved notes into a resumable, auditable dataset with native images, OCR evidence, Markdown, and integrity reports.

> Rednote Archivist is an independent personal-use project. It is not affiliated with, endorsed by, or operated by Xiaohongshu/RedNote.

## Why it exists

Browser collections are convenient but difficult to search, back up, and verify. Rednote Archivist treats archival as a data pipeline rather than a one-off scraper:

- **Resumable by design** — SQLite records every note and processing stage.
- **Evidence preserving** — raw metadata, HTML, images, OCR candidates, and hashes remain traceable.
- **Accuracy oriented** — OCR can compare PP-OCRv6 with Apple Vision and route conflicts to review.
- **Conservative on-platform** — one browser worker, configurable delays, and no CAPTCHA or signature bypass.
- **Private by default** — cookies, browser profiles, databases, logs, notes, and exports stay under ignored `data/`.
- **Verifiable** — a note is complete only when database state and required files agree.

## Architecture

```text
authenticated browser
        │
        ▼
multi-source discovery ──► SQLite state machine
        │                         │
        ▼                         ▼
metadata + native images ──► OCR evidence
                                  │
                                  ▼
                          Markdown + indexes
                                  │
                                  ▼
                          integrity verification
```

The pipeline is idempotent: interrupted work can resume without treating partially written files as complete.

## Quick start

### Requirements

- Apple Silicon macOS
- Python 3.11
- [`uv`](https://docs.astral.sh/uv/)
- Chrome or Playwright Chromium

```bash
git clone https://github.com/ChanTso/rednote-archivist.git
cd rednote-archivist

UV_CACHE_DIR=.uv-cache uv sync --locked --group dev
UV_CACHE_DIR=.uv-cache uv run playwright install chromium
cp config.example.toml config.toml
UV_CACHE_DIR=.uv-cache uv run xhs-archive doctor --json
```

Log in interactively. Complete any QR, SMS, or CAPTCHA challenge yourself:

```bash
UV_CACHE_DIR=.uv-cache uv run xhs-archive login
```

Start with a small smoke run against a collection you own or are authorized to archive:

```bash
export XHS_BOARD_URL="https://www.xiaohongshu.com/board/your-board-id"
UV_CACHE_DIR=.uv-cache uv run xhs-archive run \
  --board-url "$XHS_BOARD_URL" \
  --limit 3
```

Then verify the archive and resume the full collection:

```bash
UV_CACHE_DIR=.uv-cache uv run xhs-archive verify --json
UV_CACHE_DIR=.uv-cache uv run xhs-archive run \
  --board-url "$XHS_BOARD_URL" \
  --resume
```

## Core commands

| Command | Purpose |
| --- | --- |
| `doctor --json` | Check Python, browser, OCR backends, and local environment |
| `login` | Create or refresh the local browser profile |
| `collect` | Discover note links from a collection |
| `discover` | Reconcile network, DOM, and embedded-state evidence |
| `fetch --pending` | Save metadata, HTML, screenshots, and native images |
| `ocr --pending` | Run the configured OCR pipeline |
| `render` | Generate per-note Markdown |
| `verify --json` | Compare SQLite state with files and hashes |
| `export-index` | Build searchable aggregate indexes |
| `status --json` | Summarize pipeline progress |
| `retry --failed` | Reset failed items for a safe retry |

Run `uv run xhs-archive --help` or `uv run xhs-archive <command> --help` for the complete interface.

## Local data layout

All user data is written beneath `data/` and excluded from version control:

```text
data/
├── state.db          # pipeline source of truth
├── browser-profile/  # authenticated browser state
├── notes/            # raw evidence, images, OCR, and Markdown
├── exports/          # indexes and review reports
└── logs/             # operational logs
```

Back up `data/` separately if you need to preserve login state or archive output. Never attach it to a public issue.

## OCR strategy

The default accuracy-oriented cascade is:

1. download the strongest native image candidate;
2. run PP-OCRv6 on original pixels;
3. compare with Apple Vision when available;
4. detect disagreements in text, numbers, dates, and technical terms;
5. preserve candidates, crops, overlays, and uncertainty for review.

RapidOCR is available as a CPU fallback. Optional vision-language adjudication never silently overrides the evidence. See [OCR architecture](docs/ocr_architecture.md) for details.

## Privacy and responsible use

- Archive only content you own or have permission to process.
- Follow platform terms, applicable law, copyright, and privacy obligations.
- Do not bypass login challenges, risk controls, access restrictions, or private API signatures.
- Keep collection concurrency at `1` and use conservative delays.
- Review the [privacy guide](docs/privacy.md) before sharing logs, fixtures, or exports.

This public source tree contains no source post bodies or media, no private collection exports,
and no question banks or indexes derived from a private archive. Account identifiers, cookies,
browser profiles, databases, generated notes, and OCR output must remain in ignored local storage.
All committed fixtures are synthetic.

## Development

```bash
UV_CACHE_DIR=.uv-cache uv sync --locked --group dev
UV_CACHE_DIR=.uv-cache uv run ruff check src tests
UV_CACHE_DIR=.uv-cache uv run pytest
```

Selector changes should include synthetic fixtures and parser tests. See [CONTRIBUTING.md](CONTRIBUTING.md) for the development workflow and [SECURITY.md](SECURITY.md) for private vulnerability reporting.

## Documentation

- [OCR architecture](docs/ocr_architecture.md)
- [Selector strategy](docs/selectors.md)
- [Troubleshooting](docs/troubleshooting.md)
- [Privacy and publication checklist](docs/privacy.md)

## License

Code is available under the [MIT License](LICENSE). Content archived with this tool remains subject to its original ownership and terms.
