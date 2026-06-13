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
| 3 | H5 (new-character handling), H3 (payload + season ID from config), M1 (atomic write + guarded read) | done | `8c4ec2d` | New characters now count as a diff and are persisted; Buckler payload uses `config.user_code` + `config.target_season_id`; database writes use temp-file + `os.replace`, and corrupt reads recover as first init. |
| 4 | Test harness: pytest + pytest-cov dev group; untangle config singleton (L3); seed `do_task` tests; M4 (pydantic dep), M5 (dev-deps/metadata) | done | `a9c41b1` | Pytest/coverage dev harness added; import-time config load removed; `pydantic` is direct runtime dependency; `mypy`/`datamodel-code-generator`, `pytest`, `pytest-cov`, and `types-requests` are dev deps; the obsolete `requests` import ignore was removed. |
| 5 | Dedup phase 1 per `ALERT_DEDUPLICATION_PROPOSAL.md` §12 (supersedes M2, retires M10) | done | `bf8916c`, `ba94c14`, `ca48009`, `5c40ca9` | `IncidentManager` state machine: `stuck_farm` (emergency, with re-raise/re-arm) + `api_down` (one-shot priority=1, courtesy recovery); `notification_state.json` with `last_change_at` (retires M10); tags + startup `cancel_by_tag`; quota-header logging. State file lives in the repo root this session — step 6 relocates it (and `database.json`) to `data/`. The `send`-returns-`None`-for-priority<2 note is handled: only the emergency path relies on the receipt return; `api_down` ignores it. |
| 6 | M3 (auth-expiry incident), M6 (Accept-Encoding), M7 (log rotation), M8 (absolute paths + `data/` move per addendum), M9 cleanup + `.gitignore` | todo | — | |
| 7 | Dedup phase 2: Master-color swap incident, quota self-alert, sounds/url polish, README DND note | todo | — | |
| 8 | Status page per `STATUS_PAGE_PROPOSAL.md` | todo | — | |
| 9 | Remaining L items (opportunistic); Glances stays shelved | todo | — | From PR #1 review: `REQUEST_TIMEOUT = (10, 30)` is duplicated in `api_service.py:12` and `notifier_client.py:12`; consolidate if a shared constants home appears. |

## Session log

<!-- Newest first. Copy this template:

### YYYY-MM-DD — Session N: <scope, e.g. "roadmap steps 1–2">
- **Branch / commits:** <branch name>; <hash> <one-line>, <hash> <one-line>
- **Done:** <what was completed, mapped to finding IDs (H1, M8, ...)>
- **Verified by:** <tests run / manual demonstration performed — be specific>
- **Not done / carried over:** <anything in scope but unfinished, and why>
- **Decisions made in-session:** <small calls not covered by the docs>
-->

