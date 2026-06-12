# PR #1 Review — roadmap steps 1–2 (`codex/roadmap-steps-1-2`)

- **PR:** https://github.com/MingoDynasty/SF6-Afk-Farm-Monitor/pull/1 (draft)
- **Reviewed:** 2026-06-12, at commit `f98f903` (commits: `bc55165`, `a7e9ca4`, `f98f903`)
- **Reviewed against:** `docs/CODEBASE_REVIEW.md` Unified roadmap steps 1–2,
  `docs/ALERT_DEDUPLICATION_PROPOSAL.md` §8 client interface, `docs/IMPLEMENTATION_LOG.md`
- **Verdict:** **changes-requested** — one low-severity must-fix (F1); everything
  else verified clean.
- **Resolution status:** F1 resolved by `a6d252d`; gates re-run after the fix.

## How to use this doc

Work each open finding below, flip its status to `resolved` with the fixing
commit hash, re-run the gates (listed at the bottom), and request re-review.
This doc is self-contained; you do not need the original review conversation.

## Findings

### F1 — `PushoverClient` can raise on valid-JSON-but-non-object response body — **resolved**

- **Severity:** Low (must-fix — violates an explicit spec contract this PR claims to deliver)
- **Resolved by:** `a6d252d` (`F1: guard non-object Pushover JSON`)
- **Where:** `notifier_client.py:125-148` (`_request_json`); raise sites are
  `response_json.get("status")` at `notifier_client.py:136` and, if that were
  passed through, `response_json.get("receipt")` at `notifier_client.py:63`.
- **What:** `response.json()` can return any JSON value (list, string, number,
  null), not only a dict. If a proxy/CDN/captive portal returns HTTP 200 with
  e.g. a JSON array body, `response_json.get(...)` raises
  `AttributeError: 'list' object has no attribute 'get'`, which escapes
  `send`/`check_receipt`/`cancel`/`cancel_by_tag` to the caller.
- **Why it matters:** `ALERT_DEDUPLICATION_PROPOSAL.md:165` specifies the
  client contract as "all exceptions caught & logged, never raised to caller",
  and the PR/implementation log claim "never-raise" / "safe failure values".
  Today the H2 guard in `app.py:46-50` contains the blast radius (logged, poll
  skipped, monitor survives), but dedup phase 1 will call this client inside
  the incident state machine and rely on the safe return values.
- **Repro (verified 2026-06-12):** local HTTP server returning
  `200` + `[1, 2, 3]` body; `client.send("hi")` raised
  `AttributeError: 'list' object has no attribute 'get'`. Same server
  unreachable instead → all four methods correctly returned
  `None` / `{}` / `False` / `False` with sanitized logs.
- **Proposed fix:** in `_request_json`, after the `response.json()` try/except,
  validate the parsed value before use:

  ```python
  if not isinstance(response_json, dict):
      logger.error(
          "Pushover %s request for %s returned non-object JSON: HTTP %s",
          method, path, response.status_code,
      )
      return None
  ```

  This covers all four public methods (they all route through
  `_request_json`). Keep the existing log sanitization style (no response
  body, no exception text).
- **Resolution verification (2026-06-12):** local HTTP server returning
  `200` + `[1, 2, 3]` body for all routes; `send`, `check_receipt`, `cancel`,
  and `cancel_by_tag` returned `None` / `{}` / `False` / `False`, logged
  sanitized `returned non-object JSON: HTTP 200` messages, and printed
  `NON_OBJECT_JSON_DEMO_SURVIVED`.

## Non-blocking notes (no action required for this PR)

