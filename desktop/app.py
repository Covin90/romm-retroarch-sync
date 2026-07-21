#!/usr/bin/env python3
"""RomM RetroArch Sync — desktop window shell.

Launches the HTTP backend (backend/server.py) in-process on a free localhost
port, then opens a native WebKitGTK window pointing at it. The engine lives only
as long as this window is open: no background daemon, no systemd unit. On close
we stop the engine cleanly; sync catches up on the next launch via the startup
pass. See desktop/README.md for the architecture.

Runtime deps (host or bundled in the AppImage): PyGObject (gi) with GTK 3 +
WebKit2 4.1, plus the engine's Python deps (requests, watchdog, psutil,
cryptography, Pillow).
"""
import os
import socket
import sys
import threading
from pathlib import Path

# ── Renderer path ───────────────────────────────────────────────────────────
#
# We keep WebKitGTK's DMABUF renderer (GPU-accelerated compositing) — the same
# fast path SteamOS uses on the Deck — but disable NVIDIA's explicit GPU sync,
# which is the specific thing that breaks WebKit on the proprietary driver:
#
#   • Without this, native Wayland + DMABUF dies at startup with a Wayland
#     protocol error (Gdk "Error 71") — the DMABUF/explicit-sync handshake with
#     the NVIDIA EGL driver fails, reproducibly ~100% of the time. (XWayland
#     dodges the crash but then renders a blank GL surface.)
#   • With __NV_DISABLE_EXPLICIT_SYNC=1 the handshake uses implicit sync, the
#     crash is gone, and the UI renders fully GPU-composited — verified on this
#     box (Wayland + RTX 2080 Ti, WebKitGTK 2.52). This is the fix the wider
#     ecosystem converged on for the same bug (Tauri/Yaak/WebKit #261874).
#
# This restores GPU compositing, so live backdrop-filter blur etc. are cheap
# again — but we leave the shim.css blur removal in place as a harmless, no-cost
# simplification. Falling back to the software rasterizer is still possible by
# exporting WEBKIT_DISABLE_DMABUF_RENDERER=1 for machines where even this path
# misbehaves. Must run before WebKit2 is imported.
os.environ.setdefault("__NV_DISABLE_EXPLICIT_SYNC", "1")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2  # noqa: E402

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "backend"))

import server  # noqa: E402  (path setup must precede this)


def _free_port(host="127.0.0.1"):
    """Grab an ephemeral port from the OS and hand it back for reuse.

    Small TOCTOU window between close and rebind, but on loopback with a random
    high port a collision is vanishingly unlikely and the bind would just fail
    loudly rather than silently misbehave.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind((host, 0))
    port = s.getsockname()[1]
    s.close()
    return port


# The plugin UI is authored against the Steam Deck's fixed 1280x800 gamepad
# viewport, where SteamOS scales the whole design to the screen. A raw desktop
# WebView doesn't scale, so on a large monitor the UI renders at literal pixel
# sizes — small, sparse, "unadapted". We reproduce SteamOS's behavior by zooming
# the page so the 800px design height fills the window height.
DESIGN_HEIGHT = 800.0
MIN_ZOOM = 0.5


class AppWindow:
    def __init__(self, url, fullscreen=True):
        self.window = Gtk.Window(title="RomM RetroArch Sync")
        self.window.set_default_size(1280, 800)
        self.window.maximize()  # sensible non-fullscreen state
        self._fullscreen = fullscreen
        self.window.connect("destroy", self._on_destroy)
        self.window.connect("key-press-event", self._on_key)

        self.webview = WebKit2.WebView()
        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(True)
        settings.set_javascript_can_access_clipboard(True)
        # Re-zoom whenever the view is resized (window resize, (un)maximize,
        # (un)fullscreen, monitor change) so scaling always tracks the height.
        self.webview.connect("size-allocate", self._on_size_allocate)
        self.webview.load_uri(url)
        self.window.add(self.webview)

        # Controller input via libmanette (the W3C Gamepad API in WebKit mangles
        # Xbox-over-BT pads). Non-fatal if libmanette or a pad is missing.
        self._gamepad = None
        try:
            from gamepad_bridge import GamepadBridge
            self._gamepad = GamepadBridge(self._inject_js)
        except Exception as e:
            print(f"[gamepad] controller support unavailable: {e}", flush=True)

    def _inject_js(self, js):
        try:
            self.webview.evaluate_javascript(js, -1, None, None, None, None, None)
        except Exception:
            pass

    def show(self):
        if self._fullscreen:
            self.window.fullscreen()
        self.window.show_all()

    def _on_size_allocate(self, _widget, alloc):
        zoom = max(MIN_ZOOM, alloc.height / DESIGN_HEIGHT)
        # Avoid redundant churn (and feedback loops) from tiny deltas.
        if abs(self.webview.get_zoom_level() - zoom) > 0.01:
            self.webview.set_zoom_level(zoom)

    def _on_key(self, _widget, event):
        from gi.repository import Gdk
        if event.keyval == Gdk.KEY_F11:
            self._fullscreen = not self._fullscreen
            (self.window.fullscreen if self._fullscreen
             else self.window.unfullscreen)()
            return True
        if event.keyval == Gdk.KEY_Escape and self._fullscreen:
            self._fullscreen = False
            self.window.unfullscreen()
            return True
        return False

    def _on_destroy(self, *_):
        Gtk.main_quit()


def main():
    host = os.environ.get("ROMM_HOST", "127.0.0.1")
    port = int(os.environ["ROMM_PORT"]) if os.environ.get("ROMM_PORT") else _free_port(host)

    # serve() blocks until the engine is ready and the socket is bound, so by
    # the time it returns the URL is loadable.
    httpd, engine = server.serve(host, port)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()

    # Fullscreen by default (matches the Deck feel); set ROMM_FULLSCREEN=0 for a
    # normal maximized window. F11 toggles at runtime; Escape leaves fullscreen.
    fullscreen = os.environ.get("ROMM_FULLSCREEN", "1") not in ("0", "false", "no")
    app = AppWindow(f"http://{host}:{port}/", fullscreen=fullscreen)
    app.show()

    try:
        Gtk.main()
    finally:
        # GLib may still hold the loop; shut the backend down deterministically.
        try:
            httpd.shutdown()
        except Exception:
            pass
        engine.stop()


if __name__ == "__main__":
    main()
