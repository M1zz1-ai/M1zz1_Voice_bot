"""
Настройка логирования VoiceBot.
Логи пишутся в ~/.voicebot/logs/ с ротацией.
"""

import logging
import os
from logging.handlers import RotatingFileHandler


def setup_logger(name="voicebot", log_dir=None):
    if log_dir is None:
        log_dir = os.path.expanduser("~/.voicebot/logs")
    os.makedirs(log_dir, exist_ok=True)

    logger = logging.getLogger(name)
    if logger.handlers:
        return logger  # Already configured

    logger.setLevel(logging.DEBUG)

    # File handler with rotation (2MB, 3 backups)
    fh = RotatingFileHandler(
        os.path.join(log_dir, "voicebot.log"),
        maxBytes=2 * 1024 * 1024,
        backupCount=3,
    )
    fh.setLevel(logging.DEBUG)
    fh.setFormatter(logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))

    # Console handler
    ch = logging.StreamHandler()
    ch.setLevel(logging.INFO)
    ch.setFormatter(logging.Formatter("%(levelname)s: %(message)s"))

    logger.addHandler(fh)
    logger.addHandler(ch)

    return logger
