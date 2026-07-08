#!/usr/bin/env bash
# Canonical Decky plugin packager — the SINGLE source of truth for building the
# release zip. Do NOT hand-roll `cp`/`zip` commands: a beta once shipped without
# bin/romm-session-host (the RomM tile's exe), which silently broke Play on the
# Deck. This script bundles every required file AND then verifies the zip against
# a manifest, failing loudly if anything is missing — so that class of bug can't
# ship again.
#
# Usage:  ./scripts/package_decky.sh            # build + verify, zip at repo root
#         SKIP_BUILD=1 ./scripts/package_decky.sh   # reuse existing dist/ (build yourself)
#
# Requires: node (for the version string) and, unless SKIP_BUILD=1, a working
# frontend build. Set BUILD_CMD to override how the frontend is built
# (default: `pnpm run package`).
set -euo pipefail

ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

PLUGIN_NAME="romm-sync-monitor"
PLUGIN_DIR="decky_plugin"
NODE_BIN="${NODE_BIN:-node}"

# ── Step 1: build the frontend (produces dist/index.js[.map]) ────────────────
if [ "${SKIP_BUILD:-0}" != "1" ]; then
  BUILD_CMD="${BUILD_CMD:-pnpm run package}"
  echo "[package] building frontend: $BUILD_CMD"
  ( cd "$PLUGIN_DIR" && eval "$BUILD_CMD" )
else
  echo "[package] SKIP_BUILD=1 — reusing existing $PLUGIN_DIR/dist/"
fi

# ── Step 2: assemble the staging tree ────────────────────────────────────────
VERSION="$("$NODE_BIN" -p "require('./$PLUGIN_DIR/package.json').version.split('.').slice(0,2).join('.')")"
OUT_ZIP="RomM-RetroArch-Sync-v${VERSION}-decky.zip"
TMP_DIR="$(mktemp -d)"
trap 'rm -rf "$TMP_DIR"' EXIT
STAGE="$TMP_DIR/$PLUGIN_NAME"
mkdir -p "$STAGE/dist" "$STAGE/assets" "$STAGE/bin"

cp "$PLUGIN_DIR/plugin.json" "$PLUGIN_DIR/package.json" "$PLUGIN_DIR/LICENSE" "$PLUGIN_DIR/main.py" "$STAGE/"
cp "$PLUGIN_DIR/dist/index.js" "$PLUGIN_DIR/dist/index.js.map" "$STAGE/dist/"
cp -rL "$PLUGIN_DIR/py_modules" "$STAGE/"
cp "$PLUGIN_DIR"/assets/logo.png "$PLUGIN_DIR"/assets/romm-isotipo.svg \
   "$PLUGIN_DIR"/assets/romm-logotipo.svg "$PLUGIN_DIR"/assets/auth_background.svg \
   "$PLUGIN_DIR"/assets/retrodeck.svg "$PLUGIN_DIR"/assets/romm-*.png "$STAGE/assets/"
cp "$PLUGIN_DIR/bin/7zz"               "$STAGE/bin/" && chmod +x "$STAGE/bin/7zz"
cp "$PLUGIN_DIR/bin/romm-session-host" "$STAGE/bin/" && chmod +x "$STAGE/bin/romm-session-host"

# ── Step 3: verify the staging tree against the required-files manifest ───────
# Every entry here MUST exist (files) or be a non-empty dir (trailing /). If you
# add a runtime dependency, add it here so a missing bundle fails the build.
REQUIRED=(
  plugin.json package.json LICENSE main.py
  dist/index.js dist/index.js.map
  bin/7zz bin/romm-session-host
  assets/logo.png assets/romm-grid.png assets/romm-hero.png
  assets/romm-logo.png assets/romm-header.png assets/romm-icon.png
  py_modules/sync_core.py py_modules/bios_manager.py
  py_modules/requests/ py_modules/watchdog/ py_modules/PIL/ py_modules/pillow.libs/
  py_modules/urllib3/ py_modules/certifi/ py_modules/charset_normalizer/ py_modules/idna/
)
missing=()
for item in "${REQUIRED[@]}"; do
  path="$STAGE/$item"
  if [[ "$item" == */ ]]; then
    # directory: must exist and be non-empty
    if [ ! -d "$path" ] || [ -z "$(ls -A "$path" 2>/dev/null)" ]; then missing+=("$item"); fi
  else
    # file: must exist and be non-empty
    if [ ! -s "$path" ]; then missing+=("$item"); fi
  fi
done
# bin/romm-session-host and bin/7zz must be executable in the staging tree.
for x in bin/7zz bin/romm-session-host; do
  [ -x "$STAGE/$x" ] || missing+=("$x (not executable)")
done
# py_modules real files, not dangling symlinks (cp -rL should have deref'd them).
for f in py_modules/sync_core.py py_modules/bios_manager.py; do
  [ -L "$STAGE/$f" ] && missing+=("$f (is a symlink — cp -rL failed)")
done

if [ "${#missing[@]}" -ne 0 ]; then
  echo "[package] ✗ VERIFICATION FAILED — missing/invalid in package:" >&2
  printf '  - %s\n' "${missing[@]}" >&2
  exit 1
fi
echo "[package] ✓ manifest verified (${#REQUIRED[@]} required items present)"

# ── Step 4: zip + move to repo root ──────────────────────────────────────────
( cd "$TMP_DIR" && zip -rq "$OUT_ZIP" "$PLUGIN_NAME/" )
mv "$TMP_DIR/$OUT_ZIP" "$ROOT/"

# ── Step 5: post-zip cross-check — confirm the manifest survived zipping ──────
zip_missing=()
for item in "${REQUIRED[@]}"; do
  member="$PLUGIN_NAME/$item"
  if [[ "$item" == */ ]]; then
    unzip -l "$ROOT/$OUT_ZIP" "${member}*" >/dev/null 2>&1 || zip_missing+=("$item")
    [ "$(unzip -l "$ROOT/$OUT_ZIP" "${member}*" 2>/dev/null | tail -1 | awk '{print $2}')" = "0" ] && zip_missing+=("$item (empty)")
  else
    unzip -l "$ROOT/$OUT_ZIP" "$member" >/dev/null 2>&1 || zip_missing+=("$item")
  fi
done
if [ "${#zip_missing[@]}" -ne 0 ]; then
  echo "[package] ✗ POST-ZIP CHECK FAILED — not inside $OUT_ZIP:" >&2
  printf '  - %s\n' "${zip_missing[@]}" >&2
  rm -f "$ROOT/$OUT_ZIP"
  exit 1
fi

SIZE="$(du -h "$ROOT/$OUT_ZIP" | cut -f1)"
echo "[package] ✓ built $OUT_ZIP ($SIZE) — manifest verified inside zip"
