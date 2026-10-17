"""Validated shape of a project's settings (spec section 8). Stored in ``research_projects.settings``.

Defaults and limits live here so the form, the API and the workflow agree. Settings that raise
run time or API usage are listed in ``COSTLY_SETTINGS`` so the UI can say so.
"""

from __future__ import annotations

import re
from datetime import date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

Depth = Literal["quick", "standard", "deep", "comprehensive"]

# depth -> (max_sources, max_research_rounds, max_revision_rounds)
DEPTH_PRESETS: dict[str, tuple[int, int, int]] = {
    "quick": (8, 1, 1),
    "standard": (15, 2, 2),
    "deep": (30, 3, 2),
    "comprehensive": (50, 4, 3),
}
HARD_LIMITS = {"max_sources": 100, "max_research_rounds": 5, "max_revision_rounds": 5}
COSTLY_SETTINGS = ("depth", "max_sources", "max_research_rounds", "max_revision_rounds", "enable_critic",
                   "enable_additional_research", "output_length")  # fmt: skip

_DOMAIN = re.compile(r"^(?=.{1,253}$)([a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")


def normalise_domain(value: str) -> str:
    domain = value.strip().lower()
    domain = re.sub(r"^https?://", "", domain).split("/")[0].removeprefix("www.")
    if not _DOMAIN.match(domain):
        raise ValueError(f"{value!r} is not a valid domain name")
    return domain


class ResearchSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    depth: Depth = "standard"
    output_length: Literal["short", "medium", "long", "very_long"] = "medium"
    audience: str = Field(default="general", max_length=100)
    language: str = Field(default="en", pattern=r"^[a-z]{2,3}(-[A-Za-z]{2,4})?$")
    writing_style: str = Field(default="neutral", max_length=100)
    source_categories: list[str] = Field(default_factory=list, max_length=10)
    prioritize_domains: list[str] = Field(default_factory=list, max_length=25)
    exclude_domains: list[str] = Field(default_factory=list, max_length=50)
    date_from: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    date_to: str | None = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    max_sources: int | None = Field(default=None, ge=3, le=HARD_LIMITS["max_sources"])
    max_research_rounds: int | None = Field(default=None, ge=1, le=HARD_LIMITS["max_research_rounds"])
    max_revision_rounds: int | None = Field(default=None, ge=0, le=HARD_LIMITS["max_revision_rounds"])
    output_format: Literal["pdf", "docx", "markdown", "html"] = "pdf"
    citation_style: Literal["numeric", "apa", "mla", "chicago"] = "numeric"
    enable_critic: bool = True
    enable_additional_research: bool = True

    @field_validator("date_from", "date_to")
    @classmethod
    def _real_date(cls, value: str | None) -> str | None:
        if value is not None:
            date.fromisoformat(value)  # rejects 2026-13-45
        return value

    @field_validator("source_categories")
    @classmethod
    def _categories(cls, values: list[str]) -> list[str]:
        from research.constants import SOURCE_CATEGORIES

        unknown = [v for v in values if v not in SOURCE_CATEGORIES]
        if unknown:
            raise ValueError(f"unknown source categories: {', '.join(unknown)}")
        return list(dict.fromkeys(values))

    @field_validator("prioritize_domains", "exclude_domains")
    @classmethod
    def _domains(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(normalise_domain(v) for v in values))

    @model_validator(mode="after")
    def _consistent(self) -> ResearchSettings:
        overlap = set(self.prioritize_domains) & set(self.exclude_domains)
        if overlap:
            raise ValueError(f"domains cannot be both prioritised and excluded: {', '.join(sorted(overlap))}")
        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("date_from must not be after date_to")
        sources, research, revision = DEPTH_PRESETS[self.depth]
        self.max_sources = self.max_sources or sources
        self.max_research_rounds = self.max_research_rounds or research
        self.max_revision_rounds = (
            self.max_revision_rounds if self.max_revision_rounds is not None else revision
        )
        return self
