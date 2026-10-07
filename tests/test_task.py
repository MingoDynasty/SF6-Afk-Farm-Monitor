import json
import logging
import os
from collections.abc import Callable
from pathlib import Path

import pytest
import requests
from conftest import FakeClock, FakePushoverClient
from requests import HTTPError

import task
from api_service import AuthExpiredError
from config import ConfigData
from incident_manager import (
    API_DOWN,
    AUTH_EXPIRED,
    AUTH_EXPIRED_TAG,
    LOW_QUOTA,
    STUCK_FARM,
    SWAP_NEEDED,
    SWAP_NEEDED_TAG,
    IncidentManager,
)
from model import CharacterWinRate, WinRateResponse
from task import CharacterProgress


def make_response(character_counts: dict[str, int]) -> WinRateResponse:
    return WinRateResponse(
        character_win_rates=[
            CharacterWinRate(
                battle_count=battle_count,
                character_id=index,
                win_count=0,
                character_name=character_name,
                character_alpha=character_name.lower(),
                character_tool_name=character_name.lower(),
                character_sort=index,
            )
            for index, (character_name, battle_count) in enumerate(
                character_counts.items(), start=1
            )
        ]
    )


def write_database(
    database_path: Path,
    battle_counts: dict[str, int],
    points: dict[str, int] | None = None,
) -> None:
    """Persist progress in the monitor's format.

    Points mirror the battle counts unless given, which is the usual case: a
    battle awards a point.
    """
    points = battle_counts if points is None else points
    data = {
        name: {"battle_count": battle_count, "point": points[name]}
        for name, battle_count in battle_counts.items()
    }
    database_path.write_text(json.dumps(data), encoding="utf-8")


def read_database(database_path: Path) -> dict[str, dict[str, int]]:
    return json.loads(database_path.read_text(encoding="utf-8"))


def read_battle_counts(database_path: Path) -> dict[str, int]:
    return {
        name: progress["battle_count"]
        for name, progress in read_database(database_path).items()
    }


def read_points(database_path: Path) -> dict[str, int]:
    return {
        name: progress["point"]
        for name, progress in read_database(database_path).items()
    }


def run_task_with_response(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    incident_manager: IncidentManager,
    database_path: Path,
    win_rate_response: WinRateResponse,
    points: dict[str, int] | None = None,
) -> None:
    """Run one poll against a faked Buckler.

    Master Pass points mirror the battle counts unless ``points`` names them
    per character; a character left out of ``points`` has no Master Pass track.
    """

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        return win_rate_response

    def fake_get_master_pass_points(config: ConfigData) -> dict[int, int]:
        return {
            row.character_id: row.battle_count
            if points is None
            else points[row.character_name]
            for row in win_rate_response.character_win_rates
            if points is None or row.character_name in points
        }

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)
    monkeypatch.setattr(task, "get_master_pass_points", fake_get_master_pass_points)

    task.do_task(config_data, incident_manager, database_path)


def build_manager(
    fake_client: FakePushoverClient,
    config: ConfigData,
    fake_clock: FakeClock,
    tmp_path: Path,
) -> IncidentManager:
    state_path = tmp_path / "notification_state.json"
    return IncidentManager(fake_client, config, state_path, clock=fake_clock)


def test_new_character_counts_as_difference_and_is_persisted(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Ryu": 10})

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Ryu": 10, "Akuma": 1}),
    )

    assert read_battle_counts(database_path) == {"Akuma": 1, "Ryu": 10}
    assert fake_client.sent == []


def test_threshold_crossing_opens_swap_needed_incident(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Juri": 99})

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100}),
    )

    assert read_battle_counts(database_path) == {"Juri": 100}
    # The 99 -> 100 crossing now opens a swap_needed emergency incident
    # (replaces the ffb650b per-match re-fire).
    assert SWAP_NEEDED in manager.incidents
    assert manager.incidents[SWAP_NEEDED]["character"] == "Juri"
    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 2
    assert fake_client.sent[0]["tags"] == SWAP_NEEDED_TAG
    assert "Juri" in fake_client.sent[0]["message"]


