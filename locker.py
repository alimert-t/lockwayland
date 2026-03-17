import gi
import os
import pwd
import pam
import signal
import datetime
import threading
from pydbus import SystemBus

gi.require_version('Gtk', '4.0')
gi.require_version('Gtk4LayerShell', '1.0')
from gi.repository import Gtk, Gtk4LayerShell, GLib, Gdk

# Ignore exit signals for security
signal.signal(signal.SIGINT, signal.SIG_IGN)
signal.signal(signal.SIGTERM, signal.SIG_IGN)

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
            print("[Fingerprint] Scanner active")
        except Exception as e:
            print(f"[Fingerprint] Init failed (maybe already running?): {e}")

    def cleanup(self):
        if not self.device:
            return
    
        try:
            self.device.VerifyStop()
        except Exception as e:
            print(f"[Fingerprint] VerifyStop failed: {e}")

        if self.claimed:
            try:
                self.device.Release()
                self.claimed = False
            except Exception as e:
                print(f"[Fingerprint] Release failed: {e}")

        self.claimed = False
        self.device = None

    def on_verify_status(self, result, done):
        if result == "verify-match":
            GLib.idle_add(self.on_success)
        elif not done:
            try:
                self.device.VerifyStart("any")
            except Exception as e:
                print(f"[Fingerprint] Verify restart failed: {e}")

class LockScreen(Gtk.ApplicationWindow):
    def __init__(self, monitor, is_primary, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.username = get_username()

        self.auth_in_progress = False

        # Layer shell must be called before window is realized
        Gtk4LayerShell.init_for_window(self)
        Gtk4LayerShell.set_monitor(self, monitor)
        Gtk4LayerShell.set_layer(self, Gtk4LayerShell.Layer.OVERLAY)
        Gtk4LayerShell.set_namespace(self, "lockscreen")
        Gtk4LayerShell.set_exclusive_zone(self, -1)
        
        if is_primary:
            Gtk4LayerShell.set_keyboard_mode(self, Gtk4LayerShell.KeyboardMode.EXCLUSIVE)
        
        for edge in [Gtk4LayerShell.Edge.LEFT, Gtk4LayerShell.Edge.RIGHT, 
                    Gtk4LayerShell.Edge.TOP, Gtk4LayerShell.Edge.BOTTOM]:
            Gtk4LayerShell.set_anchor(self, edge, True)

        # ui
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        self.box.set_valign(Gtk.Align.CENTER)
        self.box.set_halign(Gtk.Align.CENTER)

        self.label_clock = Gtk.Label()
        self.box.append(self.label_clock)

        if is_primary:
            self.label_status = Gtk.Label(label="Password or Fingerprint")
            self.password_entry = Gtk.Entry(visibility=False)
            self.password_entry.connect("activate", self.on_pass_submit)
            self.box.append(self.label_status)
            self.box.append(self.password_entry)
        
        self.set_child(self.box)
        self.update_clock()
        GLib.timeout_add(1000, self.update_clock)

    def update_clock(self):
        now = datetime.datetime.now().strftime("%H:%M")
        # todo: make this customizeble via config
        self.label_clock.set_markup(
                f"<span size='60000' weight='bold' color='white'>{now}</span>")
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
        ok = pam.pam().authenticate(self.username, password, service="lockwayland")
        password = None

        if ok:
            GLib.idle_add(request_unlock, self.get_application())
        else:
            GLib.idle_add(self.fail)

    def fail(self):
        self.auth_in_progress = False
        if hasattr(self, 'password_entry'):
            self.label_status.set_label("Authentication failed!")
            self.password_entry.set_text("")
            
            def reset_password_entry():
                self.password_entry.set_sensitive(True)
                self.password_entry.grab_focus()
                self.label_status.set_label("Password or Fingerprint")
                return False

            GLib.timeout_add(500, reset_password_entry)

def get_username():
    return pwd.getpwuid(os.getuid()).pw_name

def request_unlock(app):
    if hasattr(app, "fprint") and app.fprint:
        app.fprint.cleanup()
    
    for window in app.get_windows():
        window.close()

    app.quit()

def on_activate(app):
    display = Gdk.Display.get_default()
    monitors = display.get_monitors()
    
    # Spawn windows in every monitor that is already there 
    # todo: check if a new monitor is plugged in,
    # and spawn the lockscreen there as well.
    for i in range(monitors.get_n_items()):
        monitor = monitors.get_item(i)
        win = LockScreen(monitor, is_primary=(i == 0), application=app)
        win.present()
    
    # Start fingerprint once for the whole app
    app.fprint = FingerprintManager(lambda: request_unlock(app))

if __name__ == "__main__":
    app = Gtk.Application(application_id='com.mertt.lockwayland')
    app.connect('activate', on_activate)
    app.run(None)
