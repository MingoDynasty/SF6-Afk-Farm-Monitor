import json
import os
import time
from collections.abc import Generator
from pathlib import Path

import pytest

import task
from config import ConfigData
from model import CharacterWinRate, WinRateResponse


@pytest.fixture(autouse=True)
def clear_notification_queue() -> Generator[None]:
    task.notifications_to_send.clear()
    yield
    task.notifications_to_send.clear()


@pytest.fixture
def config_data() -> ConfigData:
    return ConfigData(
        user_code=1234567890,
        target_season_id=12,
        polling_interval=60,
        battle_count_timeout=60,
        buckler_id="buckler-id",
        buckler_r_id="buckler-r-id",
        buckler_praise_date=1234567890123,
        pushover_enabled=False,
        pushover_app_key="app-key",
        pushover_user_key="user-key",
    )


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


def write_database(database_path: Path, data: dict[str, int]) -> None:
    database_path.write_text(json.dumps(data), encoding="utf-8")


def read_database(database_path: Path) -> dict[str, int]:
    return json.loads(database_path.read_text(encoding="utf-8"))


def run_task_with_response(
    monkeypatch: pytest.MonkeyPatch,
    config_data: ConfigData,
    database_path: Path,
    win_rate_response: WinRateResponse,
    sent_messages: list[str],
) -> None:
    def fake_get_character_win_rates(config: ConfigData) -> WinRateResponse:
        return win_rate_response

    def fake_send_message(message: str, config: ConfigData) -> None:
        sent_messages.append(message)

    monkeypatch.setattr(task, "get_character_win_rates", fake_get_character_win_rates)
    monkeypatch.setattr(task, "send_message", fake_send_message)

    task.do_task(config_data, database_path)


def test_new_character_counts_as_difference_and_is_persisted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_data: ConfigData
) -> None:
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Ryu": 10})
    sent_messages: list[str] = []

    run_task_with_response(
        monkeypatch,
        config_data,
        database_path,
        make_response({"Ryu": 10, "Akuma": 1}),
        sent_messages,
    )

    assert read_database(database_path) == {"Akuma": 1, "Ryu": 10}
    assert sent_messages == []


def test_threshold_crossing_sends_master_color_notification(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_data: ConfigData
) -> None:
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Juri": 99})
    sent_messages: list[str] = []

    run_task_with_response(
        monkeypatch,
        config_data,
        database_path,
        make_response({"Juri": 100}),
        sent_messages,
    )

    assert read_database(database_path) == {"Juri": 100}
    assert sent_messages == ["Finished Master color reward for character: Juri"]


def test_stuck_detection_sends_notification_when_counts_do_not_change(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_data: ConfigData
) -> None:
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / "database.json"
    write_database(database_path, {"Manon": 42})
    old_timestamp = time.time() - 120
    os.utime(database_path, (old_timestamp, old_timestamp))
    sent_messages: list[str] = []

    run_task_with_response(
        monkeypatch,
        config_data,
        database_path,
        make_response({"Manon": 42}),
        sent_messages,
    )

    assert read_database(database_path) == {"Manon": 42}
    assert len(sent_messages) == 1
    assert "afk farm might be stuck" in sent_messages[0]


def test_corrupt_database_is_replaced_from_current_response(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, config_data: ConfigData
) -> None:
    monkeypatch.chdir(tmp_path)
    database_path = tmp_path / "database.json"
    database_path.write_text("{", encoding="utf-8")
    sent_messages: list[str] = []

    run_task_with_response(
        monkeypatch,
        config_data,
        database_path,
        make_response({"Chun-Li": 7}),
        sent_messages,
    )

    assert read_database(database_path) == {"Chun-Li": 7}
    assert sent_messages == []


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

    task.write_to_database({"Ryu": 1}, database_path)

    temporary_database_path = database_path.with_name("database.json.tmp")
    assert replace_calls == [(temporary_database_path, database_path)]
    assert not temporary_database_path.exists()
    assert read_database(database_path) == {"Ryu": 1}
