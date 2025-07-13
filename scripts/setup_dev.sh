#!/bin/bash
set -e

# RomM-RetroArch Sync - Development Environment Setup
# This script sets up the development environment for contributing to the project

echo "🚀 RomM-RetroArch Sync - Development Environment Setup"
echo "====================================================="
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

print_status "Setting up development environment for RomM-RetroArch Sync..."
echo ""

# 1. Check system requirements
print_status "Checking system requirements..."

# Check OS
if [[ "$OSTYPE" != "linux-gnu"* ]]; then
    print_error "This project is designed for Linux systems"
    exit 1
fi

# Check Python version
if ! command -v python3 &> /dev/null; then
    print_error "Python 3 is required but not installed"
    exit 1
fi

PYTHON_VERSION=$(python3 -c 'import sys; print(".".join(map(str, sys.version_info[:2])))')
REQUIRED_VERSION="3.8"

if ! python3 -c "import sys; exit(0 if sys.version_info >= (3, 8) else 1)"; then
    print_error "Python 3.8+ required. Found: $PYTHON_VERSION"
    exit 1
fi

print_success "Python $PYTHON_VERSION found"

# 2. Detect package manager and install system dependencies
print_status "Installing system dependencies..."

if command -v apt &> /dev/null; then
    # Debian/Ubuntu
    print_status "Detected apt package manager (Debian/Ubuntu)"
    
    sudo apt update
    sudo apt install -y \
        python3-cryptography \
        python3-dev \
        python3-pip \
        python3-venv \
        python3-gi \
        python3-gi-cairo \
        gir1.2-gtk-4.0 \
        gir1.2-adw-1 \
        gir1.2-appindicator3-0.1 \
        libgirepository1.0-dev \
        gcc \
        pkg-config \
        wget \
        git

elif command -v dnf &> /dev/null; then
    # Fedora
    print_status "Detected dnf package manager (Fedora)"
    
    sudo dnf install -y \
        python3-cryptography \
        python3-devel \
        python3-pip \
        python3-gobject \
        gtk4-devel \
        libadwaita-devel \
        libappindicator-gtk3-devel \
        gobject-introspection-devel \
        gcc \
        pkg-config \
        wget \
        git

elif command -v pacman &> /dev/null; then
    # Arch Linux
    print_status "Detected pacman package manager (Arch Linux)"
    
    sudo pacman -S --needed \
        python \
        python-cryptography \
        python-pip \
        python-gobject \
        gtk4 \
        libadwaita \
        libappindicator-gtk3 \
        gobject-introspection \
        gcc \
        pkgconf \
        wget \
        git

else
    print_warning "Unknown package manager. Please install the following manually:"
    echo "  - Python 3.8+ development headers"
    echo "  - GTK4 and PyGObject"
    echo "  - Libadwaita"
    echo "  - AppIndicator3"
    echo "  - GObject Introspection"
    echo "  - Build tools (gcc, pkg-config)"
fi

print_success "System dependencies installed"

# 3. Set up Python virtual environment (optional but recommended)
print_status "Setting up Python virtual environment..."

if [ ! -d "venv" ]; then
    python3 -m venv venv
    print_success "Virtual environment created"
else
    print_warning "Virtual environment already exists"
fi

# Activate virtual environment
source venv/bin/activate
print_success "Virtual environment activated"

# 4. Install Python dependencies
print_status "Installing Python dependencies..."

# Upgrade pip first
pip install --upgrade pip

# Install dependencies from requirements.txt
if [ -f "requirements.txt" ]; then
    pip install -r requirements.txt
    print_success "Python dependencies installed from requirements.txt"
else
    # Fallback to manual installation
    print_warning "requirements.txt not found, installing dependencies manually"
    pip install requests watchdog cryptography  # <-- Add cryptography here
fi

# 5. Install development dependencies
print_status "Installing development dependencies..."

pip install \
    pytest \
    pytest-cov \
    flake8 \
    black \
    mypy \
    pre-commit

print_success "Development dependencies installed"

# 6. Set up pre-commit hooks (optional)
print_status "Setting up pre-commit hooks..."

if command -v pre-commit &> /dev/null; then
    # Create .pre-commit-config.yaml if it doesn't exist
    if [ ! -f ".pre-commit-config.yaml" ]; then
        cat > .pre-commit-config.yaml << 'EOF'
