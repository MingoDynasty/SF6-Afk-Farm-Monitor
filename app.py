import logging.config
import sys
import time

import schedule

from config import config
from task import do_task

# Logging setup
logging.basicConfig(
    stream=sys.stdout,
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Scheduling task for every %s seconds...", config.polling_interval)
    schedule.every(config.polling_interval).seconds.do(do_task)
    schedule.run_all()

    while True:
        schedule.run_pending()
        time.sleep(1)
