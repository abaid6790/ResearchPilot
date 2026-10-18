"""Planner: turns the request into subtopics (stored as tasks) and a report outline."""

from __future__ import annotations

import re

from agents import prompts
from agents.context import AgentContext
from agents.schemas import FollowUpQueries, ResearchPlan
from extensions import db
from research.models import ResearchTask
from research_tools.untrusted import sanitize_text

MAX_SUBTOPICS = {"quick": 3, "standard": 5, "deep": 7, "comprehensive": 9}
MAX_OUTLINE = {"quick": 4, "standard": 6, "deep": 8, "comprehensive": 10}


def _clean_query(q: str) -> str:
    return " ".join(sanitize_text(q, 200).split())


def run_planner(ctx: AgentContext) -> None:
    job, project, st = ctx.job, ctx.project, ctx.settings
    ctx.set_stage("planning", "planner", "Planning the research.")
    prompt = (
        f"<user_request>\nTitle: {project.title}\nTopic: {project.topic}\n"
        f"Objectives: {project.objectives or '(none given)'}\n</user_request>\n"
        f"Audience: {st.audience}. Writing style: {st.writing_style}. Language of the report: {st.language}. "
        f"Depth: {st.depth}. Plan at most {MAX_SUBTOPICS[st.depth]} subtopics and "
        f"{MAX_OUTLINE[st.depth]} outline sections."
    )
    plan = ctx.llm("planner", prompts.PLANNER, prompt, ResearchPlan, max_output_tokens=3000)

    subtopics, seen = [], set()
    for sub in plan.subtopics:
        key = sub.title.strip().lower()
        if key in seen:
            continue
        seen.add(key)
        subtopics.append(sub)
    if len(subtopics) > MAX_SUBTOPICS[st.depth]:
        ctx.event("warning", "The plan had more subtopics than this depth allows; extra ones were dropped.",
                  level="warning", stage="planning", agent="planner")  # fmt: skip
        subtopics = subtopics[: MAX_SUBTOPICS[st.depth]]

    for position, sub in enumerate(subtopics):
        queries = list(dict.fromkeys(q for q in (_clean_query(x) for x in sub.search_queries) if len(q) >= 3))
        db.session.add(
            ResearchTask(
                job_id=job.id, kind="subtopic", agent="researcher", title=sub.title.strip()[:300],
                description="\n".join(sub.questions)[:2000], queries=queries or [sub.title[:150]],
                evidence_needed=sub.evidence_needed or None, position=position, round=1, status="pending",
            )
        )  # fmt: skip

    titles = {s.title.strip() for s in subtopics}
    outline, keys = [], set()
    for sec in plan.outline[: MAX_OUTLINE[st.depth]]:
        if sec.key in keys or re.fullmatch(r"references|limitations.*", sec.key):
            continue
        keys.add(sec.key)
        outline.append(
            {"key": sec.key, "heading": sec.heading.strip(), "purpose": sec.purpose,
             "subtopics": [t for t in sec.subtopic_titles if t.strip() in titles]}
        )  # fmt: skip
    if not outline:
        outline = [{"key": "findings", "heading": "Findings", "purpose": "", "subtopics": sorted(titles)}]
    job.checkpoint = {**(job.checkpoint or {}), "outline": outline, "objective": plan.objective,
                      "ambiguities": plan.ambiguities[:6], "gaps": {}}  # fmt: skip
    db.session.commit()
    ctx.event("plan_ready", f"Plan ready: {len(subtopics)} subtopics, {len(outline)} report sections.",
              stage="planning", agent="planner", data={"subtopics": [s.title for s in subtopics]})  # fmt: skip


def run_followups(ctx: AgentContext) -> bool:
    """Ask for new queries for subtopics whose evidence was insufficient. True if anything was queued."""
    from sqlalchemy import select

    job = ctx.job
    gaps = dict((job.checkpoint or {}).get("gaps") or {})
    queued = False
    for task in db.session.scalars(select(ResearchTask).where(ResearchTask.job_id == job.id)):
        missing = gaps.get(str(task.id))
        if not missing:
            continue
        prompt = (
            f"Subtopic: {task.title}\nQuestions: {task.description}\nWhat is missing: {missing}\n"
            f"Queries already tried: {task.queries}"
        )
        fresh = ctx.llm("planner", prompts.FOLLOWUP, prompt, FollowUpQueries, max_output_tokens=500)
        tried = {q.lower() for q in (task.queries or [])}
        new = [q for q in (_clean_query(x) for x in fresh.queries) if len(q) >= 3 and q.lower() not in tried]
        if not new:
            continue
        task.queries = [*(task.queries or []), *new]
        task.status, task.result_summary, task.round = "pending", None, task.round + 1
        queued = True
    job.checkpoint = {**(job.checkpoint or {}), "gaps": {}}
    db.session.commit()
    return queued
