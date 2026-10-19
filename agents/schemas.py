"""Structured outputs the agents ask models for. Everything a model returns is validated
against these; the code then re-checks references (ids, sources) before using any of it."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class Subtopic(BaseModel):
    title: str = Field(min_length=3, max_length=200)
    questions: list[str] = Field(min_length=1, max_length=4)
    search_queries: list[str] = Field(min_length=1, max_length=3)
    evidence_needed: str = Field(default="", max_length=400)


class OutlineSection(BaseModel):
    key: str = Field(pattern=r"^[a-z][a-z0-9_]{1,48}$")
    heading: str = Field(min_length=2, max_length=120)
    purpose: str = Field(default="", max_length=300)
    subtopic_titles: list[str] = Field(default_factory=list, max_length=8)


class ResearchPlan(BaseModel):
    objective: str = Field(min_length=5, max_length=600)
    subtopics: list[Subtopic] = Field(min_length=1, max_length=12)
    outline: list[OutlineSection] = Field(min_length=2, max_length=12)
    ambiguities: list[str] = Field(default_factory=list, max_length=6)


class FollowUpQueries(BaseModel):
    queries: list[str] = Field(min_length=1, max_length=3)


class ClaimEvidence(BaseModel):
    excerpt_id: str = Field(max_length=16)  # "E<number>", as listed in the prompt
    relation: Literal["supports", "contradicts", "context"] = "supports"


class FindingClaim(BaseModel):
    text: str = Field(min_length=10, max_length=600)
    claim_type: Literal["fact", "estimate", "opinion", "inference"] = "fact"
    confidence: float = Field(ge=0, le=1)
    evidence: list[ClaimEvidence] = Field(min_length=1, max_length=4)


class SubtopicFindings(BaseModel):
    claims: list[FindingClaim] = Field(default_factory=list, max_length=14)
    summary: str = Field(default="", max_length=1000)
    sufficient: bool = True  # is the evidence enough to answer this subtopic?
    missing: str = Field(default="", max_length=400)


class SectionDraft(BaseModel):
    body: str = Field(min_length=20, max_length=20000)


class ReviewIssue(BaseModel):
    section_key: str = Field(max_length=64)
    severity: Literal["minor", "major"]
    kind: Literal[
        "unsupported_claim", "missing_coverage", "contradiction_ignored", "unclear", "off_topic", "overstated"
    ]
    problem: str = Field(max_length=500)
    suggestion: str = Field(default="", max_length=500)


class CriticReview(BaseModel):
    issues: list[ReviewIssue] = Field(default_factory=list, max_length=14)
    assessment: str = Field(default="", max_length=800)


class PairVerdict(BaseModel):
    pair_id: str = Field(max_length=16)
    support: Literal["direct", "partial", "unrelated"]
    reason: str = Field(default="", max_length=300)


class ValidationBatch(BaseModel):
    verdicts: list[PairVerdict] = Field(max_length=12)


class CritiqueNote(BaseModel):
    severity: Literal["minor", "major"]
    problem: str = Field(max_length=500)
    suggestion: str = Field(default="", max_length=500)


class SectionCritique(BaseModel):
    """A critique of one section on request. Advice only: nothing in it changes the report."""

    assessment: str = Field(default="", max_length=800)
    notes: list[CritiqueNote] = Field(default_factory=list, max_length=10)


class DimensionOpinion(BaseModel):
    dimension: Literal["clarity_structure", "completeness", "question_coverage"]
    score: int = Field(ge=0, le=100)
    rationale: str = Field(default="", max_length=500)


class QualityOpinion(BaseModel):
    """An AI reader's rubric-based opinion. It is not a fact check and is stored as such."""

    opinions: list[DimensionOpinion] = Field(default_factory=list, max_length=3)
