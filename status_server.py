"""Local LAN status page - a read-only view of farm progress.

A **separate process** from the monitor (``app.py``): it never imports the
monitor's task/scheduler code and the monitor never imports it. It only *reads*
``data/database.json`` and ``data/notification_state.json`` from disk on each
request (both are tiny, so there is no caching) and renders the current farm
state. Because it only reads those two files, it can crash, hang, or be absent
with zero effect on monitoring (STATUS_PAGE_PROPOSAL.md section 3).

It serves two endpoints:

- ``GET /api/status`` - the assembled state as JSON.
- ``GET /`` - a single self-contained HTML page (inline CSS/JS) that re-fetches
  ``/api/status`` every 30 s and renders the character table, finished tally,
  staleness line, and health line. No build step, no framework.

Security: the page is LAN-only by design, no auth (section 5). It serves *only* the
two ``data/`` files; it never exposes ``config.toml`` contents. ``load_config``
is used solely to read ``status_page_port``.
"""

import json
import logging
import time
from datetime import timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import humanize

from config import load_config
from paths import DATA_DIR

logger = logging.getLogger(__name__)

DATABASE_FILE = DATA_DIR / "database.json"
NOTIFICATION_STATE_FILE = DATA_DIR / "notification_state.json"

# A character's Master color is complete at 100 battles (task.py uses the same
# threshold to decide a swap is needed).
FINISHED_THRESHOLD = 100

# Character-select pseudo-entries that are not Master-color farm targets and so
# must not appear in the table or the finished tally. "Random" has no completion
# target, so counting it would permanently undercount "N / total finished". The
# monitor already drops "Any" before writing database.json, so it never reaches
# this reader in practice; it is listed here for clarity and to stay correct if
# the page is ever pointed at a raw/hand-edited file.
NON_FARMABLE_CHARACTERS = frozenset({"Any", "Random"})

# Health states derived from the open incidents in notification_state.json.
# Ordered most-severe first: AUTH_EXPIRED and API_DOWN both blind monitoring;
# STUCK means the farm stalled. swap_needed / low_quota are not health states
# (STATUS_PAGE_PROPOSAL.md section 2 enumerates exactly OK / STUCK / API DOWN / AUTH
# EXPIRED). UNKNOWN is rendered when notification_state.json is missing/corrupt
# (a healthy-unknown, never an error).
AUTH_EXPIRED_KIND = "auth_expired"
API_DOWN_KIND = "api_down"
STUCK_FARM_KIND = "stuck_farm"
SWAP_NEEDED_KIND = "swap_needed"


def _read_json(path: Path) -> Any | None:
    """Read and parse a JSON file, returning ``None`` if it is missing or
    unreadable. A torn read mid-write surfaces as a JSON error and is treated
    the same as missing (the monitor writes atomically via os.replace, M1, so
    this is a narrow window)."""
    try:
        with path.open(encoding="utf-8") as file:
            return json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Could not read %s (%s).", path, exc.__class__.__name__)
        return None


def load_status_data(
    database_path: Path = DATABASE_FILE,
    state_path: Path = NOTIFICATION_STATE_FILE,
) -> tuple[Any, Any]:
    """Read both state files from disk. Either element is ``None`` when its
    file is missing or corrupt."""
    return _read_json(database_path), _read_json(state_path)


def _build_character_rows(database: Any) -> list[dict[str, Any]]:
    """Turn the ``{name: battle_count}`` database into display rows, sorted
    unfinished-first then by descending battle count (the in-progress character
    surfaces at the top), then finished characters alphabetically."""
    if not isinstance(database, dict):
        return []
    rows: list[dict[str, Any]] = []
    for name, count in database.items():
        if name in NON_FARMABLE_CHARACTERS:
            continue
        try:
            battle_count = int(count)
        except TypeError, ValueError:  # PEP 758 multi-except (Python 3.14 target)
            # A corrupt/hand-edited row whose value is not int-like: skip it
            # rather than failing the whole page.
            continue
        finished = battle_count >= FINISHED_THRESHOLD
        rows.append(
            {
                "name": str(name),
                "battle_count": battle_count,
                "finished": finished,
                # 0-100 fill for the progress bar; finished characters clamp to
                # 100 even though their raw count can exceed it.
                "progress": max(0, min(battle_count, FINISHED_THRESHOLD)),
            }
        )
    rows.sort(
        key=lambda row: (
            row["finished"],
            0 if row["finished"] else -row["battle_count"],
            row["name"],
        )
    )
    return rows


