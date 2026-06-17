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

Run the following from the **repo root**:

```bash
# Step 1: Build frontend
cd decky_plugin && pnpm run build && cd ..

# Step 2: Package into zip
PLUGIN_NAME="romm-sync-monitor"
PLUGIN_DIR="decky_plugin"
OUT_ZIP="${PLUGIN_NAME}.zip"
TMP_DIR=$(mktemp -d)
mkdir -p "${TMP_DIR}/${PLUGIN_NAME}/dist" "${TMP_DIR}/${PLUGIN_NAME}/assets" "${TMP_DIR}/${PLUGIN_NAME}/bin"
cp "${PLUGIN_DIR}/plugin.json" "${PLUGIN_DIR}/package.json" "${PLUGIN_DIR}/LICENSE" "${PLUGIN_DIR}/main.py" "${TMP_DIR}/${PLUGIN_NAME}/"
cp "${PLUGIN_DIR}/dist/index.js" "${PLUGIN_DIR}/dist/index.js.map" "${TMP_DIR}/${PLUGIN_NAME}/dist/"
cp -rL "${PLUGIN_DIR}/py_modules" "${TMP_DIR}/${PLUGIN_NAME}/"
cp "${PLUGIN_DIR}/assets/logo.png" "${TMP_DIR}/${PLUGIN_NAME}/assets/"
cp "${PLUGIN_DIR}/bin/7zz" "${TMP_DIR}/${PLUGIN_NAME}/bin/" && chmod +x "${TMP_DIR}/${PLUGIN_NAME}/bin/7zz"
(cd "$TMP_DIR" && zip -r "$OUT_ZIP" "${PLUGIN_NAME}/")
mv "$TMP_DIR/$OUT_ZIP" .
rm -rf "$TMP_DIR"
```

The output zip is at the **repo root**: `romm-sync-monitor.zip` (~8 MB with bundled libs).

**Why `cp -rL`?** `py_modules/sync_core.py` and `py_modules/bios_manager.py` are symlinks in
the dev tree. The `-L` flag dereferences all symlinks so real file content goes into the zip.
Without it, the zip contains broken symlinks and the plugin fails to load.

### Release Naming Convention

For GitHub releases, rename the ZIP to follow the project's naming convention:

```
RomM-RetroArch-Sync-v<VERSION>-decky.zip
```

Example: `RomM-RetroArch-Sync-v1.5-decky.zip`

After building, rename with:
```bash
mv romm-sync-monitor.zip RomM-RetroArch-Sync-v1.5-decky.zip
```

This matches the AppImage naming pattern: `RomM-RetroArch-Sync-v<VERSION>.AppImage`

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
| `py_modules/sync_core.py` | YES | Sync daemon logic (symlink in dev — must be real file in zip) |
| `py_modules/bios_manager.py` | YES | BIOS management logic (symlink in dev — must be real file in zip) |
| `py_modules/requests/` | YES | Bundled dependency (not on SteamOS) |
| `py_modules/watchdog/` | YES | Bundled dependency (not on SteamOS) |
| `py_modules/PIL/` | YES | Bundled dependency (Pillow for image processing) |
| `py_modules/pillow.libs/` | YES | Bundled shared libs for Pillow C extensions |
| `py_modules/urllib3/`, `certifi/`, `charset_normalizer/`, `idna/` | YES | Transitive deps of requests |
| `assets/logo.png` | NO | Plugin icon |
| `bin/7zz` | NO | Static 7-Zip (x64) for `.7z` extraction — SteamOS has no system 7z. `sync_core._find_7z()` resolves `<plugin>/bin/7zz`. Must be `chmod +x`. Without it: `.7z` console ROMs still load via RetroArch, `.7z` PC games won't auto-extract. |

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
    sync_core.py        ← real file (symlink dereferenced by cp -rL)
    bios_manager.py     ← real file (symlink dereferenced by cp -rL)
    requests/
    watchdog/
    PIL/
    pillow.libs/
    urllib3/
    certifi/
    charset_normalizer/
    idna/
  assets/
    logo.png
```

---

## plugin.json rules

- `"flags"` **must be `[]`** — setting `["_root"]` silently blocks ZIP installation in Decky Loader
- `"api_version"` must be `2`
- `"name"` is the display name shown in Decky ("RomM Sync Monitor")

---

## Installation on SteamOS

1. Transfer the ZIP file (e.g., `RomM-RetroArch-Sync-v1.5-decky.zip`) to the SteamOS device
2. In Decky Loader: **gear icon → "Install plugin from ZIP"**
3. Select the zip file

Do **not** restart Decky Loader after installation — use the Decky QAM reload button if needed.

### Optional: Send to Steam Deck via SSH

If `sshpass` is installed and the Deck is reachable, you can send the zip directly:

```bash
sshpass -p "<password>" scp RomM-RetroArch-Sync-v1.5-decky.zip deck@<deck-ip>:~/
```

Then install from `~/RomM-RetroArch-Sync-v1.5-decky.zip` on the Deck via Decky Loader.

---

## Prerequisites

- `pnpm` must be available (`which pnpm`)
- `node` ≥ 18
- `zip` utility

---

## Known gotchas

- `zip --prefix` is not supported on this system — the packaging command uses a temp dir instead
- The `_root` flag in `plugin.json` silently blocks ZIP installation (no error shown in UI)
- `package.json` and `LICENSE` are not used at runtime but are required by the Decky validator
- The symlinks at `py_modules/sync_core.py` and `py_modules/bios_manager.py` must not be
  committed as regular files — use `cp -rL` when packaging to dereference them. Using `cp -r`
  alone copies symlinks as-is, which become broken inside the zip (no error at zip time, but
  the plugin fails with `ModuleNotFoundError` at load time)
- Missing `py_modules/requests/` (and other bundled libs) causes `No module named 'requests'`
  on a fresh Decky install — always copy the entire `py_modules/` directory, not just
  `sync_core.py`
- **Decky Loader's Python is 3.11** (AppImage bundles its own interpreter at `/tmp/_MEI*/`). Pillow and any other bundled wheels with C extensions **must be compiled for Python 3.11**, not the SteamOS system Python (3.13). To refresh `py_modules/PIL/` and `py_modules/pillow.libs/`:
  ```bash
  pip download Pillow --python-version 3.11 --platform manylinux_2_28_x86_64 --only-binary :all: -d /tmp/pillow-311/
  cd /tmp/pillow-311 && unzip -q pillow-*.whl -d extracted/
  rm -rf decky_plugin/py_modules/PIL decky_plugin/py_modules/pillow.libs decky_plugin/py_modules/pillow-*.dist-info
  cp -r extracted/PIL decky_plugin/py_modules/PIL
  cp -r extracted/pillow.libs decky_plugin/py_modules/pillow.libs
  cp -r extracted/pillow-*.dist-info decky_plugin/py_modules/
  ```
