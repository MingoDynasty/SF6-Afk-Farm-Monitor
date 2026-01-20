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

    logger.info("Scheduling task for every %s seconds...", config.polling_interval)
    schedule.every(config.polling_interval).seconds.do(do_task)
    schedule.run_all()

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
