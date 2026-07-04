"""Config validation: pydantic field constraints reject nonsensical values
(review finding L4)."""

from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from config import ConfigData, load_config, update_buckler_cookies

SAMPLE_CONFIG = """\
# A comment that must survive a cookie rewrite.
user_code = 1234567890
target_season_id = 12
polling_interval = 60
battle_count_timeout = 360

# CFN configuration
buckler_id = ""
buckler_r_id = ""
buckler_praise_date = 1234567890123

pushover_enabled = true
pushover_app_key = ""
pushover_user_key = ""
"""


def test_valid_config_constructs(make_config: Callable[..., ConfigData]) -> None:
    config = make_config()
    assert config.polling_interval == 60
    assert config.emergency_expire == 10800


@pytest.mark.parametrize(
    "overrides",
    [
        {"polling_interval": 0},
        {"polling_interval": -1},
        {"battle_count_timeout": 0},
        {"emergency_retry": 29},  # below the Pushover minimum of 30
        {"emergency_expire": 0},
        {"emergency_expire": 10801},  # above the Pushover maximum of 10800
        {"re_alert_after_ack": -1},
    ],
)
def test_out_of_range_values_are_rejected(
    make_config: Callable[..., ConfigData], overrides: dict[str, Any]
) -> None:
    with pytest.raises(ValidationError):
        make_config(**overrides)


def test_re_alert_after_ack_zero_is_allowed(
    make_config: Callable[..., ConfigData],
) -> None:
    # 0 disables the re-arm timer, so it must stay valid (ge=0, not gt=0).
    assert make_config(re_alert_after_ack=0).re_alert_after_ack == 0


def test_update_buckler_cookies_round_trips(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(SAMPLE_CONFIG, encoding="utf-8")

    update_buckler_cookies("new-id", "new-r-id", 9999999999999, config_file=config_file)

    config = load_config(config_file)
    assert config.buckler_id == "new-id"
    assert config.buckler_r_id == "new-r-id"
    assert config.buckler_praise_date == 9999999999999


def test_update_buckler_cookies_preserves_comments_and_other_keys(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(SAMPLE_CONFIG, encoding="utf-8")

    update_buckler_cookies("new-id", "new-r-id", 9999999999999, config_file=config_file)

    text = config_file.read_text(encoding="utf-8")
    assert "# A comment that must survive a cookie rewrite." in text
    assert "# CFN configuration" in text
    # praise_date stays an unquoted int; the others are quoted strings.
    assert "buckler_praise_date = 9999999999999\n" in text
    assert 'buckler_id = "new-id"\n' in text
    # Untouched keys are left exactly as they were.
    assert "polling_interval = 60\n" in text
    assert 'pushover_app_key = ""\n' in text


def test_update_buckler_cookies_appends_missing_key(tmp_path: Path) -> None:
    # A config that is missing buckler_r_id entirely: the writer appends it.
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        SAMPLE_CONFIG.replace('buckler_r_id = ""\n', ""), encoding="utf-8"
    )

    update_buckler_cookies(
        "new-id", "appended-r-id", 9999999999999, config_file=config_file
    )

    assert load_config(config_file).buckler_r_id == "appended-r-id"