def _derive_health(
    state: Any, last_change_at: float | None, now: float
) -> dict[str, Any]:
    """Derive the single health line from the open incidents."""
    if not isinstance(state, dict):
        # No monitor state to read: healthy-unknown, not an error.
        return {"status": "UNKNOWN", "label": "Unknown (no monitor state)"}

    incidents = state.get("incidents")
    if not isinstance(incidents, dict):
        incidents = {}

    if AUTH_EXPIRED_KIND in incidents:
        return {"status": "AUTH_EXPIRED", "label": "AUTH EXPIRED"}
    if API_DOWN_KIND in incidents:
        return {"status": "API_DOWN", "label": "API DOWN"}
    if STUCK_FARM_KIND in incidents:
        stuck_minutes = _stuck_minutes(incidents[STUCK_FARM_KIND], last_change_at, now)
        label = f"STUCK {stuck_minutes} min" if stuck_minutes is not None else "STUCK"
        return {"status": "STUCK", "label": label, "stuck_minutes": stuck_minutes}
    return {"status": "OK", "label": "OK"}


def _derive_swap_needed(state: Any) -> dict[str, str] | None:
    """Extract the actionable swap-needed incident, if it is well-formed."""
    if not isinstance(state, dict):
        return None
    incidents = state.get("incidents")
    if not isinstance(incidents, dict):
        return None
    incident = incidents.get(SWAP_NEEDED_KIND)
    if not isinstance(incident, dict):
        return None
    character = incident.get("character")
    if not isinstance(character, str) or not character:
        return None
    return {"character": character}


