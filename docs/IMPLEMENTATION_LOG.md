# Implementation Log

Progress tracker for the work specified in `CODEBASE_REVIEW.md` (see the
**Unified roadmap** section at the bottom of that file — it is the
authoritative work order), `ALERT_DEDUPLICATION_PROPOSAL.md`, and
`STATUS_PAGE_PROPOSAL.md`.

**Baseline:** commit `0ee3231` (working tree as audited) + `696e3d7` (docs).

## Instructions for implementing agents

- Read this file **first** in every session, before the spec docs. It tells
  you what is already done and what was learned along the way.
- Work only on the roadmap steps scoped in your session prompt.
- Update this file **before ending your session**: flip statuses in the
  roadmap table, add a session entry, and record any deviations or doc
  corrections. This log is the only memory shared between sessions —
  if it isn't written here, the next session doesn't know it.
- Statuses: `todo` → `in progress` → `done` (with commit hashes) or
  `blocked` (with a note on what input is needed).
- All design decisions in the spec docs are final; do not relitigate them.
  If a doc conflicts with code reality, stop, record the conflict under
  "Doc corrections," and ask the author.

## Roadmap status

| Step | Scope (see Unified roadmap for detail) | Status | Commits | Notes |
|---|---|---|---|---|
| 1 | H1 + H2: HTTP timeouts everywhere; exception-proof the scheduler loop | done | `bc55165` | Buckler request now uses `timeout=(10, 30)`; scheduler runs `do_task` through a guard and keeps looping after unexpected task/scheduler exceptions. |
| 2 | Replace `chump` with direct `requests`-based `PushoverClient` (resolves H4 root cause, L1, L15) | done | `a7e9ca4` | Direct `requests` client supports send, receipt check, cancel, and cancel-by-tag with timeouts and sanitized never-raise failure logging; `chump` removed from dependencies. |
| 3 | H5 (new-character handling), H3 (payload + season ID from config), M1 (atomic write + guarded read) | todo | — | |
| 4 | Test harness: pytest + pytest-cov dev group; untangle config singleton (L3); seed `do_task` tests; M4 (pydantic dep), M5 (dev-deps/metadata) | todo | — | |
| 5 | Dedup phase 1 per `ALERT_DEDUPLICATION_PROPOSAL.md` §12 (supersedes M2, retires M10) | todo | — | |
| 6 | M3 (auth-expiry incident), M6 (Accept-Encoding), M7 (log rotation), M8 (absolute paths + `data/` move per addendum), M9 cleanup + `.gitignore` | todo | — | |
| 7 | Dedup phase 2: Master-color swap incident, quota self-alert, sounds/url polish, README DND note | todo | — | |
| 8 | Status page per `STATUS_PAGE_PROPOSAL.md` | todo | — | |
| 9 | Remaining L items (opportunistic); Glances stays shelved | todo | — | |

## Session log

<!-- Newest first. Copy this template:

### YYYY-MM-DD — Session N: <scope, e.g. "roadmap steps 1–2">
- **Branch / commits:** <branch name>; <hash> <one-line>, <hash> <one-line>
- **Done:** <what was completed, mapped to finding IDs (H1, M8, ...)>
- **Verified by:** <tests run / manual demonstration performed — be specific>
- **Not done / carried over:** <anything in scope but unfinished, and why>
- **Decisions made in-session:** <small calls not covered by the docs>
-->

### 2026-06-12 — Session 1: roadmap steps 1–2
- **Branch / commits:** `codex/roadmap-steps-1-2`; `bc55165` H1/H2: add timeouts and harden scheduler, `a7e9ca4` H4/L1/L15: replace chump with PushoverClient
- **Done:** H1 Buckler timeout; H2 scheduler guard and never-raise notification send path; H4 root cause removed by dropping `chump`; L1/L15 direct `PushoverClient` added with `send`, `check_receipt`, `cancel`, and `cancel_by_tag`.
- **Verified by:** `uv run python -m black app.py api_service.py notifier_client.py`; `uv run python -m py_compile app.py api_service.py notifier_client.py task.py`; `uv run python -m mypy app.py api_service.py config.py model.py notifier_client.py task.py utilities.py` (still reports only the known baseline issues: missing `requests` stubs in `api_service.py`/`task.py`, missing `sortedcontainers` stubs, and `notifications_to_send` needing an annotation; no new errors, and the old `chump` import error is gone); short separate app invocation with Pushover sends and state writes disabled returned `APP_REAL_API_CHARACTER_ROWS=32` from the real Buckler API and did not spend Pushover quota; wrong-port `PushoverClient` demo returned `None`/`{}`/`False`/`False`, logged sanitized `ConnectTimeout` failures, and printed `PUSHOVER_FAILURE_DEMO_SURVIVED`; forced scheduler timeout demo logged `Scheduled monitor task failed; continuing.` and then printed `SCHEDULER_SURVIVED_AFTER_FORCED_POLL_FAILURE` on later ticks.
- **Not done / carried over:** Roadmap step 3+ items intentionally untouched, including H3, H5, M1, incident state, auth-expiry classification, log rotation, absolute paths/data directory, response dump cleanup, and test harness work.
- **Decisions made in-session:** Pushover request failures log endpoint path plus exception class only, not traceback/exception text, because receipt GET exceptions can include the app token query string.

## Deviations from the spec docs

*(none yet — record any place the implementation intentionally differs from
the docs, with rationale and author sign-off)*

## Doc corrections discovered

*(none yet — record any place a spec doc turned out to be wrong about the
code or an API, so the docs can be fixed)*
