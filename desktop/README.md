# RomM RetroArch Sync — desktop client

A generic build of the sync app (Linux + Windows), for people running RetroArch
**without** a Steam Deck / Decky. It reuses the Decky plugin's engine and UI
unchanged:

- **UI** — `decky_plugin/src/index.tsx`, consumed byte-identically. A Vite alias
  points `@decky/ui` and `@decky/api` at `src/shim/*` (web implementations of
  the Decky primitives) instead of forking the 9k-line file.
- **Engine** — the plugin's `Plugin` class (`decky_plugin/main.py`) and
  `sync_core.py`, imported as-is by `backend/server.py`. Decky's `callable` IPC
  is replaced by HTTP: the shim's `callable` POSTs to `/api/<method>`, the
  server dispatches to `plugin.<method>(*args)`.

```
browser (webview)  ──POST /api/get_status──▶  backend/server.py ──▶ plugin.get_status()
   shim callable    ◀──── {"result": …} ────                    (sync_core, watchdog)
```

## Run it (dev)

Backend — needs a venv with the engine deps (NOT the Deck-only vendored ones):

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements.txt
.venv/bin/python backend/server.py          # serves API + built UI on :8723
```

Frontend:

```bash
npm install
npm run build            # emits dist/, which the backend serves at :8723
# or, for hot-reload dev with the backend proxied:
npm run dev              # Vite on :5173, /api proxied to :8723
```

Then open <http://127.0.0.1:8723> (built) or <http://127.0.0.1:5173> (dev).

## Native window shell (Electron)

`electron/main.cjs` is the desktop window — one codebase for **Linux and
Windows**. It replaces the old Linux-only GTK3/WebKit2GTK shell (`app.py`, kept
for reference). Same lifecycle as `app.py`: spawn `backend/server.py` on a free
localhost port at launch, load a `BrowserWindow` at it, and stop the backend
cleanly on window close — no background daemon.

```bash
npm run electron       # build the UI, then launch the window
npm run electron:dev   # launch against the Vite dev server (hot reload)
```

The interpreter is `desktop/.venv/bin/python` (or `.venv/Scripts/python.exe` on
Windows) if present, else `python3`/`python`; override with `ROMM_PYTHON`.

What the shell reproduces from `app.py`:

- **Zoom-to-fit** — the UI is authored for the Deck's 1280×800 gamepad viewport,
  so `webContents.setZoomFactor` scales the page so the 800px design height fills
  the window height, re-applied on every resize/(un)fullscreen.
- **Fullscreen by default** — F11 toggles, Escape leaves fullscreen. Set
  `ROMM_FULLSCREEN=0` for a normal window.
- **Gamepad** — `electron/preload.cjs` polls Chromium's native
  `navigator.getGamepads()` and drives the same `window.__rommGamepad` API the UI
  installs. No native module: the GTK build's `libmanette` bridge existed only to
  work around a WebKitGTK Bluetooth-pad bug that Chromium doesn't share. The
  "standard" mapping matches what `src/shim/gamepad.ts` expects (see the mapping
  table in `preload.cjs`). If some pad ever misbehaves, `preload.cjs` is the one
  place to add a `node-hid`/XInput fallback.

The NVIDIA/Wayland `__NV_DISABLE_EXPLICIT_SYNC` workaround from `app.py` is
deliberately **not** ported — it's a WebKitGTK-specific bug.

Installers/AppImage packaging are a separate follow-up.

## Status

Wired and working end-to-end: the engine boots, connects to RomM, and every
`/api/<method>` dispatches to the real plugin backend. The Electron shell boots
the backend, loads the UI, and the round-trip (window → shim `callable` → POST
`/api` → engine) is verified. There is intentionally **no background daemon** —
the engine runs only while the app is open (see the project discussion); sync
catches up on next launch via the startup pass.

## What is NOT here

No Steam system bars and no in-Gaming-Mode background sync — those are
Deck/Decky-native and stay in the plugin. Gamepad *focus navigation* works here
via the shim; this client targets desktop Linux/Windows with a controller or
mouse + keyboard.
