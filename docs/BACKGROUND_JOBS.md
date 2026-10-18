# Background jobs, live progress and the report page (Phase 7)

## Starting a run

The project page has **Start research**. It refuses (with the reason) when no AI provider key or no
search key is configured, when the project is archived, when the project already has a run, or when you
already have `MAX_ACTIVE_JOBS_PER_USER` (default 2) runs in progress. A failed run that stopped for a
temporary reason (provider outage, missing key, call budget, internal error) offers **Resume from saved
progress**; nothing already searched or written is repeated. **Stop this run** asks the run to stop at its
next step; a run no worker has picked up yet is cancelled immediately.

## Runners (`JOB_RUNNER`)

| Value | What it is | Use it for |
|---|---|---|
| `thread` (default) | A small thread pool (`JOB_WORKERS`, default 2) inside the web process | One user, development. Jobs stop if the process restarts; run `flask recover-jobs` to continue them |
| `celery` | A separate Celery worker with Redis as the broker | Production. Survives web restarts; a job whose worker dies is picked up again |
| `manual` | Nothing starts by itself | Tests, and running jobs yourself with `flask run-research` |

Celery:

```powershell
# Redis must be running and REDIS_URL set; then in a second terminal:
$env:JOB_RUNNER = "celery"          # for the web process too
celery -A jobs.worker.celery_app worker --loglevel=info --concurrency=2 --pool=threads   # Windows
celery -A jobs.worker.celery_app worker --loglevel=info --concurrency=2                  # Linux/macOS
```

Results are stored in the database, not in Redis. Tasks are acknowledged after they finish and rejected
if the worker is lost, so Redis re-delivers them.

## Never running a job twice

Every runner calls `jobs.run_by_public_id`, which first *claims* the job with one atomic database
update. A job is claimable if nobody has run it, or if its `heartbeat_at` is older than
`JOB_STALE_AFTER_S` (default 300 s). The run refreshes the heartbeat at least every 10 seconds while it
works (and around each AI call), so a dead worker's job becomes claimable again and **resumes** from the
saved state. `flask recover-jobs` re-queues such jobs; it is safe to run from cron or at startup.

## Live progress

The page shows the stage, a step indicator and a log. It uses Server-Sent Events
(`/research/<project>/jobs/<job>/stream`) and falls back to polling
(`.../events?after=<seq>`) if the stream fails. Each connection lasts at most `JOB_SSE_MAX_SECONDS`
(default 60) and the browser reconnects with `Last-Event-ID`, so no event is lost and no web worker is held
for long. The page works without JavaScript (it shows the last events and reloads show the result). All
text from the server is inserted with `textContent`.

**Server setting:** a streaming connection occupies a worker while open. With Gunicorn use threads, for
example `gunicorn --worker-class gthread --threads 8 --workers 2 app:app`; plain sync workers will be
blocked by open streams. Behind Nginx keep buffering off for this path (the response sends
`X-Accel-Buffering: no`).

## The report page

`/research/<project>/report` shows the latest report read-only (editing is Phase 9). Section text is
Markdown from a model, so it is sanitised with an allow-list *before* citation markers are turned into
numbered links; the markup can therefore not smuggle in scripts, images or links. Markers that point to
sources not in the report are dropped. References come from the database and show the URL, dates, whether
the page was read in full or only a search snippet was available, and any warning on the source. A banner
says plainly when the automatic checks found problems.

## Verification status

Automated tests (`tests/test_jobs.py`): start rules, ownership, caps, claiming and stale takeover, thread
and Celery dispatch (the Celery task is executed through Celery's own machinery), cancel, resume, events
and SSE (including `Last-Event-ID` and the connection time limit), CSP-safe markup, the report page and its
sanitising, recovery command.

Also checked by hand: the real thread runner with the real web server, a real browser watching the progress
update live and the report open (no console errors, no horizontal scroll at 390 px), and a real Redis
server with a real Celery worker process picking up a job sent by the web process.

**Not verified:** a long run with real AI providers and real websites; many concurrent users; Redis
outages during a run; Windows. In `thread` mode a restart kills running jobs (they resume with
`flask recover-jobs`, not automatically).
