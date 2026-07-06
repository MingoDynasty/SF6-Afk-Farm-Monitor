# Proposal: Status Page UX Improvements — Surfacing Action & Liveness

- **Date:** 2026-07-06
- **Status:** Draft for discussion / implementation handoff
- **Builds on:** `STATUS_PAGE_PROPOSAL.md` (the v1 page, shipped as `status_server.py`)
- **Source:** UI/UX audit of the running page against live `data/` files, 2026-07-06

---

## 1. Problem

The v1 page is solid — safe rendering, proper dark mode, graceful handling of
missing/corrupt files. Its gaps are **informational**: things it knows (or could
know) but doesn't say. The live state at audit time demonstrated both big ones
at once:

1. **An open `swap_needed` incident was invisible.** Ryu finished 8 days ago,
   `notification_state.json` held an open swap incident with
   `"character": "Ryu"` — and the page showed a green **OK** pill. The page's
   core question is "do I need to do anything?" and it answered wrong.
2. **"OK" can mean "the monitor isn't running."** 8 days without a
   battle-count change would open a `stuck_farm` incident within
   `battle_count_timeout` (6 min) *if the monitor were polling*. No such
   incident existed, so the monitor was stopped — yet the pill said OK. "OK"
   really means "no open incidents as of the last time the monitor ran," and
   nothing on the page distinguishes that from "healthy right now."

Plus a set of smaller comprehension and polish gaps (§3).

## 2. Scope and slicing

Two PRs, because they have different blast radii:

- **PR 1 — status page only.** Touches `status_server.py` and
  `tests/test_status_server.py`. No monitor changes, zero risk to monitoring.
- **PR 2 — monitor heartbeat.** Small monitor-side write each poll plus page
  rendering. Touches `task.py` / `incident_manager.py`, so it goes through the
  usual monitor-reliability scrutiny separately.

PR 1 does not depend on PR 2; ship it first.

## 3. PR 1 — page-only fixes

### 3.1 "Swap needed" action pill (the headline fix)

- **Payload:** `build_status()` gains a top-level field:
  `"swap_needed": {"character": "<name>"} | null`, derived from
  `state["incidents"]["swap_needed"]["character"]` with the same
  defensive-typing style as `_derive_health` (missing/malformed → `null`).
  Health derivation is unchanged — swap stays out of the health enum, as
  decided in `STATUS_PAGE_PROPOSAL.md` §2 (it's an action item, not a
  malfunction).
- **UI:** a second pill in `.header-actions`, before the health pill, shown
  only when non-null: `SWAP NEEDED: Ryu`. Reuse the stuck (amber) palette —
  amber = "needs attention," red stays reserved for broken. `.header-actions`
  already flex-wraps, so a second pill degrades fine on mobile.
- **Optional (default: skip):** an equivalent `low_quota` pill. Same pattern,
  much lower value; add only if it ever bites.

### 3.2 Empty state with guidance

When `characters` is empty, render one full-width row in `tbody`
(`colspan="3"`, muted/italic): "No character data yet — is the monitor
running? (expects `data/database.json`)". Keeps the header visible so the page
still looks like itself on first run.

### 3.3 State the 100-battle target

Change the `Battles` column header to `Battles / 100`. One string; removes the
"what does the bar mean?" guess. (`FINISHED_THRESHOLD` is already the single
source of truth server-side; interpolate it rather than hardcoding a second
100 in the HTML if convenient.)

### 3.4 Don't let a page-fetch failure overwrite farm health

Today a failed `/api/status` fetch rewrites the *farm* health pill to "Status
unavailable" (red). Keep the last-known health pill instead; the existing
`refresh-status` line already says `Last fetch failed: …` and that's the right
(and sufficient) place for *page* health. Exception: if the **first** fetch
fails (nothing rendered yet), the pill may show "Status unavailable" — there
is no last-known state to preserve.

### 3.5 Tab title reflects state

