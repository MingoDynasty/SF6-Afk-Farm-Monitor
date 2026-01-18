import json
import logging.config
import sys
from pathlib import Path

from sortedcontainers import SortedDict

from api_service import get_character_win_rates
from utilities import get_duration_since_file_modified

# Logging setup
logging.basicConfig(
    stream=sys.stdout,
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

# User code of the profile to monitor
USER_CODE = 2885430127

DATABASE_FILENAME = "database.json"


def write_to_database(data):
    with open(DATABASE_FILENAME, "w") as file:
        json_string = json.dumps(data, indent=2)
        file.write(json_string)
    return


def main() -> None:
    win_rate_response = get_character_win_rates(USER_CODE)
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

    send_notification = False
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
            logger.info("Finished Master color reward for character: %s", character)
            send_notification = True

    duration, minutes = get_duration_since_file_modified(DATABASE_FILENAME)
    if minutes >= 0 and not data_differs:
        send_notification = True
        logger.warning(
            "It has been (%s) without an update. Probably the afk farm is stuck.",
            duration,
        )

    # Update database with current data
    if data_differs:
        write_to_database(current_character_to_battle_count)

    # TODO: send notification here
    if send_notification:
        pass


if __name__ == "__main__":
    main()
