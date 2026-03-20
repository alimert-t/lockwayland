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
logger = logging.getLogger("lockwayland")

class FingerprintManager:
    def __init__(self, on_success_callback):
        self.bus = None
        self.manager = None
        self.device = None
        self.claimed = False

        try:
            self.bus = SystemBus()
            self.on_success = on_success_callback
            self.manager = self.bus.get(
                "net.reactivated.Fprint", "/net/reactivated/Fprint/Manager")
            self.device_path = self.manager.GetDefaultDevice()
            self.device = self.bus.get("net.reactivated.Fprint", self.device_path)
            self.device.VerifyStatus.connect(self.on_verify_status)
            self.username = get_username()
            self.device.Claim(self.username)
            self.claimed = True
            self.device.VerifyStart("any")
            logger.info("[Fingerprint] Scanner active")
        except Exception as e:
            logger.warning(
                    "[Fingerprint] Init failed (maybe already running?): %s", e)

    def cleanup(self):
        if not self.device:
            return
    
        try:
            self.device.VerifyStop()
        except Exception as e:
            logger.warning("[Fingerprint] VerifyStop failed: %s", e)

        if self.claimed:
            try:
                self.device.Release()
                self.claimed = False
            except Exception as e:
                logger.warning(f"[Fingerprint] Release failed: %s", e)

        self.claimed = False
        self.device = None

    def on_verify_status(self, result, done):
        if result == "verify-match":
            GLib.idle_add(self.on_success)
        elif not done and self.device:
            try:
                self.device.VerifyStart("any")
            except Exception as e:
                logger.warning(f"[Fingerprint] Verify restart failed: %s", e)

class LockScreen(Gtk.ApplicationWindow):
    def __init__(self, monitor, is_primary, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.username = get_username()
        self.auth_in_progress = False

        # Layer shell
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_monitor(self, monitor)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_namespace(self, "lockscreen")
        Gtk4LayerShell.set_exclusive_zone(self, -1)

        if is_primary:
            Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)

        for edge in [
            Gtk4LayerShell.Edge.LEFT,
            Gtk4LayerShell.Edge.RIGHT,
            Gtk4LayerShell.Edge.TOP,
            Gtk4LayerShell.Edge.BOTTOM,
        ]:
            Gtk4LayerShell.set_anchor(self, edge, True)

        # UI
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self.box.set_valign(Gtk.Align.CENTER)
        self.box.set_halign(Gtk.Align.CENTER)

        self.label_clock = Gtk.Label()
        self.box.append(self.label_clock)

        if is_primary:
            status_ready = self.get_application().config.get(
                    "status_ready", "Password or Fingerprint")
            self.label_status = Gtk.Label(label=status_ready)
            self.password_entry = Gtk.Entry(visibility=False)
            self.password_entry.connect("activate", self.on_pass_submit)
            self.box.append(self.label_status)
            self.box.append(self.password_entry)

        self.set_child(self.box)

        # CSS classes
        self.add_css_class("lock-window")
        self.box.add_css_class("lock-container")
        self.label_clock.add_css_class("lock-clock")

        if is_primary:
            self.label_status.add_css_class("lock-status")
            self.password_entry.add_css_class("lock-entry")

        self.update_clock()
        GLib.timeout_add(1000, self.update_clock)

    def update_clock(self):
        clock_format = self.get_application().config.get("clock_format", "%H:%M")
        now = datetime.datetime.now().strftime(clock_format)
        self.label_clock.set_text(now)
        return True

    def on_pass_submit(self, entry):
        if self.auth_in_progress:
            return

        self.auth_in_progress = True
        password = entry.get_text()
        entry.set_sensitive(False)
        threading.Thread(
                target=self.check_pam,
                args=(password,),
                daemon=True,
                ).start()
    
    def check_pam(self, password):
        ok = pam.pam().authenticate(
                self.username, password, service="lockwayland")
        password = None

        if ok:
            logger.info(
                    "Password authentication succeeded for user %s",
                    self.username)
            GLib.idle_add(request_unlock, self.get_application())
        else:
            logger.info(
                    "Password authentication failed for user %s",
                    self.username)
            GLib.idle_add(self.fail)

    def fail(self):
        self.auth_in_progress = False

        if hasattr(self, "password_entry"):
            app_config = self.get_application().config
            status_fail = app_config.get("status_fail", "Authentication failed!")
            status_ready = app_config.get("status_ready", "Password or Fingerprint")
            retry_delay = get_config_int(app_config, "auth_retry_delay_ms", 500)

            self.label_status.set_label(status_fail)
            self.password_entry.set_text("")

            def reset_password_entry():
                self.password_entry.set_sensitive(True)
                self.password_entry.grab_focus()
                self.label_status.set_label(status_ready)
                return False

            GLib.timeout_add(retry_delay, reset_password_entry)

def get_username():
    return pwd.getpwuid(os.getuid()).pw_name

