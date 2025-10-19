#!/bin/bash
set -e

echo "🚀 Building RomM - RetroArch Sync AppImage..."

# Get script directory and project root
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"

echo "📁 Project root: $PROJECT_ROOT"

# Add version variable at the top
VERSION="1.2.1"
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

# Clean any git metadata that might interfere with version detection
echo "🧹 Cleaning git metadata from AppDir..."
find "$APPDIR" -name ".git*" -type f -delete 2>/dev/null || true

# Copy application
echo "📋 Copying application files..."
cp "$PROJECT_ROOT"/src/*.py "$APPDIR/usr/bin/"
echo "✅ All Python modules bundled"

# Copy Decky plugin if it exists
# echo "📱 Checking for Decky plugin..."
# DECKY_PLUGIN_SOURCE="$PROJECT_ROOT/decky_plugin"
# if [ -d "$DECKY_PLUGIN_SOURCE" ]; then
#     echo "📋 Copying Decky plugin..."
#     mkdir -p "$APPDIR/usr/share/romm-retroarch-sync"
#     cp -r "$DECKY_PLUGIN_SOURCE" "$APPDIR/usr/share/romm-retroarch-sync/"
#     echo "✅ Decky plugin bundled"
# else
#     echo "ℹ️ No Decky plugin found (optional)"
# fi

# Create version files for better version detection
echo "📝 Creating comprehensive version metadata..."
echo "$VERSION" > "$APPDIR/usr/bin/VERSION"
echo "$VERSION" > "$APPDIR/VERSION"
echo "$VERSION" > "$APPDIR/.version"

# Create a version info file in a standard location
mkdir -p "$APPDIR/usr/share/romm-retroarch-sync"
echo "$VERSION" > "$APPDIR/usr/share/romm-retroarch-sync/version"

# Also embed version in a way that AppImage tools can read
cat > "$APPDIR/version.json" << EOF
{
  "version": "$VERSION",
  "name": "RomM - RetroArch Sync",
  "build_date": "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
}
EOF

# Copy and setup icons
echo "🎨 Setting up icons..."
cp "$SOURCE_ICON" "$APPDIR/usr/share/icons/hicolor/256x256/apps/com.romm.retroarch.sync.png"
cp "$SOURCE_ICON" "$APPDIR/com.romm.retroarch.sync.png"
cp "$SOURCE_ICON" "$APPDIR/.DirIcon"
cp "$SOURCE_ICON" "$APPDIR/usr/bin/romm_icon.png"

# Method 1: Use --no-deps to avoid dependency checking for conflicting packages
pip3 install --target="$APPDIR/usr/lib/python3/dist-packages" \
    --no-deps \
    requests watchdog cryptography psutil

# Install any missing sub-dependencies manually
pip3 install --target="$APPDIR/usr/lib/python3/dist-packages" \
    --no-deps \
    urllib3 charset-normalizer idna certifi cffi pycparser

echo "✅ Dependencies installed with conflict avoidance"

# Bundle libadwaita and GI typelibs for Steam Deck compatibility
echo "📦 Bundling libadwaita and GI typelibs for Steam Deck..."

# Create directories for GObject introspection
mkdir -p "$APPDIR/usr/lib/girepository-1.0"
mkdir -p "$APPDIR/usr/lib/x86_64-linux-gnu"

# Copy GI typelib files if available
for typelib in Gtk-4.0.typelib GObject-2.0.typelib Gio-2.0.typelib Adw-1.typelib; do
    for path in /usr/lib/girepository-1.0 /usr/lib/x86_64-linux-gnu/girepository-1.0; do
        if [ -f "$path/$typelib" ]; then
            cp "$path/$typelib" "$APPDIR/usr/lib/girepository-1.0/" 2>/dev/null || true
            echo "✅ Copied $typelib"
            break
        fi
    done
done

# Copy libadwaita shared libraries if available
echo "📦 Bundling only essential libraries..."
for lib in libadwaita-1.so*; do
    for libpath in /usr/lib/x86_64-linux-gnu /usr/lib64 /usr/lib; do
        if ls "$libpath"/$lib 1> /dev/null 2>&1; then
            cp "$libpath"/$lib "$APPDIR/usr/lib/x86_64-linux-gnu/" 2>/dev/null || true
            echo "✅ Copied $lib"
            break
        fi
    done
done

echo "✅ Dependencies bundled without PyGObject rebuild"

# Alternative Method 2: Use ignore-conflicts flag (uncomment if Method 1 doesn't work)
# pip3 install --target="$APPDIR/usr/lib/python3/dist-packages" \
#     --force-reinstall \
#     --no-warn-conflicts \
#     requests watchdog

# Create desktop file
echo "🖥️ Creating desktop file..."
cat > "$APPDIR/usr/share/applications/com.romm.retroarch.sync.desktop" << EOF
[Desktop Entry]
Type=Application
Name=RomM - RetroArch Sync
Comment=Sync game library between RomM and RetroArch
Exec=AppRun
Icon=com.romm.retroarch.sync
Categories=Game;
Terminal=false
StartupNotify=true
StartupWMClass=com.romm.retroarch.sync
X-AppImage-Version=${VERSION}
EOF

# Create AppStream metadata
echo "📝 Creating AppStream metadata..."
cat > "$APPDIR/usr/share/metainfo/com.romm.retroarch.sync.appdata.xml" << EOF
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
    <release version="${VERSION}" date="2025-10-19">
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
export GI_TYPELIB_PATH="${HERE}/usr/lib/girepository-1.0:${GI_TYPELIB_PATH}"
export LD_LIBRARY_PATH="${HERE}/usr/lib/x86_64-linux-gnu:${LD_LIBRARY_PATH}"

cd "${HERE}/usr/bin"
exec /usr/bin/python3 "${HERE}/usr/bin/romm_sync_app.py" "$@"
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

# FIXED: Build AppImage with proper update information
echo "🔧 Building AppImage with update information..."
cd "$BUILD_DIR"

# Set environment variables for appimagetool
export VERSION="$VERSION"
export ARCH="x86_64"

# Try multiple update information formats for better compatibility
UPDATE_INFO="gh-releases-zsync|Covin90|romm-retroarch-sync|latest|RomM-RetroArch-Sync-v*.AppImage.zsync"

echo "📝 Using update info: $UPDATE_INFO"
echo "📝 Using version: $VERSION"

# Build with update information (removed --appimage-version as it's not supported)
"$APPIMAGETOOL_CMD" \
    --updateinformation="$UPDATE_INFO" \
    --verbose \
    AppDir "$APPIMAGE_NAME"

if [ -f "$APPIMAGE_NAME" ]; then
    echo "✅ AppImage built successfully with update information!"
    echo "📍 Location: $BUILD_DIR/$APPIMAGE_NAME"
    echo "🚀 You can now run: ./build/$APPIMAGE_NAME"
    
    # Verify update info was embedded
    echo "🔍 Verifying embedded update information..."
    if readelf --string-dump=.upd_info --wide "$APPIMAGE_NAME" 2>/dev/null | grep -q "gh-releases-zsync"; then
        echo "✅ Update information successfully embedded!"
    else
        echo "⚠️ Warning: Update information may not be properly embedded"
    fi
    
    # Check for version information in the AppImage
    echo "🔍 Checking for version information..."
    if strings "$APPIMAGE_NAME" | grep -q "$VERSION"; then
        echo "✅ Version $VERSION found in AppImage!"
    else
        echo "⚠️ Version information may not be embedded"
    fi
else
    echo "❌ AppImage build failed!"
    exit 1
fi