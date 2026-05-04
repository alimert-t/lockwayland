import os
import re
import gi
import pwd
import pam
import signal
import datetime
import threading
import json
import logging
from pathlib import Path
from PIL import Image, ImageFilter
from pydbus import SystemBus

gi.require_version('Gtk', '4.0')
gi.require_version('Gtk4LayerShell', '1.0')
from gi.repository import Gtk, Gtk4LayerShell, GLib, Gdk

# Ignore exit signals for security
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("lockwayland.locker")

def get_username():
    return pwd.getpwuid(os.getuid()).pw_name

def load_config():
    config = {
        "clock_format": "%H:%M",
        "status_ready": "Password or Fingerprint",
        "status_fail": "Authentication failed!",
        "auth_retry_delay_ms": "500",
        "wallpaper_blur": "false",
        "wallpaper_blur_radius": "8",
    }

    config_path = BASE_DIR / "lockwayland.conf"
    if not config_path.exists():
        logger.info(
                f"No config file found at {config_path}, using defaults")
        return config

    with open(config_path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue

            key, value = line.split("=", 1)
            config[key.strip()] = value.strip()

    logger.info(f"Loaded config from {config_path}")
    return config

def get_config_bool(config, key, default=False):
    value = config.get(key, str(default)).strip().lower()
    return value in ("1", "true", "yes", "on")

def get_config_int(config, key, default):
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default