### 2026-06-12 — Session 3: roadmap step 5 (alert dedup phase 1)
- **Branch / commits:** `dedup-phase-1`; `bf8916c` add emergency-alert config knobs, `ba94c14` log Pushover quota remaining, `ca48009` incident state machine (stuck_farm + api_down), `5c40ca9` route do_task through the IncidentManager, `af3a83b` drive-by: remove dead `get_duration_since_file_modified`
- **Done:** New `incident_manager.py` with `IncidentManager` implementing the §2 edge-triggered state machine and owning `notification_state.json` (atomic write, in the repo root for now). `stuck_farm`: emergency priority (`retry`/`expire`/`tags` from config); one alert on CLOSED→OPEN; silent while OPEN; closes **and cancels the receipt** only on observed recovery; ack records `acked_at` but never closes; §4 re-raise on un-acked local expiry (`opened_at + expire`); §4 re-arm 600 s after an ack that isn't followed by recovery. `api_down`: one-shot `priority=1`; closes on the first successful poll with a "recovered after X" courtesy message. `last_change_at` in the state file replaces the `database.json` mtime check (retires M10); stuck condition is `seconds_since_last_change() >= battle_count_timeout`. Startup reconciliation: `cancel_by_tag(sf6mon-stuck_farm)` when the state file is missing/corrupt, then a clean rebuild; failed cancels parked in `pending_cancel` and retried each poll; receipt-check failures treated as "no new information" (stay OPEN, silent). `PushoverClient` now logs `X-Limit-App-Remaining` on each request (quota self-alert itself is step 7). New config keys `emergency_retry`/`emergency_expire`/`re_alert_after_ack` (defaults 120/10800/600) added to `ConfigData`, `example.toml`, README (with the §5 DND deployment note) and the gitignored `config.toml` (not committed). `do_task` now computes `api_down`/`stuck` booleans and hands them to the manager instead of calling `send_message`; the "Finished Master color" ≥100 messages keep their current repeat-on-every-change `send_message` behavior (conversion is step 7).
- **Verified by:** `uv run python -m black …` (clean) and `uv run python -m mypy app.py api_service.py config.py model.py notifier_client.py task.py utilities.py incident_manager.py tests` (`Success: no issues found in 12 source files`); `uv run python -m pytest --basetemp .pytest_cache\tmp` → **26 passed**. New `tests/test_incident_manager.py` (18 tests, fake clock + fake no-network Pushover client) covers, at minimum: open-on-stuck sends exactly one emergency message; staleness while OPEN sends nothing; receipt-check failure stays OPEN+silent; observed recovery closes **and cancels the receipt**; ack alone does NOT close (records `acked_at`, no send); re-arm fires at 600 s after ack (and is disabled at `re_alert_after_ack = 0`); re-raise fires on un-acked local expiry; open-send failure records no incident; failed cancel is retried next poll; api_down sends one message then recovers with the courtesy line; corrupt/missing state file triggers `cancel_by_tag` and a clean rebuild; a valid file does not reconcile; an open incident survives a reload; disabled Pushover still drives transitions with no network; `record_change` resets the stuck timer. `tests/conftest.py` holds the shared `FakeClock`/`FakePushoverClient`/`make_config`. Live-ish demo (`ignore/demo_dedup.py`, `pushover_enabled = false`, `battle_count_timeout = 2`, fake clock) prints the `notification_state.json` transitions and log lines for stuck **open → silent → recover** plus api_down **open → recover (courtesy)** — output matches the spec.
- **Not done / carried over:** Later roadmap steps intentionally untouched (step 6: M3 auth-expiry incident, M6–M9, the `data/` move that relocates `notification_state.json`; step 7: Master-color swap incident, quota self-alert, sounds/url polish; step 8 status page; step 9 L items). The `"Random"` row filter (review open-question 5) was **not** added — it is not in this session's scope. `sort_database_by_value` left dead in `utilities.py` (its removal is tracked under the status-page L9 cleanup).
- **Decisions made in-session:**
  - **Two `evaluate_*` methods instead of one `evaluate(type, …)`.** The §8 sketch shows a single `evaluate(type, condition_is_active, build_message)`; I split it into `evaluate_stuck_farm(active, build_message)` and `evaluate_api_down(active, down_message)` because the emergency and one-shot policies genuinely diverge (receipt/re-raise/re-arm/cancel vs. courtesy-recovery), and two explicit methods read more clearly than one parameterized one. The intent — `do_task` hands condition booleans to the manager instead of calling `send_message` — is unchanged. Flag if you'd prefer a single dispatcher.
  - **`pending_cancel` is a top-level list** (not a per-incident field). §6 says "keep a pending_cancel receipt"; an incident is deleted on close, so a top-level `list[str]` retried each poll is the natural home for orphaned receipts.
  - **Courtesy "recovered after X" message uses `priority=0`** (normal); §7 leaves the priority unspecified for this informational message.
  - **State file in the repo root**, per this session's prompt (the `data/` move is step 6). `.gitignore` gained `notification_state.json`; step 6 swaps the per-file entries for `data/`.
