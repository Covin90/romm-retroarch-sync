# Decky Plugin Deployment Guide

> **For AI agents and developers:** This file documents the complete build and packaging process
> for the RomM Sync Monitor Decky plugin. Read this before making any changes to the build process.

---

## Overview

The plugin has two components:
- **Frontend** — TypeScript/React in `src/index.tsx`, compiled to `dist/index.js` via rollup
- **Backend** — `main.py` + `py_modules/sync_core.py` (Python, runs inside Decky Loader)

`py_modules/sync_core.py` is a **dev symlink** to `../../src/sync_core.py`. It is resolved to a
real file during the build step. Never commit the resolved copy — the symlink is intentional.

---

## Build & Package

Run the build script from the `decky_plugin/` directory:

```bash
cd decky_plugin
./decky-build.sh
```

This will:
1. Run `pnpm run build`:
   - **prebuild**: copies `../src/sync_core.py` → `py_modules/sync_core.py` (resolves the symlink)
   - **build**: runs rollup → `dist/index.js` + `dist/index.js.map`
2. Package everything into `../romm-sync-monitor.zip`

The output zip is at the **repo root**: `romm-sync-monitor.zip`

---

## Required files in the ZIP

Decky Loader's installer validates all of these. **Any missing file causes silent installation failure.**

| File | Required | Why |
|------|----------|-----|
| `plugin.json` | YES | Plugin metadata — see rules below |
| `package.json` | YES | Decky Loader validator requires it |
| `LICENSE` | YES | Decky Loader validator requires it |
| `main.py` | YES | Python backend entrypoint |
| `dist/index.js` | YES | Compiled frontend |
| `dist/index.js.map` | YES | Source map |
| `py_modules/sync_core.py` | YES | Sync daemon logic |
| `assets/logo.png` | NO | Plugin icon |

### ZIP structure

The zip must have a **single top-level directory** named `romm-sync-monitor/`:

```
romm-sync-monitor/
  plugin.json
  package.json
  LICENSE
  main.py
  dist/
    index.js
    index.js.map
  py_modules/
    sync_core.py
  assets/
    logo.png
```

---

## plugin.json rules

- `"flags"` **must be `[]`** — setting `["_root"]` silently blocks ZIP installation in Decky Loader
- `"api_version"` must be `2`
- `"name"` is the display name shown in Decky ("RomM Sync Monitor")

---

## Installation on Steam Deck

1. Transfer `romm-sync-monitor.zip` to the Steam Deck
2. In Decky Loader: **gear icon → "Install plugin from ZIP"**
3. Select the zip file

Do **not** restart Decky Loader after installation — use the Decky QAM reload button if needed.

---

## Prerequisites

- `pnpm` must be available (`which pnpm`)
- `node` ≥ 18
- `zip` utility

---

## Known gotchas

- `zip --prefix` is not supported on this system — `decky-build.sh` uses a temp dir instead
- The `_root` flag in `plugin.json` silently blocks ZIP installation (no error shown in UI)
- `package.json` and `LICENSE` are not used at runtime but are required by the Decky validator
- The symlink at `py_modules/sync_core.py` must not be committed as a regular file
