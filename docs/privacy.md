# Privacy and Publication Guide

Rednote Archivist processes authenticated browsing state and content that may belong to other people. Treat the entire local `data/` directory as private.

## Data classification

| Data | Examples | Version control |
| --- | --- | --- |
| Authentication | cookies, browser profile, headers, tokens | Never commit |
| Personal identifiers | account IDs, display IDs, names, profile links | Never commit |
| Source identifiers | collection URLs, note IDs, signed media URLs | Never commit |
| Archived content | HTML, screenshots, images, OCR text, Markdown | Never commit |
| Derived content | excerpts, question banks, indexes, labels, summaries | Never commit |
| Operational data | SQLite databases, logs, review crops, exports | Never commit |
| Test data | synthetic HTML, generated images, fake identifiers | Allowed |

## Before publishing

1. Confirm `git status --ignored` shows user data only as ignored.
2. Search the full Git history, not only the current checkout.
3. Run a secret scanner against all commits.
4. Replace real IDs and names in tests with unmistakably synthetic values.
5. Verify that examples use placeholders and redacted query parameters.
6. Review screenshots and binary fixtures manually.

If sensitive or source-derived data was committed previously, deleting the current file is
insufficient. Create a clean history with no contaminated branches, tags, or pull-request refs
before changing repository visibility, then rotate any exposed credential.

## Sharing diagnostics

Prefer the JSON summary from `doctor`, `status`, or `verify`. Before sharing, remove absolute paths and inspect exception text. Never upload `data/`, a browser profile, or an unreviewed log archive to a public issue.
