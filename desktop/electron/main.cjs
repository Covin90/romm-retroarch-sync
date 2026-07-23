// RomM RetroArch Sync — Electron desktop shell (Linux + Windows).
//
// Replaces the GTK3/WebKit2GTK shell (desktop/app.py). Same lifecycle: spawn the
// existing Python backend (backend/server.py) on a free localhost port at launch,
// load a BrowserWindow at it, and stop the backend cleanly on window close — no
// background daemon. See desktop/README.md for the architecture.
//
// Differences from app.py, on purpose:
//   • Gamepad input uses Chromium's native Gamepad API (see preload.js), not the
//     libmanette bridge — that native workaround existed only for a WebKitGTK
//     Bluetooth-pad bug that Chromium doesn't share.
//   • No __NV_DISABLE_EXPLICIT_SYNC: that's a WebKitGTK/NVIDIA-Wayland bug and
//     doesn't apply to Electron's Chromium renderer.

const { app, BrowserWindow, ipcMain } = require("electron");
const { spawn } = require("child_process");
const net = require("net");
const path = require("path");
const fs = require("fs");

// ── GPU / compositing (Linux) ────────────────────────────────────────────────
//
// Chromium (and so Steam Big Picture, which is CEF) performs badly on Linux when
// its GPU process fails to initialize: it either drops to the software
// rasterizer or renders through XWayland, where a hardcoded 60Hz vsync path
// stutters. On a Wayland session we ask Ozone to run NATIVELY on Wayland
// (`auto` = Wayland when in a Wayland session, X11 otherwise), which restores
// GPU compositing and proper frame pacing instead of the XWayland fallback.
// Opt out with ROMM_OZONE=0 (or force a backend, e.g. ROMM_OZONE=x11) if a
// specific driver misbehaves.
if (process.platform === "linux") {
  const ozone = process.env.ROMM_OZONE ?? "auto";
  if (ozone && !["0", "false", "no"].includes(ozone.toLowerCase())) {
    app.commandLine.appendSwitch("ozone-platform-hint", ozone);
    app.commandLine.appendSwitch("enable-features", "WaylandWindowDecorations");
  }
}

const DESKTOP_DIR = path.resolve(__dirname, "..");
const SERVER_PY = path.join(DESKTOP_DIR, "backend", "server.py");

// The plugin UI is authored against the Deck's fixed 1280x800 gamepad viewport,
// where SteamOS scales the whole design to the screen. We reproduce that by
// zooming the page so the 800px design height fills the window height.
const DESIGN_HEIGHT = 800;
const MIN_ZOOM = 0.5;

let backend = null; // the Python child process
let win = null;

// ── Backend lifecycle ────────────────────────────────────────────────────────

// Grab an ephemeral port from the OS and hand it back (same approach as
// app.py._free_port). Tiny TOCTOU window, but on loopback with a random high
// port a collision is vanishingly unlikely and a failed bind is loud, not silent.
function freePort(host = "127.0.0.1") {
  return new Promise((resolve, reject) => {
    const srv = net.createServer();
    srv.on("error", reject);
    srv.listen(0, host, () => {
      const { port } = srv.address();
      srv.close(() => resolve(port));
    });
  });
}

// Locate the Python interpreter: prefer a project venv (created per README),
// then an explicit override, then the platform default.
function pythonExe() {
  if (process.env.ROMM_PYTHON) return process.env.ROMM_PYTHON;
  const venv = process.platform === "win32"
    ? path.join(DESKTOP_DIR, ".venv", "Scripts", "python.exe")
    : path.join(DESKTOP_DIR, ".venv", "bin", "python");
  if (fs.existsSync(venv)) return venv;
  return process.platform === "win32" ? "python" : "python3";
}

// Start backend/server.py on `host:port` and resolve once it announces it's
// listening. server.serve() binds the socket only after the engine is ready, so
// the "[server] listening" line means the URL is loadable.
function startBackend(host, port) {
  return new Promise((resolve, reject) => {
    const env = { ...process.env, ROMM_HOST: host, ROMM_PORT: String(port) };
    const child = spawn(pythonExe(), [SERVER_PY], {
      env,
      // Line-buffered pipes so we can watch for the readiness banner.
      stdio: ["ignore", "pipe", "pipe"],
    });
    backend = child;

    let ready = false;
    const onData = (buf) => {
      const text = buf.toString();
      process.stdout.write(text); // mirror engine logs to the shell console
      if (!ready && text.includes("[server] listening")) {
        ready = true;
        resolve();
      }
    };
    child.stdout.on("data", onData);
    child.stderr.on("data", (b) => process.stderr.write(b.toString()));

    child.on("error", reject);
    child.on("exit", (code) => {
      backend = null;
      if (!ready) {
        reject(new Error(`backend exited before ready (code ${code})`));
      } else if (win) {
        // Engine died while the window is up — nothing to show; quit.
        app.quit();
      }
    });
  });
}

