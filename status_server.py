"""Local LAN status page — a read-only view of farm progress.

A **separate process** from the monitor (``app.py``): it never imports the
monitor's task/scheduler code and the monitor never imports it. It only *reads*
``data/database.json`` and ``data/notification_state.json`` from disk on each
request (both are tiny, so there is no caching) and renders the current farm
state. Because it only reads those two files, it can crash, hang, or be absent
with zero effect on monitoring (STATUS_PAGE_PROPOSAL.md §3).

Endpoint:

- ``GET /api/status`` — the assembled state as JSON.

(``GET /`` serves the HTML view; added in the next commit.)

Security: the page is LAN-only by design, no auth (§5). It serves *only* the
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

# Health states derived from the open incidents in notification_state.json.
# Ordered most-severe first: AUTH_EXPIRED and API_DOWN both blind monitoring;
# STUCK means the farm stalled. swap_needed / low_quota are not health states
# (STATUS_PAGE_PROPOSAL.md §2 enumerates exactly OK / STUCK / API DOWN / AUTH
# EXPIRED). UNKNOWN is rendered when notification_state.json is missing/corrupt
# (a healthy-unknown, never an error).
AUTH_EXPIRED_KIND = "auth_expired"
API_DOWN_KIND = "api_down"
STUCK_FARM_KIND = "stuck_farm"


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
    surfaces at the top), with the name as a stable tiebreak."""
    if not isinstance(database, dict):
        return []
    rows: list[dict[str, Any]] = []
    for name, count in database.items():
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
    rows.sort(key=lambda row: (row["finished"], -row["battle_count"], row["name"]))
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


def _stuck_minutes(
    incident: Any, last_change_at: float | None, now: float
) -> int | None:
    """Whole minutes the farm has been stuck — the staleness since the last
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
    parsed contents of the two state files. Never raises on bad input — missing
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
        "last_change_at": last_change_at,
        "seconds_since_last_change": seconds_since,
        "time_since_last_change": time_since,
        "finished_count": finished_count,
        "total_count": len(characters),
        "characters": characters,
    }


class StatusRequestHandler(BaseHTTPRequestHandler):
    """Serves the status JSON and (added in the HTML-view commit) the page."""

    def do_GET(self) -> None:  # noqa: N802 (BaseHTTPRequestHandler API)
        route = self.path.split("?", 1)[0]
        if route == "/api/status":
            self._serve_status_json()
        else:
            self._send_not_found()

    def _serve_status_json(self) -> None:
        database, state = load_status_data()
        payload = build_status(database, state, time.time())
        self._send_bytes(
            200,
            "application/json; charset=utf-8",
            json.dumps(payload, indent=2).encode("utf-8"),
        )

    def _send_not_found(self) -> None:
        self._send_bytes(404, "text/plain; charset=utf-8", b"Not found\n")

    def _send_bytes(self, status: int, content_type: str, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # State changes at most once per polling interval; never cache.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        # Route the default stderr access log through logging instead.
        logger.info("%s - %s", self.address_string(), format % args)


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )
    # Only status_page_port is read; no other config value is ever served.
    port = load_config().status_page_port
    # Bind 0.0.0.0 for LAN access (decided, §4). The first LAN request triggers
    # a Windows Firewall prompt — allow on Private networks.
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