def request_unlock(app):
    logger.info("Unlock requested")
    if getattr(app, "unlocking", False):
        return False

    app.unlocking = True
    
    if hasattr(app, "fprint") and app.fprint:
        app.fprint.cleanup()
    
    for window in list(app.get_windows()):
        window.close()

    app.quit()
    return False

def on_activate(app):
    app.unlocking = False

    logger.info("Activating lockwayland") 

    app.config = load_config()
    load_css(app.config)

    display = Gdk.Display.get_default()
    monitors = display.get_monitors()
    
    # Spawn windows in every monitor that is already there 
    # todo: check if a new monitor is plugged in,
    # and spawn the lockscreen there as well.
    # todo: there is a critical bug, when switching to different
    # tty and coming back, one of the monitors becoems unlocked while 
    # the other is locked. Should be fixed ASAP. 
    logger.info("Detected %d monitor(s)", monitors.get_n_items())
    for i in range(monitors.get_n_items()):
        monitor = monitors.get_item(i)
        win = LockScreen(monitor, is_primary=(i == 0), application=app)
        win.present()
    
    # Start fingerprint once for the whole app
    app.fprint = FingerprintManager(lambda: request_unlock(app))


def load_css(app_config):
    default_css_path = BASE_DIR / "lockwayland_default.css"
    config_css_path = BASE_DIR / "config.css"

    provider = Gtk.CssProvider()
    with open(default_css_path, "rb") as f:
        provider.load_from_data(f.read())

    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
    )

    config_css_text = None

    if config_css_path.exists():
        config_css_text = config_css_path.read_text(encoding="utf-8")

        override_provider = Gtk.CssProvider()
        override_provider.load_from_data(config_css_text.encode("utf-8"))
        Gtk.StyleContext.add_provider_for_display(
            Gdk.Display.get_default(),
            override_provider,
            Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
        )
    logger.info("Loaded CSS override from %s", config_css_path)   
    blur_enabled = get_config_bool(app_config, "wallpaper_blur", False)
    blur_radius = get_config_int(app_config, "wallpaper_blur_radius", 8)

    if blur_enabled and config_css_text:
        wallpaper_url = extract_wallpaper_url(config_css_text)
        wallpaper_path = resolve_wallpaper_path(wallpaper_url)

        if wallpaper_path and wallpaper_path.exists():
            try:
                blurred_path = build_blurred_wallpaper(wallpaper_path, blur_radius)
                blur_css = f'''
window.lock-window {{
    background-image: url("file://{blurred_path}");
}}
'''
                load_css_provider_from_text(blur_css)
                logger.info(
                        "Applying blurred wallpaper from %s with radius %s",
                        wallpaper_path, blur_radius) 
            except Exception as e:
                logger.warning(f"[Blur] Failed to blur wallpaper: %s", e)

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
                "No config file found at %s, using defaults",
                config_path)
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
    logger.info("Loaded config from %s", config_path)
    return config

def get_config_bool(config, key, default=False):
    value = config.get(key, str(default)).strip().lower()
    return value in ("1", "true", "yes", "on")

def get_config_int(config, key, default):
    try:
        return int(config.get(key, default))
    except (TypeError, ValueError):
        return default

def extract_wallpaper_url(css_text):
    match = re.search(r'background-image\s*:\s*url\(["\']?(.*?)["\']?\)', css_text)
    if not match:
        return None
    return match.group(1)

def resolve_wallpaper_path(url_value):
    if not url_value:
        return None

    if url_value.startswith("file://"):
        return Path(url_value[7:])

    path = Path(url_value)
    if path.is_absolute():
        return path

    return BASE_DIR / path

def build_blurred_wallpaper(source_path, blur_radius):
    source_path = Path(source_path).resolve()
    output_path = BASE_DIR / ".lockwayland_blurred.png"
    meta_path = BASE_DIR / ".lockwayland_blurred.meta"

    try:
        source_mtime = source_path.stat().st_mtime
    except Exception as e:
        raise RuntimeError(f"Could not stat wallpaper {source_path}: {e}")

    cache_data = {
        "source_path": str(source_path),
        "source_mtime": source_mtime,
        "blur_radius": blur_radius,
    }

    if output_path.exists() and meta_path.exists():
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                existing_cache = json.load(f)

            if existing_cache == cache_data:
                return output_path
        except Exception as e:
            logger.warning(f"[Blur] Cache read failed, regenerating: %s", e)

    with Image.open(source_path) as img:
        blurred = img.filter(ImageFilter.GaussianBlur(radius=blur_radius))
        blurred.save(output_path)

    try:
        with open(meta_path, "w", encoding="utf-8") as f:
            json.dump(cache_data, f)
    except Exception as e:
        logger.warning(f"[Blur] Cache write failed: %s", e)

    return output_path

def load_css_provider_from_text(css_text):
    provider = Gtk.CssProvider()
    provider.load_from_data(css_text.encode("utf-8"))
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(),
        provider,
        Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,)

def setup_logging():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",)

if __name__ == "__main__":
    setup_logging()
    app = Gtk.Application(application_id='com.mertt.lockwayland')
    app.connect('activate', on_activate)
    app.run(None)
