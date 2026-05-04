import os
import time 

import ctypes
import logging
import signal
import sys 

from dataclasses import dataclass

import wayland
from wayland.client import wayland_class
from wayland.client.memory_pool import SharedMemoryPool

from PIL import Image, ImageDraw, ImageFont

from locker import LockController

LOG_PATH = "lockwayland-ext-session.log"
logging.basicConfig(
        level = logging.INFO,
        format = "%(asctime)s %(levelname)s [%(name)s] %(message)s",
        handlers=[
            logging.FileHandler(LOG_PATH, mode="w"),
            logging.StreamHandler(),
            ],
        )
logger = logging.getLogger("lockwayland.ext_session")

@dataclass
class SurfaceState:
    output: "Output"
    wl_surface: "wayland.wl_surface"
    lock_surface: "LockSurface"
    width: int = 0
    height: int = 0
    configured: bool = False
    is_interactive: bool = False
    buffer: object = None

@wayland_class("wl_callback")
class SyncCallback(wayland.wl_callback):
    def __init__(self, app):
        super().__init__(app=app)
        self.app = app
        self.done = False

    def on_done(self, callback_data):
        self.done = True

@wayland_class("wl_display")
class Display(wayland.wl_display):
    def __init__(self, app):
        super().__init__(app=app)
        self.app = app 

    def on_error(self, object_id, code, message):
        logger.error(
                f"Wayland protocol error: objects={object_id} code={code} message={message}"
        )
        self.app.running=False

# Shared memory
@wayland_class("wl_shm")
class Shm(wayland.wl_shm):
    def __init__(self, app):
        super().__init__(app=app)
        self.app = app 
        self.pool = SharedMemoryPool(self)

@wayland_class("wl_output")
class Output(wayland.wl_output):
    def __init__(self, app):
        super().__init__(app=app)
        self.app = app
        self.global_name = None
        self.make = ""
        self.model = ""
        self.description = ""
        self.width = 0
        self.height = 0
        self.done = False

    def on_geometry(
        self,
        x,
        y,
        physical_width,
        physical_height,
        subpixel,
        make,
        model,
        transform,
    ):
        self.make = make
        self.model = model

    def on_mode(self, flags, width, height, refresh):
        if flags & 1:
            self.width = width
            self.height = height

    def on_description(self, description):
        self.description = description

    def on_done(self):
        self.done = True
        self.app.maybe_start_lock()

@wayland_class("ext_session_lock_v1")
class SessionLock(wayland.ext_session_lock_v1):
    def __init__(self, app):
        super().__init__(app=app)
        self.app = app
        self.locked_received = False

    def on_locked(self):
        self.locked_received = True
        logger.info("Session lock established.")

    def on_finished(self):
        logger.warning("Compositor finished the session lock!")
        self.app.running = False

@wayland_class("wl_registry")
class Registry(wayland.wl_registry):
    def __init__(self, app):
        super().__init__(app=app)
        self.app = app
        self.wl_compositor = None
        self.wl_shm = None
        self.ext_session_lock_manager_v1 = None
        self.outputs = {}

    def on_global(self, name, interface, version):
        if interface == "wl_compositor":
            self.wl_compositor = self.bind(name, interface, version)
            logger.info(f"Bound wl_compositor v{version}")

        elif interface == "wl_shm":
            self.wl_shm = self.bind(name, interface, version)
            self.app.shm = self.wl_shm
            logger.info(f"Bound wl_shm v{version}")

        elif interface == "ext_session_lock_manager_v1":
            self.ext_session_lock_manager_v1 = self.bind(name, interface, version)
            logger.info(f"Bound ext_session_lock_manager_v1 v{version}")

        elif interface == "wl_output":
            output = self.bind(name, interface, version)
            output.global_name = name
            self.outputs[name] = output
            logger.info(f"Bound wl_output name={name} v{version}")

            if self.app.session_lock is not None:
                self.app.create_lock_surface_for_output(output)

        self.app.maybe_start_lock()

    def on_global_remove(self, name):
        logger.info(f"Global removed: name={name}")

        if getattr(self.app, "unlock_requested", False):
            logger.info(f"Ignoring global remove during unlock: name={name}")
            return

        if name in self.outputs:
            self.app.remove_output(name)
            del self.outputs[name]

