import logging.config
import sys

from task import do_task

# Logging setup
logging.basicConfig(
    stream=sys.stdout,
    level=logging.DEBUG,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)

if __name__ == "__main__":
    logger.info("Starting...")

    # TODO: set this up as a Scheduled Task, running periodically.
    do_task()
    logger.info("Finished.")
