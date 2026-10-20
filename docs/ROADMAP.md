# Roadmap

Status of the build is in [PROGRESS.md](PROGRESS.md). This file lists intentions, not promises.

## Next: Phase 13 - hardening and final verification
Full review against the specification, dependency audit, load and failure testing of runners and the
progress stream, accessibility pass, final end-to-end run with real provider keys.

## Known gaps worth closing
* Check `robots.txt` before fetching and offer a per-domain allow/deny list.
* OCR for scanned PDFs; optional JavaScript rendering for pages that need it.
* PDF support for right-to-left scripts; page numbers in the Word contents list.
* Automatic scheduling of `purge-accounts` and `recover-jobs` (today the operator schedules them).
* A Dockerfile and example process-supervisor files.
* Health checks that include the queue and worker.
* Retention settings the operator can configure (usage records, audit events, inactive accounts).
* Measuring real report quality against human-reviewed samples.

## Not planned
Billing, teams or sharing between users, and a public API: none exists in the code today.
