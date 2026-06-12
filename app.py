import logging.config
import os
import sys
import time

import schedule

from config import config
from task import do_task


def main():
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

    os.makedirs("logs", exist_ok=True)

    # Log to File - Info
    info_file_handler = logging.FileHandler("logs/info.log")
    info_file_handler.setFormatter(formatter)
    info_file_handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(info_file_handler)

    # Log to File - Debug
    debug_file_handler = logging.FileHandler("logs/debug.log")
    debug_file_handler.setFormatter(formatter)
    debug_file_handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(debug_file_handler)

    logger = logging.getLogger(__name__)

    def run_task_safely():
        try:
            do_task()
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
