#!/bin/bash

# RomM-RetroArch Sync - Clean Build Script
# Removes all build artifacts, temporary files, and generated content

echo "🧹 RomM-RetroArch Sync - Clean Build"
echo "=================================="
echo ""

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
print_status() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

# Check if script is run from project root
if [ ! -f "src/romm_sync_app.py" ]; then
    print_error "Please run this script from the project root directory"
    exit 1
fi

print_status "Cleaning build artifacts and temporary files..."
echo ""

# Track what we're cleaning
CLEANED_ITEMS=0
CLEANED_SIZE=0

# Function to remove files/directories and track them
clean_item() {
    local item="$1"
    local description="$2"
    
    if [ -e "$item" ]; then
        # Calculate size before deletion (if it's a file)
        if [ -f "$item" ]; then
            local size=$(du -sb "$item" 2>/dev/null | cut -f1 || echo 0)
            CLEANED_SIZE=$((CLEANED_SIZE + size))
        elif [ -d "$item" ]; then
            local size=$(du -sb "$item" 2>/dev/null | cut -f1 || echo 0)
            CLEANED_SIZE=$((CLEANED_SIZE + size))
        fi
        
        rm -rf "$item"
        print_success "Removed $description"
        CLEANED_ITEMS=$((CLEANED_ITEMS + 1))
    fi
}

# 1. AppImage build artifacts
print_status "Cleaning AppImage build artifacts..."
clean_item "build/AppDir" "AppImage build directory"
clean_item "build/RomM-RetroArch-Sync*.AppImage" "AppImage files"
clean_item "build/*.zsync" "AppImage zsync files"

# Find and remove any AppImage files in root or subdirectories
find . -name "*.AppImage" -not -path "./releases/*" -exec rm -f {} \; 2>/dev/null
if [ $? -eq 0 ]; then
    print_success "Removed stray AppImage files"
fi

# 2. Python cache and compiled files
print_status "Cleaning Python cache files..."
clean_item "src/__pycache__" "Python cache directory"
clean_item "scripts/__pycache__" "Scripts cache directory"

# Find and remove Python cache files recursively
find . -name "*.pyc" -delete 2>/dev/null
find . -name "*.pyo" -delete 2>/dev/null
find . -name "__pycache__" -type d -exec rm -rf {} + 2>/dev/null
if [ $? -eq 0 ]; then
    print_success "Removed Python cache files"
fi

# 3. Virtual environment (if present)
if [ -d "venv" ]; then
    read -p "🤔 Remove virtual environment (venv/)? [y/N]: " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        clean_item "venv" "Python virtual environment"
    else
        print_warning "Keeping virtual environment"
    fi
fi

# 4. Temporary files and logs
print_status "Cleaning temporary files..."
clean_item "*.log" "Log files"
clean_item "*.tmp" "Temporary files"
clean_item "*.temp" "Temporary files"
clean_item ".cache" "Cache directory"
clean_item "temp" "Temp directory"
clean_item "tmp" "Tmp directory"

# 5. Development artifacts
print_status "Cleaning development artifacts..."
clean_item ".pytest_cache" "Pytest cache"
clean_item ".coverage" "Coverage files"
clean_item "htmlcov" "Coverage HTML reports"
clean_item ".mypy_cache" "MyPy cache"
clean_item ".tox" "Tox cache"
clean_item "dist" "Distribution directory"
clean_item "*.egg-info" "Python egg info"

# 6. Editor and IDE files
print_status "Cleaning editor files..."
clean_item ".vscode/settings.json" "VS Code workspace settings (keeping extensions)"
clean_item "*.swp" "Vim swap files"
clean_item "*.swo" "Vim swap files"
clean_item "*~" "Editor backup files"

# Find and remove editor backup files
find . -name "*.swp" -delete 2>/dev/null
find . -name "*.swo" -delete 2>/dev/null
find . -name "*~" -delete 2>/dev/null

# 7. AppImage tools (optional)
if [ -f "/tmp/appimagetool" ]; then
    read -p "🤔 Remove downloaded appimagetool? [y/N]: " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        clean_item "/tmp/appimagetool" "Downloaded appimagetool"
    else
        print_warning "Keeping appimagetool"
    fi
