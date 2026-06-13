import logging
import sys
import time
from logging.handlers import RotatingFileHandler

import schedule

from config import load_config
from incident_manager import IncidentManager
from notifier_client import PushoverClient
from paths import DATA_DIR, LOGS_DIR
from task import do_task


def main():
    config = load_config()

    #
    # Logging setup
    #
    logging.getLogger().setLevel(logging.DEBUG)
    logging.getLogger("urllib3").setLevel(logging.INFO)

    # Shared Log Formatter
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)s | %(name)s | %(message)s"
    )

    # Log to Console
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setFormatter(formatter)
    console_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(console_handler)

    # Anchor logs/ and data/ to the source directory and create them at startup
    # so the app works regardless of the launching CWD (review finding M8).
    LOGS_DIR.mkdir(exist_ok=True)
    DATA_DIR.mkdir(exist_ok=True)

    # Log to File - Info (rotates to keep disk usage bounded on a 24/7 process)
    info_file_handler = RotatingFileHandler(
        LOGS_DIR / "info.log",
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    info_file_handler.setFormatter(formatter)
    info_file_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(info_file_handler)

    # Log to File - Debug (larger budget; debug.log is far chattier)
    debug_file_handler = RotatingFileHandler(
        LOGS_DIR / "debug.log",
        maxBytes=5 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    debug_file_handler.setFormatter(formatter)
    debug_file_handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(debug_file_handler)

    logger = logging.getLogger(__name__)

    pushover_client = PushoverClient(config.pushover_app_key, config.pushover_user_key)
    incident_manager = IncidentManager(pushover_client, config)
    incident_manager.reconcile_on_startup()

    def run_task_safely():
        try:
            do_task(config, incident_manager)
        except Exception:
            logger.exception("Scheduled monitor task failed; continuing.")

    logger.info("Scheduling task for every %s seconds...", config.polling_interval)
    schedule.every(config.polling_interval).seconds.do(run_task_safely)
    run_task_safely()

    while True:
        try:
            schedule.run_pending()
        except Exception:
            logger.exception("Scheduler loop failed; continuing.")
        time.sleep(1)


if __name__ == "__main__":
    main()
