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

# WebKitGTK's DMABUF renderer trips a Wayland protocol error (Gdk "Error 71")
# on many compositors, killing the window at startup. Disabling it forces the
# stable GLES path. Must be set before WebKit2 is imported. Overridable.
os.environ.setdefault("WEBKIT_DISABLE_DMABUF_RENDERER", "1")

import gi

gi.require_version("Gtk", "3.0")
gi.require_version("WebKit2", "4.1")
from gi.repository import Gtk, WebKit2, GLib  # noqa: E402

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


class AppWindow:
    def __init__(self, url):
        self.window = Gtk.Window(title="RomM RetroArch Sync")
        self.window.set_default_size(1100, 780)
        self.window.connect("destroy", self._on_destroy)

        self.webview = WebKit2.WebView()
        settings = self.webview.get_settings()
        settings.set_enable_developer_extras(True)
        settings.set_javascript_can_access_clipboard(True)
        self.webview.load_uri(url)
        self.window.add(self.webview)

    def show(self):
        self.window.show_all()

    def _on_destroy(self, *_):
        Gtk.main_quit()


def main():
    host = os.environ.get("ROMM_HOST", "127.0.0.1")
    port = int(os.environ["ROMM_PORT"]) if os.environ.get("ROMM_PORT") else _free_port(host)

    # serve() blocks until the engine is ready and the socket is bound, so by
    # the time it returns the URL is loadable.
    httpd, engine = server.serve(host, port)
    threading.Thread(target=httpd.serve_forever, name="http", daemon=True).start()

    app = AppWindow(f"http://{host}:{port}/")
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