repos:
  - repo: https://github.com/psf/black
    rev: 23.3.0
    hooks:
      - id: black
        language_version: python3

  - repo: https://github.com/pycqa/flake8
    rev: 6.0.0
    hooks:
      - id: flake8
        args: [--max-line-length=88, --extend-ignore=E203]

  - repo: https://github.com/pre-commit/pre-commit-hooks
    rev: v4.4.0
    hooks:
      - id: trailing-whitespace
      - id: end-of-file-fixer
      - id: check-yaml
      - id: check-added-large-files
EOF
        print_success "Created .pre-commit-config.yaml"
    fi
    
    pre-commit install
    print_success "Pre-commit hooks installed"
else
    print_warning "pre-commit not available, skipping hooks setup"
fi

# 7. Verify installation
print_status "Verifying installation..."

# Test Python imports
python3 -c "
import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')
from gi.repository import Gtk, Adw
import requests
import watchdog
print('✅ All imports successful')
" && print_success "Python dependencies verified" || print_error "Python import verification failed"

# Test syntax of main application
python3 -m py_compile src/romm_sync_app.py && print_success "Main application syntax verified" || print_error "Main application syntax check failed"

# 8. Install appimagetool for building
print_status "Setting up AppImage build tools..."

if [ ! -f "/usr/local/bin/appimagetool" ] && [ ! -f "/tmp/appimagetool" ]; then
    print_status "Downloading appimagetool..."
    wget -O /tmp/appimagetool https://github.com/AppImage/AppImageKit/releases/download/continuous/appimagetool-x86_64.AppImage
    chmod +x /tmp/appimagetool
    
    # Ask if user wants to install system-wide
    echo ""
    read -p "Install appimagetool system-wide? [y/N]: " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        sudo mv /tmp/appimagetool /usr/local/bin/appimagetool
        print_success "appimagetool installed system-wide"
    else
        print_success "appimagetool downloaded to /tmp/appimagetool"
    fi
else
    print_success "appimagetool already available"
fi

# 9. Test build process
print_status "Testing build process..."

if [ -f "build/build_appimage.sh" ]; then
    cd build
    ./build_appimage.sh > /dev/null 2>&1 && print_success "AppImage build test passed" || print_warning "AppImage build test failed (check dependencies)"
    cd ..
else
    print_warning "Build script not found"
fi

# 10. Final setup
print_status "Final setup..."

# Create development helper script
cat > dev_run.sh << 'EOF'
#!/bin/bash
# Development runner script

# Activate virtual environment if it exists
if [ -d "venv" ]; then
    source venv/bin/activate
    echo "🐍 Virtual environment activated"
fi

# Run the application
echo "🚀 Starting RomM-RetroArch Sync..."
python3 src/romm_sync_app.py
EOF

chmod +x dev_run.sh
print_success "Created dev_run.sh helper script"

# Summary
echo ""
echo "🎉 Development Environment Setup Complete!"
echo "========================================"
echo ""
echo "📋 What was installed:"
echo "  ✅ System dependencies (GTK4, PyGObject, etc.)"
echo "  ✅ Python virtual environment (venv/)"
echo "  ✅ Project dependencies (requests, watchdog)"
echo "  ✅ Development tools (pytest, flake8, black)"
echo "  ✅ Pre-commit hooks (code quality)"
echo "  ✅ AppImage build tools"
echo ""
echo "🚀 Quick Start:"
echo "  # Run the application:"
echo "  ./dev_run.sh"
echo ""
echo "  # Or manually:"
echo "  source venv/bin/activate"
echo "  python3 src/romm_sync_app.py"
echo ""
echo "  # Build AppImage:"
echo "  cd build && ./build_appimage.sh"
echo ""
echo "  # Run tests:"
echo "  source venv/bin/activate"
echo "  pytest"
echo ""
echo "📚 Development Guide:"
echo "  - Code style: Use 'black' for formatting"
echo "  - Linting: Use 'flake8' for style checking"
echo "  - Testing: Add tests in tests/ directory"
echo "  - Git hooks: Pre-commit hooks ensure code quality"
echo ""
echo "🤝 Ready to contribute! Happy coding! 🎯"