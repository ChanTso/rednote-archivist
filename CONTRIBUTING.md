# Contributing

Thanks for helping improve Rednote Archivist.

## Before opening a change

- Use a focused branch and keep each pull request narrowly scoped.
- Open an issue first for substantial behavior or data-model changes.
- Never include real account data, collection URLs, note IDs, cookies, screenshots, or archived content.
- Use synthetic fixtures that cannot be mistaken for production data.

## Development setup

```bash
UV_CACHE_DIR=.uv-cache uv sync --locked --group dev
UV_CACHE_DIR=.uv-cache uv run playwright install chromium
```

Run the checks before opening a pull request:

```bash
UV_CACHE_DIR=.uv-cache uv run ruff check src tests
UV_CACHE_DIR=.uv-cache uv run pytest
```

## Pull requests

Describe the motivation, user-visible effect, validation performed, and any privacy or platform impact. Add or update tests for behavior changes. Direct pushes to `main` are not part of the contribution workflow.

By contributing, you agree that your contribution is licensed under the MIT License.