- **Manual acceptance (run once after merge — the one path tests can't cover: a real emergency send + ack + cancel).** Sends exactly one real Pushover emergency alert.
  1. Stop the running monitor. In `config.toml` set `battle_count_timeout = 120`; confirm `pushover_enabled = true` and `re_alert_after_ack = 600`. Delete any stale `notification_state.json`.
  2. Make sure the farm is **not** progressing (close the match queue) so battle counts stay flat.
  3. Start it: `uv run python app.py`. Within ~2 min of no count change you should get **exactly one** emergency (priority 2) alert that Pushover re-delivers every 120 s. `notification_state.json` shows an open `stuck_farm` incident with a real `receipt`.
  4. Confirm **no new** app alerts arrive while it stays stuck (only Pushover's own re-delivery of the same alert) — the app stays OPEN and silent.
  5. **Acknowledge** the alert on your phone. Pushover stops re-delivering. On the next poll, `notification_state.json` gains a non-zero `acked_at` and the app still sends nothing. (Optional: leave it stuck ~10 min instead of fixing it and confirm exactly one re-arm alert fires.)
  6. Play one match so a battle count increments. On the next poll the app observes recovery, **closes** the incident and **cancels** the receipt: confirm `stuck_farm` is gone from `notification_state.json` and the nagging has stopped.
  7. Restore `battle_count_timeout = 360` and restart normally. (The `ignore/demo_dedup.py` script reproduces the whole lifecycle offline with `pushover_enabled = false` if you want to watch the transitions without spending quota.)

### 2026-06-12 — Session 2: roadmap steps 3–4
- **Branch / commits:** `codex/roadmap-steps-3-4`; `8c4ec2d` H3/H5/M1/L3: fix config-driven roster persistence, `a9c41b1` M4/M5/L3: add pytest harness and dev deps
- **Done:** H3 Buckler API payload now builds `targetShortId` from `config.user_code` and `targetSeasonId` from new `config.target_season_id`; H5 new characters now set `data_differs` and are persisted instead of skipped forever; M1 database writes are atomic via temp file + `os.replace`, and corrupt reads are treated as first init and rewritten from the current API response; L3 import-time config singleton removed so modules import without `config.toml`; test harness added with pytest + pytest-cov; M4 `pydantic` declared as a direct runtime dependency; M5 project metadata fixed and `mypy`/`datamodel-code-generator` moved to the uv dev group; `types-requests` installed and the now-unused `requests` import ignore removed from `notifier_client.py`.
- **Verified by:** `uv run python -m black app.py api_service.py config.py model.py notifier_client.py task.py utilities.py tests`; `uv run python -m mypy app.py api_service.py config.py model.py notifier_client.py task.py utilities.py tests` (`Success: no issues found in 9 source files`); `uv run python -m pytest --basetemp .pytest_cache\tmp --cov=.` (`6 passed`, total coverage 68%); live Buckler API check with Pushover disabled in-memory returned `APP_REAL_API_CHARACTER_ROWS=32` and `APP_REAL_API_FULL_ROSTER=True`; corrupt-copy M1 demo against `.pytest_cache\m1-demo\database.json` with Pushover disabled in-memory returned `M1_CORRUPT_COPY_RECOVERED=True` and `M1_RECOVERED_CHARACTER_ROWS=31`.
- **Not done / carried over:** Later roadmap steps intentionally untouched, including dedup, M3, M6–M10, Random-row filtering, status page work, and remaining L items.
- **Decisions made in-session:** `types-sortedcontainers` does not exist on PyPI, so `sortedcontainers` remains in place with a narrow `# type: ignore[import-untyped]`; removing that dependency is still the later L8/step 9 cleanup. Pytest was run with `--basetemp .pytest_cache\tmp` because the machine's default pytest temp root under `AppData\Local\Temp` was not accessible.

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
