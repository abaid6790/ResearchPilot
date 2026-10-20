# HTTP routes

ResearchPilot AI is a server-rendered application. Almost every route returns HTML; a few return JSON
or a stream. There is no public API token mechanism: routes authenticate with the session cookie
(`rp_session`) and every state-changing request needs the CSRF token (form field `csrf_token`, or the
`X-CSRFToken` header for scripted requests). Generated from `flask --app app routes` and the route
decorators. "Verified" means a signed-in user with a verified email.

Conventions: `<public_id>` is the project's random 32-character id; unknown and foreign ids both give
404. Rate limits are per client IP (`RATELIMIT_STORAGE_URI`; use Redis in production). Limits shown are
the ones in the code.

## Public
| Method | Path | Notes |
|---|---|---|
| GET | `/` | Landing page; signed-in users are redirected to the dashboard |
| GET | `/health` | JSON `{"status","checks":{"app","database"}}`; 200 when ok, 503 when degraded; not rate limited; no secrets |
| GET | `/legal/`, `/legal/<slug>` | Policies: privacy, cookies, terms, acceptable-use, ai-usage, retention, security, contact |

## Authentication (`/auth`)
| Method | Path | Limit | Notes |
|---|---|---|---|
| GET, POST | `/auth/register` | 5/hour (POST) | Same response whether or not the email exists |
| GET, POST | `/auth/login` | 10/min, 60/hour (POST) | `next` must be a same-site path |
| POST | `/auth/logout` | | Login required |
| GET | `/auth/verify-pending` | | Login required |
| POST | `/auth/resend-verification` | 5/hour | Login required |
| GET, POST | `/auth/verify/<token>` | 20/hour (POST) | GET shows a button; token consumed on POST |
| GET, POST | `/auth/forgot-password` | 5/hour (POST) | Same response for unknown emails |
| GET, POST | `/auth/reset/<token>` | 10/hour (POST) | |

## Dashboard and projects
| Method | Path | Limit | Notes |
|---|---|---|---|
| GET | `/dashboard` | | Verified; counts and activity come from the database |
| GET | `/research/` | | Project list; query parameters for search, status, paging |
| GET, POST | `/research/new` | 30/hour (POST) | Create a project (uploads via multipart) |
| GET | `/research/<public_id>` | | Project page |
| POST | `/research/<public_id>/archive` | | |
| POST | `/research/<public_id>/delete` | 30/hour | Requires typing the title |
| GET | `/research/reports` | | Saved reports, searchable, paged |
| GET | `/research/history` | | Run history |

## Runs and progress
| Method | Path | Limit | Notes |
|---|---|---|---|
| POST | `/research/<public_id>/start` | 10/hour | Refuses with a reason if keys missing, archived, already running, or user at `MAX_ACTIVE_JOBS_PER_USER` |
| POST | `/research/<public_id>/jobs/<job_id>/cancel` | | |
| POST | `/research/<public_id>/jobs/<job_id>/resume` | 10/hour | Only for resumable failures |
| GET | `/research/<public_id>/jobs/<job_id>/events?after=<seq>` | exempt | JSON: job summary and events after `seq` |
| GET | `/research/<public_id>/jobs/<job_id>/stream` | exempt | Server-Sent Events: `event: summary` and progress events; honours `Last-Event-ID`; closes after `JOB_SSE_MAX_SECONDS` so the browser reconnects. Needs threaded workers ([DEPLOYMENT.md](DEPLOYMENT.md)) |

## Reports, sources, editor, quality, export
| Method | Path | Limit | Notes |
|---|---|---|---|
| GET | `/research/<public_id>/report` | | Read-only report with numbered citations |
| GET | `/research/<public_id>/sources`, `/sources/<id>` | | Source explorer and detail |
| GET | `/research/<public_id>/report/edit` | | Editor; paused while a run is active |
| POST | `.../report/edit/<key>/save` | 240/hour | Optimistic lock (`version`); 409 if stale; JSON when requested by the script |
| POST | `.../report/edit/<key>/regenerate` | 20/hour | Stores a *proposed* revision |
| POST | `.../report/edit/<key>/critique` | 20/hour | |
| POST | `.../report/revisions/<revision_id>/<action>` | 120/hour | Apply, discard or restore |
| GET | `.../report/text` | | Plain-text view |
| GET | `.../report/quality` | | Scores and gates |
| POST | `.../report/quality/run` | 30/hour | Deterministic checks |
| POST | `.../report/quality/assess` | 10/hour | Optional model opinion; never affects gates |
| GET | `.../report/export/<fmt>` | 60/hour | `fmt` is `md`, `html`, `docx` or `pdf` |

## Settings (`/settings`)
| Method | Path | Limit | Notes |
|---|---|---|---|
| GET, POST | `/settings/profile` | | |
| POST | `/settings/password` | 10/hour | Invalidates other sessions |
| GET, POST | `/settings/preferences` | | Theme, finish notifications |
| GET, POST | `/settings/defaults`; POST `/settings/defaults/reset` | | Prefill for the new-project form |
| GET | `/settings/privacy` | | |
| POST | `/settings/privacy/export` | 5/day | ZIP download |
| POST | `/settings/privacy/delete` | 5/hour | Password + typed phrase; starts the grace period |
| POST | `/settings/privacy/cancel-deletion` | | |

## Operations
| Method | Path | Notes |
|---|---|---|
| GET | `/health/providers` | Login required; 30/minute; provider and key-health summary, never key values |

## Errors
HTML error pages for 403, 404, 429 and 500; JSON bodies where the caller asked for JSON. A 429 means a
rate limit; a 409 on save means the section changed since it was loaded.

## Command-line interface
`flask --app app <command>`: `db` (migrations), `llm-status`, `llm-ping`, `llm-reset`, `search-ping`,
`fetch-check <url>`, `run-research <project id>`, `show-report <job id>`, `recover-jobs`,
`purge-accounts`, `limiter`.
