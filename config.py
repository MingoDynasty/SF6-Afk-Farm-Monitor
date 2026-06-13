"""
Manages the config file for the app, and shares that data to all other modules.
"""

import tomllib
from pathlib import Path
from typing import Annotated

from pydantic import Field
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
    # Polling cadence and the stuck-farm timeout are durations in seconds; both
    # must be positive or the scheduler/stuck logic is nonsensical (review L4).
    polling_interval: Annotated[int, Field(gt=0)]
    battle_count_timeout: Annotated[int, Field(gt=0)]
    buckler_id: str
    buckler_r_id: str
    buckler_praise_date: int
    pushover_enabled: bool
    pushover_app_key: str
    pushover_user_key: str
    # Emergency-priority alert tuning (see ALERT_DEDUPLICATION_PROPOSAL.md §4, §8).
    # retry has a Pushover-imposed minimum of 30 s; expire has a maximum of
    # 10800 s (3 h); re_alert_after_ack accepts 0 to disable the re-arm (review L4).
    emergency_retry: Annotated[int, Field(ge=30)] = 120
    emergency_expire: Annotated[int, Field(gt=0, le=10800)] = 10800
    re_alert_after_ack: Annotated[int, Field(ge=0)] = 600
    # Port for the optional local status page (STATUS_PAGE_PROPOSAL.md §4). Has a
    # default so a monitor config.toml without the key still loads; not 8080,
    # which Steam occupies on the author's machine.
    status_page_port: int = 8675


def load_config(config_file: str | Path = CONFIG_FILE) -> ConfigData:
    """Loads the config file for this app."""
    with Path(config_file).open("rb") as _file:
        config_dict = tomllib.load(_file)
    return ConfigData(**config_dict)
