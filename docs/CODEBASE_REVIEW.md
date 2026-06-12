# Codebase Review — SF6 Afk Farm Monitor

- **Date:** 2026-06-11
- **Reviewed at commit:** `4103f7d` plus uncommitted working-tree changes to `api_service.py`, `task.py`, `utilities.py` (and untracked `shortened.json`)
- **Scope:** Full audit of all application source (`app.py`, `api_service.py`, `config.py`, `model.py`, `notifier_client.py`, `task.py`, `utilities.py`), project configuration (`pyproject.toml`, `uv.lock`, `.gitignore`, `example.toml`), and runtime behavior of the third-party libraries actually used (`chump`, `requests`, `schedule`).
- **Context:** This is a long-running, single-process monitoring daemon. Its entire value is *reliability of notification* — so findings are rated against that goal. "Production" is assumed to mean an unattended personal machine running 24/7.

Verification performed during review (read-only): `mypy` run against all modules, dependency graph inspected via `uv.lock`, `chump` 1.6.0 source inspected in the venv, git status/ignore rules checked.

---

## Executive summary

The codebase is small, readable, and the module split (config / API / model / notifier / task / scheduler) is sensible for its size. However, there are several defects that directly undermine the tool's core purpose: **the monitor can silently hang forever (no HTTP timeout), can crash outright when Pushover errors (unhandled exceptions escape the scheduler loop), monitors a hardcoded account ID instead of the configured one, and leaks Pushover credentials into `logs/debug.log`**. There is also a logic bug where a newly added roster character causes endless false "stuck farm" alarms. None of these are hard to fix, but all of them should be fixed before treating this as production.

A meta-concern worth stating up front: **the monitor has no monitoring**. Several findings below are crash paths, and when this process dies, the user's signal is silence — which is indistinguishable from "everything is fine." A supervisor (Windows Scheduled Task / NSSM service with restart) and/or a dead-man-switch heartbeat should be part of the production deployment story.

---

## HIGH severity

### H1. No timeout on the Capcom API request — monitor can hang forever
`api_service.py:40`
```python
response = requests.request("POST", url, headers=headers, data=payload)
```
`requests` has **no default timeout**. If the connection stalls (TCP black hole, Capcom load balancer holding the socket, Wi-Fi drop mid-read), this call blocks indefinitely. Because `schedule` runs jobs synchronously on the single main thread, the entire scheduler stops: no more polls, no notifications, no error — the monitor is dead while appearing to run. For a watchdog tool this is the worst failure mode.

The Pushover side has the same problem: `chump` uses `urllib` with the default (infinite) socket timeout, so `send_message` can also hang forever.

**Fix:** pass `timeout=(connect, read)` (e.g. `timeout=(10, 30)`) to the requests call. For chump, either set a global socket default timeout at startup (`socket.setdefaulttimeout(30)`) or replace chump (see M9/L-list).

### H2. Exceptions from notification sending crash the whole app
`notifier_client.py:21-24`, `task.py:38, 43, 93-95`, `app.py:49-51`

`send_message` does no error handling, and every call site in `task.py` is **outside** the `try/except` that guards the API call (including the calls inside the two `except` blocks themselves). chump raises `urllib.error.URLError` on network failure and `chump.APIError` on API rejection (e.g. monthly quota exhausted, invalid token). Any of these propagates out of `do_task` → out of `schedule.run_pending()` → out of the bare `while True` loop in `app.py` → **process exits**. Ironic worst case: Capcom goes down, the app tries to notify you, Pushover hiccups once, and the monitor dies silently.

The "catchall exception for CYA insurance" (commit `8045c24`) only protects `get_character_win_rates`, not the notify path or the database read (see H5).

**Fix:** wrap `send_message`'s body in try/except (log and continue), and/or wrap the `do_task` invocation so no exception can kill the scheduler loop.

