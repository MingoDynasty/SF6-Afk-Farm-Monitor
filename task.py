"""Poll Buckler, persist character progress, and drive monitor incidents."""

import json
import logging
import os
from collections.abc import Mapping
from datetime import timedelta
from pathlib import Path
from typing import NamedTuple

import humanize
from requests import RequestException

from api_service import (
    AuthExpiredError,
    get_character_win_rates,
    get_master_pass_points,
)
from config import ConfigData
from incident_manager import IncidentManager
from paths import DATA_DIR

logger = logging.getLogger(__name__)

DATABASE_FILENAME = DATA_DIR / "database.json"

# A character's Master color reward unlocks at 100 Master Pass points; crossing
# this threshold is what opens a swap_needed incident. Battle count usually
# equals the points but can run a battle or two ahead (a battle that awards no
# point), so it is not the trigger. The status page uses the same number for
# its finished/progress display (status_server.FINISHED_THRESHOLD).
MASTER_COLOR_THRESHOLD = 100

# "Any" is the Buckler "all characters" aggregate row, not a real character, so
# it must be filtered out of the per-character logic: its total is always large,
# so left in it reads as a permanently-finished character that would prematurely
# close swap incidents and open bogus ones. ("Random" is inert at ~0 and stays
# in database.json; it is filtered only by the status-page view, by decision.)
AGGREGATE_CHARACTER = "Any"

AUTH_EXPIRED_MESSAGE = (
    "Buckler session expired — run `uv run python login.py` to re-capture cookies, "
    "then restart the monitor. All monitoring is blind until then."
)

UNEXPECTED_FAILURE_MESSAGE = (
    "Unexpected error while polling Buckler. This was not a network "
    "failure, so the response format may have changed or the monitor "
    "has a bug. Check logs/info.log."
)


class CharacterProgress(NamedTuple):
    """One character's battle count and Master Pass points at a poll."""

    battle_count: int
    point: int


def write_to_database(
    data: Mapping[str, CharacterProgress],
    database_filename: str | Path = DATABASE_FILENAME,
) -> None:
    """Atomically persist the latest per-character progress."""
    database_path = Path(database_filename)
    temporary_database_path = database_path.with_name(f"{database_path.name}.tmp")
    with temporary_database_path.open("w", encoding="utf-8") as file:
        # sort_keys keeps the on-disk file alphabetical (previously achieved by
        # building a SortedDict; review finding L8 dropped that dependency).
        json_string = json.dumps(
            {name: progress._asdict() for name, progress in data.items()},
            indent=2,
            sort_keys=True,
        )
        file.write(json_string)
        file.write("\n")

    os.replace(temporary_database_path, database_path)


def read_database(
    database_filename: str | Path,
) -> dict[str, CharacterProgress] | None:
    """Load persisted character progress, returning None when unusable."""
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

    # A file from before Master Pass points were stored maps each name to a
    # bare battle count. It fails here like any other unusable file, and the
    # poll rewrites it in the current shape.
    try:
        return {
            str(character_name): CharacterProgress(
                battle_count=int(progress["battle_count"]),
                point=int(progress["point"]),
            )
            for character_name, progress in data.items()
        }
    except (TypeError, ValueError, KeyError) as exc:
        logger.warning(
            "%s contained invalid character progress (%s); treating it as first init.",
            database_path,
            exc.__class__.__name__,
        )
        return None


