"""Tests for the status-page assembly logic (status_server.py).

These cover the parts the proposal calls out: unfinished-first sorting, the
finished tally, health derivation from incident state, and graceful handling of
missing/corrupt state files (a missing notification_state.json must render as
healthy-unknown, never a 500).
"""

import http.client
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from http.server import ThreadingHTTPServer
from typing import cast

import status_server
from status_server import (
    PAGE_HTML,
    StatusRequestHandler,
    build_status,
    load_status_data,
)

NOW = 1_000_000.0


def _database(**counts: int) -> dict[str, int]:
    return dict(counts)


def _state(incidents: dict | None = None, last_change_at: float | None = NOW) -> dict:
    state: dict = {"incidents": incidents or {}, "pending_cancel": []}
    if last_change_at is not None:
        state["last_change_at"] = last_change_at
    return state


@contextmanager
def _run_status_server() -> Iterator[tuple[str, int]]:
    server = ThreadingHTTPServer(("127.0.0.1", 0), StatusRequestHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield cast(tuple[str, int], server.server_address)
    finally:
        server.shutdown()
        thread.join(timeout=5)
        server.server_close()


def _http_request(
    address: tuple[str, int], method: str, path: str
) -> tuple[int, dict[str, str], bytes]:
    connection = http.client.HTTPConnection(*address, timeout=5)
    try:
        connection.request(method, path)
        response = connection.getresponse()
        body = response.read()
        return response.status, dict(response.getheaders()), body
    finally:
        connection.close()


def test_status_page_includes_dark_mode_toggle() -> None:
    assert 'id="theme-toggle"' in PAGE_HTML
    assert 'aria-label="Toggle color scheme"' in PAGE_HTML
    assert "header-actions" in PAGE_HTML
    assert "meta-row" in PAGE_HTML
    assert 'id="refresh-status"' in PAGE_HTML
    assert 'id="staleness-value"' in PAGE_HTML
    assert "moon-icon" in PAGE_HTML
    assert "sun-icon" in PAGE_HTML
    assert "data-theme" in PAGE_HTML
    assert "sf6-status-theme" in PAGE_HTML
    assert "tbody tr:hover td" in PAGE_HTML
    assert ".count { text-align: right" in PAGE_HTML
    assert "td.count { text-align: right" not in PAGE_HTML
    assert '"Refreshes every 30s"' in PAGE_HTML
    assert "toLocaleTimeString" not in PAGE_HTML
    assert 'id="footer"' not in PAGE_HTML
    assert 'getElementById("footer")' not in PAGE_HTML
    assert "setInterval(updateStalenessLine, 1000)" in PAGE_HTML


def test_status_page_includes_pr1_ux_hooks() -> None:
    assert 'rel="icon"' in PAGE_HTML
    assert "data:image/svg+xml" in PAGE_HTML
    assert 'id="swap-needed"' in PAGE_HTML
    assert 'role="status"' in PAGE_HTML
    assert "SWAP NEEDED: " in PAGE_HTML
    assert "Progress to 100" in PAGE_HTML
    assert "No character data yet \\u2014 is the monitor running?" in PAGE_HTML
    assert "empty.colSpan = 3" in PAGE_HTML
    assert 'bar.setAttribute("aria-hidden", "true")' in PAGE_HTML
    assert 'fill.setAttribute("aria-hidden", "true")' in PAGE_HTML
    assert 'const ALERT_TITLE_PREFIX = "\\u26A0 "' in PAGE_HTML
    assert (
        'document.title = (actionable ? ALERT_TITLE_PREFIX : "") + BASE_TITLE'
        in PAGE_HTML
    )
    assert "--footer: #666;" in PAGE_HTML
    assert "--column-label: #666;" in PAGE_HTML
    assert "position: sticky" in PAGE_HTML
    assert "background: var(--bg)" in PAGE_HTML
    assert "innerHTML" not in PAGE_HTML


# -- character rows / sorting ------------------------------------------------


def test_character_rows_sorted_unfinished_first_then_finished_by_name() -> None:
    database = _database(Ken=16, Luke=103, Marisa=12, Ryu=0, Zangief=106)
    status = build_status(database, _state(), NOW)
    order = [row["name"] for row in status["characters"]]
    # Unfinished (<100) first, by descending count; finished (>=100) by name.
    assert order == ["Ken", "Marisa", "Ryu", "Luke", "Zangief"]


def test_rows_with_equal_count_break_ties_by_name() -> None:
    database = _database(Zangief=5, Akuma=5, Mai=5)
    status = build_status(database, _state(), NOW)
    assert [row["name"] for row in status["characters"]] == ["Akuma", "Mai", "Zangief"]


def test_finished_tally_and_totals() -> None:
    database = _database(Ken=16, Luke=106, Manon=103, Marisa=12, Ryu=0)
    status = build_status(database, _state(), NOW)
    assert status["finished_count"] == 2
    assert status["total_count"] == 5


def test_progress_clamped_to_100_for_finished_character() -> None:
    status = build_status(_database(Luke=106), _state(), NOW)
    luke = status["characters"][0]
    assert luke["finished"] is True
    assert luke["progress"] == 100
    assert luke["battle_count"] == 106


def test_exactly_100_counts_as_finished() -> None:
    status = build_status(_database(Cammy=100), _state(), NOW)
    assert status["characters"][0]["finished"] is True
    assert status["finished_count"] == 1


def test_invalid_battle_count_row_is_skipped() -> None:
    # A non-integer value (corrupt row) is dropped rather than crashing.
    status = build_status({"Ken": 16, "Broken": "oops"}, _state(), NOW)
    assert [row["name"] for row in status["characters"]] == ["Ken"]


def test_non_farmable_characters_excluded_from_rows_and_tally() -> None:
    # "Random" has no Master-color target, so it must not appear in the table
    # or inflate the denominator. "Any" never reaches the file (the monitor
    # strips it) but is excluded defensively the same way.
    database = {"Random": 0, "Any": 5, "Ken": 16, "Luke": 106}
    status = build_status(database, _state(), NOW)
    names = [row["name"] for row in status["characters"]]
    assert names == ["Ken", "Luke"]
    assert status["total_count"] == 2
    assert status["finished_count"] == 1


# -- health derivation -------------------------------------------------------


def test_health_ok_when_no_incidents() -> None:
    status = build_status(_database(Ken=16), _state(incidents={}), NOW)
    assert status["health"]["status"] == "OK"
    assert status["health"]["label"] == "OK"


def test_health_stuck_reports_minutes_since_last_change() -> None:
    # last_change_at was 7 minutes (420 s) ago.
    state = _state(
        incidents={"stuck_farm": {"opened_at": NOW}}, last_change_at=NOW - 420
    )
    status = build_status(_database(Ken=16), state, NOW)
    assert status["health"]["status"] == "STUCK"
    assert status["health"]["label"] == "STUCK 7 min"
    assert status["health"]["stuck_minutes"] == 7


def test_health_api_down() -> None:
    state = _state(incidents={"api_down": {"opened_at": NOW}})
    status = build_status(_database(Ken=16), state, NOW)
    assert status["health"]["status"] == "API_DOWN"
    assert status["health"]["label"] == "API DOWN"


def test_health_auth_expired() -> None:
    state = _state(incidents={"auth_expired": {"opened_at": NOW}})
    status = build_status(_database(Ken=16), state, NOW)
    assert status["health"]["status"] == "AUTH_EXPIRED"
    assert status["health"]["label"] == "AUTH EXPIRED"


def test_health_priority_auth_over_api_over_stuck() -> None:
    incidents = {
        "stuck_farm": {"opened_at": NOW},
        "api_down": {"opened_at": NOW},
        "auth_expired": {"opened_at": NOW},
    }
    status = build_status(_database(Ken=16), _state(incidents=incidents), NOW)
    assert status["health"]["status"] == "AUTH_EXPIRED"


def test_swap_and_low_quota_incidents_do_not_change_health() -> None:
    # Only the four enumerated states are health states (section 2).
    incidents = {"swap_needed": {"opened_at": NOW}, "low_quota": {"opened_at": NOW}}
    status = build_status(_database(Ken=16), _state(incidents=incidents), NOW)
    assert status["health"]["status"] == "OK"


def test_swap_needed_emitted_for_valid_incident_without_affecting_health() -> None:
    incidents = {"swap_needed": {"opened_at": NOW, "character": "Ryu"}}
    status = build_status(_database(Ryu=100), _state(incidents=incidents), NOW)
    assert status["swap_needed"] == {"character": "Ryu"}
    assert status["health"]["status"] == "OK"


def test_swap_needed_is_null_when_absent_or_malformed() -> None:
    states = [
        _state(),
        _state(incidents={"swap_needed": {}}),
        _state(incidents={"swap_needed": {"character": 123}}),
        _state(incidents={"swap_needed": "Ryu"}),
        {"incidents": []},
        ["not", "a", "dict"],
    ]
    for state in states:
        assert build_status(_database(Ryu=100), state, NOW)["swap_needed"] is None


def test_stuck_minutes_falls_back_to_opened_at_without_last_change() -> None:
    # No last_change_at, but the stuck incident has been open 5 minutes.
    state = {"incidents": {"stuck_farm": {"opened_at": NOW - 300}}}
    status = build_status(_database(Ken=16), state, NOW)
    assert status["health"]["label"] == "STUCK 5 min"


# -- staleness line ----------------------------------------------------------


def test_time_since_last_change_is_humanized() -> None:
    status = build_status(_database(Ken=16), _state(last_change_at=NOW - 200), NOW)
    assert status["seconds_since_last_change"] == 200
    assert status["time_since_last_change"] == "3 minutes ago"
    assert status["last_change_at"] == NOW - 200


# -- missing / corrupt state -------------------------------------------------


def test_missing_state_file_renders_healthy_unknown() -> None:
    # The whole point: a missing notification_state.json must not 500.
    status = build_status(_database(Ken=16), None, NOW)
    assert status["health"]["status"] == "UNKNOWN"
    assert status["last_change_at"] is None
    assert status["seconds_since_last_change"] is None
    assert status["time_since_last_change"] is None
    # The character table still renders.
    assert status["total_count"] == 1


def test_corrupt_state_not_a_dict_renders_unknown() -> None:
    status = build_status(_database(Ken=16), ["not", "a", "dict"], NOW)
    assert status["health"]["status"] == "UNKNOWN"


def test_missing_database_yields_empty_table() -> None:
    status = build_status(None, _state(), NOW)
    assert status["characters"] == []
    assert status["finished_count"] == 0
    assert status["total_count"] == 0
    # Health still derives from the (present) state.
    assert status["health"]["status"] == "OK"


def test_corrupt_database_not_a_dict_yields_empty_table() -> None:
    status = build_status(["not", "a", "dict"], _state(), NOW)
    assert status["characters"] == []


def test_build_status_never_raises_on_all_missing() -> None:
    status = build_status(None, None, NOW)
    assert status["characters"] == []
    assert status["health"]["status"] == "UNKNOWN"


# -- file loading ------------------------------------------------------------


def test_load_status_data_returns_none_for_missing_files(tmp_path) -> None:
    database, state = load_status_data(
        tmp_path / "database.json", tmp_path / "notification_state.json"
    )
    assert database is None
    assert state is None


def test_load_status_data_returns_none_for_corrupt_json(tmp_path) -> None:
    bad = tmp_path / "database.json"
    bad.write_text("{ not valid json", encoding="utf-8")
    good = tmp_path / "notification_state.json"
    good.write_text(json.dumps({"incidents": {}}), encoding="utf-8")
    database, state = load_status_data(bad, good)
    assert database is None
    assert state == {"incidents": {}}


# -- HTTP handler ------------------------------------------------------------


def test_head_root_matches_get_headers_and_has_empty_body() -> None:
    with _run_status_server() as address:
        get_status, get_headers, get_body = _http_request(address, "GET", "/")
        head_status, head_headers, head_body = _http_request(address, "HEAD", "/")

    assert get_status == 200
    assert head_status == 200
    assert head_body == b""
    assert head_headers["Content-Type"] == get_headers["Content-Type"]
    assert head_headers["Content-Length"] == get_headers["Content-Length"]
    assert int(head_headers["Content-Length"]) == len(get_body)


def test_head_api_status_matches_get_headers_and_has_empty_body(monkeypatch) -> None:
    monkeypatch.setattr(
        status_server,
        "load_status_data",
        lambda: (_database(Ken=16), _state()),
    )
    monkeypatch.setattr(status_server.time, "time", lambda: NOW)

    with _run_status_server() as address:
        get_status, get_headers, get_body = _http_request(address, "GET", "/api/status")
        head_status, head_headers, head_body = _http_request(
            address, "HEAD", "/api/status"
        )

    assert get_status == 200
    assert head_status == 200
    assert head_body == b""
    assert head_headers["Content-Type"] == get_headers["Content-Type"]
    assert head_headers["Content-Length"] == get_headers["Content-Length"]
    assert int(head_headers["Content-Length"]) == len(get_body)


def test_head_unknown_route_returns_404_without_body() -> None:
    with _run_status_server() as address:
        get_status, _, get_body = _http_request(address, "GET", "/missing")
        head_status, head_headers, head_body = _http_request(
            address, "HEAD", "/missing"
        )

    assert get_status == 404
    assert get_body == b"Not found\n"
    assert head_status == 404
    assert head_body == b""
    assert head_headers["Content-Length"] == str(len(get_body))