def _stuck_minutes(
    incident: Any, last_change_at: float | None, now: float
) -> int | None:
    """Whole minutes the farm has been stuck - the staleness since the last
    battle-count change. Falls back to the incident's ``opened_at`` if
    last_change_at is unavailable."""
    reference = last_change_at
    if reference is None and isinstance(incident, dict):
        opened_at = incident.get("opened_at")
        if isinstance(opened_at, (int, float)):
            reference = float(opened_at)
    if reference is None:
        return None
    return int(max(0.0, now - reference) // 60)


def _parse_last_change_at(state: Any) -> float | None:
    if not isinstance(state, dict):
        return None
    raw = state.get("last_change_at")
    if isinstance(raw, (int, float)) and raw > 0:
        return float(raw)
    return None


def build_status(database: Any, state: Any, now: float) -> dict[str, Any]:
    """Assemble the ``/api/status`` payload from the (possibly missing/corrupt)
    parsed contents of the two state files. Never raises on bad input - missing
    or malformed data degrades to empty rows / healthy-unknown."""
    characters = _build_character_rows(database)
    finished_count = sum(1 for row in characters if row["finished"])

    last_change_at = _parse_last_change_at(state)
    if last_change_at is not None:
        seconds_since = max(0.0, now - last_change_at)
        time_since = humanize.naturaltime(timedelta(seconds=seconds_since))
    else:
        seconds_since = None
        time_since = None

    return {
        "generated_at": now,
        "health": _derive_health(state, last_change_at, now),
        "swap_needed": _derive_swap_needed(state),
        "last_change_at": last_change_at,
        "seconds_since_last_change": seconds_since,
        "time_since_last_change": time_since,
        "finished_count": finished_count,
        "total_count": len(characters),
        "characters": characters,
    }


# Static data-URI favicon; kept URL-encoded so the Python source stays ASCII.
FAVICON_HREF = (
    "data:image/svg+xml,"
    "%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 64 64'%3E"
    "%3Crect width='64' height='64' rx='12' fill='%231976d2'/%3E"
    "%3Cpath d='M18 20h28v8H18zM18 36h20v8H18z' fill='white'/%3E"
    "%3C/svg%3E"
)

# Single self-contained page. Vanilla JS re-fetches /api/status every 30 s and
# re-renders; character names are written via textContent (never innerHTML), so
# nothing from the data files can inject markup. Kept ASCII-only (the checkmark
# is a CSS \2713 escape) so the source has no encoding surprises.
PAGE_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SF6 Afk Farm Monitor</title>
<link rel="icon" href="__FAVICON_HREF__">
<script>
(function () {
  let theme = "light";
  try {
    const stored = localStorage.getItem("sf6-status-theme");
    if (stored === "dark" || stored === "light") {
      theme = stored;
    } else if (window.matchMedia &&
               window.matchMedia("(prefers-color-scheme: dark)").matches) {
      theme = "dark";
    }
  } catch (err) {
    // Storage can be unavailable in private modes; keep the light default.
  }
  document.documentElement.dataset.theme = theme;
})();
</script>
<style>
  :root {
    color-scheme: light;
    --bg: #f4f5f7;
    --text: #1a1a1a;
    --muted: #555;
    --column-label: #666;
    --border: #ddd;
    --row-border: #eee;
    --row-hover: #e9edf2;
    --bar-bg: #e0e0e0;
    --fill: #1976d2;
    --finished-fill: #2e7d32;
    --finished-text: #2e7d32;
    --footer: #666;
    --toggle-bg: #fff;
    --toggle-border: #c9ced6;
    --toggle-hover-bg: #e9edf2;
    --toggle-icon: #1976d2;
    --health-ok-bg: #e6f4ea;
    --health-ok-text: #1e7e34;
    --health-stuck-bg: #fff4e5;
    --health-stuck-text: #a15c00;
    --health-down-bg: #fdecea;
    --health-down-text: #b71c1c;
    --health-unknown-bg: #eceff1;
    --health-unknown-text: #546e7a;
  }
  :root[data-theme="dark"] {
    color-scheme: dark;
    --bg: #1a1b1e;
    --text: #c1c2c5;
    --muted: #a6a7ab;
    --column-label: #909296;
    --border: #373a40;
    --row-border: #2c2e33;
    --row-hover: #25262b;
    --bar-bg: #373a40;
    --fill: #1971c2;
    --finished-fill: #2b8a3e;
    --finished-text: #8ce99a;
    --footer: #909296;
    --toggle-bg: #25262b;
    --toggle-border: #373a40;
    --toggle-hover-bg: #2c2e33;
    --toggle-icon: #fab005;
    --health-ok-bg: #1d3b27;
    --health-ok-text: #8ce99a;
    --health-stuck-bg: #3b2f18;
    --health-stuck-text: #ffd43b;
    --health-down-bg: #3b1f24;
    --health-down-text: #ffa8a8;
    --health-unknown-bg: #2c2e33;
    --health-unknown-text: #c1c2c5;
  }
  * { box-sizing: border-box; }
  body { font-family: system-ui, -apple-system, "Segoe UI", Arial, sans-serif;
         margin: 0; background: var(--bg); color: var(--text); }
  .wrap { max-width: 720px; margin: 0 auto; padding: 1.25rem 1rem; }
  .header { display: flex; align-items: center; justify-content: space-between;
            gap: 1rem; margin-bottom: 0.35rem; }
  .header-actions { align-items: center; display: flex; flex: 0 0 auto;
                    flex-wrap: wrap; gap: 0.5rem; justify-content: flex-end; }
  h1 { font-size: 1.3rem; margin: 0; }
  .theme-toggle { border: 1px solid var(--toggle-border);
                  background: var(--toggle-bg); color: var(--text);
                  border-radius: 999px; cursor: pointer; flex: 0 0 auto;
                  display: inline-grid; height: 2rem; place-items: center;
                  padding: 0; width: 2rem; }
  .theme-toggle:hover { background: var(--toggle-hover-bg); }
  .theme-icon { color: var(--toggle-icon); display: block; grid-area: 1 / 1;
                height: 1rem; width: 1rem; }
  .sun-icon { display: none; }
  :root[data-theme="dark"] .moon-icon { display: none; }
  :root[data-theme="dark"] .sun-icon { display: block; }
  .health { display: inline-block; font-weight: 600; padding: 0.35rem 0.8rem;
            border-radius: 999px; font-size: 0.95rem; }
  .health.ok { background: var(--health-ok-bg); color: var(--health-ok-text); }
  .health.stuck { background: var(--health-stuck-bg);
                  color: var(--health-stuck-text); }
  .health.down, .health.auth { background: var(--health-down-bg);
                               color: var(--health-down-text); }
  .health.unknown { background: var(--health-unknown-bg);
                    color: var(--health-unknown-text); }
  .meta-row { align-items: baseline; display: flex; gap: 1rem;
              justify-content: space-between; margin-bottom: 0.55rem; }
  .meta { color: var(--muted); font-size: 0.9rem; margin: 0; }
  .refresh-status { color: var(--footer); font-size: 0.8rem;
                    white-space: nowrap; }
  .staleness { margin-left: auto; text-align: right; }
  table { width: 100%; border-collapse: collapse; margin-top: 0; }
  th { text-align: left; font-size: 0.8rem; text-transform: uppercase;
       letter-spacing: 0.03em; color: var(--column-label);
       background: var(--bg); border-bottom: 1px solid var(--border);
       padding: 0 0.45rem 0.3rem; position: sticky; top: 0; }
  td { padding: 0.22rem 0.45rem; border-bottom: 1px solid var(--row-border); }
  tbody tr:hover td { background: var(--row-hover); }
  .empty-row td { color: var(--muted); font-style: italic; text-align: center; }
  td.bar-cell { width: 55%; }
  .count { text-align: right; font-variant-numeric: tabular-nums;
           white-space: nowrap; }
  .bar { background: var(--bar-bg); border-radius: 4px; height: 12px;
         overflow: hidden; }
  .fill { height: 100%; background: var(--fill); transition: width 0.3s; }
  tr.finished .fill { background: var(--finished-fill); }
  tr.finished td.name::after { content: " \\2713"; color: var(--finished-text); }
  @media (max-width: 560px) {
    .header { align-items: flex-start; }
    .meta-row { display: block; margin-bottom: 0.5rem; }
    .staleness { margin-top: 0.15rem; text-align: left; }
  }
</style>
</head>
<body>
<div class="wrap">
  <div class="header">
    <h1>SF6 Afk Farm Monitor</h1>
    <div class="header-actions">
      <span id="refresh-status" class="refresh-status">Refreshes every 30s</span>
      <div id="swap-needed" class="health stuck" role="status" hidden></div>
      <div id="health" class="health unknown" role="status">Loading...</div>
      <button id="theme-toggle" class="theme-toggle" type="button"
              aria-label="Toggle color scheme" aria-pressed="false"
              title="Toggle color scheme">
        <svg class="theme-icon moon-icon" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round"
             stroke-linejoin="round" aria-hidden="true">
          <path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"></path>
        </svg>
        <svg class="theme-icon sun-icon" viewBox="0 0 24 24" fill="none"
             stroke="currentColor" stroke-width="2" stroke-linecap="round"
             stroke-linejoin="round" aria-hidden="true">
          <circle cx="12" cy="12" r="4"></circle>
          <path d="M12 2v2"></path><path d="M12 20v2"></path>
          <path d="m4.9 4.9 1.4 1.4"></path>
          <path d="m17.7 17.7 1.4 1.4"></path>
          <path d="M2 12h2"></path><path d="M20 12h2"></path>
          <path d="m4.9 19.1 1.4-1.4"></path>
          <path d="m17.7 6.3 1.4-1.4"></path>
        </svg>
      </button>
    </div>
  </div>
  <div class="meta-row">
    <p class="meta" id="tally"></p>
    <p class="meta staleness" id="staleness">
      <span>Last battle-count change: </span><span id="staleness-value">unknown</span>
    </p>
  </div>
  <table>
    <thead><tr>
      <th>Character</th><th class="bar-cell">Progress to __FINISHED_THRESHOLD__</th><th class="count">Battles</th>
    </tr></thead>
    <tbody id="rows"></tbody>
  </table>
</div>
<script>
const BASE_TITLE = "SF6 Afk Farm Monitor";
const ALERT_TITLE_PREFIX = "\\u26A0 ";
const HEALTH_CLASS = { OK: "ok", STUCK: "stuck", API_DOWN: "down",
                       AUTH_EXPIRED: "auth", UNKNOWN: "unknown" };
const THEME_STORAGE_KEY = "sf6-status-theme";
let hasRenderedStatus = false;
let stalenessClock = null;

function currentTheme() {
  return document.documentElement.dataset.theme === "dark" ? "dark" : "light";
}

function setTheme(theme, persist) {
  document.documentElement.dataset.theme = theme;
  const toggle = document.getElementById("theme-toggle");
  if (toggle) {
    const isDark = theme === "dark";
    toggle.setAttribute("aria-pressed", String(isDark));
    toggle.setAttribute(
      "aria-label",
      isDark ? "Switch to light mode" : "Switch to dark mode"
    );
    toggle.title = isDark ? "Switch to light mode" : "Switch to dark mode";
  }
  if (persist) {
    try {
      localStorage.setItem(THEME_STORAGE_KEY, theme);
    } catch (err) {
      // The visual toggle still works when storage is blocked.
    }
  }
}

function setupThemeToggle() {
  const toggle = document.getElementById("theme-toggle");
  if (!toggle) return;
  setTheme(currentTheme(), false);
  toggle.addEventListener("click", () => {
    setTheme(currentTheme() === "dark" ? "light" : "dark", true);
  });
}

function plural(value, unit) {
  return value + " " + unit + (value === 1 ? "" : "s") + " ago";
}

function formatElapsedSeconds(seconds) {
  const elapsed = Math.max(0, Math.floor(seconds));
  if (elapsed < 5) return "just now";
  if (elapsed < 60) return plural(elapsed, "second");
  if (elapsed < 3600) return plural(Math.floor(elapsed / 60), "minute");
  if (elapsed < 86400) return plural(Math.floor(elapsed / 3600), "hour");
  return plural(Math.floor(elapsed / 86400), "day");
}

function updateStalenessLine() {
  const stalenessValue = document.getElementById("staleness-value");
  if (!stalenessValue) return;
  let text = "unknown";
  if (!stalenessClock) {
    if (stalenessValue.textContent !== text) {
      stalenessValue.textContent = text;
    }
    return;
  }
  const elapsed = stalenessClock.secondsAtFetch +
                  (Date.now() - stalenessClock.fetchedAtMs) / 1000;
  text = formatElapsedSeconds(elapsed);
  if (stalenessValue.textContent !== text) {
    stalenessValue.textContent = text;
  }
}

async function refresh() {
  try {
    const res = await fetch("/api/status", { cache: "no-store" });
    if (!res.ok) throw new Error("HTTP " + res.status);
    render(await res.json());
  } catch (err) {
    if (!hasRenderedStatus) {
      const health = document.getElementById("health");
      health.className = "health down";
      health.textContent = "Status unavailable";
      document.title = ALERT_TITLE_PREFIX + BASE_TITLE;
    }
    document.getElementById("refresh-status").textContent =
      "Last fetch failed: " + err.message;
  }
}

function updateDocumentTitle(data) {
  const healthStatus = data.health.status;
  const actionable = data.swap_needed ||
                     (healthStatus !== "OK" && healthStatus !== "UNKNOWN");
  document.title = (actionable ? ALERT_TITLE_PREFIX : "") + BASE_TITLE;
}

function render(data) {
  hasRenderedStatus = true;
  updateDocumentTitle(data);

  const swapNeeded = document.getElementById("swap-needed");
  if (data.swap_needed) {
    const swapLabel = "SWAP NEEDED: " + data.swap_needed.character;
    swapNeeded.hidden = false;
    if (swapNeeded.textContent !== swapLabel) {
      swapNeeded.textContent = swapLabel;
    }
  } else {
    swapNeeded.hidden = true;
    if (swapNeeded.textContent !== "") {
      swapNeeded.textContent = "";
    }
  }

  const health = document.getElementById("health");
  const healthClass = "health " + (HEALTH_CLASS[data.health.status] || "unknown");
  if (health.className !== healthClass) {
    health.className = healthClass;
  }
  if (health.textContent !== data.health.label) {
    health.textContent = data.health.label;
  }

  document.getElementById("tally").textContent =
    data.finished_count + " / " + data.total_count + " characters finished";
  stalenessClock = Number.isFinite(data.seconds_since_last_change) ?
    {
      secondsAtFetch: data.seconds_since_last_change,
      fetchedAtMs: Date.now(),
    } :
    null;
  updateStalenessLine();

  const rows = document.getElementById("rows");
  rows.textContent = "";
  if (data.characters.length === 0) {
    const tr = document.createElement("tr");
    tr.className = "empty-row";
    const empty = document.createElement("td");
    empty.colSpan = 3;
    empty.textContent =
      "No character data yet \\u2014 is the monitor running? (expects data/database.json)";
    tr.appendChild(empty);
    rows.appendChild(tr);
  }
  for (const character of data.characters) {
    const tr = document.createElement("tr");
    if (character.finished) tr.className = "finished";

    const name = document.createElement("td");
    name.className = "name";
    name.textContent = character.name;

    const barCell = document.createElement("td");
    barCell.className = "bar-cell";
    const bar = document.createElement("div");
    bar.className = "bar";
    bar.setAttribute("aria-hidden", "true");
    const fill = document.createElement("div");
    fill.className = "fill";
    fill.style.width = character.progress + "%";
    bar.appendChild(fill);
    barCell.appendChild(bar);

    const count = document.createElement("td");
    count.className = "count";
    count.textContent = character.battle_count;

    tr.append(name, barCell, count);
    rows.appendChild(tr);
  }
  document.getElementById("refresh-status").textContent = "Refreshes every 30s";
}

setupThemeToggle();
refresh();
setInterval(updateStalenessLine, 1000);
setInterval(refresh, 30000);
</script>
</body>
</html>
""".replace("__FAVICON_HREF__", FAVICON_HREF).replace(
    "__FINISHED_THRESHOLD__", str(FINISHED_THRESHOLD)
)
PAGE_BYTES = PAGE_HTML.encode("utf-8")


class StatusRequestHandler(BaseHTTPRequestHandler):
    """Serves the HTML status page and the status JSON."""

    def do_GET(self) -> None:
        """Serve the status page, JSON status, or a not-found response."""
        self._handle_request()

    def do_HEAD(self) -> None:
        """Serve the same headers as GET without writing a response body."""
        self._handle_request(head=True)

    def _handle_request(self, head: bool = False) -> None:
        route = self.path.split("?", 1)[0]
        if route == "/":
            self._serve_html(head=head)
        elif route == "/api/status":
            self._serve_status_json(head=head)
        else:
            self._send_not_found(head=head)

    def _serve_html(self, head: bool = False) -> None:
        self._send_bytes(200, "text/html; charset=utf-8", PAGE_BYTES, head=head)

    def _serve_status_json(self, head: bool = False) -> None:
        database, state = load_status_data()
        payload = build_status(database, state, time.time())
        self._send_bytes(
            200,
            "application/json; charset=utf-8",
            json.dumps(payload, indent=2).encode("utf-8"),
            head=head,
        )

    def _send_not_found(self, head: bool = False) -> None:
        self._send_bytes(404, "text/plain; charset=utf-8", b"Not found\n", head=head)

    def _send_bytes(
        self, status: int, content_type: str, body: bytes, head: bool = False
    ) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # State changes at most once per polling interval; never cache.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        if not head:
            self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        """Route HTTP access messages through the application logger."""
        logger.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    """Run the LAN-accessible status server until interrupted."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # Only status_page_port is read; no other config value is ever served.
    port = load_config().status_page_port
    # Bind 0.0.0.0 for LAN access (decided, section 4). The first LAN request
    # triggers a Windows Firewall prompt - allow on Private networks.
    server = ThreadingHTTPServer(("0.0.0.0", port), StatusRequestHandler)
    logger.info(
        "Status page serving on http://0.0.0.0:%s (LAN-accessible). "
        "Press Ctrl+C to stop.",
        port,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        logger.info("Status page shutting down.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
