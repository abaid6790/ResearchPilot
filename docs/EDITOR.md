# Source explorer and report editor

## Source explorer (`/research/<project>/sources`)
Shows the sources of the project's latest run: title, address (http/https links only), domain, publication date (only if the source states it), retrieval time, category, the research question it was collected for, an excerpt, how many claims use it, whether the report cites it, and warnings (snippet only, unreachable, blocked, paywalled, metadata mismatch). Filters: search (titles, domains, excerpts), domain, category, cited / not cited / with problems.

"Support" is taken from the per-claim excerpt judgements (`direct`, `partial`, `unrelated`) and is "not yet reviewed" until a check has run. A reachable link is never presented as proof.

## Report editor (`/research/<project>/report/edit`)
- Edit one section at a time; the references list is generated and read-only.
- Save (manual) and autosave (4 s after typing stops; one rolling history entry). Saves carry the section version loaded; a stale version is refused with a conflict and the text is not overwritten. Autosave stops after a conflict and says so.
- Every manual save, restore and applied suggestion is a revision in `report_revisions`. Compare any version with the current text; restore creates a new revision.
- Suggest a rewrite: the model writes from the stored claims only; the result is stored as a `proposed` revision with a diff. Apply or discard. Unsupported citation markers are removed. A suggestion older than the latest edit cannot be applied.
- Critique a section: advice only, nothing is stored or changed.
- Citation checks run after each save: markers to sources outside this report, quotations of five or more words not found in the cited sources, long uncited paragraphs. They cannot prove a statement is true.
- Copy report text: citation markers become [n] with a reference list.
- After an edit the report is no longer "checked" until Phase 10 re-runs validation.
