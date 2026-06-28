# Decky Plugin Deployment Guide

> **For AI agents and developers:** This file documents the complete build and packaging process
> for the RomM Sync Monitor Decky plugin. Read this before making any changes to the build process.

---

## Overview

The plugin has two components:
- **Frontend** — TypeScript/React in `src/index.tsx`, compiled to `dist/index.js` via rollup
- **Backend** — `main.py` + `py_modules/sync_core.py` (Python, runs inside Decky Loader)

`py_modules/sync_core.py` is a **dev symlink** to `../../src/sync_core.py`. It is resolved to a
real file during packaging. Never commit the resolved copy — the symlink is intentional.
`pnpm run package` builds the frontend and a `postpackage` hook **restores the symlink** afterward,
so the working tree is always left clean.

---

## Loose deploy (dev iteration loop)

When the dev machine **is** the Steam/Decky machine, the fastest loop is to overwrite the
installed plugin's files in place — no zip, no reinstall, no `sudo` (the install dir is
user-writable). The plugin lives at:

```
~/homebrew/plugins/romm-sync-monitor/
```

After editing source, copy only what changed, then reload the plugin from the Decky QAM
(or toggle it off/on):

```bash
cd /home/covin/romm-retroarch-sync
DEST=~/homebrew/plugins/romm-sync-monitor

# Frontend change (src/index.tsx):
(cd decky_plugin && pnpm run build) && cp decky_plugin/dist/index.js decky_plugin/dist/index.js.map "$DEST/dist/"

# Backend change (main.py):
cp decky_plugin/main.py "$DEST/main.py"

# Shared sync logic (src/sync_core.py) — DEST has a real copy, not a symlink:
cp src/sync_core.py "$DEST/py_modules/sync_core.py"
```

Notes:
- A **full plugin reload** is required for `main.py` / `sync_core.py` changes (Python is only
  imported at load). Frontend (`dist/index.js`) also needs a reload to re-evaluate.
- Backend Python logs land in `~/homebrew/logs/romm-sync-monitor/<timestamp>.log`. Frontend
  `console.*`/toasts do **not**; bridge them to the backend with a temporary `debug_log`
  callable when diagnosing frontend issues.
- Loose deploy is for iteration only — always cut a real zip (below) for releases/handoff.

---

## Build & Package (release zip)

The recipe below is **cwd-independent** — it anchors everything to the repo root via `git`, so
it works whether you paste it from the repo root or from inside `decky_plugin/`. `pnpm run
package` builds the frontend and restores the `py_modules/sync_core.py` symlink via its
`postpackage` hook.

> ⚠️ Always run `pnpm run package` in a **subshell** `(cd decky_plugin && …)` — it leaves the
> shell in `decky_plugin/`, so a bare `cd decky_plugin && pnpm run package` breaks every
> repo-root-relative `cp` that follows.

```bash
# Anchor to the repo root no matter where this is pasted from.
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

# Step 1: Build frontend (auto-restores the symlink afterward). Subshell keeps cwd at ROOT.
(cd decky_plugin && pnpm run package)

# Step 2: Package into the versioned zip.
# VERSION is derived from decky_plugin/package.json (the single source of truth),
# not hardcoded — bump it there and the zip name follows automatically.
VERSION="$(node -p "require('./decky_plugin/package.json').version.split('.').slice(0,2).join('.')")"
PLUGIN_NAME="romm-sync-monitor"
PLUGIN_DIR="decky_plugin"
OUT_ZIP="RomM-RetroArch-Sync-v${VERSION}-decky.zip"
TMP_DIR=$(mktemp -d)
mkdir -p "${TMP_DIR}/${PLUGIN_NAME}/dist" "${TMP_DIR}/${PLUGIN_NAME}/assets" "${TMP_DIR}/${PLUGIN_NAME}/bin"
cp "${PLUGIN_DIR}/plugin.json" "${PLUGIN_DIR}/package.json" "${PLUGIN_DIR}/LICENSE" "${PLUGIN_DIR}/main.py" "${TMP_DIR}/${PLUGIN_NAME}/"
cp "${PLUGIN_DIR}/dist/index.js" "${PLUGIN_DIR}/dist/index.js.map" "${TMP_DIR}/${PLUGIN_NAME}/dist/"
cp -rL "${PLUGIN_DIR}/py_modules" "${TMP_DIR}/${PLUGIN_NAME}/"
cp "${PLUGIN_DIR}"/assets/logo.png "${PLUGIN_DIR}"/assets/romm-isotipo.svg "${PLUGIN_DIR}"/assets/romm-logotipo.svg "${PLUGIN_DIR}"/assets/auth_background.svg "${PLUGIN_DIR}"/assets/romm-*.png "${TMP_DIR}/${PLUGIN_NAME}/assets/"
cp "${PLUGIN_DIR}/bin/7zz" "${TMP_DIR}/${PLUGIN_NAME}/bin/" && chmod +x "${TMP_DIR}/${PLUGIN_NAME}/bin/7zz"
cp "${PLUGIN_DIR}/bin/romm-session-host" "${TMP_DIR}/${PLUGIN_NAME}/bin/" && chmod +x "${TMP_DIR}/${PLUGIN_NAME}/bin/romm-session-host"  # Steam session-host: the RomM tile's exe; execs the picked emulator as a Steam-tracked child so the overlay works on Deck Gaming Mode
(cd "$TMP_DIR" && zip -rq "$OUT_ZIP" "${PLUGIN_NAME}/")
mv "$TMP_DIR/$OUT_ZIP" .
rm -rf "$TMP_DIR"

# Step 3 (optional): sanity-check a fix is actually in the bundled sync_core
unzip -p "$OUT_ZIP" "${PLUGIN_NAME}/py_modules/sync_core.py" | grep -c "_host_subprocess_env"
```