// Stop the backend the way app.py does on window close: cleanly, so the engine's
// _unload() runs. server.py's main() calls engine.stop() on KeyboardInterrupt /
// normal exit, so on POSIX we send SIGINT to trigger that path; Windows has no
// usable SIGINT-to-child, so we terminate and let the daemon threads die.
function stopBackend() {
  if (!backend) return;
  const child = backend;
  backend = null;
  try {
    child.kill(process.platform === "win32" ? undefined : "SIGINT");
  } catch { /* already gone */ }
  // Hard-stop if it doesn't exit promptly.
  setTimeout(() => { try { child.kill("SIGKILL"); } catch {} }, 3000).unref();
}

// ── Window ───────────────────────────────────────────────────────────────────

// Reproduce app.py._on_size_allocate: zoom so the 800px design height fills the
// actual content height, re-applied on every resize/(un)fullscreen/monitor move.
function applyZoom() {
  if (!win) return;
  const [, height] = win.getContentSize();
  const zoom = Math.max(MIN_ZOOM, height / DESIGN_HEIGHT);
  win.webContents.setZoomFactor(zoom);
}

function createWindow(url, fullscreen) {
  win = new BrowserWindow({
    width: 1280,
    height: 800,
    title: "RomM RetroArch Sync",
    backgroundColor: "#0b0e14",
    fullscreen,
    autoHideMenuBar: true,
    webPreferences: {
      preload: path.join(__dirname, "preload.cjs"),
      // The page and preload share one world so the gamepad poller can call the
      // window.__rommGamepad API the app installs. This is a local, first-party
      // bundle over loopback — no remote content — so it's a safe trade.
      contextIsolation: false,
      nodeIntegration: false,
      // setZoomFactor must not be clamped/undone by pinch or Ctrl+wheel.
      zoomFactor: 1,
    },
  });

  win.setMenuBarVisibility(false);

  // Keep zoom tracking the height. setZoomFactor only sticks once a document is
  // committed, so (re)apply on load and on every resize.
  win.webContents.on("did-finish-load", applyZoom);
  win.on("resize", applyZoom);
  win.on("enter-full-screen", applyZoom);
  win.on("leave-full-screen", applyZoom);

  // F11 toggles fullscreen; Escape leaves it — matching app.py._on_key.
  win.webContents.on("before-input-event", (event, input) => {
    if (input.type !== "keyDown") return;
    if (input.key === "F11") {
      win.setFullScreen(!win.isFullScreen());
      event.preventDefault();
    } else if (input.key === "Escape" && win.isFullScreen()) {
      win.setFullScreen(false);
      event.preventDefault();
    }
  });

  win.on("closed", () => { win = null; });
  win.loadURL(url);
}

// ── Boot ─────────────────────────────────────────────────────────────────────

async function boot() {
  const host = process.env.ROMM_HOST || "127.0.0.1";
  const port = process.env.ROMM_PORT
    ? parseInt(process.env.ROMM_PORT, 10)
    : await freePort(host);

  await startBackend(host, port);

  // Fullscreen by default (matches the Deck feel); ROMM_FULLSCREEN=0 for a normal
  // window. F11 toggles at runtime; Escape leaves fullscreen.
  const fullscreen = !["0", "false", "no"].includes(
    (process.env.ROMM_FULLSCREEN || "1").toLowerCase()
  );

  // ROMM_URL lets a dev point at the Vite server (which proxies /api to the
  // backend on ROMM_PORT); default loads the backend-served built UI.
  const url = process.env.ROMM_URL || `http://${host}:${port}/`;
  createWindow(url, fullscreen);
}

// Single instance: a second launch focuses the existing window rather than
// spawning a rival backend.
if (!app.requestSingleInstanceLock()) {
  app.quit();
} else {
  // Renderer's Exit row (desktop-only UserMenu item) asks the shell to quit.
  ipcMain.on("romm:quit", () => app.quit());

  app.on("second-instance", () => {
    if (win) { if (win.isMinimized()) win.restore(); win.focus(); }
  });

  app.whenReady().then(boot).catch((err) => {
    console.error("[electron] failed to start:", err);
    app.quit();
  });

  app.on("window-all-closed", () => {
    stopBackend();
    app.quit();
  });

  // Ensure the backend is reaped even on unusual exit paths.
  app.on("before-quit", stopBackend);
  process.on("exit", stopBackend);
}
