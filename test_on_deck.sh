#!/bin/bash
# Quick test script for Steam Deck
# Run this on your Steam Deck to update and test the plugin

set -e

echo "🔄 Updating RomM Sync Monitor plugin..."

# Plugin directory
PLUGIN_DIR="$HOME/homebrew/plugins/romm-sync-monitor"

# Check if plugin directory exists
if [ ! -d "$PLUGIN_DIR" ]; then
    echo "❌ Plugin directory not found: $PLUGIN_DIR"
    echo "Creating directory..."
    mkdir -p "$PLUGIN_DIR"
fi

# Get script directory
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"

# Check if we need to build TypeScript
if [ -f "$SCRIPT_DIR/decky_plugin/src/index.tsx" ]; then
    echo "🔨 Building TypeScript plugin..."
    cd "$SCRIPT_DIR/decky_plugin"

    # Check if node_modules exists
    if [ ! -d "node_modules" ]; then
        echo "📦 Installing dependencies (first time only)..."
        npm install
    fi

    # Build the plugin
    npm run build

    # Copy built files
    echo "📁 Copying built files..."
    if [ -d "dist" ]; then
        cp -r dist/* "$PLUGIN_DIR/"
    fi

    cd "$SCRIPT_DIR"
else
    # Just copy Python files if no TypeScript
    echo "📁 Copying Python files..."
    cp "$SCRIPT_DIR/decky_plugin/main.py" "$PLUGIN_DIR/main.py"
    cp "$SCRIPT_DIR/decky_plugin/plugin.json" "$PLUGIN_DIR/plugin.json"
fi

echo "✅ Files copied"

# Clear the log for fresh testing
LOG_FILE="$HOME/.config/romm-retroarch-sync/decky_debug.log"
echo "🗑️  Clearing debug log..."
> "$LOG_FILE"

# Restart plugin loader
echo "🔄 Restarting Decky Loader..."
if command -v systemctl &> /dev/null; then
    sudo systemctl restart plugin_loader
    echo "✅ Decky Loader restarted"
else
    echo "⚠️  systemctl not found, please restart Decky manually"
fi

echo ""
echo "🎉 Plugin updated successfully!"
echo ""
echo "📋 To view debug logs, run:"
echo "   tail -f $LOG_FILE"
echo ""
echo "🧪 Test the plugin by:"
echo "   1. Open Decky menu (... button)"
echo "   2. Look for RomM Sync Monitor"
echo "   3. Check the service status"