On each render set `document.title`: prefix `"⚠ "` (warning sign — keep
the source ASCII via the JS escape, matching the file's ASCII-only rule) when
health is not `OK`/`UNKNOWN` **or** the swap pill is active; plain
`SF6 Afk Farm Monitor` otherwise. Makes a left-open tab useful without
switching to it.

### 3.6 Favicon

Inline SVG data-URI `<link rel="icon">` (URL-encoded, ASCII-safe). Kills the
default-icon look *and* the `/favicon.ico` 404 line in the access log on every
visit. Static icon only — dynamic state-colored favicons are rejected (§5).

### 3.7 `HEAD` support

`BaseHTTPRequestHandler` returns 501 for `HEAD /` today (observed from a
preview probe; link previewers and uptime checkers use HEAD). Add `do_HEAD`
that routes like `do_GET` but sends headers only — thread a
`head: bool = False` parameter through `_send_bytes` (still sets
`Content-Length`).

### 3.8 Contrast fixes (WCAG AA, light theme)

- `--footer: #999` on `#f4f5f7` ≈ 2.6:1 — used by the always-visible
  "Refreshes every 30s" line. Darken to `#666` (≈ 5.2:1).
- `--column-label: #777` ≈ 4.1:1 — borderline for the 0.8 rem headers. Same
  fix: `#666` works for both (they can share a value or stay separate vars).
- Dark theme already passes; leave it alone.

### 3.9 Accessibility touches

- Health pill: add `role="status"` (implicit `aria-live="polite"`) so state
  changes are announced. Same on the new swap pill.
- Progress bars: add `aria-hidden="true"` to the bar div — the Battles column
  already carries the value, so hiding the decoration beats wiring up
  `role="progressbar"` attributes on every re-render.

### 3.10 Sticky table header

`th { position: sticky; top: 0; background: var(--bg); }` — 30 rows scroll
past a phone viewport and the headers vanish. The explicit background is
required (headers are transparent otherwise and rows show through).

### 3.11 Optional, flagged separately

- **Alphabetize the finished block** (recommended, ~1 line): keep
  unfinished-first by descending count, but sort finished rows by name —
  "did Blanka finish?" becomes scannable. Adjust the affected sort test.
- **"Updated Xs ago" from `generated_at`** (default: skip): the refresh line
  plus the failure path already communicate page freshness.
- **`matchMedia` change listener** for live OS theme switches (default: skip).

## 4. PR 2 — monitor heartbeat ("is the monitor even running?")

The one deliberate scope expansion, because it's the difference between a
status page and a green light that lies.

### 4.1 Monitor side

`IncidentManager` gains `record_poll()`: sets `last_poll_at = clock()` and
saves. `run_task()` calls it **at the top of every poll, before the API call**
— a poll that ends in `api_down`/`auth_expired` still proves the monitor is
alive (the incident conveys the failure; the heartbeat conveys liveness).

- **Recommended: store `last_poll_at` in `notification_state.json`.** The page
  already reads that file, the incident manager already owns atomic writes to
  it, and no new artifact appears (the roadmap just finished consolidating
  artifacts). Cost: one small atomic write per poll (~60 s cadence) —
  negligible.
- **Alternative (rejected): separate `data/heartbeat.json`.** Keeps the state
  file change-only, but adds a third data artifact and a second write path for
  no functional gain.
- File mtime as a heartbeat stays rejected (M10; state files are written on
  change only).

### 4.2 Page side

- **Payload:** add `"monitor": {"last_poll_at": float|null,
  "seconds_since_poll": float|null, "stale": bool}`.
- **Staleness rule:** `stale` when `now - last_poll_at >
  max(3 * polling_interval, 300)`. Three missed polls tolerates scheduler
  jitter; the 5-minute floor avoids flapping if someone sets a short interval.
  `load_config()` is already called for the port; reading `polling_interval`
  from the same config adds no new exposure (still never served raw).
  `last_poll_at` missing entirely (pre-PR-2 state file) → `stale: false`,
  render nothing new — graceful for mixed versions.
- **Health precedence:** stale monitor **overrides everything** — when the
  state file is stale, its incidents are stale too. Pill: `MONITOR STALE`
  (amber). Meta row gains `Monitor: last polled 8 days ago` (client-ticking,
  like the existing staleness line) whenever `last_poll_at` is present.

## 5. Considered and rejected

- **Staleness-line color escalation** (turn "8 days ago" red past
  `battle_count_timeout`): redundant with the STUCK pill while the monitor
  runs, and a permanent false alarm whenever farming is deliberately paused.
  The heartbeat (§4) addresses the real ambiguity.
- **Dynamic state-colored favicon:** the title prefix (§3.5) delivers the same
  glanceability for a fraction of the JS.
- **Search/filter box, frameworks, SSE/WebSockets, configurable refresh:**
  v1's simplicity-first reasoning stands (`STATUS_PAGE_PROPOSAL.md` §2); 30 s
  polling on a 60 s-cadence source is already correct.
- **Incident history:** still the most useful *next* feature after this
  proposal ("what happened overnight?"), but it needs a monitor-side
  closed-incident log first. Remains deferred, per v1 open question 3.

## 6. Test plan

Extend `tests/test_status_server.py` (existing harness):

- `build_status` emits `swap_needed` when the incident is present with a valid
  `character`; `null` when absent or malformed; health enum unaffected.
- `do_HEAD /` and `/api/status`: 200, same `Content-Type`/`Content-Length` as
  GET, empty body; unknown route still 404.
- PR 2: `monitor.stale` derivation with a fake `now` — fresh poll,
  three-missed-polls boundary, missing `last_poll_at` (mixed-version file);
  stale overrides `OK` *and* incident-derived states.
- Sort change (if 3.11 alphabetization is taken): finished block ordered by
  name, unfinished-first ordering preserved.

Client-side rendering changes (pills, empty row, title, sticky header) stay
manual-verify — the page has no JS test harness and shouldn't grow one for
this.

## 7. Open questions

1. §3.11 alphabetize-finished: take it in PR 1? *Default if unanswered: yes
   (it's one sort-key line plus a test tweak).*
2. §4.2 `MONITOR STALE` pill wording — alternatives: `MONITOR DOWN?`,
   `NOT POLLING`. *Default: `MONITOR STALE`.*
3. Should PR 2's meta line replace the header's static "Refreshes every 30s"
   text on small screens to save a wrap? *Default: no, keep both; revisit only
   if the header wraps badly on a real phone.*
