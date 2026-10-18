"""Shared run context: database access, LLM calls with a budget, progress events, cancellation."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

from pydantic import BaseModel

from core.db_utils import utcnow
from extensions import db
from providers.base import LLMRequest, LLMRequestError, LLMUnavailableError, NoProviderConfiguredError
from research import constants as c
from research.models import ResearchJob, ResearchProject
from research.schemas import ResearchSettings
from research.services import append_event

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class AgentFailure(Exception):
    """The run cannot continue. ``code`` is machine readable, ``message`` is safe to show."""

    def __init__(self, code: str, message: str, retry_at: float | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_at = retry_at


class Cancelled(Exception):
    """The user asked the job to stop."""


@dataclass
class AgentContext:
    job_id: int
    router: Any  # anything with generate(LLMRequest) -> LLMResponse
    search: Any  # TavilyClient-like
    fetcher_factory: Callable[[], Any]
    max_llm_calls: int = 120
    fetch_workers: int = 4
    llm_calls: int = field(default=0)
    llm_failures: int = field(default=0)

    # ---------------------------------------------------------------- data access
    @property
    def job(self) -> ResearchJob:
        return db.session.get(ResearchJob, self.job_id)

    @property
    def project(self) -> ResearchProject:
        return self.job.project

    @property
    def settings(self) -> ResearchSettings:
        return ResearchSettings.model_validate(self.job.config_snapshot)

    # ---------------------------------------------------------------- progress
    def event(self, event_type: str, message: str, *, level: str = "info", stage: str | None = None,
              agent: str | None = None, data: dict | None = None) -> None:  # fmt: skip
        append_event(self.job, event_type, message, level=level, stage=stage, agent=agent, data=data)
        db.session.commit()

    def set_stage(self, status: str, agent: str, message: str) -> None:
        job = self.job
        job.status = status
        job.current_stage = status
        job.current_agent = agent
        job.checkpoint = {**(job.checkpoint or {}), "stage": status, "research_round": job.research_round,
                          "revision_round": job.revision_round}  # fmt: skip
        append_event(job, "stage_changed", message, stage=status, agent=agent)
        db.session.commit()

    def check_cancel(self) -> None:
        db.session.expire(self.job, ["cancel_requested_at"])
        if self.job.cancel_requested_at is not None:
            raise Cancelled

    # ---------------------------------------------------------------- LLM
    def llm(self, task: str, system: str, prompt: str, model: type[T], *, max_output_tokens: int = 4096) -> T:
        """One structured model call. Never returns unvalidated output."""
        self.check_cancel()
        if self.llm_calls >= self.max_llm_calls:
            raise AgentFailure(
                "budget_exceeded",
                "This run reached its limit on AI calls and stopped. Partial results are kept.",
            )
        self.llm_calls += 1
        request = LLMRequest.simple(
            prompt, system=system, task=task, response_model=model, max_output_tokens=max_output_tokens,
            job_id=self.job_id, user_id=self.project.user_id,
        )  # fmt: skip
        try:
            response = self.router.generate(request)
        except NoProviderConfiguredError as exc:
            raise AgentFailure(
                "llm_not_configured",
                "No AI provider keys are configured. Add GEMINI_API_KEY_1, GROQ_API_KEY_1 or OPENROUTER_API_KEY_1.",
            ) from exc
        except LLMUnavailableError as exc:
            self.llm_failures += 1
            raise AgentFailure(
                "llm_unavailable",
                "No AI provider could answer right now (rate limits, quota or an outage). "
                "Your progress is saved; try again later.",
                retry_at=getattr(exc, "retry_at", None),
            ) from exc
        except LLMRequestError as exc:
            raise AgentFailure("llm_rejected", "The AI provider rejected a request from this run.") from exc
        parsed = response.parsed
        if not isinstance(parsed, model):
            raise AgentFailure("llm_bad_output", "The AI provider returned output that could not be used.")
        return parsed

    def finish(self, status: str, *, error_code: str | None = None, error_message: str | None = None,
               issues: list | None = None) -> None:  # fmt: skip
        job = self.job
        job.status = status
        job.current_stage = status
        job.finished_at = utcnow()
        job.error_code = error_code
        job.error_message = error_message
        job.unresolved_issues = issues or None
        assert status in c.JOB_FINISHED_STATUSES
