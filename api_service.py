"""Fetch and validate the monitored player's progress from the Buckler API."""

import json
import logging
from typing import Any

import requests
from pydantic import ValidationError
from requests import HTTPError, Response

from config import ConfigData
from model import LoginDataResponse, MasterPassResponse, WinRateResponse

logger = logging.getLogger(__name__)

url = "https://www.streetfighter.com/6/buckler/api/profile/play/act/characterwinrate"
MASTER_PASS_URL = "https://www.streetfighter.com/6/buckler/api/masterpass"
LOGIN_DATA_URL = "https://www.streetfighter.com/6/buckler/api/auth/getlogindata"
REQUEST_TIMEOUT = (10, 30)
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0"
)


class AuthExpiredError(Exception):
    """The Buckler session cookies appear to have expired.

    Signalled by HTTP 401/403, a non-JSON (e.g. HTML login page) body, a JSON
    body missing the ``response`` key, or login data reporting no logged-in
    account — all of which mean the configured cookies need refreshing, not
    that Capcom is down (review finding M3).
    """


def _cookie_header(config: ConfigData) -> str:
    return (
        f"buckler_id={config.buckler_id}; "
        f"buckler_r_id={config.buckler_r_id}; "
        f"buckler_praise_date={config.buckler_praise_date}"
    )


def _decode_json(response: Response) -> Any:
    """Return a Buckler response's JSON body, classifying the failures.

    Expired cookies surface as 401/403, or as an HTML login page served with a
    200; both are auth-expiry, not an outage (M3). On every failure path the
    raw response body is logged at DEBUG so the exact evidence lands in
    debug.log; this replaces the old per-poll response.json dump (review
    finding M9).
    """
    if response.status_code in (401, 403):
        logger.debug(
            "Buckler returned HTTP %s; response body: %s",
            response.status_code,
            response.text,
        )
        raise AuthExpiredError(
            f"Buckler returned HTTP {response.status_code} (session cookies expired?)."
        )

    try:
        response.raise_for_status()
    except HTTPError:
        logger.debug(
            "Buckler returned HTTP %s; response body: %s",
            response.status_code,
            response.text,
        )
        raise

    try:
        return response.json()
    except ValueError as exc:
        logger.debug("Buckler returned a non-JSON body: %s", response.text)
        raise AuthExpiredError(
            "Buckler returned a non-JSON body (session cookies expired?)."
        ) from exc


def get_character_win_rates(config: ConfigData) -> WinRateResponse:
    """Fetch the configured player's per-character battle counts."""
    payload = json.dumps(
        {
            "targetShortId": config.user_code,
            "targetSeasonId": config.target_season_id,
            "targetModeId": 2,
            "lang": "en",
        }
    )
    headers = {
        "Accept": "*/*",
        "Cookie": _cookie_header(config),
        "Origin": "https://www.streetfighter.com",
        "Connection": "keep-alive",
        "User-Agent": USER_AGENT,
        "Content-Type": "application/json",
        "Sec-Fetch-Dest": "empty",
        "Sec-Fetch-Mode": "cors",
        "Sec-Fetch-Site": "same-origin",
        # Accept-Encoding deliberately left unset: no brotli/zstd decoder is
        # installed, so let requests advertise only encodings it can decode
        # (review finding M6). The Host header is also omitted — requests sets
        # it correctly from the URL.
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": f"https://www.streetfighter.com/6/buckler/profile/{config.user_code}/play",
    }
    response = requests.request(
        "POST", url, headers=headers, data=payload, timeout=REQUEST_TIMEOUT
    )
    response_body = _decode_json(response)

    if "response" not in response_body:
        logger.debug(
            "Buckler response is missing the 'response' key: %s", response.text
        )
        raise AuthExpiredError(
            "Buckler response missing the 'response' key (session cookies expired?)."
        )

    try:
        return WinRateResponse.model_validate(response_body["response"])
    except ValidationError:
        logger.debug("Buckler response failed schema validation: %s", response.text)
        raise


def _get(config: ConfigData, endpoint_url: str) -> Response:
    """GET a Buckler endpoint that answers for the account the cookies belong to."""
    headers = {
        "Accept": "*/*",
        "Cookie": _cookie_header(config),
        "User-Agent": USER_AGENT,
        "Accept-Language": "en-US,en;q=0.9",
        "Referer": "https://www.streetfighter.com/6/buckler/reward/masterpass",
    }
    return requests.request(
        "GET", endpoint_url, headers=headers, timeout=REQUEST_TIMEOUT
    )


def get_master_pass_points(config: ConfigData) -> dict[int, int]:
    """Fetch the logged-in account's Master Pass points, keyed by character ID.

    Buckler serves the pass only for the account the cookies belong to; the
    endpoint takes no player parameter. Raises ``ValueError`` when the
    configured season has no open pass, so a season rollover fails loudly
    instead of reading as zero points.
    """
    response = _get(config, MASTER_PASS_URL)
    response_body = _decode_json(response)

    try:
        master_pass_response = MasterPassResponse.model_validate(response_body)
    except ValidationError:
        logger.debug("Master Pass response failed schema validation: %s", response.text)
        raise

    master_passes = master_pass_response.message_list.master_rate_pass_list
    for master_pass in master_passes:
        if (
            master_pass.season_id == config.target_season_id
            and master_pass.characters is not None
        ):
            return {
                character.character_id: character.point
                for character in master_pass.characters
            }

    logger.debug(
        "Master Pass response has no open season %s: %s",
        config.target_season_id,
        response.text,
    )
    returned_seasons = [master_pass.season_id for master_pass in master_passes]
    raise ValueError(
        f"Buckler returned no open Master Pass for season {config.target_season_id} "
        f"(seasons returned: {returned_seasons}). Check target_season_id in config.toml."
    )


def get_logged_in_short_id(config: ConfigData) -> int:
    """Fetch the short ID (user code) of the account the cookies belong to."""
    response = _get(config, LOGIN_DATA_URL)
    response_body = _decode_json(response)

    try:
        login_user = LoginDataResponse.model_validate(response_body).login_user
    except ValidationError:
        logger.debug("Login-data response failed schema validation: %s", response.text)
        raise

    if not login_user.flg:
        logger.debug("Buckler reports no logged-in account: %s", response.text)
        raise AuthExpiredError(
            "Buckler reports no logged-in account (session cookies expired?)."
        )
    return login_user.short_id