def test_swap_needed_stays_open_and_silent_while_same_character_gains(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Juri": 99})

    # Crossing opens the incident.
    run_task_with_response(
        monkeypatch, config_data, manager, database_path, make_response({"Juri": 100})
    )
    receipt = manager.incidents[SWAP_NEEDED]["receipt"]
    fake_client.receipt_info[receipt] = {"acknowledged": 0}

    # Continued matches on the *same* finished character: OPEN and silent.
    for count in (101, 102, 103):
        fake_clock.advance(60)
        run_task_with_response(
            monkeypatch,
            config_data,
            manager,
            database_path,
            make_response({"Juri": count}),
        )

    assert SWAP_NEEDED in manager.incidents
    assert len(fake_client.sent) == 1


def test_swap_needed_closes_when_different_character_increases(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Juri": 99, "Cammy": 5})

    # Juri crosses 100 -> incident opens (finished character = Juri).
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100, "Cammy": 5}),
    )
    receipt = manager.incidents[SWAP_NEEDED]["receipt"]
    assert SWAP_NEEDED in manager.incidents

    # The user swaps: a *different* character (Cammy) starts gaining -> CLOSED,
    # receipt cancelled. (Juri also still gaining the same poll must not block
    # the close.)
    fake_clock.advance(60)
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 101, "Cammy": 6}),
    )

    assert SWAP_NEEDED not in manager.incidents
    assert fake_client.cancelled == [receipt]


def test_swap_needed_waits_for_the_points_not_the_battle_count(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Juri": 99})

    # The 100th battle awards no point. The reward is still locked, so paging
    # for a swap now would strand Juri one point short.
    fake_clock.advance(60)
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100}),
        points={"Juri": 99},
    )

    assert SWAP_NEEDED not in manager.incidents
    assert fake_client.sent == []
    assert read_database(database_path) == {"Juri": {"battle_count": 100, "point": 99}}
    # The battle still counts as farm progress.
    assert manager.last_change_at == fake_clock()
    assert read_last_increased_characters(tmp_path) == ["Juri"]

    # The next battle awards the 100th point.
    fake_clock.advance(60)
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 101}),
        points={"Juri": 100},
    )

    assert manager.incidents[SWAP_NEEDED]["character"] == "Juri"
    assert len(fake_client.sent) == 1


def test_points_crossing_without_a_battle_count_change_opens_swap_needed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    # A long timeout keeps the stuck incident out of this test's two polls.
    config_data = make_config(battle_count_timeout=600)
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Juri": 99})
    last_change_at = manager.last_change_at

    # Buckler has been seen to report a match's point one poll before its
    # battle count. The crossing is on this poll and would be gone by the next.
    fake_clock.advance(60)
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 99}),
        points={"Juri": 100},
    )

    assert manager.incidents[SWAP_NEEDED]["character"] == "Juri"
    assert read_points(database_path) == {"Juri": 100}
    # Only a battle count moves the stuck timer.
    assert manager.last_change_at == last_change_at

    # The battle count catching up is the same character: open and silent.
    fake_clock.advance(60)
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100}),
        points={"Juri": 100},
    )

    assert SWAP_NEEDED in manager.incidents
    assert len(fake_client.sent) == 1


def test_points_crossing_is_kept_while_the_previous_swap_is_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    # A long timeout keeps the stuck incident out of this test's flat poll.
    config_data = make_config(battle_count_timeout=600)
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Juri": 99, "Cammy": 99})

    # Juri finishes: her swap incident opens.
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100, "Cammy": 99}),
    )
    juri_receipt = manager.incidents[SWAP_NEEDED]["receipt"]

    # The user swaps onto Cammy, one match from done. Buckler reports that
    # match's point a poll before its battle count, so no character has gained
    # a battle here. Cammy's crossing must not be dropped behind Juri's
    # incident: it is saved to the database on this poll and never seen again.
    fake_clock.advance(60)
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100, "Cammy": 99}),
        points={"Juri": 100, "Cammy": 100},
    )

    assert manager.incidents[SWAP_NEEDED]["character"] == "Cammy"
    assert fake_client.cancelled == [juri_receipt]

    # Cammy's battle count catches up. Her incident stays, with no second alert.
    fake_clock.advance(60)
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100, "Cammy": 100}),
    )

    assert manager.incidents[SWAP_NEEDED]["character"] == "Cammy"
    assert [sent["tags"] for sent in fake_client.sent] == [SWAP_NEEDED_TAG] * 2
    assert "Cammy" in fake_client.sent[1]["message"]