### H3. Configured `user_code` is ignored — the queried account is hardcoded
`api_service.py:15-17, 36-39`
```python
payload = json.dumps(
    {"targetShortId": 2885430127, "targetSeasonId": 12, "targetModeId": 2, "lang": "en"}
)
...
def get_character_win_rates(user_code) -> WinRateResponse:
    headers["Referer"] = f".../profile/{user_code}/play"
```
`config.user_code` is only used to build the **Referer header**. The actual account queried is the hardcoded `targetShortId: 2885430127` in the module-level payload. Anyone who follows the README, sets their own `user_code` in `config.toml`, and runs the app will silently monitor the *author's* account, not their own. The README explicitly documents `user_code` as "User code of the CFN profile to monitor," so this is a bug, not a doc problem.

Secondary issues in the same spot:
- `targetSeasonId: 12` is hardcoded (the existing TODO acknowledges this). When a new season starts and counts accrue under season 13, this query pins season 12 → battle counts freeze → **endless false "stuck farm" alarms every poll** until someone edits source. This has already bitten once (working tree bumps 10 → 12). Should be config-driven or auto-detected.
- The personal `targetShortId` is committed to public git history (minor privacy note).

**Fix:** build the payload inside the function from `config.user_code` (and move `targetSeasonId` to config or fetch the current season).

### H4. Pushover credentials are written to `logs/debug.log`
`app.py:16, 38-41` + `chump/__init__.py:320-325`

`app.py` sets the **root logger** to DEBUG and attaches a DEBUG file handler. chump's `Application._request` does:
```python
data['token'] = self.token
...
logger.debug('Making request ({request}): {data}'.format(...))
```
Message sends include both the application token and the user key in `data`. So **every Pushover send writes the app token and user key in plaintext into `logs/debug.log`**, which sits in the repo directory and is only protected by a `.gitignore` line. Anyone with read access to the logs (backups, sync tools, accidental log sharing while debugging) gets credentials to send notifications as you / to you.

**Fix:** stop running third-party loggers at DEBUG — e.g. `logging.getLogger("chump").setLevel(logging.INFO)` (and consider the same for `urllib3`), or attach the DEBUG handler only to your own loggers instead of root.

### H5. New roster character ⇒ permanent false "stuck" alarms and data never persisted
`task.py:64-67, 83-91`
```python
if character not in previous_character_to_battle_count:
    logger.warning("Found a new character: %s", character)
    continue
```
A character that exists in the API response but not in `database.json` (i.e., any newly released DLC character) is logged and skipped — it never sets `data_differs`, and the database is only rewritten when `data_differs` is true. Consequences:

1. The new character is **never added to the database** (the warning repeats every poll forever) until some *other* character's count happens to change.
2. If the new character is the one being farmed (the most likely scenario — new character, new Master color to grind), its count changes never register: `data_differs` stays false, `database.json`'s mtime ages past `battle_count_timeout`, and the app fires a **false "afk farm might be stuck" alarm on every poll, indefinitely**, while the farm is actually working.

**Fix:** treat a new character as a difference (set `data_differs = True`, include it in the written data — it already is in `current_character_to_battle_count`, so simply not `continue`-ing past the write decision is most of the fix).

---

## MEDIUM severity

### M1. Corrupt or partially written `database.json` crashes the app permanently
`task.py:22-25, 60-61`

- `write_to_database` writes in place (`open(..., "w")`). If the process is killed mid-write (reboot, OOM, Ctrl-C), the file is left truncated/corrupt.
- The read path `json.load(file)` at `task.py:60-61` is outside the try/except. A corrupt file raises `JSONDecodeError`, which kills the scheduler loop (same propagation as H2) — and because the file is still corrupt on restart, the app **crash-loops until someone manually deletes the file**.

**Fix:** atomic write (write to a temp file in the same directory, then `os.replace`), and guard the read — on parse failure, log, treat as first-init, and rewrite.

### M2. Notification spam can exhaust the Pushover quota and mute real alarms
`task.py:35-44, 83-87` (acknowledged by the TODO in `notifier_client.py:14-20`)

