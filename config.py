"""
Manages the config file for the app, and shares that data to all other modules.
"""

import os
import re
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


def _toml_basic_string(value: str) -> str:
    """Render a string as a TOML basic (double-quoted) string."""
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def _line_ending(line: str) -> str:
    """Return the trailing newline of a line ("\\r\\n", "\\n", or "" at EOF)."""
    if line.endswith("\r\n"):
        return "\r\n"
    if line.endswith("\n"):
        return "\n"
    return ""


def update_buckler_cookies(
    buckler_id: str,
    buckler_r_id: str,
    buckler_praise_date: int,
    config_file: str | Path = CONFIG_FILE,
) -> None:
    """Rewrite the three ``buckler_*`` values in ``config.toml`` in place.

    A surgical line-replace that preserves comments and every other setting:
    ``tomllib`` is read-only and a full parse-and-redump would drop the file's
    comments. ``buckler_id``/``buckler_r_id`` are written as TOML basic strings;
    ``buckler_praise_date`` is written unquoted to match its ``int`` type on
    :class:`ConfigData`. The file is written via a temp file + :func:`os.replace`
    so a crash mid-write cannot corrupt ``config.toml`` (review finding M1).
    """
    config_path = Path(config_file)
    updates = {
        "buckler_id": _toml_basic_string(buckler_id),
        "buckler_r_id": _toml_basic_string(buckler_r_id),
        "buckler_praise_date": str(int(buckler_praise_date)),
    }

    lines = config_path.read_text(encoding="utf-8").splitlines(keepends=True)
    remaining = dict(updates)
    for index, line in enumerate(lines):
        for key in list(remaining):
            # Match `key =` / `key=` at the start of the line (ignoring leading
            # whitespace), but never a commented-out line.
            if re.match(
                rf"\s*{re.escape(key)}\s*=", line
            ) and not line.lstrip().startswith("#"):
                lines[index] = f"{key} = {remaining.pop(key)}{_line_ending(line)}"
                break

    # Any key absent from the file is appended (defensive — the example.toml
    # ships all three, so this should not normally trigger).
    if remaining:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        for key, value in remaining.items():
            lines.append(f"{key} = {value}\n")

    temporary_path = config_path.with_name(f"{config_path.name}.tmp")
    temporary_path.write_text("".join(lines), encoding="utf-8")
    os.replace(temporary_path, config_path)