def open_legacy_swap_incident(
    manager: IncidentManager, fake_client: FakePushoverClient, character: str
) -> str:
    """Open a swap incident the way the monitor did under the 100-battle rule.

    Returns its receipt.
    """
    manager.evaluate_swap_needed(
        increased_characters=[character],
        crossed_characters=[character],
        build_message=lambda name: (
            f"Finished Master color reward for character: {name}."
        ),
    )
    receipt = manager.incidents[SWAP_NEEDED]["receipt"]
    assert isinstance(receipt, str)
    return receipt


def test_upgrade_withdraws_a_swap_alert_the_points_do_not_support(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config(battle_count_timeout=600)
    database_path = tmp_path / "database.json"
    # The old monitor paged for Juri at 100 battles, and stored bare counts.
    old_manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    legacy_receipt = open_legacy_swap_incident(old_manager, fake_client, "Juri")
    database_path.write_text(json.dumps({"Juri": 100, "Cammy": 5}), encoding="utf-8")

    # The upgraded monitor starts and loads that incident from disk.
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    manager.reconcile_on_startup()
    assert manager.incidents[SWAP_NEEDED]["character"] == "Juri"

    # Its first poll finds Juri one point short: her reward is still locked, so
    # the alert telling the user to swap away from her has to go.
    fake_clock.advance(60)
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100, "Cammy": 5}),
        points={"Juri": 99, "Cammy": 5},
    )

    assert SWAP_NEEDED not in manager.incidents
    assert fake_client.cancelled == [legacy_receipt]
    assert len(fake_client.sent) == 1

    # The alert comes back when the reward really unlocks.
    fake_clock.advance(60)
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 101, "Cammy": 5}),
        points={"Juri": 100, "Cammy": 5},
    )

    assert manager.incidents[SWAP_NEEDED]["character"] == "Juri"
    assert len(fake_client.sent) == 2


def test_upgrade_keeps_a_swap_alert_the_points_support(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config(battle_count_timeout=600)
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    legacy_receipt = open_legacy_swap_incident(manager, fake_client, "Juri")
    database_path.write_text(json.dumps({"Juri": 100, "Cammy": 5}), encoding="utf-8")

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100, "Cammy": 5}),
    )

    # Juri has her 100 points, so the alert was right and stays as it was.
    assert manager.incidents[SWAP_NEEDED]["receipt"] == legacy_receipt
    assert fake_client.cancelled == []
    assert len(fake_client.sent) == 1


def test_first_poll_without_a_database_withdraws_an_unsupported_swap_alert(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config(battle_count_timeout=600)
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    # The incident outlived the database (the file was deleted before upgrading).
    legacy_receipt = open_legacy_swap_incident(manager, fake_client, "Juri")

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100}),
        points={"Juri": 99},
    )

    assert SWAP_NEEDED not in manager.incidents
    assert fake_client.cancelled == [legacy_receipt]


def test_character_without_a_master_pass_track_is_stored_with_zero_points(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Ryu": 5, "Random": 1}),
        points={"Ryu": 5},
    )

    assert read_database(database_path) == {
        "Random": {"battle_count": 1, "point": 0},
        "Ryu": {"battle_count": 5, "point": 5},
    }


def test_database_from_before_points_is_rewritten_as_first_init(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    # The shape the monitor wrote before it stored points.
    database_path.write_text(json.dumps({"Juri": 99, "Cammy": 5}), encoding="utf-8")

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100, "Cammy": 5}),
    )

    assert read_database(database_path) == {
        "Cammy": {"battle_count": 5, "point": 5},
        "Juri": {"battle_count": 100, "point": 100},
    }
    # The upgrade poll has no usable previous counts to diff against, so it
    # alerts on nothing, like any first init.
    assert manager.incidents == {}
    assert fake_client.sent == []


