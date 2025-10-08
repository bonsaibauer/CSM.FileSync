import logging
from logging import Handler
from datetime import datetime
from .settings_store import get_appdata_dir
from .constants import LOGS_DIR_NAME

class TkTextHandler(Handler):
    """Leitet Logeinträge in eine GUI-Callback-Funktion um."""
    def __init__(self, write_cb):
        super().__init__()
        self.write_cb = write_cb

    def emit(self, record: logging.LogRecord) -> None:
        try:
            msg = self.format(record)
            self.write_cb(msg)
        except Exception:
            pass

def configure_logging(write_cb=None) -> logging.Logger:
    logger = logging.getLogger("hws")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()

    fmt = logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s", datefmt="%H:%M:%S")

    # File Handler
    logs_dir = get_appdata_dir() / LOGS_DIR_NAME
    logs_dir.mkdir(parents=True, exist_ok=True)
    logfile = logs_dir / f"{datetime.now():%Y-%m-%d}.log"
    fh = logging.FileHandler(logfile, encoding="utf-8")
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(fmt)
    logger.addHandler(fh)

    # Console Handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(fmt)
    logger.addHandler(ch)

    # Tkinter Handler
    if write_cb is not None:
        th = TkTextHandler(write_cb)
        th.setLevel(logging.DEBUG)
        th.setFormatter(fmt)
        logger.addHandler(th)

    logger.debug("Logger initialisiert. Logfile: %s", logfile)
    return logger
