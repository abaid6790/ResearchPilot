# Agents and the research workflow (Phase 6)

Code: `agents/`. The workflow is a LangGraph graph (`agents/graph.py`); `agents/runner.py` runs one
job to the end. Phase 7 will call `run_job` from a background worker; today you can run a job
yourself with `flask run-research <project id>`.

```
plan -> research -> analyze --(evidence thin, rounds left)--> follow-up queries -> research
                       \--> write -> critic <-> revise -> validate -> finalize
```

## What each step does

| Step | Uses a model? | What it does |
|---|---|---|
| Planner | yes | Subtopics (saved as tasks), search queries, report outline. Also proposes new queries when evidence is thin |
| Researcher | no | Runs Tavily searches, saves candidate sources (deduplicated, ranked, capped at `max_sources`), reads pages with the safe fetcher, stores verbatim excerpts. Uploaded documents become sources too |
| Analyst | yes | Extracts claims from numbered excerpts, each citing excerpt ids. Cites nothing that was not provided: unknown ids are dropped |
| Writer | yes | One call per section, using only the claims given. Cites `[@S<id>]` markers; markers to sources it was not given are removed |
| Critic | yes | Independent review of the whole draft against the claims. Major issues trigger a revision (up to `max_revision_rounds`) |
| Validator | partly | Code checks citations and quotations; a model judges each (claim, excerpt) pair as direct / partial / unrelated |
| Finalize | no | Adds the *Limitations* and *References* sections from database facts (never model text), sets the job status |

## Guarantees and their limits

* **No tools for models.** A model only receives text and returns JSON validated against a schema
  (`agents/schemas.py`). Fetching and searching are ordinary code with fixed rules.
* **Untrusted content** (pages, uploads) is wrapped as data with a random boundary and the system
  prompt says to ignore instructions inside it. Pages with instruction-like text are flagged. This
  reduces but cannot eliminate injection risk; see `docs/SECURITY.md`.
* **No invented sources.** Citations are checked in code against this job's sources. The references
  list is built from the database.
* **No invented quotations.** The writer is told to paraphrase; any quoted passage of five or more
  words that is not in a stored excerpt is flagged as an issue.
* **Reachable is not verified.** `link_status` only records that a URL answered. Support is stored per
  claim/excerpt link (`support_level`) and per claim (`support_status`), and the report says so.
* **Search snippets** are marked `snippet_only`; claims resting only on snippets get confidence
  capped at 0.6.
* **Honest outcomes.** A run ends `completed` only with no major issue. Otherwise it is
  `completed_with_warnings` (uncovered topics, unsupported or contradicted claims, unverified
  quotations, critic or validator unavailable, unresolved critic issues) and the report is not
  marked export-ready. Issues are stored on the job and listed in the report.
* **Failure and resume.** Provider outage, missing search key or an exhausted call budget end the job
  as `failed` with a clear reason; completed work stays in the database. Re-running the same job
  continues from where it stopped (`flask run-research <id> --resume`): the graph inspects the
  database to choose its entry point, so it does not re-plan or re-search.
* **Bounds.** At most `AGENT_MAX_LLM_CALLS` (default 120) model calls per job, `max_research_rounds`
  search rounds, `max_revision_rounds` revisions, 4 parallel page fetches, and a graph recursion limit.
* **Cancellation** is checked between steps and before every model call.
* **User edits are never overwritten** by the revision step (`edited_by_user` sections are skipped).

## Running it

```powershell
flask llm-ping            # at least one LLM key works
flask search-ping         # Tavily key works
flask run-research <project id> --show
```

The project id is the last part of the project page's URL. This spends API quota.

## Verification status

Tested with a scripted LLM, a fake search client and a local fake website (`tests/test_agents.py`):
full run, source cap, injection flagging, fabricated evidence/citations/quotations, unsupported
claims, validator and critic outages, revision loop and limits, follow-up research and its bound,
resume after an outage, cancel, call budget, missing search key, uploads-only run, crash handling.

**Not verified:** behaviour with real models. Real models will sometimes return weaker plans,
miss claims, or cite poorly; the checks above catch structural problems, not subtle misreadings.
Prompt wording (`agents/prompts.py`) is untuned. Tune it after seeing real runs.

Not built yet: the quality score rubric and exports (Phase 10), report editor (Phase 9), and the
web button, live progress and background execution (Phase 7). The numeric citation style is a
display concern: text stores `[@S<id>]` markers and later phases render them as numbers or
APA/MLA/Chicago.
