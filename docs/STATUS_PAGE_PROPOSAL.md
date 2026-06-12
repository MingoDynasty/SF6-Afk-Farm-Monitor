# Proposal: Local Status Page — Live Farm Progress View

- **Date:** 2026-06-12
- **Status:** Draft for discussion
- **Replaces:** the shelved Glances idea (`GLANCES_PROPOSAL.md` — Apple Watch–only, author has none) as the answer to "check farm progress on demand."
- **Scheduling:** quality-of-life — after the `CODEBASE_REVIEW.md` high-priority fixes and `ALERT_DEDUPLICATION_PROPOSAL.md` phase 1, same slot Glances occupied.

---

## 1. Problem

The current progress-check workflow is opening `database.json` (all characters) or `shortened.json` (unfinished only) in an editor. It works, but it's raw JSON, shows no "how stale is this / is the farm healthy right now" context, and is only practical at the PC itself.

## 2. Proposal: a tiny LAN status page — polling, not WebSockets

A small, **separate-process** web server on the PC serving one page:

- **`GET /`** — a single static HTML page: character table sorted unfinished-first with progress bars (the 0–100 grind maps directly onto a bar), finished-character tally, time since last battle-count change, and current health (OK / STUCK n min / API DOWN — read from `notification_state.json` once dedup phase 1 lands).
- **`GET /api/status`** — the same data as JSON (the page fetches this; also handy for anything else later).
- The page **re-fetches every 30 s** with a few lines of vanilla JS. No build step, no framework, one HTML file.

Reachable at `http://localhost:<port>` on the PC and `http://<pc-ip>:<port>` from a phone on the same Wi-Fi — which restores the "glance from the couch" value Glances was supposed to provide, with a strictly better display.

### Why not WebSockets (pushing back on this one)

Real-time push is the wrong tool here, for the same simplicity-first reason the review keeps hammering:

- **The data changes at most once per `polling_interval` (60 s).** A page that re-fetches every 30 s is at most ~30 s staler than a WebSocket — on a dashboard a human looks at a few times a day. Nobody can perceive the difference.
- WebSockets drag in an async server (or a second framework), connection lifecycle, reconnect/heartbeat handling, and a stateful coupling between monitor and viewer. That's real complexity purchased to make a 60-second-cadence number arrive 30 seconds sooner.
- If push ever genuinely matters (it shouldn't), **Server-Sent Events** is the escalation path — one-directional, plain HTTP, trivially consumed by `EventSource` — not WebSockets. Noted only as the contingency; not proposed.

## 3. Architecture: separate process, reads the same files

```
app.py (monitor)  ──writes──►  database.json / notification_state.json
                                       ▲
status_server.py  ──reads─────────────┘   (serves HTML + /api/status)
```

- **Separate process, not a thread in the monitor.** The entire codebase review is about the monitor's reliability; a web server living inside it is a new way to destabilize it. As a separate script that only *reads* the JSON files, the status page can crash, hang, or be absent with zero effect on monitoring. It also needs no access to config secrets — character counts only.
- **Stack: Python stdlib `http.server.ThreadingHTTPServer`** — roughly 60–80 lines including the embedded HTML, zero new dependencies. Flask would be marginally nicer but buys nothing at two endpoints; can revisit if the page grows features. Reads the JSON files per-request (they're under 1 KB; no caching needed).
- **Stale-read edge:** reading `database.json` mid-write is the same torn-read risk the review flagged as M1; once M1's atomic-write fix (`os.replace`) lands, readers always see a complete file. Another small reason M1 precedes this.

## 4. Operational details

- **Port:** *decided 2026-06-12:* configurable via config key (e.g. `status_page_port`); default anything unclaimed — **not 8080** (occupied by Steam on the author's machine, a known local foot-gun). Default `8675`.
- **Bind address:** *decided 2026-06-12:* LAN access is wanted — default `0.0.0.0`. First LAN use will trigger a Windows Firewall inbound prompt — expected, allow on Private networks.
- **Security posture:** no auth, LAN-only by design. Contents are character battle counts — not sensitive. Must **never** be port-forwarded/exposed beyond the LAN; if that need ever appears, that's a different proposal.
- **Lifecycle:** started independently (same supervisor/scheduled task that runs the monitor can run it). The monitor never knows it exists.

## 5. Relationship to existing artifacts

- Once the page exists, it strictly dominates the `shortened.json` workflow (the unfinished-first sort and <100 filter become a view concern). That unlocks the review's L9 cleanup: retire `truncated_database` / `sort_database_by_value` / `shortened.json` / `sorted_by_value.json` entirely, leaving `database.json` as the single state artifact. Keep `shortened.json` until the page is actually in use.
- Health display depends on `notification_state.json` from dedup phase 1 (`last_change_at`, open incidents). If built earlier, the page can fall back to `database.json` mtime with the M10 caveats — better to just sequence it after phase 1.

## 6. Open questions

1. ~~Is phone/LAN access wanted?~~ **RESOLVED 2026-06-12: yes** — bind `0.0.0.0` by default (§4).
2. ~~Default port?~~ **RESOLVED 2026-06-12:** no preference; make it configurable (done — §4) and default `8675`.
3. Should the page also show recent alert history (last N incidents with timestamps)? Cheap to add from `notification_state.json` if the dedup work keeps a small closed-incident log. *Default if unanswered: skip for v1.*
4. Auto-start: bundle a second entry in whatever supervisor setup the monitor gets, or run on demand only? *Default if unanswered: on demand for v1; revisit when the supervisor story (review Low item 17) is settled.*
