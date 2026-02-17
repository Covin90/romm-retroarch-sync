#!/bin/bash
# Build and package the RomM Sync Monitor Decky plugin as a ZIP for installation
# via Decky Loader → gear icon → "Install plugin from ZIP".
set -e

PLUGIN_NAME="romm-sync-monitor"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
OUT_ZIP="${SCRIPT_DIR}/../${PLUGIN_NAME}.zip"

echo "==> Building frontend..."
cd "$SCRIPT_DIR"
pnpm run build

echo "==> Packaging zip..."
TMP_DIR=$(mktemp -d)
mkdir -p "${TMP_DIR}/${PLUGIN_NAME}/dist"
mkdir -p "${TMP_DIR}/${PLUGIN_NAME}/py_modules"
mkdir -p "${TMP_DIR}/${PLUGIN_NAME}/assets"

cp "${SCRIPT_DIR}/plugin.json"             "${TMP_DIR}/${PLUGIN_NAME}/"
cp "${SCRIPT_DIR}/package.json"            "${TMP_DIR}/${PLUGIN_NAME}/"
cp "${SCRIPT_DIR}/LICENSE"                 "${TMP_DIR}/${PLUGIN_NAME}/"
cp "${SCRIPT_DIR}/main.py"                 "${TMP_DIR}/${PLUGIN_NAME}/"
cp "${SCRIPT_DIR}/dist/index.js"           "${TMP_DIR}/${PLUGIN_NAME}/dist/"
cp "${SCRIPT_DIR}/dist/index.js.map"       "${TMP_DIR}/${PLUGIN_NAME}/dist/"
cp "${SCRIPT_DIR}/py_modules/sync_core.py" "${TMP_DIR}/${PLUGIN_NAME}/py_modules/"
cp "${SCRIPT_DIR}/assets/logo.png"         "${TMP_DIR}/${PLUGIN_NAME}/assets/"

rm -f "$OUT_ZIP"
(cd "$TMP_DIR" && zip -r "$OUT_ZIP" "${PLUGIN_NAME}/")
rm -rf "$TMP_DIR"

echo "==> Done: ${OUT_ZIP}"
