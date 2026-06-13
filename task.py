import json
import logging.config
import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path

import humanize
from requests import HTTPError
from sortedcontainers import SortedDict  # type: ignore[import-untyped]

from api_service import AuthExpiredError, get_character_win_rates
from config import ConfigData
from incident_manager import IncidentManager
from paths import DATA_DIR
from utilities import truncated_database

logger = logging.getLogger(__name__)

DATABASE_FILENAME = DATA_DIR / "database.json"

AUTH_EXPIRED_MESSAGE = (
    "Buckler session expired — refresh buckler_id / buckler_r_id / "
    "buckler_praise_date in config.toml. All monitoring is blind until then."
)


def write_to_database(
    data: Mapping[str, int], database_filename: str | Path = DATABASE_FILENAME
) -> None:
    database_path = Path(database_filename)
    temporary_database_path = database_path.with_name(f"{database_path.name}.tmp")
    with temporary_database_path.open("w", encoding="utf-8") as file:
        json_string = json.dumps(data, indent=2)
        file.write(json_string)
        file.write("\n")

    os.replace(temporary_database_path, database_path)

    # sort_database_by_value(database_path)
    truncated_database(database_path)


def read_database(database_filename: str | Path) -> dict[str, int] | None:
    database_path = Path(database_filename)
    try:
        with database_path.open(encoding="utf-8") as file:
            data = json.load(file)
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning(
            "Could not read %s (%s); treating it as first init.",
            database_path,
            exc.__class__.__name__,
        )
        return None

    if not isinstance(data, dict):
        logger.error(
            "%s did not contain a JSON object; treating it as first init.",
            database_path,
        )
        return None

    try:
        return {
            str(character_name): int(battle_count)
            for character_name, battle_count in data.items()
        }
    except (TypeError, ValueError) as exc:
        logger.warning(
            "%s contained invalid battle counts (%s); treating it as first init.",
            database_path,
            exc.__class__.__name__,
        )
        return None


def do_task(
    config: ConfigData,
    incident_manager: IncidentManager,
    database_filename: str | Path = DATABASE_FILENAME,
) -> None:
    incident_manager.retry_pending_cancels()

    try:
        win_rate_response = get_character_win_rates(config)
    except AuthExpiredError:
        # Expired cookies are actionable and blind all monitoring; an emergency
        # incident nags until the user refreshes them (review finding M3).
        logger.error(AUTH_EXPIRED_MESSAGE, exc_info=True)
        incident_manager.evaluate_auth_expired(
            active=True, build_message=lambda: AUTH_EXPIRED_MESSAGE
        )
        return
    except HTTPError:
        message = "Capcom Buckler website down?"
        logger.error(message, exc_info=True)
        incident_manager.evaluate_api_down(active=True, down_message=message)
        return
    except Exception:
        message = "Caught generic Exception. This isn't an HTTPError? Capcom Buckler website must be completely borked."
        logger.error(message, exc_info=True)
        incident_manager.evaluate_api_down(active=True, down_message=message)
        return

    # The poll succeeded: clear any open api_down / auth_expired incident.
    incident_manager.evaluate_api_down(active=False)
    incident_manager.evaluate_auth_expired(
        active=False, build_message=lambda: AUTH_EXPIRED_MESSAGE
    )

    current_character_to_battle_count: SortedDict[str, int] = SortedDict()
    for character_win_rate in win_rate_response.character_win_rates:
        if character_win_rate.character_name == "Any":
            continue
        current_character_to_battle_count[character_win_rate.character_name] = (
            character_win_rate.battle_count
        )

    # On first init, we don't have any previous data.
    database_path = Path(database_filename)
    if not database_path.exists():
        write_to_database(current_character_to_battle_count, database_path)
        incident_manager.record_change()
        return

    # Compare current data with previous data
    previous_character_to_battle_count = read_database(database_path)
    if previous_character_to_battle_count is None:
        write_to_database(current_character_to_battle_count, database_path)
        incident_manager.record_change()
        return

    data_differs = False
    increased_characters: list[str] = []
    crossed_threshold: list[str] = []
    for character, current_battle_count in current_character_to_battle_count.items():
        if character not in previous_character_to_battle_count:
            logger.warning("Found a new character: %s", character)
            data_differs = True
            continue
        previous_battle_count = previous_character_to_battle_count[character]
        if current_battle_count == previous_battle_count:
            continue
        data_differs = True
        logger.info(
            "Character (%s) has a new battle count: %s -> %s",
            character,
            previous_battle_count,
            current_battle_count,
        )
        if current_battle_count > previous_battle_count:
            increased_characters.append(character)
            if previous_battle_count < 100 <= current_battle_count:
                logger.info("Finished Master color reward for character: %s", character)
                crossed_threshold.append(character)

    # Update database with current data. last_change_at (owned by the incident
    # manager) is the stuck-timer source, replacing the database.json mtime
    # check (retires review finding M10).
    if data_differs:
        incident_manager.record_change()
        write_to_database(current_character_to_battle_count, database_path)

    stuck = incident_manager.seconds_since_last_change() >= config.battle_count_timeout

    def build_stuck_message() -> str:
        duration = timedelta(seconds=incident_manager.seconds_since_last_change())
        return f"It has been ({humanize.precisedelta(duration)}) without an update. The afk farm might be stuck."

    incident_manager.evaluate_stuck_farm(
        active=stuck, build_message=build_stuck_message
    )

    # Master-color swap incident (§7): a character crossing 100 opens an
    # emergency incident that nags until a *different* character starts gaining
    # counts (the swap happened). Replaces the per-match re-fire from ffb650b.
    def build_swap_message(character: str) -> str:
        return (
            f"Finished Master color reward for character: {character}. "
            "Swap to a different character to keep earning rewards."
        )

    incident_manager.evaluate_swap_needed(
        increased_characters=increased_characters,
        crossed_characters=crossed_threshold,
        build_message=build_swap_message,
    )
