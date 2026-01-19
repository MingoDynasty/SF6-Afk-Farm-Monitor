import json
import logging.config
from collections import deque
from pathlib import Path

import humanize
from sortedcontainers import SortedDict

from api_service import get_character_win_rates
from config import config
from notifier_client import send_message
from utilities import get_duration_since_file_modified

logger = logging.getLogger(__name__)

DATABASE_FILENAME = "database.json"

notifications_to_send = deque()


def write_to_database(data):
    with open(DATABASE_FILENAME, "w") as file:
        json_string = json.dumps(data, indent=2)
        file.write(json_string)
    return


def do_task() -> None:
    win_rate_response = get_character_win_rates(config.user_code)
    current_character_to_battle_count = SortedDict()
    for character_win_rate in win_rate_response.character_win_rates:
        if character_win_rate.character_name == "Any":
            continue
        current_character_to_battle_count[character_win_rate.character_name] = (
            character_win_rate.battle_count
        )

    # On first init, we don't have any previous data.
    if not Path(DATABASE_FILENAME).exists():
        write_to_database(current_character_to_battle_count)
        return

    # Compare current data with previous data
    with open(DATABASE_FILENAME) as file:
        previous_character_to_battle_count = json.load(file)

    data_differs = False
    for character, current_battle_count in current_character_to_battle_count.items():
        if character not in previous_character_to_battle_count:
            logger.warning("Found a new character: %s", character)
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
        if current_battle_count >= 100 > previous_battle_count:
            message = f"Finished Master color reward for character: {character}"
            logger.info(message)
            notifications_to_send.append(message)

    duration = get_duration_since_file_modified(DATABASE_FILENAME)
    if duration.total_seconds() >= config.battle_count_timeout and not data_differs:
        message = f"It has been ({humanize.precisedelta(duration)}) without an update. The afk farm might be stuck."
        logger.warning(message)
        notifications_to_send.append(message)

    # Update database with current data
    if data_differs:
        write_to_database(current_character_to_battle_count)

    while len(notifications_to_send) > 0:
        message = notifications_to_send.popleft()
        send_message(message)