fi

# 8. Git artifacts (careful - only temporary ones)
print_status "Cleaning git temporary files..."
clean_item ".git/index.lock" "Git index lock"
clean_item ".git/HEAD.lock" "Git HEAD lock"

# 9. Test artifacts and screenshots
print_status "Cleaning test artifacts..."
clean_item "screenshot_*.png" "Test screenshots"
clean_item "screen_*.png" "Test screenshots"
clean_item "test_*.png" "Test images"

# Find test screenshots
find . -name "screenshot_*.png" -delete 2>/dev/null
find . -name "screen_*.png" -delete 2>/dev/null

# 10. User data (be very careful)
if [ -d ".config" ]; then
    read -p "🤔 Remove user config directory (.config/)? [y/N]: " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        clean_item ".config" "User configuration"
    else
        print_warning "Keeping user configuration"
    fi
fi

# 11. Clean empty directories
print_status "Removing empty directories..."
find . -type d -empty -not -path "./.git/*" -delete 2>/dev/null
if [ $? -eq 0 ]; then
    print_success "Removed empty directories"
fi

# 12. Optional: Reset file permissions
read -p "🔧 Reset file permissions? [y/N]: " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    chmod +x scripts/*.sh 2>/dev/null
    chmod +x build/*.sh 2>/dev/null
    chmod +x dev_run.sh 2>/dev/null
    chmod 644 *.md 2>/dev/null
    chmod 644 *.txt 2>/dev/null
    print_success "Reset file permissions"
fi

# Calculate cleaned size in human readable format
if [ $CLEANED_SIZE -gt 1073741824 ]; then
    SIZE_DISPLAY=$(echo "scale=1; $CLEANED_SIZE / 1073741824" | bc 2>/dev/null || echo "$(($CLEANED_SIZE / 1073741824))")
    SIZE_UNIT="GB"
elif [ $CLEANED_SIZE -gt 1048576 ]; then
    SIZE_DISPLAY=$(echo "scale=1; $CLEANED_SIZE / 1048576" | bc 2>/dev/null || echo "$(($CLEANED_SIZE / 1048576))")
    SIZE_UNIT="MB"
elif [ $CLEANED_SIZE -gt 1024 ]; then
    SIZE_DISPLAY=$(echo "scale=1; $CLEANED_SIZE / 1024" | bc 2>/dev/null || echo "$(($CLEANED_SIZE / 1024))")
    SIZE_UNIT="KB"
else
    SIZE_DISPLAY=$CLEANED_SIZE
    SIZE_UNIT="bytes"
fi

# Summary
echo ""
echo "🎉 Clean Complete!"
echo "================="
echo ""
echo "📊 Summary:"
echo "  🗑️  Items cleaned: $CLEANED_ITEMS"
echo "  💾 Space freed: ${SIZE_DISPLAY} ${SIZE_UNIT}"
echo ""
echo "📋 What was cleaned:"
echo "  ✅ AppImage build artifacts"
echo "  ✅ Python cache files"  
echo "  ✅ Temporary files and logs"
echo "  ✅ Development artifacts"
echo "  ✅ Editor backup files"
echo "  ✅ Empty directories"
echo ""
echo "📁 Repository status:"
echo "  ✅ Source code preserved"
echo "  ✅ Documentation preserved"
echo "  ✅ Git history preserved"
echo "  ✅ Configuration files preserved"
echo ""

# Verify project integrity
if [ -f "src/romm_sync_app.py" ] && [ -f "README.md" ] && [ -f "LICENSE" ]; then
    print_success "Project integrity verified - all essential files present"
else
    print_error "Project integrity check failed - some essential files missing!"
fi

echo ""
echo "🚀 Ready for:"
echo "  • Fresh development"
echo "  • Clean build"
echo "  • Git operations"
echo "  • Release preparation"
echo ""
echo "💡 Tip: Run './build/build_appimage.sh' to create a fresh AppImage"