@pytest.mark.parametrize(
    ("exception", "incident"),
    [
        (AuthExpiredError("Buckler returned HTTP 403"), AUTH_EXPIRED),
        (HTTPError("HTTP 502"), API_DOWN),
        (ValueError("Buckler returned no open Master Pass for season 12"), API_DOWN),
    ],
)
def test_master_pass_failure_fails_the_poll_like_a_win_rate_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    exception: Exception,
    incident: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Juri": 99})

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        return make_response({"Juri": 100})

    def fake_get_master_pass_points(config: ConfigData) -> dict[int, int]:
        raise exception

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)
    monkeypatch.setattr(task, "get_master_pass_points", fake_get_master_pass_points)

    # Two polls: an outage is only confirmed on the second consecutive failure.
    task.do_task(config_data, manager, database_path)
    task.do_task(config_data, manager, database_path)

    assert list(manager.incidents) == [incident]
    # Without points the poll knows nothing about rewards, so it records nothing.
    assert read_battle_counts(database_path) == {"Juri": 99}


def test_stuck_detection_opens_emergency_incident(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config(battle_count_timeout=60)
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Manon": 42})

    # No count change, and last_change_at is now older than the timeout.
    fake_clock.advance(120)

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Manon": 42}),
    )

    assert read_battle_counts(database_path) == {"Manon": 42}
    # Stuck farm is an emergency incident.
    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 2
    assert "afk farm might be stuck" in fake_client.sent[0]["message"]
    assert STUCK_FARM in manager.incidents


def test_count_change_resets_stuck_timer(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config(battle_count_timeout=60)
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Manon": 42})

    # Plenty of elapsed time, but the count changes this poll, so the timer
    # resets and no stuck incident opens.
    fake_clock.advance(120)

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Manon": 43}),
    )

    assert read_battle_counts(database_path) == {"Manon": 43}
    assert fake_client.sent == []
    assert STUCK_FARM not in manager.incidents


def read_last_increased_characters(tmp_path: Path) -> list[str]:
    state = json.loads((tmp_path / "notification_state.json").read_text("utf-8"))
    return state["last_increased_characters"]


def test_gaining_character_is_recorded_for_the_status_page(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    # Mai has the higher count, but Elena is the one being farmed.
    write_database(database_path, {"Elena": 14, "Mai": 92})

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Elena": 15, "Mai": 92}),
    )

    assert read_last_increased_characters(tmp_path) == ["Elena"]


def test_poll_without_an_increase_keeps_the_recorded_characters(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Elena": 14, "Mai": 92})
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Elena": 15, "Mai": 92}),
    )

    # A flat poll, then a phase reset that drops every count to 0: neither has
    # a gaining character, so neither says who is farming.
    for counts in ({"Elena": 15, "Mai": 92}, {"Elena": 0, "Mai": 0}):
        fake_clock.advance(60)
        run_task_with_response(
            monkeypatch, config_data, manager, database_path, make_response(counts)
        )

    assert read_battle_counts(database_path) == {"Elena": 0, "Mai": 0}
    assert read_last_increased_characters(tmp_path) == ["Elena"]


def test_two_gaining_characters_are_both_recorded(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Ryu": 100, "Ingrid": 0, "Ken": 3})

    # The monitor was off across a swap, so one poll sees both characters gain.
    # It records the fact and leaves the tie to the status page.
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Ryu": 101, "Ingrid": 59, "Ken": 3}),
    )

    assert read_last_increased_characters(tmp_path) == ["Ryu", "Ingrid"]


def test_corrupt_database_is_replaced_from_current_response(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    database_path.write_text("{", encoding="utf-8")

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Chun-Li": 7}),
    )

    assert read_battle_counts(database_path) == {"Chun-Li": 7}
    assert fake_client.sent == []


