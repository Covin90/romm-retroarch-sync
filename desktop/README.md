# RomM RetroArch Sync — desktop client

A generic Linux build of the sync app, for people running RetroArch **without**
a Steam Deck / Decky. It reuses the Decky plugin's engine and UI unchanged:

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

## Status

Wired and working end-to-end: the engine boots, connects to RomM, and every
`/api/<method>` dispatches to the real plugin backend. Not yet done: a native
window shell (pywebview) and AppImage/Flatpak packaging. There is intentionally
**no background daemon** — the engine runs only while the app is open (see the
project discussion); sync catches up on next launch via the startup pass.

## What is NOT here

No gamepad focus navigation, no Steam system bars, no in-Gaming-Mode background
sync — those are Deck/Decky-native and stay in the plugin. This client targets
desktop Linux with mouse + keyboard.
