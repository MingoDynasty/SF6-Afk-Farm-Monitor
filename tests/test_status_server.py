"""Tests for the status-page assembly logic (status_server.py).

These cover the parts the proposal calls out: unfinished-first sorting, the
finished tally, health derivation from incident state, and graceful handling of
missing/corrupt state files (a missing notification_state.json must render as
healthy-unknown, never a 500).
"""

import json

from status_server import build_status, load_status_data

NOW = 1_000_000.0


def _database(**counts: int) -> dict[str, int]:
    return dict(counts)


def _state(incidents: dict | None = None, last_change_at: float | None = NOW) -> dict:
    state: dict = {"incidents": incidents or {}, "pending_cancel": []}
    if last_change_at is not None:
        state["last_change_at"] = last_change_at
    return state


# -- character rows / sorting ------------------------------------------------


def test_character_rows_sorted_unfinished_first_then_descending() -> None:
    database = _database(Ken=16, Luke=106, Manon=103, Marisa=12, Ryu=0)
    status = build_status(database, _state(), NOW)
    order = [row["name"] for row in status["characters"]]
    # Unfinished (<100) first, by descending count; finished (>=100) after.
    assert order == ["Ken", "Marisa", "Ryu", "Luke", "Manon"]


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
    # Only the four enumerated states are health states (§2).
    incidents = {"swap_needed": {"opened_at": NOW}, "low_quota": {"opened_at": NOW}}
    status = build_status(_database(Ken=16), _state(incidents=incidents), NOW)
    assert status["health"]["status"] == "OK"


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