class LockwaylandSessionApp:
    def __init__(self):
        logger.info(f"Lockwayland ext-session test process pid={os.getpid()}")
        # To-do: deactivate this when release
        time.sleep(5)

        self.last_clock_redraw = 0 

        self.shm = None

        self.running = True
        self.unlock_requested = False
        self.lock_started = False

        self.display = Display(app=self)
        self.registry = self.display.get_registry()

        self.session_lock = None
        self.surfaces: dict[int, SurfaceState] = {}

        self.controller = LockController(
            on_unlock_requested=self.request_unlock,
            on_state_changed=self.redraw_all,
        )

        signal.signal(signal.SIGINT, signal.SIG_IGN)
        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        signal.signal(signal.SIGUSR1, self.on_test_unlock_signal)

    def redraw_all(self):
        for output_name in list(self.surfaces.keys()):
            self.redraw(output_name)

    def on_test_unlock_signal(self, signum, frame):
        logger.warning("Received SIGUSR1; test unlock requested")
        self.unlock_requested = True

    def have_required_globals(self) -> bool:
        return all(
            [
                self.registry.wl_compositor is not None,
                self.shm is not None,
                self.registry.ext_session_lock_manager_v1 is not None,
            ]
        )

    def maybe_start_lock(self):
        if self.lock_started:
            return

        if not self.have_required_globals():
            return

        if not self.registry.outputs:
            return

        self.lock_started = True
        logger.info("Starting ext-session-lock")

        self.session_lock = self.registry.ext_session_lock_manager_v1.lock()

        for output in list(self.registry.outputs.values()):
            self.create_lock_surface_for_output(output)

    def maybe_redraw_clock(self):
        now = time.monotonic()
        if now - self.last_clock_redraw >= 1:
            self.last_clock_redraw = now
            self.redraw_all()

    def create_lock_surface_for_output(self, output: Output):
        if output.global_name in self.surfaces:
            return

        wl_surface = self.registry.wl_compositor.create_surface()
        lock_surface = self.session_lock.get_lock_surface(wl_surface, output)
        lock_surface.output_name = output.global_name

        is_interactive = not any(
                existing.is_interactive for existing in self.surfaces.values()
        )

        state = SurfaceState(
                output=output,
                wl_surface=wl_surface,
                lock_surface=lock_surface,
                is_interactive=is_interactive,
        )


        self.surfaces[output.global_name] = state

        logger.info(
            f"Created lock surface for output {output.global_name} "
            f"({output.make} {output.model}) interactive={is_interactive}"
        )

    def remove_output(self, output_name: int):
        state = self.surfaces.pop(output_name, None)
        if state is None:
            return
        
        logger.warning(f"Removing lock surface for output {output_name}")

        try:
            state.lock_surface.destroy()
        except Exception as e:
            logger.warning(
                f"Failed to destroy the lock surface for output {output_name}: {e}"
            )

        try:
            state.wl_surface.destroy()
        except Exception as e:
            logger.warning(
                f"Failed to destroy the wl_surface for output {output_name}: {e}"
            )

    def configure_surface(self, output_name: int, width: int, height: int):
        state = self.surfaces.get(output_name)
        if state is None:
            logger.warning(f"Configure for unknown output {output_name}")
            return

        state.width = width
        state.height = height
        state.configured = True

        logger.info(
            f"Configured lock surface for output {output_name} to {width}x{height}."
        )

        self.redraw(output_name)

    def redraw(self, output_name: int):
        state = self.surfaces.get(output_name)
        if state is None or not state.configured:
            return

        width = state.width
        height = state.height

        logger.info(
                f"Redrawing output {output_name} at {state.width}x{state.height}")

        buffer, ptr = self.shm.pool.create_buffer(width, height)
        state.buffer = buffer

        image = Image.new("RGBA", (width, height), (0,0,0,255))
        draw = ImageDraw.Draw(image)

        clock_font = load_font(72)
        status_font = load_font(24)
        password_font = load_font(32)
        clock_text = self.controller.clock_text()
        status_text = self.controller.status_text
        password_text = self.controller.password_display_text()

        center_x = width // 2 
        center_y = height // 2 

        draw_centered_text(
            draw,
            clock_text, clock_font,
            center_x, center_y - 120,
            (255,255,255,255)
        )

        if state.is_interactive:
            draw_centered_text(
                draw,
                status_text, status_font,
                center_x, center_y - 20,
                (220,220,220,255)
        )

            if password_text:
                draw_centered_text(
                draw,
                password_text, password_font,
                center_x, center_y + 30,
                (255,255,255,255)
            )

        copy_image_to_argb8888(image, ptr)

        state.wl_surface.attach(buffer, 0, 0)
        state.wl_surface.damage_buffer(0, 0, width, height)
        state.wl_surface.commit()
        logger.info(f"Committed buffer for output {output_name}")

    def perform_unlock(self):
        if self.session_lock is None:
            logger.warning("Unlock requested before session lock exists!")
            self.running = False
            return

        if not self.session_lock.locked_received:
            logger.warning("Unlock requested before locked event. Ignoring.")
            self.unlock_requested = False
            return

        logger.info("Unlocking session.")
        self.session_lock.unlock_and_destroy()

        # Give the compositor a chance to process unlock_and_destroy().
        try:
            self.display.dispatch_timeout(0.05)
            self.display.dispatch_timeout(0.05)
            self.display.dispatch_timeout(0.05)
        except Exception as e:
            logger.warning(f"Dispatch after unlock failed: {e}")
        self.running = False

    def run(self):
        logger.info("Waiting for Wayland events.")
        while self.running:
            if self.unlock_requested:
                self.perform_unlock()

            self.maybe_redraw_clock()
            self.display.dispatch_timeout(1/30)

        logger.info("Exiting.")

    def request_unlock(self):
        logger.info("Controller requested unlock")
        self.unlock_requested = True

@wayland_class("ext_session_lock_surface_v1")
class LockSurface(wayland.ext_session_lock_surface_v1):
    def __init__(self, app):
        super().__init__(app=app)
        self.app = app
        self.output_name = None

    def on_configure(self, serial, width, height):
        self.ack_configure(serial)
        
        if self.output_name is None:
            logger.warning(
                "Lock surface configured before output_name was set.")
            return

        self.app.configure_surface(self.output_name, width, height)

# Classless functions / helpers
def load_font(size):
    try:
        return ImageFont.truetype("DejaVuSans.ttf", size)
    except OSError:
        return ImageFont.load_default()

def draw_centered_text(draw, text, font, x_center, y, fill):
    bbox = draw.textbbox((0, 0), text, font=font)
    text_width = bbox[2] - bbox[0]
    draw.text((x_center - text_width // 2, y), text, font=font, fill=fill)

def copy_image_to_argb8888(image, ptr):
    image = image.convert("RGBA")
    pixels = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32))

    for i, (r, g, b, a) in enumerate(image.getdata()):
        pixels[i] = (a<<24) | (r<<16) | (g<<8) | b

if __name__ == "__main__":
    try:
        app = LockwaylandSessionApp()
        app.run()
    except KeyboardInterrupt:
        pass 
    except Exception as e:
        logger.exception(f"Fatal error! {e}")
        sys.exit(1)
