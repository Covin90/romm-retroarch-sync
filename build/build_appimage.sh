#!/bin/bash
set -e

echo "🚀 Building RomM - RetroArch Sync AppImage..."

# Get script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "📁 Project root: $PROJECT_ROOT"

# Add version variable at the top
VERSION="1.0.3"
APPIMAGE_NAME="RomM-RetroArch-Sync-v${VERSION}.AppImage"

# Define paths
SOURCE_APP="$PROJECT_ROOT/src/romm_sync_app.py"
SOURCE_ICON="$PROJECT_ROOT/assets/icons/romm_icon.png"
BUILD_DIR="$SCRIPT_DIR"
APPDIR="$BUILD_DIR/AppDir"

# Clean previous build
if [ -d "$APPDIR" ]; then
    echo "🧹 Cleaning previous build..."
    rm -rf "$APPDIR"
fi

# Verify source files
if [ ! -f "$SOURCE_APP" ]; then
    echo "❌ Error: Source app not found at $SOURCE_APP"
    exit 1
fi

if [ ! -f "$SOURCE_ICON" ]; then
    echo "❌ Error: Icon not found at $SOURCE_ICON"
    exit 1
fi

echo "✅ Source files verified"

# Create AppDir structure
echo "📁 Creating AppDir structure..."
mkdir -p "$APPDIR/usr/bin"
mkdir -p "$APPDIR/usr/lib/python3/dist-packages"
mkdir -p "$APPDIR/usr/share/applications"
mkdir -p "$APPDIR/usr/share/icons/hicolor/256x256/apps"
mkdir -p "$APPDIR/usr/share/metainfo"

# Copy main application
echo "📋 Copying main application..."
cp "$SOURCE_APP" "$APPDIR/usr/bin/"

# Copy and setup icons
echo "🎨 Setting up icons..."
cp "$SOURCE_ICON" "$APPDIR/usr/share/icons/hicolor/256x256/apps/com.romm.retroarch.sync.png"
cp "$SOURCE_ICON" "$APPDIR/com.romm.retroarch.sync.png"
cp "$SOURCE_ICON" "$APPDIR/.DirIcon"
cp "$SOURCE_ICON" "$APPDIR/usr/bin/romm_icon.png"

# Install Python dependencies with conflict resolution
echo "📦 Installing Python dependencies..."

# Method 1: Use --no-deps to avoid dependency checking for conflicting packages
pip3 install --target="$APPDIR/usr/lib/python3/dist-packages" \
    --no-deps \
    requests watchdog cryptography

# Install any missing sub-dependencies manually
pip3 install --target="$APPDIR/usr/lib/python3/dist-packages" \
    --no-deps \
    urllib3 charset-normalizer idna certifi cffi pycparser

echo "✅ Dependencies installed with conflict avoidance"

# Alternative Method 2: Use ignore-conflicts flag (uncomment if Method 1 doesn't work)
# pip3 install --target="$APPDIR/usr/lib/python3/dist-packages" \
#     --force-reinstall \
#     --no-warn-conflicts \
#     requests watchdog

# Create desktop file
echo "🖥️ Creating desktop file..."
cat > "$APPDIR/usr/share/applications/com.romm.retroarch.sync.desktop" << 'EOF'
[Desktop Entry]
Type=Application
Name=RomM - RetroArch Sync
Comment=Sync game library between RomM and RetroArch
Exec=romm_sync_app.py
Icon=com.romm.retroarch.sync
Categories=Game;Utility;
Terminal=false
StartupNotify=true
StartupWMClass=com.romm.retroarch.sync
EOF

# Create AppStream metadata (CORRECTED VERSION)
echo "📝 Creating AppStream metadata..."
cat > "$APPDIR/usr/share/metainfo/com.romm.retroarch.sync.appdata.xml" << 'EOF'
<?xml version="1.0" encoding="UTF-8"?>
<component type="desktop-application">
  <id>com.romm.retroarch.sync</id>
  <metadata_license>MIT</metadata_license>
  <project_license>GPL-3.0+</project_license>
  <name>RomM - RetroArch Sync</name>
  <summary>Sync game library between RomM and RetroArch</summary>
  <description>
    <p>A desktop application for managing your retro game library by syncing ROMs, saves, and save states between RomM server and RetroArch.</p>
    <p>Features include automatic game downloading, save file synchronization, and RetroArch integration for seamless retro gaming.</p>
  </description>
  
  <launchable type="desktop-id">com.romm.retroarch.sync.desktop</launchable>
  
  <url type="homepage">https://github.com/Covin90/romm-retroarch-sync</url>
  <url type="bugtracker">https://github.com/Covin90/romm-retroarch-sync/issues</url>
  
  <developer id="com.romm.retroarch.sync">
    <name>RomM - RetroArch Sync Developers</name>
  </developer>
  
  <content_rating type="oars-1.1">
    <content_attribute id="social-info">mild</content_attribute>
  </content_rating>
  
  <categories>
    <category>Game</category>
    <category>Utility</category>
  </categories>
  
  <releases>
    <release version="1.0.3" date="2025-07-26">
      <description>
        <p>Fix pagination and save sync, add loading feedback.</p>
      </description>
    </release>
  </releases>
</component>
EOF

# Copy desktop file to AppDir root
cp "$APPDIR/usr/share/applications/com.romm.retroarch.sync.desktop" "$APPDIR/"

# Create AppRun script
echo "⚙️ Creating AppRun script..."
cat > "$APPDIR/AppRun" << 'EOF'
#!/bin/bash
HERE="$(dirname "$(readlink -f "${0}")")"
export PYTHONPATH="${HERE}/usr/lib/python3/dist-packages:${PYTHONPATH}"
export PATH="${HERE}/usr/bin:${PATH}"
cd "${HERE}/usr/bin"
exec python3 "${HERE}/usr/bin/romm_sync_app.py" "$@"
EOF

chmod +x "$APPDIR/AppRun"

# Check for appimagetool
if ! command -v appimagetool &> /dev/null; then
    echo "❌ appimagetool not found. Installing..."
    
    # Download appimagetool if not found
    APPIMAGETOOL_URL="https://github.com/AppImage/appimagetool/releases/download/continuous/appimagetool-x86_64.AppImage"
    wget -O /tmp/appimagetool "$APPIMAGETOOL_URL"
    chmod +x /tmp/appimagetool
    
    echo "✅ Downloaded appimagetool to /tmp/appimagetool"
    APPIMAGETOOL_CMD="/tmp/appimagetool"
else
    APPIMAGETOOL_CMD="appimagetool"
fi

# Update the final build command
echo "🔧 Building AppImage..."
cd "$BUILD_DIR"
"$APPIMAGETOOL_CMD" --no-appstream AppDir "$APPIMAGE_NAME"

if [ -f "$APPIMAGE_NAME" ]; then
    echo "✅ AppImage built successfully!"
    echo "📍 Location: $BUILD_DIR/$APPIMAGE_NAME"
    echo "🚀 You can now run: ./build/$APPIMAGE_NAME"
else
    echo "❌ AppImage build failed!"
    exit 1
fi