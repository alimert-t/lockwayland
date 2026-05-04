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

from locker import LockController

logging.basicConfig(
        level = logging.INFO,
        format = "%(asctime)s %(levelname)s [%(name)s] %(message)s"
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

        if name in self.outputs:
            self.app.remove_output(name)
            del self.outputs[name]

class LockwaylandSessionApp:
    def __init__(self):
        logger.info(f"Lockwayland ext-session test process pid={os.getpid()}")
        time.sleep(5)

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

    def create_lock_surface_for_output(self, output: Output):
        if output.global_name in self.surfaces:
            return

        wl_surface = self.registry.wl_compositor.create_surface()
        lock_surface = self.session_lock.get_lock_surface(wl_surface, output)

        state = SurfaceState(
            output=output,
            wl_surface=wl_surface,
            lock_surface=lock_surface,
        )

        self.surfaces[output.global_name] = state 

        # initial testing without a buffer
        # the protocol forbids attaching/committing
        # a buffer before the first configure
        wl_surface.commit()

        logger.info(
            f"Created lock surface for output {output.global_name} ({output.make} {output.model})"
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

        buffer, ptr = self.shm.pool.create_buffer(width, height)

        # Fill ARGB8888 little-endian buffer with opaque black.
        pixels = ctypes.cast(ptr, ctypes.POINTER(ctypes.c_uint32))
        count = width * height
        for i in range(count):
            pixels[i] = 0xFF000000

        state.wl_surface.attach(buffer, 0, 0)
        state.wl_surface.damage_buffer(0, 0, width, height)
        state.wl_surface.commit()

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
        self.running = False

    def run(self):
        logger.info("Waiting for Wayland events.")
        while self.running:
            if self.unlock_requested:
                self.perform_unlock()

            self.display.dispatch_timeout(1/30)

        logger.info("Exiting.")

    def request_unlock(self):
        logger.info("Controller requested unlock")
        self.unlock_requested = True

@wayland_class("ext_session_lock_surface_v1")
class LockSurface(wayland.ext_session_lock_surface_v1):
    def __init__(self, app, output_name):
        super().__init__(app=app)
        self.app = app
        self.output_name = output_name

    def on_configure(self, serial, width, height):
        self.ack_configure(serial)
        self.app.configure_surface(self.output_name, width, height)

if __name__ == "__main__":
    try:
        app = LockwaylandSessionApp()
        app.run()
    except KeyboardInterrupt:
        pass 
    except Exception as e:
        logger.exception(f"Fatal error! {e}")
        sys.exit(1)
