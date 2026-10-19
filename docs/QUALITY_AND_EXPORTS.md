# Quality scoring and exports

## Two kinds of evaluation, never mixed
1. **Calculated (deterministic).** Computed by code from stored data; the formula for each dimension is on the Quality page and in `reports/quality.py` (`METHODS`, rubric version 1). Reproducible. These drive the quality gates and the "checked" mark.
2. **AI assessment (opinion).** Optional. A model reads the report against a written rubric and scores clarity/structure, completeness and question coverage. It is stored as `evaluator = llm`, shown separately, labelled as an opinion, and **never affects a gate**. It cannot verify facts.

A dimension that cannot be measured is stored as `not_assessed` with no score (for example source relevance when no source has a relevance score, contradiction handling when the evidence has no contradiction). It is shown as "not assessed", never as 0 or 100.

## Dimensions (calculated)
Question coverage, source diversity, source relevance, citation integrity, evidence coverage, contradiction handling, completeness, limitations identified; clarity and structure is AI-only. Overall = weighted mean of the assessed dimensions (weights shown on the page).

## Gates (all must pass for the "checked" mark)
Citations valid and no unverified quotation; no claim unsupported or contradicted; claim support checking finished; every planned section present with content; Limitations section present; independent review ran and left no major issue open (sections you edited are warned about instead, because the review did not re-read them).
"Checked" means the automatic checks found no problem. It is not proof that statements are true.

## Re-checking
Editing clears the mark. **Re-run checks** (Quality page) recomputes everything on the current text, stores a new evaluation and sets the mark from the gates. No AI call is made.

## Warnings listed
Insufficient evidence, missing or invalid citations, unavailable or snippet-only sources, contradictions, missing sections, failed gates, edited-after-review sections.

## Exports (`/research/<project>/report/export/<pdf|docx|md|html>`)
- Owner only (404 otherwise); each export is written to the audit log.
- Content: cover/title and generation date, notice if not verified, research scope, methodology (counts from the stored run), contents list (4+ sections), the report, quality assessment and warnings, numbered references with addresses, dates and read status.
- Citations `[@S12]` become `[n]`, linking to the reference list in PDF, Word and HTML.
- Escaping: HTML passes the report-page sanitiser; Markdown escapes `<`; Word and PDF insert text as plain runs / escaped markup. Only http(s) addresses become links. The download is served as an attachment with a restrictive CSP.
- PDF: ReportLab, A4, page numbers, contents page. Needs DejaVu fonts for non-Latin text; scripts needing shaping (Arabic, Urdu) are not supported in PDF.
- Word: python-docx; contents list links to headings without page numbers.
