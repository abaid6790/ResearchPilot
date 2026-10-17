"""Vocabularies shared by models, services and the UI. Each is enforced by a CHECK constraint."""

from __future__ import annotations

# Job lifecycle (spec section 5). "completed_with_warnings" means finished but a quality gate
# or validation found unresolved issues the user should look at.
JOB_QUEUED = "queued"
JOB_PLANNING = "planning"
JOB_RESEARCHING = "researching"
JOB_ANALYZING = "analyzing"
JOB_WRITING = "writing"
JOB_REVIEWING = "reviewing"
JOB_VALIDATING = "validating"
JOB_COMPLETED = "completed"
JOB_COMPLETED_WITH_WARNINGS = "completed_with_warnings"
JOB_FAILED = "failed"
JOB_CANCELLED = "cancelled"

JOB_ACTIVE_STATUSES = (
    JOB_QUEUED, JOB_PLANNING, JOB_RESEARCHING, JOB_ANALYZING, JOB_WRITING, JOB_REVIEWING, JOB_VALIDATING,
)  # fmt: skip
JOB_FINISHED_STATUSES = (JOB_COMPLETED, JOB_COMPLETED_WITH_WARNINGS, JOB_FAILED, JOB_CANCELLED)
JOB_STATUSES = JOB_ACTIVE_STATUSES + JOB_FINISHED_STATUSES

DEPTHS = ("quick", "standard", "deep", "comprehensive")

EVENT_LEVELS = ("info", "warning", "error")

TASK_KINDS = ("question", "subtopic", "search", "specialist", "analysis", "writing", "review", "validation")
TASK_STATUSES = ("pending", "running", "done", "failed", "skipped", "cancelled")
AGENTS = ("planner", "researcher", "analyst", "writer", "critic", "validator", "specialist", "system")

SOURCE_ORIGINS = ("web_search", "upload", "user_supplied")
FETCH_STATUSES = ("pending", "retrieved", "snippet_only", "failed", "blocked", "paywalled")
# URL availability is NOT factual verification (spec section 4, agent 6): link_status says whether
# the URL answered; support is judged per claim in claim_sources.support_level.
LINK_STATUSES = ("unchecked", "reachable", "redirected", "unreachable", "unsafe")
METADATA_STATUSES = ("unchecked", "matched", "mismatch", "unavailable")
SOURCE_CATEGORIES = (
    "official_documentation", "academic", "news", "industry", "government", "reference", "blog", "forum",
    "company", "other",
)  # fmt: skip

CLAIM_TYPES = ("fact", "estimate", "opinion", "inference")
CLAIM_SUPPORT = ("unassessed", "supported", "partial", "insufficient", "contradicted")
CLAIM_RELATIONS = ("supports", "contradicts", "context")
SUPPORT_LEVELS = ("unreviewed", "direct", "partial", "unrelated")

REPORT_STATUSES = ("draft", "final", "archived")
REVISION_KINDS = ("section", "document")
REVISION_SOURCES = ("agent_writer", "agent_editor", "user_edit", "regeneration", "restore")
REVISION_STATUSES = ("applied", "proposed", "discarded")

EVALUATORS = ("deterministic", "llm")
QUALITY_DIMENSIONS = (
    "question_coverage", "source_diversity", "source_relevance", "citation_integrity", "evidence_coverage",
    "contradiction_handling", "clarity_structure", "completeness", "limitations", "overall",
)  # fmt: skip
EVALUATION_STATUSES = ("scored", "not_assessed")

# Inline citation marker used inside report text, e.g. "[@S12]" (12 = research_sources.id).
# Citations therefore always point at persistent source rows, never at free-form URLs.
CITATION_MARKER_PATTERN = r"\[@S(\d{1,12})\]"
