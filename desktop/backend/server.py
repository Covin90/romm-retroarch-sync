#!/usr/bin/env python3
"""Desktop HTTP backend for the RomM RetroArch Sync UI.

The desktop app has no Decky, so the React bundle (served here as static files)
can't reach the Python engine through Decky's `callable` IPC. This server is the
bridge the @decky/api shim's `callable` targets: it imports the plugin's Plugin
class UNMODIFIED and dispatches POST /api/<method> onto its coroutine methods.

  browser  --POST /api/get_status {args:[...]}-->  this server
                                                      -> await plugin.get_status(*args)
                                                   <-- {"result": ...}

The engine (sync_core, watchdog, the RomM client) is the same code the plugin
runs. Only the transport differs: HTTP here, Decky IPC there.
"""
import asyncio
import inspect
import json
import mimetypes
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

# ── Import the plugin engine ────────────────────────────────────────────────
#
# The vendored decky_plugin/py_modules ships Deck-only binary builds (PIL etc.
# compiled for cpython-311). main.py prepends py_modules to sys.path at import
# time, so those would shadow our environment's working builds. Pre-importing
# the good copies caches them in sys.modules, and the late path insertion then
# resolves them from cache. sync_core/activity_log are pure Python and load
# fine from py_modules.
for _dep in ("PIL.Image", "requests", "watchdog", "psutil"):
    try:
        __import__(_dep)
    except Exception:
        pass  # surfaced later if a method actually needs it

REPO_ROOT = Path(__file__).resolve().parents[2]
PLUGIN_DIR = REPO_ROOT / "decky_plugin"

sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(PLUGIN_DIR))
sys.path.append(str(PLUGIN_DIR / "py_modules"))  # activity_log lives here only

# Decky injected these; supply equivalents so main.py's module-level reads work.
os.environ.setdefault("DECKY_PLUGIN_RUNTIME_DIR",
                      str(Path.home() / ".config" / "romm-retroarch-sync"))
if not os.environ.get("DECKY_PLUGIN_VERSION"):
    try:
        pkg = json.loads((PLUGIN_DIR / "package.json").read_text())
        os.environ["DECKY_PLUGIN_VERSION"] = pkg.get("version", "0.0.0")
    except Exception:
        pass

import main as plugin_module  # noqa: E402  (path setup must precede this)

STATIC_ROOT = Path(__file__).resolve().parent.parent / "dist"


# ── Async engine on a background loop ───────────────────────────────────────

class Engine:
    """Owns the Plugin instance and the asyncio loop its coroutines run on.

    The loop runs in its own thread; HTTP handler threads submit coroutines to
    it with run_coroutine_threadsafe and block on the result. This keeps every
    plugin method — and the sync threads they spawn — on one consistent loop.
    """

    def __init__(self):
        self.loop = asyncio.new_event_loop()
        self.plugin = plugin_module.Plugin()
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, name="engine-loop",
                                         daemon=True)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        # _main() constructs the sync managers and starts the watchdog threads,
        # exactly as Decky's load path does.
        self.loop.run_until_complete(self.plugin._main())
        self._ready.set()
        self.loop.run_forever()

    def start(self, timeout=60):
        self._thread.start()
        if not self._ready.wait(timeout):
            raise RuntimeError("engine _main() did not complete in time")

    def call(self, method: str, args: list):
        fn = getattr(self.plugin, method, None)
        # Only public coroutine methods are reachable — never dunders, private
        # helpers, or plain attributes.
        if (method.startswith("_") or fn is None
                or not inspect.iscoroutinefunction(fn)):
            raise LookupError(method)
        fut = asyncio.run_coroutine_threadsafe(fn(*args), self.loop)
        return fut.result()

    def stop(self):
        try:
            fut = asyncio.run_coroutine_threadsafe(self.plugin._unload(),
                                                   self.loop)
            fut.result(timeout=10)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)


ENGINE: Engine | None = None


# ── Non-plugin RPC: filesystem listing for the file picker ──────────────────