Every failure mode notifies on **every poll**: Capcom outage → 1 message/minute; stuck farm → 1 message/minute (the DB mtime never advances while stuck, so the condition re-fires each run). At the default 60 s interval that is 1,440 messages/day; Pushover's free tier is 10,000 messages/month per application, so a multi-day outage or an unattended stuck farm **exhausts the quota in ~7 days**, after which Pushover rejects sends — including future *real* alarms (and with H2, the rejection currently crashes the app).

**Fix options (the TODO's emergency-message idea is good):** rate-limit/dedupe repeated identical alerts (e.g. re-notify at most every N minutes with exponential backoff), or use Pushover emergency priority with retry/expire and wait for acknowledgement.

### M3. Expired Buckler cookies will be misdiagnosed as "website down/borked"
`api_service.py:40-42`, `task.py:35-44`

The Buckler session cookies (`buckler_id` etc.) expire periodically; this is a *routine* failure for this app, not an exotic one. When that happens the server returns 401/403 (→ generic "Capcom Buckler website down?" alert) or an HTML login page with HTTP 200 (→ `response.json()` raises, caught by the catch-all, "website must be completely borked"). Either way the user gets a misleading message and no hint that the actual fix is "refresh your cookies." Also note `response.json()["response"]` raises `KeyError` if the JSON shape shifts — same misleading bucket.

**Fix:** detect 401/403 and non-JSON/missing-`response` bodies explicitly and send a distinct "Buckler session expired — update cookies in config.toml" notification.

### M4. `pydantic` is not a declared dependency
`pyproject.toml:7-15`, confirmed in `uv.lock`

`config.py` and `model.py` import `pydantic` at runtime, but it is **not** in `[project] dependencies` — it is only pulled in transitively via `datamodel-code-generator` (a code-generation dev tool that has no business being a runtime dependency, see M5). If `datamodel-code-generator` is ever removed or moved to a dev group, the app stops importing.

**Fix:** add `pydantic>=2` to dependencies.

### M5. Dev tools shipped as runtime dependencies; placeholder project metadata
`pyproject.toml`

- `mypy` and `datamodel-code-generator` are listed as runtime dependencies. They belong in a dev dependency group (`[dependency-groups] dev = [...]` for uv).
- `name = "My-Python-Project"`, `description = "Add your description here"` are template placeholders.
- No tool configuration is committed (no `[tool.mypy]`, no formatter/linter config despite Black being used per commit history), and no entry point (commented-out stub remains).

### M6. Hardcoded `Accept-Encoding: gzip, deflate, br, zstd` without brotli/zstd decoders
`api_service.py:31`

The venv contains neither `brotli`/`brotlicffi` nor `zstandard` (confirmed in `uv.lock`). By overriding `Accept-Encoding`, the client *advertises* encodings it cannot decode. If Capcom's CDN ever starts answering with `Content-Encoding: br` or `zstd`, `response.json()` will fail on undecodable bytes — and per M3 it would be misreported as "website borked." This works today only because the server happens to pick gzip.

**Fix:** delete the `Accept-Encoding` header (requests sets a correct one automatically), or install the decoders.

### M7. Unbounded log growth
`app.py:32-41`

Two `FileHandler`s with no rotation, on a process designed to run 24/7. `debug.log` receives DEBUG output from every library on every poll (urllib3 connection chatter, chump payloads) — this will grow without bound until disk pressure or manual cleanup.

**Fix:** use `RotatingFileHandler`/`TimedRotatingFileHandler` (and add `encoding="utf-8"` while at it; the handlers currently use the Windows default codepage).

### M8. Everything is CWD-relative
`config.py:9`, `task.py:17`, `app.py:29-38`, `utilities.py:19, 33`

`config.toml`, `database.json`, `response.json`, `shortened.json`, `sorted_by_value.json`, and `logs/` are all opened by bare relative paths. Running the app from any directory other than the repo root either crashes (config not found) or scatters state files elsewhere. For a daemon started by a scheduler/service (the realistic production mode), the working directory is often *not* the repo dir.

**Fix:** anchor paths to the script location, e.g. `BASE_DIR = Path(__file__).resolve().parent`.

### M9. Uncommitted regression: per-poll `response.json` debug dump re-added
`api_service.py:44-47` (working tree only; contradicts commit `490bd16` which removed exactly this)

Every poll rewrites `response.json` to disk; the TODO says "debugging only." Decide before shipping: remove it (matching the earlier commit's intent) or keep it deliberately. Related hygiene: the working tree also adds `truncated_database` writing `shortened.json` on every DB update, plus dead `sort_database_by_value`/`sorted_by_value.json` — and **neither `shortened.json` nor `sorted_by_value.json` is gitignored** (`shortened.json` is currently showing as an untracked file at risk of accidental commit). `.gitignore` covers `config.toml`, `database.json`, `response.json`, `logs/` only.

### M10. Stuck-farm detection rests on filesystem mtime and naive local time
`task.py:83-87`, `utilities.py:6-10`

Two distinct fragilities:
- **mtime as state:** the "last update" timestamp is the mtime of `database.json`. Anything that touches the file (backup/restore, sync tools, copying the directory, manual edits) silently resets or skews the stuck timer. Storing a `last_updated` timestamp inside the JSON (or alongside it) is more robust.
- **Naive local-time arithmetic:** `datetime.now() - datetime.fromtimestamp(mtime)` breaks across DST transitions — the duration jumps ±1 h, which can either fire a false stuck alarm or suppress a real one for an hour. `time.time() - os.path.getmtime(f)` is immune and simpler.

---

## LOW severity / code quality / tech debt

1. **`chump` (Pushover client) is abandonware** — v1.6.0, released 2018, Python-2-era codebase (`urllib2` fallbacks, custom connection pool). It happens to import on Python 3.14 today, but it is unmaintained, untyped, has the DEBUG credential-logging behavior (H4), and no timeout support. The Pushover REST API is a single `requests.post` — replacing chump with ~10 lines using the already-present `requests` removes a dependency and fixes H4's root cause and half of H1.
2. **Conditional module-level globals in `notifier_client.py:9-11`** — `app`/`user` only exist when `pushover_enabled` is true. `send_message` guards correctly today, but any future code path touching `user` gets a `NameError`, and type checkers flag possibly-undefined names. Encapsulate (lazy init or a small class), or define `user = None` and check.
3. **Import-time side effects:** `config.py:34` reads the file system at import (`config = load_config()`), and `notifier_client.py` constructs the Pushover client at import. Any module import requires a valid `config.toml` in CWD, which makes unit testing effectively impossible and turns a missing config into a raw `FileNotFoundError` traceback instead of a friendly message. Also `ConfigData(**config_dict)` produces an unhelpful `TypeError` on any unknown/missing TOML key.
4. **No config value validation** — nothing stops `polling_interval = 0` or negative timeouts; pydantic validates types only. Cheap to add bounds via pydantic field constraints.
5. **Module-level mutable request state in `api_service.py`:** the shared `headers` dict is mutated per call (`headers["Referer"] = ...`) and the payload is a module-level pre-serialized string. Build both inside the function. Also: duplicate `"Host"`/`"host"` keys (`api_service.py:20-21`), manually setting `Host` at all is unnecessary, `Connection: keep-alive` does nothing without a `requests.Session` (which would also be a minor perf win for a poller), and `requests.request("POST", ...)` is an odd spelling of `requests.post(...)`.
6. **Global `notifications_to_send` deque (`task.py:19`)** — module-level mutable state used only within a single `do_task` invocation; a local list is simpler. (Side effect of the current design plus H2: if a send throws mid-drain, already-popped messages are lost and the rest survive into the next run — half-intentional at best.)
7. **Magic number `100`** (Master color battle-count threshold) duplicated in `task.py:78` and `utilities.py:30`; magic string `"Any"` filter at `task.py:48`. Promote to named constants. (Note `"Random"` is *not* filtered — verify that's intended.)
8. **`SortedDict` dependency is unnecessary** — it exists only to keep JSON keys alphabetical; `json.dumps(data, sort_keys=True)` does the same with a plain dict, dropping the `sortedcontainers` dependency.
9. **Dead/confusing utility code:** `sort_database_by_value` is dead (its only call is commented out at `task.py:27`); `truncated_database` is misnamed (it doesn't truncate the database — it writes a *different* report file, `shortened.json`) and wastefully re-reads the JSON file that `write_to_database` just wrote instead of receiving the dict.
10. **mypy is a dependency but not actually passing or enforced:** running it yields 5 errors (missing `types-requests` stubs, untyped `chump`/`sortedcontainers`, missing annotation on `notifications_to_send`). No `[tool.mypy]` config, no CI, no pre-commit. Either wire it up (install stubs, add config, fix the annotation) or drop it.
11. **Sparse typing:** `utilities.py` functions and `get_character_win_rates(user_code)` lack parameter/return annotations despite the mypy intent.
12. **`import logging.config` unused** in both `app.py:1` and `task.py:2` (only `logging` is used).
13. **No tests at all.** Even two or three tests around the `do_task` diff logic (new character, threshold crossing, stuck detection) would have caught H5. The import-time config coupling (item 3) is the main blocker to writing them.
14. **File handles opened without `encoding=`** throughout (`task.py`, `utilities.py`, `api_service.py`) — on Windows this is the legacy codepage; harmless today because `json.dumps` defaults to ASCII-escaped output, but fragile.
15. **`send_message` return value** (`notifier_client.py:24`) is never used and is `None` when disabled vs a chump `Message` otherwise — just drop the return.
16. **Plaintext secrets in `config.toml`** (Buckler session cookies = full account session; Pushover keys). Gitignored, which is reasonable for a personal tool, but worth stating in the audit: the protection is one `.gitignore` line, and H4 currently copies part of these secrets into logs anyway. Environment variables or OS keyring would be stronger; at minimum keep H4 fixed.
17. **Monitor cannot report its own death** (see executive summary): after fixing the crash paths, still consider (a) running under a supervisor with auto-restart, and (b) a heartbeat/dead-man switch (e.g. healthchecks.io ping per successful poll) so silence becomes an alert.
18. **README gaps:** doesn't mention that `targetSeasonId` needs manual bumping per season (until H3's fix lands), nor the generated artifact files; "Example running output" shows behavior, not failure modes/cookie-refresh procedure (M3).

---

## What's in good shape

- Clear, small modules with one responsibility each; easy to navigate.
- Pydantic validation of the API response (generated model with provenance header) is a solid choice — schema drift fails loudly instead of corrupting state.
- Secrets and state files are correctly gitignored (with the M9 exceptions noted).
- `uv` + committed lock file gives reproducible installs.
- Honest TODOs that correctly identify two of the real problems (notification spam design, season ID).
- Log format is consistent and the info/debug log split is a reasonable idea (it just needs rotation and the root-DEBUG fix).

---

## Open questions for the author

*Status updated 2026-06-12:*

1. **H3:** ~~Is `targetShortId: 2885430127` the same account as your `config.user_code`?~~ **RESOLVED** — verified against the local `config.toml`: `user_code` is the same value as the hardcoded `targetShortId`. They are one identifier; the fix is simply "build the payload from `config.user_code`," no second config field needed.
2. **M9:** **RESOLVED** (2026-06-12). `shortened.json` is **a feature in active use** — the author's progress-check workflow is to open `database.json`/`shortened.json` directly. Keep `truncated_database` (add `shortened.json` to `.gitignore`; `STATUS_PAGE_PROPOSAL.md` will eventually absorb this workflow and allow retiring it). The `response.json` per-poll dump: **drop it** (author delegated the call). Replace with logging the raw response body at DEBUG **only on failure paths** (HTTP error, non-JSON body, missing `response` key, pydantic validation error). Rationale: the file only ever held the *latest* response — usually already overwritten by healthy polls by the time anyone investigates — while failure-time logging lands the exact evidence, timestamped and interleaved with the error in `debug.log`, without ~10 MB/day of healthy-poll dumps.
3. **M2:** **RESOLVED** — emergency-priority incident design accepted; full design in `ALERT_DEDUPLICATION_PROPOSAL.md` (supersedes simple rate-limiting).
4. **RESOLVED** (2026-06-12): production = the author's own always-on PC; other users are a possible future, not a current requirement. Implications: plaintext `config.toml` secrets remain acceptable (item 16 stays low); M8 (CWD-relative paths) is still worth fixing — a scheduled-task/service start often has a different working directory even on one machine; H3's simple fix (payload from `config.user_code`) is sufficient.
5. **RESOLVED** (2026-06-12): **filter `"Random"` like `"Any"`.** Rationale: Random is the one row with no Master-color reward at 100, so the `>= 100` notification would be spurious for it. (Author notes one Random match grants an MR rating — a one-time manual act, not something to monitor.)
6. **NEW — testing scope agreed** (2026-06-12): the fix pass should include a test harness, not just the listed fixes. Concretely: `pytest` + `pytest-cov` in a uv dev dependency group; untangle the import-time config singleton (Low item 3) first, since it currently makes modules untestable; seed tests around the `do_task` diff logic (new-character handling, threshold crossing, stuck detection — the H5 class of bug) and the incident state machine from `ALERT_DEDUPLICATION_PROPOSAL.md`; establish baseline coverage reporting and ratchet it up over time rather than chasing a number up front.

**All review open questions are now resolved.** Remaining decisions live in the proposal docs (`ALERT_DEDUPLICATION_PROPOSAL.md` §11, `STATUS_PAGE_PROPOSAL.md` §6).

## Unified roadmap (updated 2026-06-12 — sequences this review with both proposal docs)

The review fixes and `ALERT_DEDUPLICATION_PROPOSAL.md` are **not** sequential bodies of work: the proposal's "phase 0" *is* the top of this fix list, and two findings (M2, M10) are implemented/retired *by* the proposal rather than fixed standalone. The merged order:

1. **H1 + H2** — timeouts on all HTTP calls + exception-proof the scheduler loop. Turns "monitor silently dies" into "monitor keeps working"; smallest diffs, biggest payoff.
2. **Replace `chump` with a direct `requests`-based `PushoverClient`** — resolves H4's root cause (plus silence third-party DEBUG in the file log), L1, L15, and is the foundation the dedup work builds on (receipt/cancel/cancel_by_tag support).
3. **H5** (new-character handling), **H3** (payload + season ID from config), **M1** (atomic write + guarded read) — small, independent correctness fixes.
4. **Test harness** (open-questions item 6): pytest + dev dependency group + config-singleton untangling (L3), seeded with tests for the `do_task` logic — landed alongside step 3 so the fixes arrive tested. Also fold in the trivial dependency fixes here (M4 pydantic, M5 dev-deps/metadata).
5. **Dedup phase 1** per `ALERT_DEDUPLICATION_PROPOSAL.md` §12 — **supersedes M2** (do not implement rate-limiting separately) and **retires M10** (`last_change_at` replaces the mtime check).
6. **Remaining M items:** M3 (auth-expiry detection — becomes an incident type, so it slots naturally after phase 1), M6 (Accept-Encoding), M7 (log rotation), M8 (absolute paths), M9 cleanup + `.gitignore` additions.
7. **Dedup phase 2** (Master-color swap incident, quota self-alert, sounds/url polish, README DND note).
8. **Status page** per `STATUS_PAGE_PROPOSAL.md` (depends on M1's atomic writes and phase 1's `notification_state.json`).
9. Remaining L items opportunistically; Glances stays shelved (`GLANCES_PROPOSAL.md`).
