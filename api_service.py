import json
import logging

import requests

from config import config
from model import WinRateResponse

logger = logging.getLogger(__name__)

url = "https://www.streetfighter.com/6/buckler/api/profile/play/act/characterwinrate"

payload = json.dumps(
    {"targetShortId": 2885430127, "targetSeasonId": 10, "targetModeId": 2, "lang": "en"}
)

headers = {
    "Host": "www.streetfighter.com",
    "host": "www.streetfighter.com",
    "Accept": "*/*",
    "Cookie": f"buckler_id={config.buckler_id}; buckler_r_id={config.buckler_r_id}; buckler_praise_date={config.buckler_praise_date}",
    "Origin": "https://www.streetfighter.com",
    "Connection": "keep-alive",
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:147.0) Gecko/20100101 Firefox/147.0",
    "Content-Type": "application/json",
    "Sec-Fetch-Dest": "empty",
    "Sec-Fetch-Mode": "cors",
    "Sec-Fetch-Site": "same-origin",
    "Accept-Encoding": "gzip, deflate, br, zstd",
    "Accept-Language": "en-US,en;q=0.9",
}


def get_character_win_rates(user_code) -> WinRateResponse:
    headers["Referer"] = (
        f"https://www.streetfighter.com/6/buckler/profile/{user_code}/play"
    )
    response = requests.request("POST", url, headers=headers, data=payload)
    response.raise_for_status()
    response_json = response.json()["response"]
    return WinRateResponse.model_validate(response_json)
