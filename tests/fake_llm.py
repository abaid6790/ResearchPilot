"""A scripted stand-in for the LLM router, and a fake search client, for workflow tests."""

from __future__ import annotations

import re

from agents.schemas import (
    ClaimEvidence,
    CriticReview,
    FindingClaim,
    FollowUpQueries,
    OutlineSection,
    PairVerdict,
    ResearchPlan,
    ReviewIssue,
    SectionDraft,
    Subtopic,
    SubtopicFindings,
    ValidationBatch,
)  # fmt: skip
from providers.base import AttemptFailure, ErrorKind, LLMResponse, LLMUnavailableError, Usage
from research_tools.search import SearchHit, SearchResponse, SearchUnavailableError


def respond(model, parsed) -> LLMResponse:
    return LLMResponse(text="{}", provider="fake", model="fake", key_label="fake#1", usage=Usage(), latency_s=0.0,
                       attempts=1, request_id="r", parsed=parsed)  # fmt: skip


class FakeLLM:
    """Well-behaved by default; override behaviour per task with ``overrides[task] = callable(request, self)``."""

    def __init__(self, **overrides):
        self.calls: list[tuple[str, str, str]] = []  # (task, system, prompt)
        self.overrides = overrides
        self.fail_tasks: set[str] = set()
        self.sufficient_after = 0  # analyst reports "insufficient" this many times first
        self._analyses = 0

    def count(self, task: str) -> int:
        return sum(1 for t, _, _ in self.calls if t == task)

    def generate(self, request):
        task = request.task
        prompt = request.messages[0].content
        self.calls.append((task, request.system or "", prompt))
        if task in self.fail_tasks:
            raise LLMUnavailableError(
                "all providers down", [AttemptFailure("fake", None, None, ErrorKind.RATE_LIMIT, 429, "x")]
            )
        if task in self.overrides:
            return respond(request.response_model, self.overrides[task](request, self))
        return respond(request.response_model, getattr(self, f"_{task}")(request, prompt))

    # ----------------------------------------------------------------- default behaviours
    def _planner(self, request, prompt):
        if request.response_model is FollowUpQueries:
            return FollowUpQueries(queries=["solar efficiency temperature coefficient study"])
        return ResearchPlan(
            objective="Understand solar panel efficiency",
            subtopics=[
                Subtopic(title="Efficiency today", questions=["How efficient are panels?"], search_queries=["solar panel efficiency"]),
                Subtopic(title="Temperature effects", questions=["How does heat matter?"], search_queries=["solar temperature effect"]),
            ],
            outline=[
                OutlineSection(key="overview", heading="Overview", purpose="Summary", subtopic_titles=["Efficiency today"]),
                OutlineSection(key="temperature", heading="Temperature effects", subtopic_titles=["Temperature effects"]),
            ],
            ambiguities=["Which region is meant?"],
        )  # fmt: skip

    def _analyst(self, request, prompt):
        ids = re.findall(r"\[E(\d+)\]", prompt)
        self._analyses += 1
        if not ids:
            return SubtopicFindings(claims=[], sufficient=False, missing="nothing")
        sufficient = self._analyses > self.sufficient_after
        return SubtopicFindings(
            claims=[
                FindingClaim(text="Modern panels convert about 22 percent of sunlight into electricity.", claim_type="fact",
                             confidence=0.9, evidence=[ClaimEvidence(excerpt_id=f"E{ids[0]}")]),
                FindingClaim(text="Efficiency depends on temperature and on the materials used.", claim_type="fact",
                             confidence=0.8, evidence=[ClaimEvidence(excerpt_id=f"E{ids[-1]}")]),
            ],
            summary="Summary of findings.", sufficient=sufficient, missing="" if sufficient else "more detail",
        )  # fmt: skip

    def _writer(self, request, prompt):
        sources = sorted(set(re.findall(r"S(\d+) = ", prompt)), key=int)
        cite = "".join(f"[@S{s}]" for s in sources[:2])
        return SectionDraft(body=f"Modern panels convert about 22 percent of sunlight into electricity {cite}. " + "More detail follows here. " * 4 + f"{cite}")  # fmt: skip

    def _critic(self, request, prompt):
        return CriticReview(issues=[], assessment="No problems found.")

    def _validator(self, request, prompt):
        return ValidationBatch(verdicts=[PairVerdict(pair_id=f"P{i}", support="direct", reason="states it") for i in re.findall(r"\[P(\d+)\]", prompt)])  # fmt: skip


def major_issue(section_key: str):
    return ReviewIssue(section_key=section_key, severity="major", kind="unsupported_claim", problem="Sentence goes beyond the claims.", suggestion="Remove it.")  # fmt: skip


class FakeSearch:
    def __init__(self, urls: list[str], *, error: SearchUnavailableError | None = None):
        self.urls = urls
        self.error = error
        self.queries: list[str] = []

    def search(self, query, **kwargs):
        self.queries.append(query)
        if self.error:
            raise self.error
        start = (len(self.queries) - 1) * 2 % max(1, len(self.urls))
        chosen = self.urls[start : start + 2] or self.urls  # each query finds different pages
        hits = [SearchHit(f"Solar panel efficiency {i}", url, "Solar panels convert sunlight; efficiency about 22 percent.", 0.8 - i / 100)
                for i, url in enumerate(chosen)]  # fmt: skip
        return SearchResponse(query, hits)
