import logging.config
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

    # Log to File
    file_handler = logging.FileHandler("debug.log")
    file_handler.setFormatter(formatter)
    file_handler.setLevel(logging.DEBUG)
    logging.getLogger().addHandler(file_handler)

    logger = logging.getLogger(__name__)

    logger.info("Scheduling task for every %s seconds...", config.polling_interval)
    schedule.every(config.polling_interval).seconds.do(do_task)
    schedule.run_all()

    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
