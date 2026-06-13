import json
import logging

import requests
from pydantic import ValidationError
from requests import HTTPError

from config import ConfigData
from model import WinRateResponse

logger = logging.getLogger(__name__)

url = "https://www.streetfighter.com/6/buckler/api/profile/play/act/characterwinrate"
REQUEST_TIMEOUT = (10, 30)


class AuthExpiredError(Exception):
    """The Buckler session cookies appear to have expired.

    Signalled by HTTP 401/403, a non-JSON (e.g. HTML login page) body, or a
    JSON body missing the ``response`` key — all of which mean the configured
    cookies need refreshing, not that Capcom is down (review finding M3).
    """


def get_character_win_rates(config: ConfigData) -> WinRateResponse:
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
        "Cookie": f"buckler_id={config.buckler_id}; buckler_r_id={config.buckler_r_id}; buckler_praise_date={config.buckler_praise_date}",
        "Origin": "https://www.streetfighter.com",
        "Connection": "keep-alive",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
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

    # Expired cookies surface as 401/403 (or an HTML login page served with a
    # 200, handled below). Classify these as auth-expiry, not an outage (M3).
    # On every failure path, log the raw response body at DEBUG so the exact
    # evidence lands in debug.log; this replaces the old per-poll response.json
    # dump (review finding M9).
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
        response_body = response.json()
    except ValueError as exc:
        logger.debug("Buckler returned a non-JSON body: %s", response.text)
        raise AuthExpiredError(
            "Buckler returned a non-JSON body (session cookies expired?)."
        ) from exc

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
