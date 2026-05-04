import os
import pwd
import pam
import datetime
import threading
import json
import logging
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
logger = logging.getLogger("lockwayland.locker")

def get_username():
    return pwd.getpwuid(os.getuid()).pw_name

def load_config():
    config = {
        "clock_format": "%H:%M",
        "status_ready": "Password or fingerprint",
        "status_authing": "Checking...",
        "status_fail": "Authentication failed!",
        "auth_retry_delay_ms": "500",
        "pam_service": "lockwayland",
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

class LockController:
    def __init__(self, on_unlock_requested, on_state_changed=None):
        self.config = load_config()
        self.username = get_username()

        self.on_unlock_requested = on_unlock_requested
        self.on_state_changed = on_state_changed

        self.password_buffer = ""
        self.auth_in_progress = False
        self.status_text = self.config.get(
                "status_ready", "Password or Fingerprint"
        )

    def notify_state_changed(self):
        if self.on_state_changed:
            self.on_state_changed()

    def clock_text(self):
        clock_format = self.config.get("clock_format", "%H:%M")
        return datetime.datetime.now().strftime(clock_format)

    def password_display_text(self):
        return "*" * len(self.password_buffer)

    def append_text(self, text):
        if self.auth_in_progress:
            return

        if not text:
            return

        self.password_buffer += text
        self.notify_state_changed()

    def backspace(self):
        if self.auth_in_progress:
            return

        if self.password_buffer:
            self.password_buffer = self.password_buffer[:-1]
            self.notify_state_changed()

    def clear_password(self):
        if self.auth_in_progress:
            return

        if self.password_buffer:
            self.password_buffer = ""
            self.notify_state_changed()

    def submit_password(self):
        if self.auth_in_progress:
            return
        
        if not self.password_buffer:
            return

        password = self.password_buffer
        self.password_buffer = ""
        self.auth_in_progress = True
        self.status_text = self.config.get("status_authing", "Checking...")
        self.notify_state_changed()

        threading.Thread(
                target=self._check_pam,
                args=(password,),
                daemon=True
        ).start()

    def _check_pam(self, password):
        service = self.config.get("pam_service", "lockwayland")
        ok = pam.pam().authenticate(self.username, password, service=service)
        password = None

        if ok:
            logger.info(f"Password authenticate succeeded for user {self.username}.")
            self.on_unlock_requested()
            return

        logger.info(f"Password authentication failed for user {self.username}")
        self.auth_in_progress = False
        self.status_text = self.config.get("status_fail", "Authentication failed!")
        self.notify_state_changed()

    def reset_status(self):
        self.status_text = self.config.get(
                "status_ready", "Password or fingerprint")
        self.notify_state_changed()
