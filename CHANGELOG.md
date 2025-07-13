# Changelog

All notable changes to RomM-RetroArch Sync will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Initial repository structure and build system
- Professional project organization

## [1.0.0] - 2025-07-13

### Added
- RomM server connection and authentication
- Game library browsing and management
- ROM downloading with progress tracking
- RetroArch integration and game launching
- Save file and save state synchronization
- Auto-sync functionality with file monitoring
- Bulk download and delete operations
- Game search and filtering
- Tree view game library interface
- System tray integration
- AppImage build system
- GTK4/Adwaita UI design

### Features
- **RomM Integration**: Connect to RomM servers, browse game library, download ROMs
- **RetroArch Support**: Auto-detect RetroArch installation, launch games with appropriate cores
- **Save Sync**: Upload/download save files and save states with screenshot support
- **Auto-Sync**: Monitor RetroArch save directories and automatically sync changes
- **Bulk Operations**: Download or delete multiple games at once
- **Modern UI**: GTK4-based interface with tree view library organization
- **Offline Mode**: Cached game data for offline browsing
- **Cross-Platform**: Linux desktop application with AppImage distribution

### Technical
- Python 3.x with GTK4/PyGObject
- Requests library for HTTP/API communication
- Watchdog for file system monitoring
- AppImage packaging for easy distribution
- Professional repository structure

## [0.1.0] - Development

### Added
- Initial development and feature implementation
- Core RomM API integration
- Basic UI framework
- File synchronization prototype