- **N1 — stale local venv:** `uv pip list` may still show `chump 1.6.0`
  locally; `uv run` syncs inexactly (doesn't remove extraneous packages).
  `uv.lock` is consistent (`uv lock --check` passes) and chump-free. Run
  `uv sync` to physically remove it. Environment artifact, not a code issue.
- **N2 — for step 4:** when `types-requests` stubs land, remove the
  `# type: ignore[import-untyped]` at `notifier_client.py:5` or it becomes an
  unused ignore.
- **N3 — for step 5 implementer:** `PushoverClient.send` returns `None` both
  on failure and on a *successful* non-emergency send (Pushover only returns a
  receipt for priority 2). This matches the spec interface
  (`ALERT_DEDUPLICATION_PROPOSAL.md:160-161`); just don't use the return value
  as a success signal for priority < 2 sends.
- **N4 — cosmetic:** `REQUEST_TIMEOUT = (10, 30)` is duplicated in
  `api_service.py:12` and `notifier_client.py:12`. Fine for now; consolidate
  opportunistically if a shared constants home ever appears.

## Claims verified

| PR / log claim | Result |
|---|---|
| Buckler request has bounded timeout (H1) | ✓ `api_service.py:41-43`, `timeout=(10, 30)`; only HTTP call in that module |
| Scheduler survives task/send failures (H2) | ✓ `app.py:46-61` — `do_task` wrapped in `run_task_safely` (catches `Exception`, not `BaseException`, so Ctrl-C still works); belt-and-suspenders try around `run_pending()` |
| First-run behavior preserved | ✓ direct `run_task_safely()` call replaces `schedule.run_all()`; next-run time was already set at `.do()` registration, difference is milliseconds |
| chump removed (H4 root cause, L1) | ✓ gone from `pyproject.toml`, `uv.lock`, and all imports (repo-wide grep: only doc mentions remain) |
| Third-party DEBUG silenced for credential paths | ✓ `app.py:17` sets urllib3 to INFO — this matters because `check_receipt` puts the app token in a GET query string, which urllib3 logs at DEBUG |
| Sanitized failure logging | ✓ verified by demo: failure logs contain endpoint path + exception class name only; fake token/user key did not appear anywhere in captured output |
| Safe failure values (`None`/`{}`/`False`/`False`) | ✓ verified by demo against unreachable endpoint; F1 edge case covered by `a6d252d` |
| Client interface matches dedup spec | ✓ `send(message, priority, retry, expire, tags, sound, url, url_title) -> receipt \| None`, `check_receipt -> dict`, `cancel -> bool`, `cancel_by_tag -> bool` per `ALERT_DEDUPLICATION_PROPOSAL.md:159-165`; Pushover endpoints/paths correct (`messages.json`, `receipts/{r}.json`, `receipts/{r}/cancel.json`, `receipts/cancel_by_tag/{t}.json`) |
| `send_message` returns `None` always (L15) | ✓ `notifier_client.py:158-161`; no caller uses the return |
| L2 incidentally fixed | ✓ `pushover_client` is always defined (`None` when disabled), guarded at the call site |
| mypy baseline unchanged | ✓ ran locally: exactly the 4 claimed baseline errors (requests stubs ×2, sortedcontainers stubs, `notifications_to_send` annotation); no new errors; chump import error gone |
| `IMPLEMENTATION_LOG.md` updated accurately | ✓ statuses/commit hashes match `git log`; session entry matches the diff |
| Real-API poll (`APP_REAL_API_CHARACTER_ROWS=32`) | Accepted on inspection — not re-run (would spend the author's Buckler session); the change adds only `timeout=` to an otherwise identical call |
| Step 3+ scope untouched (H3, H5, M1, …) | ✓ confirmed — hardcoded `targetShortId`/season, new-character skip, non-atomic writes all still present, as intended |

## Gates run (2026-06-12, initially on `f98f903`; re-run after `a6d252d`)

| Gate | Result |
|---|---|
| `uv run python -m mypy app.py api_service.py config.py model.py notifier_client.py task.py utilities.py` | 4 errors — identical to documented baseline, no new (re-run after `a6d252d`) |
| `uv run python -m black --check` (all 7 modules) | clean (re-run after `a6d252d`) |
| `uv run python -m py_compile` (all 7 modules) | clean (re-run after `a6d252d`) |
| `uv lock --check` | clean (re-run after `a6d252d`) |
| `uv run pytest` | n/a — no test harness yet (roadmap step 4) |
| Line endings (`git ls-files --eol`) | all committed blobs LF; no false-positive risk |