def shim_list_dir(path: str, include_files: bool = False) -> dict:
    """Backs the shim's openFilePicker — a browser can't enumerate the FS."""
    p = Path(path).expanduser()
    if not p.is_dir():
        p = Path.home()
    entries = []
    try:
        for child in sorted(p.iterdir(),
                            key=lambda c: (not c.is_dir(), c.name.lower())):
            is_dir = child.is_dir()
            if not is_dir and not include_files:
                continue
            if child.name.startswith("."):
                continue
            entries.append({"name": child.name, "path": str(child),
                            "isdir": is_dir})
    except PermissionError:
        pass
    parent = str(p.parent) if p.parent != p else None
    return {"path": str(p), "parent": parent, "entries": entries}


ASSETS_DIR = (PLUGIN_DIR / "assets").resolve()


def get_image(path: str = ""):
    """Serve bundled /assets/* images locally, delegate the rest to the engine.

    The plugin's get_image fetches every path over the authenticated RomM
    session and returns nothing when there's no connection (main.py: `if not
    self._romm_client.authenticated: return`). On the Deck the plugin is always
    connected, so V2Bg's default background art (`/assets/auth_background.svg`)
    loads. On desktop — and especially in the setup wizard, which runs BEFORE
    any connection exists — that fetch fails and the wizard falls back to a flat
    colour. Those /assets/* files are shipped in the plugin bundle, so serve
    them straight off disk (no connection needed); anything else falls through
    to the engine's get_image exactly as before.
    """
    if path.startswith("/assets/"):
        f = (ASSETS_DIR / path[len("/assets/"):]).resolve()
        if ASSETS_DIR in f.parents and f.is_file():
            import base64
            mime = mimetypes.guess_type(str(f))[0] or "application/octet-stream"
            b64 = base64.b64encode(f.read_bytes()).decode()
            return {"success": True, "data_uri": f"data:{mime};base64,{b64}"}
    return ENGINE.call("get_image", [path])


LOCAL_RPC = {"shim_list_dir": shim_list_dir, "get_image": get_image}


# ── HTTP ────────────────────────────────────────────────────────────────────

class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quieter default logging
        pass

    def _json(self, code: int, payload: dict):
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            self._json(404, {"error": "not found"})
            return
        method = unquote(parsed.path[len("/api/"):])
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            args = (json.loads(raw or b"{}") or {}).get("args", [])
        except Exception as e:
            self._json(400, {"error": f"bad request: {e}"})
            return

        try:
            if method in LOCAL_RPC:
                result = LOCAL_RPC[method](*args)
            else:
                result = ENGINE.call(method, args)
            self._json(200, {"result": result})
        except LookupError:
            self._json(404, {"error": f"unknown method: {method}"})
        except Exception as e:
            # Mirror the plugin convention: surface the error to the UI rather
            # than 500ing, so call sites' try/catch can present it.
            self._json(200, {"error": str(e)})

    def do_GET(self):
        parsed = urlparse(self.path)
        rel = unquote(parsed.path.lstrip("/")) or "index.html"
        target = (STATIC_ROOT / rel).resolve()
        # SPA + traversal guard: anything outside dist/, or any unknown path,
        # falls back to index.html so client-side routes resolve.
        if STATIC_ROOT not in target.parents and target != STATIC_ROOT:
            target = STATIC_ROOT / "index.html"
        if not target.is_file():
            target = STATIC_ROOT / "index.html"
        if not target.is_file():
            self._json(404, {"error": "no built UI — run `npm run build`"})
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        data = target.read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)


def serve(host="127.0.0.1", port=8723):
    """Start the engine + HTTP server and return (httpd, engine).

    Blocks until the engine is ready and the socket is bound, then returns
    without serving — the caller drives serve_forever() (in a thread for the
    desktop shell, or directly for the CLI). This lets the window shell run the
    backend in-process and know exactly when the UI can be loaded.
    """
    global ENGINE
    ENGINE = Engine()
    print("[server] starting engine…", flush=True)
    ENGINE.start()
    print("[server] engine ready", flush=True)

    httpd = ThreadingHTTPServer((host, port), Handler)
    print(f"[server] listening on http://{host}:{port}", flush=True)
    return httpd, ENGINE


def main():
    host = os.environ.get("ROMM_HOST", "127.0.0.1")
    port = int(os.environ.get("ROMM_PORT", "8723"))
    httpd, engine = serve(host, port)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        engine.stop()


if __name__ == "__main__":
    main()
