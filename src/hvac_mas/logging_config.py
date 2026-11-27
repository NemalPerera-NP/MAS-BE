"""Readable operational logs without credentials or raw XMPP payloads."""
import logging


def configure_logging(settings):
    logging.basicConfig(level=logging.WARNING,
                        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s", force=True)
    logging.getLogger("hvac_mas").setLevel(settings.log_level)