The output zip is at the **repo root**: `RomM-RetroArch-Sync-v<VERSION>-decky.zip` (~11 MB with
bundled libs), where `<VERSION>` comes from `decky_plugin/package.json`. To release a new
version, bump `"version"` in that file — the zip name (and this whole flow) follows. The name
matches the AppImage pattern `RomM-RetroArch-Sync-v<VERSION>.AppImage`.

**Why `cp -rL`?** `py_modules/sync_core.py` and `py_modules/bios_manager.py` are symlinks in
the dev tree. The `-L` flag dereferences all symlinks so real file content goes into the zip.
Without it, the zip contains broken symlinks and the plugin fails to load.

**Why `pnpm run package` and not `pnpm run build`?** `package` runs the build then a
`postpackage` hook that re-creates the `py_modules/sync_core.py` symlink, so the working tree
isn't left with a resolved copy. (Plain `build` also works but won't touch the symlink.)

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
| `assets/logo.png` | NO | Plugin icon (also Steam library-tile artwork via `get_plugin_logo`) |
| `assets/romm-isotipo.svg` | NO | RomM brand mark shown on the setup-wizard welcome; served pre-connection by `get_romm_logo` (bundled because RomM server assets need auth) |
| `assets/romm-{grid,hero,logo,header,icon}.png` | NO | RomM-branded Steam library artwork for the optional 'RomM' launcher tile (Steam asset types 0/1/2/3/4). Served by `get_romm_artwork`, painted via `SetCustomArtworkForApp`. Regenerate with `scripts/gen_romm_artwork.py` (needs cairosvg; dev-only). |
| `bin/7zz` | NO | Static 7-Zip (x64) for `.7z` extraction — SteamOS has no system 7z. `sync_core._find_7z()` resolves `<plugin>/bin/7zz`. Must be `chmod +x`. Without it: `.7z` console ROMs still load via RetroArch, `.7z` PC games won't auto-extract. |
| `bin/romm-session-host` | NO | Exe behind the "RomM" Steam tile. When a game is picked, the backend `prepare_steam_launch` writes a launch-spec and the frontend RunGame's the tile; Steam launches this script as a tracked game (opening the overlay session) and it `exec`s the resolved emulator argv in-place, so the emulator inherits the Steam overlay on Deck Gaming Mode. Returned by `get_session_host_path`. Must be `chmod +x`. Without it: Play falls back to a direct daemon launch (no working overlay). |

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
    romm-isotipo.svg
```

---

## plugin.json rules

- `"flags"` **must be `[]`** — setting `["_root"]` silently blocks ZIP installation in Decky Loader
- `"api_version"` must be `2`
- `"name"` is the display name shown in Decky ("RomM Sync Monitor")

---

## Installation on SteamOS

1. Transfer the ZIP file (e.g., `RomM-RetroArch-Sync-v1.6-decky.zip`) to the SteamOS device
2. In Decky Loader: **gear icon → "Install plugin from ZIP"**
3. Select the zip file

Do **not** restart Decky Loader after installation — use the Decky QAM reload button if needed.

### Optional: Send to Steam Deck via SSH

If `sshpass` is installed and the Deck is reachable, you can send the zip directly:

```bash
sshpass -p "<password>" scp RomM-RetroArch-Sync-v1.6-decky.zip deck@<deck-ip>:~/
```

Then install from `~/RomM-RetroArch-Sync-v1.6-decky.zip` on the Deck via Decky Loader.

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