def test_api_failure_opens_api_down_incident(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        raise RuntimeError("boom")

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    # One failed poll is unconfirmed; the second consecutive one opens it.
    task.do_task(config_data, manager, database_path)
    assert fake_client.sent == []

    task.do_task(config_data, manager, database_path)

    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 1


def test_single_failed_poll_is_one_warning_and_no_alert(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Mai": 58})

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        raise HTTPError("502 Server Error: Bad Gateway for url: https://example.test")

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    with caplog.at_level(logging.DEBUG):
        task.do_task(config_data, manager, database_path)
        fake_clock.advance(60)
        # The blip is gone by the next poll, which also carries a new count.
        run_task_with_response(
            monkeypatch, config_data, manager, database_path, make_response({"Mai": 59})
        )

    loud_records = [
        record for record in caplog.records if record.levelno >= logging.WARNING
    ]
    assert len(loud_records) == 1
    assert loud_records[0].exc_info is None
    assert "502 Server Error" in loud_records[0].getMessage()
    assert fake_client.sent == []
    state = json.loads((tmp_path / "notification_state.json").read_text("utf-8"))
    assert API_DOWN not in state["incidents"]
    # The missed poll loses nothing: the next one diffs against the last write.
    assert read_battle_counts(database_path) == {"Mai": 59}


@pytest.mark.parametrize(
    "exception",
    [
        HTTPError("HTTP 500"),
        requests.ConnectionError("no route to host"),
        requests.Timeout("read timed out"),
    ],
)
def test_outage_errors_open_api_down_not_auth_expired(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    exception: Exception,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        raise exception

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    task.do_task(config_data, manager, database_path)
    assert API_DOWN not in manager.incidents

    task.do_task(config_data, manager, database_path)

    assert API_DOWN in manager.incidents
    assert AUTH_EXPIRED not in manager.incidents
    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 1


@pytest.mark.parametrize(
    "exception",
    [
        HTTPError("502 Server Error: Bad Gateway for url: https://example.test"),
        requests.ConnectionError("no route to host"),
        requests.Timeout("read timed out"),
    ],
)
def test_upstream_failure_logs_one_warning_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    caplog: pytest.LogCaptureFixture,
    exception: Exception,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        raise exception

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    with caplog.at_level(logging.DEBUG):
        task.do_task(config_data, manager, tmp_path / "database.json")

    task_records = [record for record in caplog.records if record.name == "task"]
    assert len(task_records) == 1
    assert task_records[0].levelno == logging.WARNING
    assert task_records[0].exc_info is None
    assert type(exception).__name__ in task_records[0].getMessage()
    assert str(exception) in task_records[0].getMessage()


def test_auth_expiry_logs_one_error_without_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    exception = AuthExpiredError(
        "Buckler returned HTTP 403 (session cookies expired?)."
    )

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        raise exception

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    with caplog.at_level(logging.DEBUG):
        task.do_task(config_data, manager, tmp_path / "database.json")

    task_records = [record for record in caplog.records if record.name == "task"]
    assert len(task_records) == 1
    assert task_records[0].levelno == logging.ERROR
    assert task_records[0].exc_info is None
    assert task.AUTH_EXPIRED_MESSAGE in task_records[0].getMessage()
    assert str(exception) in task_records[0].getMessage()


def test_unexpected_failure_still_logs_a_traceback(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        raise RuntimeError("boom")

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    with caplog.at_level(logging.DEBUG):
        task.do_task(config_data, manager, tmp_path / "database.json")

    task_records = [record for record in caplog.records if record.name == "task"]
    assert len(task_records) == 1
    assert task_records[0].levelno == logging.ERROR
    assert task_records[0].exc_info is not None


def test_unexpected_failure_message_is_logged_and_pushed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        raise RuntimeError("boom")

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    with caplog.at_level(logging.DEBUG):
        task.do_task(config_data, manager, database_path)
        fake_clock.advance(60)
        task.do_task(config_data, manager, database_path)

    task_records = [record for record in caplog.records if record.name == "task"]
    assert [record.getMessage() for record in task_records] == [
        task.UNEXPECTED_FAILURE_MESSAGE,
        task.UNEXPECTED_FAILURE_MESSAGE,
    ]
    assert all(record.exc_info is not None for record in task_records)
    # The second consecutive failure confirms the incident: one push, same text.
    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 1
    assert fake_client.sent[0]["message"] == task.UNEXPECTED_FAILURE_MESSAGE


def test_auth_expired_opens_emergency_incident(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"

    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        raise AuthExpiredError("cookies expired")

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)

    task.do_task(config_data, manager, database_path)

    # Emergency incident (priority 2) with its own tag, not an api_down.
    assert AUTH_EXPIRED in manager.incidents
    assert API_DOWN not in manager.incidents
    assert len(fake_client.sent) == 1
    assert fake_client.sent[0]["priority"] == 2
    assert fake_client.sent[0]["tags"] == AUTH_EXPIRED_TAG


def test_successful_poll_closes_open_auth_expired_incident(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Ryu": 1})

    # An auth_expired incident is already open from a previous failed poll.
    manager.evaluate_auth_expired(active=True, build_message=lambda: "refresh cookies")
    receipt = manager.incidents[AUTH_EXPIRED]["receipt"]

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Ryu": 1}),
    )

    # First successful poll closes the incident and cancels the receipt.
    assert AUTH_EXPIRED not in manager.incidents
    assert fake_client.cancelled == [receipt]


def test_swap_needed_reopens_for_character_crossing_while_closing(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    # Both characters sit one match from Master color.
    write_database(database_path, {"Juri": 99, "Cammy": 99})

    # Juri 99 -> 100 opens the swap incident (finished character = Juri).
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100, "Cammy": 99}),
    )
    juri_receipt = manager.incidents[SWAP_NEEDED]["receipt"]

    # Next poll: the user has swapped onto Cammy, who crosses 100 the same poll
    # that closes Juri's incident. The crossing must not be lost.
    fake_clock.advance(60)
    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Juri": 100, "Cammy": 100}),
    )

    assert SWAP_NEEDED in manager.incidents
    assert manager.incidents[SWAP_NEEDED]["character"] == "Cammy"
    assert fake_client.cancelled == [juri_receipt]


def test_do_task_opens_low_quota_incident_when_remaining_below_floor(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    fake_client: FakePushoverClient,
    fake_clock: FakeClock,
    make_config: Callable[..., ConfigData],
) -> None:
    monkeypatch.chdir(tmp_path)
    config_data = make_config()
    manager = build_manager(fake_client, config_data, fake_clock, tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Ryu": 1})

    # A prior Pushover call this session reported a low remaining count.
    fake_client.last_remaining = 400

    run_task_with_response(
        monkeypatch,
        config_data,
        manager,
        database_path,
        make_response({"Ryu": 1}),
    )

    assert LOW_QUOTA in manager.incidents


def test_write_to_database_uses_atomic_replace(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / "database.json"
    real_replace = os.replace
    replace_calls: list[tuple[Path, Path]] = []

    def record_replace(source: str | Path, destination: str | Path) -> None:
        replace_calls.append((Path(source), Path(destination)))
        real_replace(source, destination)

    monkeypatch.setattr(task.os, "replace", record_replace)

    task.write_to_database({"Ryu": CharacterProgress(1, 0)}, database_path)

    temporary_database_path = database_path.with_name("database.json.tmp")
    assert replace_calls == [(temporary_database_path, database_path)]
    assert not temporary_database_path.exists()
    assert read_database(database_path) == {"Ryu": {"battle_count": 1, "point": 0}}


def test_write_to_database_sorts_keys_alphabetically(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The on-disk file used to be alphabetical because it was built from a
    # SortedDict; after L8 dropped sortedcontainers, json.dumps(sort_keys=True)
    # must preserve that ordering. Insertion order here is deliberately not
    # alphabetical so the assertion proves sorting, not luck.
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / "database.json"

    task.write_to_database(
        {
            "Ryu": CharacterProgress(1, 1),
            "Akuma": CharacterProgress(2, 2),
            "Cammy": CharacterProgress(3, 3),
        },
        database_path,
    )

    # json.loads preserves the file's key order, so this reflects on-disk order.
    written = json.loads(database_path.read_text(encoding="utf-8"))
    assert list(written.keys()) == ["Akuma", "Cammy", "Ryu"]