def do_task(  # noqa: PLR0912, PLR0915  # Keep the monitor poll sequence linear.
    config: ConfigData,
    incident_manager: IncidentManager,
    database_filename: str | Path = DATABASE_FILENAME,
) -> None:
    """Poll Buckler once and reconcile persisted state and notifications."""
    incident_manager.retry_pending_cancels()

    try:
        win_rate_response = get_character_win_rates(config)
        master_pass_points = get_master_pass_points(config)
    except AuthExpiredError as exc:
        # Expired cookies are actionable and blind all monitoring; an emergency
        # incident nags until the user refreshes them (review finding M3). The
        # condition is already classified, so it gets one line, not a traceback;
        # the exception text says which signal fired.
        logger.error("%s (%s)", AUTH_EXPIRED_MESSAGE, exc)
        incident_manager.evaluate_auth_expired(
            active=True, build_message=lambda: AUTH_EXPIRED_MESSAGE
        )
        return
    except RequestException as exc:
        # An upstream failure (5xx, connection error, timeout) is expected from
        # Buckler now and then, so it gets one line instead of a traceback that
        # reads like a bug here. api_service already logged any response body
        # at DEBUG.
        logger.warning("Buckler poll failed (%s): %s", type(exc).__name__, exc)
        incident_manager.evaluate_api_down(
            active=True, down_message="Capcom Buckler website down?"
        )
        return
    except Exception:
        # Request failures were handled above, so this is a response that no
        # longer validates or a bug here; the traceback says which.
        logger.exception(UNEXPECTED_FAILURE_MESSAGE)
        incident_manager.evaluate_api_down(
            active=True, down_message=UNEXPECTED_FAILURE_MESSAGE
        )
        return

    # The poll succeeded: clear any open api_down / auth_expired incident.
    incident_manager.evaluate_api_down(active=False)
    incident_manager.evaluate_auth_expired(
        active=False, build_message=lambda: AUTH_EXPIRED_MESSAGE
    )

    # The win-rate roster supplies the names; the Master Pass has only IDs.
    current_character_to_progress: dict[str, CharacterProgress] = {}
    for character_win_rate in win_rate_response.character_win_rates:
        if character_win_rate.character_name == AGGREGATE_CHARACTER:
            continue
        current_character_to_progress[character_win_rate.character_name] = (
            CharacterProgress(
                battle_count=character_win_rate.battle_count,
                # A roster entry with no Master Pass track ("Random") has no
                # points to earn.
                point=master_pass_points.get(character_win_rate.character_id, 0),
            )
        )

    # A poll with no usable previous data has nothing to diff, so it cannot
    # open a swap incident. It can still check one that is already open: an
    # incident opened under the old rule, at 100 battles, may name a character
    # whose points are short of the reward, and its alert would have the user
    # swap away and strand that character. This is the poll that first sees
    # points after an upgrade, so it withdraws such an incident.
    unfinished_characters = [
        character
        for character, progress in current_character_to_progress.items()
        if progress.point < MASTER_COLOR_THRESHOLD
    ]

    # On first init, we don't have any previous data.
    database_path = Path(database_filename)
    if not database_path.exists():
        write_to_database(current_character_to_progress, database_path)
        incident_manager.record_change()
        incident_manager.withdraw_swap_needed(unfinished_characters)
        return

    # Compare current data with previous data
    previous_character_to_progress = read_database(database_path)
    if previous_character_to_progress is None:
        write_to_database(current_character_to_progress, database_path)
        incident_manager.record_change()
        incident_manager.withdraw_swap_needed(unfinished_characters)
        return

    # Battle counts say the farm is playing: they move on every match, so they
    # drive the stuck timer and the in-progress highlight. Points say a reward
    # is unlocked, so they drive the swap incident. The two are compared
    # separately because either can move on a poll where the other does not.
    battle_counts_differ = False
    points_differ = False
    increased_characters: list[str] = []
    crossed_threshold: list[str] = []
    for character, current_progress in current_character_to_progress.items():
        if character not in previous_character_to_progress:
            logger.warning("Found a new character: %s", character)
            battle_counts_differ = True
            continue
        previous_progress = previous_character_to_progress[character]
        if current_progress.battle_count != previous_progress.battle_count:
            battle_counts_differ = True
            logger.info(
                "Character (%s) has a new battle count: %s -> %s",
                character,
                previous_progress.battle_count,
                current_progress.battle_count,
            )
            if current_progress.battle_count > previous_progress.battle_count:
                increased_characters.append(character)
        if current_progress.point != previous_progress.point:
            points_differ = True
            logger.info(
                "Character (%s) has new Master Pass points: %s -> %s",
                character,
                previous_progress.point,
                current_progress.point,
            )
            if (
                previous_progress.point
                < MASTER_COLOR_THRESHOLD
                <= current_progress.point
            ):
                logger.info("Finished Master color reward for character: %s", character)
                crossed_threshold.append(character)

    # Update database with current data. last_change_at (owned by the incident
    # manager) is the stuck-timer source, replacing the database.json mtime
    # check (retires review finding M10). The same write records which
    # characters gained, for the status page's in-progress highlight.
    if battle_counts_differ:
        incident_manager.record_change(increased_characters)
    if battle_counts_differ or points_differ:
        write_to_database(current_character_to_progress, database_path)

    stuck = incident_manager.seconds_since_last_change() >= config.battle_count_timeout

    def build_stuck_message() -> str:
        duration = timedelta(seconds=incident_manager.seconds_since_last_change())
        return f"It has been ({humanize.precisedelta(duration)}) without an update. The afk farm might be stuck."

    incident_manager.evaluate_stuck_farm(
        active=stuck, build_message=build_stuck_message
    )

    # Master-color swap incident (§7): a character crossing 100 points opens an
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

    # Self-alert on low Pushover quota using the remaining count captured by any
    # send this poll, so quota exhaustion never silently mutes real alerts (§9.2).
    incident_manager.evaluate_low_quota()
