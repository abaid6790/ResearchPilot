"""The LangGraph workflow.

plan -> research -> analyze -> (follow-up research while evidence is thin and rounds remain)
     -> write -> critic <-> revise -> validate -> finalize

State between nodes is tiny; everything else lives in the database, so a stopped job can resume
from where it was (``route_start`` inspects the database to choose the entry point).
"""

from __future__ import annotations

from typing import TypedDict

from langgraph.graph import END, START, StateGraph
from sqlalchemy import func, select

from agents.analyst import run_analysis
from agents.context import AgentContext, AgentFailure
from agents.planner import run_followups, run_planner
from agents.researcher import run_research
from agents.review import current_report, major_issues, run_critic, run_revision
from agents.validator import finalize, run_validation
from agents.writer import run_writer
from extensions import db
from reports.models import ReportDocument
from research.models import ResearchSource, ResearchTask

SOFT_FAILURES = {"llm_unavailable", "budget_exceeded", "llm_bad_output", "llm_rejected"}


class WorkflowState(TypedDict, total=False):
    queued: bool
    issues: list


def build_graph(ctx: AgentContext):
    def has_report() -> bool:
        return (
            db.session.scalar(
                select(func.count()).select_from(ReportDocument).where(ReportDocument.job_id == ctx.job_id)
            )
            > 0
        )

    def route_start(_state) -> str:
        job = ctx.job
        tasks = list(db.session.scalars(select(ResearchTask).where(ResearchTask.job_id == job.id)))
        if not tasks:
            return "plan"
        if any(t.status in ("pending", "running") for t in tasks):
            return "research"
        if any(t.status == "done" and t.result_summary is None for t in tasks):
            return "analyze"
        if not has_report():
            return "write"
        if ctx.settings.enable_critic and "review" not in (job.checkpoint or {}):
            return "critic"
        return "revise" if needs_revision() else "validate"

    def needs_revision() -> bool:
        return (
            ctx.settings.enable_critic
            and bool(major_issues(ctx.job))
            and ctx.job.revision_round < ctx.settings.max_revision_rounds
        )

    def after_analyze(_state) -> str:
        job, st = ctx.job, ctx.settings
        gaps = (job.checkpoint or {}).get("gaps") or {}
        have = db.session.scalar(
            select(func.count()).select_from(ResearchSource).where(ResearchSource.job_id == job.id)
        )
        if (
            gaps
            and st.enable_additional_research
            and job.research_round < st.max_research_rounds
            and have < st.max_sources
        ):
            return "followups"
        return "write"

    def followups(_state) -> dict:
        return {"queued": run_followups(ctx)}

    def after_followups(state) -> str:
        return "research" if state.get("queued") else "write"

    def critic(_state) -> dict:
        try:
            run_critic(ctx)
        except AgentFailure as exc:
            if exc.code not in SOFT_FAILURES:
                raise
            ctx.event("warning", "The critic review could not run; the draft is marked as not reviewed.", level="warning",
                      stage="reviewing", agent="critic", data={"code": exc.code})  # fmt: skip
            ctx.job.checkpoint = {**(ctx.job.checkpoint or {}), "review": {"issues": [], "unavailable": True}}
            db.session.commit()
        return {}

    def after_write(_state) -> str:
        return "critic" if ctx.settings.enable_critic else "validate"

    def after_critic(_state) -> str:
        return "revise" if needs_revision() else "validate"

    def validate(_state) -> dict:
        return {"issues": run_validation(ctx)}

    def wrap(fn):
        def node(state):
            fn(ctx)
            return {}

        return node

    g = StateGraph(WorkflowState)
    g.add_node("plan", wrap(run_planner))
    g.add_node("research", wrap(run_research))
    g.add_node("analyze", wrap(run_analysis))
    g.add_node("followups", followups)
    g.add_node("write", wrap(run_writer))
    g.add_node("critic", critic)
    g.add_node("revise", wrap(run_revision))
    g.add_node("validate", validate)
    g.add_node("finalize", lambda state: (finalize(ctx, state.get("issues") or []), {})[1])

    g.add_conditional_edges(
        START, route_start, ["plan", "research", "analyze", "write", "critic", "revise", "validate"]
    )
    g.add_edge("plan", "research")
    g.add_edge("research", "analyze")
    g.add_conditional_edges("analyze", after_analyze, ["followups", "write"])
    g.add_conditional_edges("followups", after_followups, ["research", "write"])
    g.add_conditional_edges("write", after_write, ["critic", "validate"])
    g.add_conditional_edges("critic", after_critic, ["revise", "validate"])
    g.add_edge("revise", "critic")
    g.add_edge("validate", "finalize")
    g.add_edge("finalize", END)
    return g.compile()


__all__ = ["build_graph", "current_report"]
