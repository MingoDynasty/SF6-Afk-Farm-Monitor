"""
Manages the config file for the app, and shares that data to all other modules.
"""

import tomllib
from pathlib import Path

from pydantic.dataclasses import dataclass

from paths import BASE_DIR

# config.toml is user-edited and stays in the repo root, but is resolved
# relative to the source directory so the app finds it from any CWD (M8).
CONFIG_FILE = BASE_DIR / "config.toml"


@dataclass()
class ConfigData:
    """Dataclass models configuration for this app."""

    user_code: int
    target_season_id: int
    polling_interval: int
    battle_count_timeout: int
    buckler_id: str
    buckler_r_id: str
    buckler_praise_date: int
    pushover_enabled: bool
    pushover_app_key: str
    pushover_user_key: str
    # Emergency-priority alert tuning (see ALERT_DEDUPLICATION_PROPOSAL.md §4, §8).
    emergency_retry: int = 120
    emergency_expire: int = 10800
    re_alert_after_ack: int = 600


def load_config(config_file: str | Path = CONFIG_FILE) -> ConfigData:
    """Loads the config file for this app."""
    with Path(config_file).open("rb") as _file:
        config_dict = tomllib.load(_file)
    return ConfigData(**config_dict)
