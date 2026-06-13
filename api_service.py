import json
import logging

import requests

from config import ConfigData
from model import WinRateResponse

logger = logging.getLogger(__name__)

url = "https://www.streetfighter.com/6/buckler/api/profile/play/act/characterwinrate"
REQUEST_TIMEOUT = (10, 30)


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
    response.raise_for_status()
    response_json = response.json()["response"]

    # TODO: debugging only
    with open("response.json", "w") as file:
        json_string = json.dumps(response_json, indent=2)
        file.write(json_string)

    return WinRateResponse.model_validate(response_json)
