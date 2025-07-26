#!/usr/bin/env python3

import gi
import requests
import json
import os
import shutil
import threading
import pickle
import time
from pathlib import Path
from urllib.parse import urljoin, quote
import socket
import configparser
import html
import webbrowser
import base64
import datetime

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import queue
from collections import defaultdict

gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, Adw, GLib, Gio, GObject

class GameDataCache:
    """Cache RomM game data locally for offline use"""
    
    def __init__(self, settings_manager):
        self.settings = settings_manager
        self.cache_dir = Path.home() / '.config' / 'romm-retroarch-sync' / 'cache'
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        
        # Cache files
        self.games_cache_file = self.cache_dir / 'games_data.json'
        self.platform_mapping_file = self.cache_dir / 'platform_mapping.json'
        self.filename_mapping_file = self.cache_dir / 'filename_mapping.json'
        
        # Cache expiry (24 hours)
        self.cache_expiry = 24 * 60 * 60
        
        # Load existing cache and metadata
        self.cached_games = self.load_games_cache()
        self.platform_mapping = self.load_platform_mapping()
        self.filename_mapping = self.load_filename_mapping()
    
    def save_games_data(self, games_data):
        """Non-blocking cache save for instant UI response"""
        import threading
        import time
        
        def save_in_background():
            """Background thread to save cache without blocking UI"""
            try:
                start_time = time.time()
                
                # Use compact JSON without indentation for speed
                cache_data = {
                    'timestamp': time.time(),
                    'games': games_data,
                    'count': len(games_data)
                }
                
                temp_file = self.games_cache_file.with_suffix('.tmp')
                
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, separators=(',', ':'))  # Compact JSON
                
                temp_file.rename(self.games_cache_file)
                
                # Just call directly:
                self.update_mappings(games_data)
                self.cached_games = games_data
                
                elapsed = time.time() - start_time
                print(f"✅ Background: Cached {len(games_data):,} games in {elapsed:.2f}s")
                
            except Exception as e:  # ADD THIS EXCEPT BLOCK
                print(f"❌ Background cache save failed: {e}")
        
        # Start background save immediately
        cache_thread = threading.Thread(target=save_in_background, daemon=True)
        cache_thread.start()
        
        print(f"📦 Caching {len(games_data):,} games in background (non-blocking)...")
    
    def load_games_cache(self):
        """Load cached games data"""
        try:
            if not self.games_cache_file.exists():
                return []
            
            with open(self.games_cache_file, 'r', encoding='utf-8') as f:
                cache_data = json.load(f)
            
            # Check if cache is still valid
            if time.time() - cache_data.get('timestamp', 0) > self.cache_expiry:
                print("📅 Games cache expired, will refresh on next connection")
                return []
            
            games = cache_data.get('games', [])
            print(f"📂 Loaded {len(games)} games from cache")
            return games
            
        except Exception as e:
            print(f"⚠️ Failed to load games cache: {e}")
            return []
    
    def update_mappings(self, games_data):
        """Create mapping dictionaries for offline lookup"""
        platform_mapping = {}
        filename_mapping = {}
        
        for game in games_data:
            if not isinstance(game, dict):
                continue
                
            romm_data = game.get('romm_data')
            if not romm_data or not isinstance(romm_data, dict):  # Add null check
                continue
                
            # Platform mapping: directory name -> RomM platform name
            platform_name = (romm_data.get('platform_name') or 
                            romm_data.get('platform_slug') or 
                            game.get('platform', 'Unknown'))
                
            # Try to guess what directory name this would create
            dir_names = [
                platform_name,
                platform_name.replace(' ', '_'),
                platform_name.replace(' ', ''),
                romm_data.get('platform_slug', ''),
            ]
            
            for dir_name in dir_names:
                if dir_name:
                    platform_mapping[dir_name] = platform_name
            
            # Filename mapping: local filename -> RomM game data
            file_name = romm_data.get('fs_name', game.get('file_name', ''))
            fs_name_no_ext = romm_data.get('fs_name_no_ext')
            game_name = game.get('name', romm_data.get('name', ''))
            
            if file_name:
                filename_mapping[file_name] = {
                    'name': game_name,
                    'platform': platform_name,
                    'rom_id': game.get('rom_id'),
                    'romm_data': romm_data
                }
            
            if fs_name_no_ext:
                filename_mapping[fs_name_no_ext] = {
                    'name': game_name,
                    'platform': platform_name,
                    'rom_id': game.get('rom_id'),
                    'romm_data': romm_data
                }
                
                # Also map common variations
                variations = [
                    fs_name_no_ext + ext for ext in ['.zip', '.7z', '.bin', '.iso', '.chd']
                ]
                for variation in variations:
                    filename_mapping[variation] = {
                        'name': game_name,
                        'platform': platform_name,
                        'rom_id': game.get('rom_id'),
                        'romm_data': romm_data
                    }
        
        # Save mappings
        self.save_platform_mapping(platform_mapping)
        self.save_filename_mapping(filename_mapping)
        
        self.platform_mapping = platform_mapping
        self.filename_mapping = filename_mapping
    
    def save_platform_mapping(self, mapping):
        """Save platform mapping to file"""
        try:
            with open(self.platform_mapping_file, 'w', encoding='utf-8') as f:
                json.dump(mapping, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Failed to save platform mapping: {e}")
    
    def save_filename_mapping(self, mapping):
        """Save filename mapping to file"""
        try:
            with open(self.filename_mapping_file, 'w', encoding='utf-8') as f:
                json.dump(mapping, f, indent=2, ensure_ascii=False)
        except Exception as e:
            print(f"Failed to save filename mapping: {e}")
    
    def load_platform_mapping(self):
        """Load platform mapping from file"""
        try:
            if self.platform_mapping_file.exists():
                with open(self.platform_mapping_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Failed to load platform mapping: {e}")
        return {}
    
    def load_filename_mapping(self):
        """Load filename mapping from file"""
        try:
            if self.filename_mapping_file.exists():
                with open(self.filename_mapping_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Failed to load filename mapping: {e}")
        return {}
    
    def get_platform_name(self, directory_name):
        """Get proper platform name from directory name"""
        return self.platform_mapping.get(directory_name, directory_name)
    
    def get_game_info(self, filename):
        """Get game info from filename"""
        # Try exact match first
        if filename in self.filename_mapping:
            return self.filename_mapping[filename]
        
        # Try without extension
        file_stem = Path(filename).stem
        if file_stem in self.filename_mapping:
            return self.filename_mapping[file_stem]
        
        # Try with common extensions
        for ext in ['.zip', '.7z', '.bin', '.iso', '.chd']:
            test_name = file_stem + ext
            if test_name in self.filename_mapping:
                return self.filename_mapping[test_name]
        
        return None
    
    def is_cache_valid(self):
        """Check if cache is still valid"""
        return bool(self.cached_games) and self.games_cache_file.exists()
    
    def clear_cache(self):
        """Clear all cached data"""
        try:
            for cache_file in [self.games_cache_file, self.platform_mapping_file, self.filename_mapping_file]:
                if cache_file.exists():
                    cache_file.unlink()
            
            self.cached_games = []
            self.platform_mapping = {}
            self.filename_mapping = {}
            
            print("🗑️ Cache cleared")
            
        except Exception as e:
            print(f"❌ Failed to clear cache: {e}")

class TrayIcon:
    """Cross-desktop tray icon using subprocess for AppIndicator"""
    
    def __init__(self, app, window):
        self.app = app
        self.window = window
        self.tray_process = None
        self.desktop = self.detect_desktop()
        
        print(f"🖥️ Detected desktop environment: {self.desktop}")
        self.setup_tray()
    
    def detect_desktop(self):
        """Detect current desktop environment"""
        desktop_env = os.environ.get('XDG_CURRENT_DESKTOP', '').lower()
        if 'gnome' in desktop_env or 'cinnamon' in desktop_env:
            return 'gnome'
        elif 'kde' in desktop_env:
            return 'kde'
        return 'other'
    
    def setup_tray(self):
        """Setup tray icon using subprocess"""
        import subprocess
        import sys
        import os
        
        # Get the correct icon path for the new structure
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(script_dir)
        
        # Try multiple icon locations
        icon_locations = [
            os.path.join(project_root, 'assets', 'icons', 'romm_icon.png'),
            os.path.join(os.environ.get('APPDIR', ''), 'usr/bin/romm_icon.png'),
            os.path.join(script_dir, 'romm_icon.png'),
            'romm_icon.png'
        ]
        
        custom_icon_path = None
        for location in icon_locations:
            if os.path.exists(location):
                custom_icon_path = location
                break
        
        # Create the tray script content with corrected icon path
        tray_script = f'''import gi
gi.require_version('AppIndicator3', '0.1')
gi.require_version('Gtk', '3.0')
from gi.repository import AppIndicator3, Gtk
import sys
import os

class TrayIndicator:
    def __init__(self):
        # Use the discovered icon path
        custom_icon_path = "{custom_icon_path}"
        
        if custom_icon_path and os.path.exists(custom_icon_path):
            self.indicator = AppIndicator3.Indicator.new(
                "romm-retroarch-sync",
                custom_icon_path,
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS
            )
            print(f"Using custom tray icon: {{custom_icon_path}}")
        else:
            # Fallback to system icon
            self.indicator = AppIndicator3.Indicator.new(
                "romm-retroarch-sync",
                "application-x-executable",
                AppIndicator3.IndicatorCategory.APPLICATION_STATUS
            )
            print("Using fallback system icon for tray")
        
        self.indicator.set_status(AppIndicator3.IndicatorStatus.ACTIVE)
        self.indicator.set_title("RomM - RetroArch Sync")
        
        # Create menu
        menu = Gtk.Menu()
        
        show_item = Gtk.MenuItem(label="Show/Hide Window")
        show_item.connect('activate', self.on_toggle)
        menu.append(show_item)
        
        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect('activate', self.on_quit)
        menu.append(quit_item)
        
        menu.show_all()
        self.indicator.set_menu(menu)
    
    def on_toggle(self, item):
        os.system('pkill -USR1 -f romm_sync_app.py')
    
    def on_quit(self, item):
        os.system('pkill -TERM -f romm_sync_app.py')
        Gtk.main_quit()

if __name__ == "__main__":
    try:
        indicator = TrayIndicator()
        Gtk.main()
    except KeyboardInterrupt:
        pass
'''
        
        # Write script to temp file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(tray_script)
            script_path = f.name
        
        try:
            # Start tray process
            self.tray_process = subprocess.Popen([sys.executable, script_path])
            print("✅ Tray icon started in subprocess")
            
            # Setup signal handlers
            import signal
            signal.signal(signal.SIGUSR1, self._on_toggle_signal)
            signal.signal(signal.SIGTERM, self._on_quit_signal)
            
        except Exception as e:
            print(f"❌ Tray setup failed: {e}")
    
    def _on_toggle_signal(self, signum, frame):
        """Handle toggle signal from tray"""
        GLib.idle_add(self.on_toggle_window)
    
    def _on_quit_signal(self, signum, frame):
        """Handle quit signal from tray"""
        GLib.idle_add(self.on_quit)
    
    def on_toggle_window(self):
        """Toggle window visibility"""
        try:
            if self.window.is_visible():
                self.window.set_visible(False)
            else:
                self.window.set_visible(True)
                self.window.present()
        except Exception as e:
            print(f"❌ Window toggle error: {e}")
    
    def on_quit(self):
        """Quit application"""
        try:
            self.cleanup()
            self.app.quit()
        except Exception as e:
            print(f"❌ Quit error: {e}")
    
    def cleanup(self):
        """Clean up tray process"""
        if self.tray_process:
            self.tray_process.terminate()
            print("✅ Tray icon cleaned up")
        
class GameItem(GObject.Object):
    def __init__(self, game_data):
        super().__init__()
        self.game_data = game_data
    
    def __eq__(self, other):
        """Enable proper equality comparison for GameItem objects"""
        if not isinstance(other, GameItem):
            return False
        return self.game_data.get('rom_id') == other.game_data.get('rom_id')
    
    def __hash__(self):
        """Enable GameItem to be used in sets"""
        return hash(self.game_data.get('rom_id', id(self.game_data)))

    @GObject.Property(type=str, default='Unknown')
    def name(self):
        return self.game_data.get('name', 'Unknown')
    
    @GObject.Property(type=bool, default=False)
    def is_downloaded(self):
        return self.game_data.get('is_downloaded', False)
    
    @GObject.Property(type=str, default='Not downloaded')
    def size_text(self):
        if self.game_data.get('is_downloaded'):
            size = self.game_data.get('local_size', 0)
            if size > 1024 * 1024:
                return f"{size / (1024*1024):.1f} MB"
            return f"{size / 1024:.1f} KB" if size > 1024 else f"{size} bytes"
        return "Not downloaded"

class PlatformItem(GObject.Object):
    def __init__(self, platform_name, games):
        super().__init__()
        self.platform_name = platform_name
        self.games = games
        self.child_store = Gio.ListStore()
        
        for game in games:
            self.child_store.append(GameItem(game))


    @GObject.Property(type=str, default='Unknown Platform')
    def name(self):
        # Just return the platform name without counts (counts are shown in status column)
        return self.platform_name
    
    @GObject.Property(type=str, default='0/0')
    def status_text(self):
        downloaded = sum(1 for g in self.games if g.get('is_downloaded'))
        result = f"{downloaded}/{len(self.games)}"
        return result
    
    @GObject.Property(type=bool, default=False)
    def is_downloaded(self):
        return False  # Platforms don't have download status
    
    @GObject.Property(type=str, default='0 KB')
    def size_text(self):
        # Calculate downloaded size (local files)
        downloaded_size = sum(g.get('local_size', 0) for g in self.games if g.get('is_downloaded'))
        
        def format_size(size_bytes):
            if size_bytes > 1024**3:
                return f"{size_bytes / (1024**3):.1f} GB"
            elif size_bytes > 1024**2:
                return f"{size_bytes / (1024**2):.1f} MB"
            else:
                return f"{size_bytes / 1024:.1f} KB"
        
        # Better detection: check if we're truly connected vs using cached data
        # If ALL games in the platform are downloaded, we're probably in offline mode
        all_games_downloaded = len(self.games) > 0 and all(g.get('is_downloaded', False) for g in self.games)
        
        # Calculate total library size from RomM data
        total_library_size = 0
        for g in self.games:
            romm_data = g.get('romm_data')
            if romm_data and isinstance(romm_data, dict):
                total_library_size += romm_data.get('fs_size_bytes', 0)
        
        # Only show downloaded/total format if:
        # 1. We have total library size data AND
        # 2. NOT all games are downloaded (indicating we're seeing the full library) AND  
        # 3. Total size is significantly larger than downloaded size
        should_show_total = (
            total_library_size > 0 and 
            not all_games_downloaded and
            total_library_size > downloaded_size * 1.1  # At least 10% larger
        )
        
        if should_show_total:
            result = f"{format_size(downloaded_size)} / {format_size(total_library_size)}"
            return result
        else:
            # When offline, all downloaded, or sizes are equal, just show downloaded size
            result = format_size(downloaded_size)
            return result
    
    def force_property_update(self):
        """Manually force property updates - for debugging"""
        print(f"🔄 Forcing property update for {self.platform_name}")
        
        # Use notify with property names (this should work)
        self.notify('name')
        self.notify('status-text')
        self.notify('size-text')
        
        # Alternative approach: get the current values and use freeze/thaw
        try:
            current_name = self.name
            current_status = self.status_text
            current_size = self.size_text
            print(f"🔍 Current values: name='{current_name}', status='{current_status}', size='{current_size}'")
            
            # Force a freeze/thaw cycle to trigger updates
            self.freeze_notify()
            self.thaw_notify()
        except Exception as e:
            print(f"⚠️ Error in freeze/thaw: {e}")
            
        print(f"✅ Property update completed for {self.platform_name}")

class LibraryTreeModel:
    def __init__(self):
        self.root_store = Gio.ListStore()
        self.tree_model = Gtk.TreeListModel.new(
            self.root_store,
            False,  # passthrough: false to show expanders
            False,  # autoexpand
            self.create_child_model
        )
        self._last_platforms = {}
        self._persistent_expansion_state = {}
        self._is_updating = False
        self._last_stable_state = {}
        self._expansion_connections = {}  # Track signal connections
        
    def create_child_model(self, item):
        if isinstance(item, PlatformItem):
            return item.child_store
        return None
    
    def update_library(self, games):
        """Update library with stable expansion state preservation"""
        if not games:
            self.root_store.remove_all()
            self._last_platforms = {}
            self._clear_expansion_connections()
            return
        
        # Set updating flag to prevent state saves during transition
        self._is_updating = True
        
        try:
            # Group games by platform
            new_platforms = {}
            for game in games:
                platform = game.get('platform', 'Unknown')
                new_platforms.setdefault(platform, []).append(game)
            
            # Check if we can do a true in-place update
            can_update_in_place = self._can_update_in_place(new_platforms)
            
            if can_update_in_place:
                self._update_in_place(new_platforms)
            else:
                self._rebuild_with_preservation(new_platforms)
            
            self._last_platforms = new_platforms.copy()
            
        finally:
            # Clear updating flag and schedule state stabilization
            self._is_updating = False
            # Connect to expansion events for manual detection
            GLib.timeout_add(100, self._setup_expansion_tracking)
            GLib.timeout_add(500, self._stabilize_expansion_state)
    
    def _setup_expansion_tracking(self):
        """Set up tracking for manual expansion changes"""
        try:
            self._clear_expansion_connections()
            
            if not self.tree_model:
                return False
                
            total_items = self.tree_model.get_n_items()
            
            for i in range(total_items):
                tree_item = self.tree_model.get_item(i)
                if tree_item and tree_item.get_depth() == 0:  # Platform level
                    platform_item = tree_item.get_item()
                    if isinstance(platform_item, PlatformItem):
                        platform_name = platform_item.platform_name
                        
                        # Connect to notify::expanded signal
                        connection_id = tree_item.connect('notify::expanded', 
                                                        self._on_expansion_changed, 
                                                        platform_name)
                        self._expansion_connections[platform_name] = (tree_item, connection_id)
                        
            
        except Exception as e:
            print(f"❌ Error setting up expansion tracking: {e}")
        
        return False  # Don't repeat
    
    def _on_expansion_changed(self, tree_item, pspec, platform_name):
        """Handle manual expansion changes"""
        if self._is_updating:
            return  # Ignore during updates
            
        try:
            is_expanded = tree_item.get_expanded()
            
            # Update persistent state immediately
            self._persistent_expansion_state[platform_name] = is_expanded
            self._last_stable_state[platform_name] = is_expanded
            
            # Save current state
            self._save_current_expansion_state()
            
        except Exception as e:
            print(f"❌ Error handling expansion change: {e}")
    
    def _save_current_expansion_state(self):
        """Save current expansion state from all visible items"""
        if self._is_updating:
            return
            
        try:
            current_state = self._get_current_expansion_state()
            expanded_count = sum(1 for expanded in current_state.values() if expanded)
            
            if expanded_count > 0 or not self._last_stable_state:
                self._last_stable_state = current_state.copy()
                self._persistent_expansion_state = current_state.copy()
                
                # Debug: show what was saved
                for platform, expanded in current_state.items():
                    if expanded:
                        print(f"   📂 {platform} = EXPANDED")
            
        except Exception as e:
            print(f"❌ Error saving expansion state: {e}")
    
    def _clear_expansion_connections(self):
        """Clear all expansion signal connections"""
        for platform_name, (tree_item, connection_id) in self._expansion_connections.items():
            try:
                tree_item.disconnect(connection_id)
            except:
                pass  # Connection might already be invalid
        
        self._expansion_connections.clear()
    
    def _get_current_expansion_state(self):
        """Get current expansion state without side effects"""
        expansion_state = {}
        if not self.tree_model:
            return expansion_state
            
        try:
            for i in range(self.tree_model.get_n_items()):
                item = self.tree_model.get_item(i)
                if item and item.get_depth() == 0:
                    tree_item_obj = item.get_item()
                    if isinstance(tree_item_obj, PlatformItem):
                        platform_name = tree_item_obj.platform_name
                        is_expanded = item.get_expanded()
                        expansion_state[platform_name] = is_expanded
        except Exception as e:
            print(f"Error getting expansion state: {e}")
            
        return expansion_state
    
    def _stabilize_expansion_state(self):
        """Check and report final expansion state"""
        if self._is_updating:
            return True  # Keep checking
            
        try:
            current_state = self._get_current_expansion_state()
            expanded_count = sum(1 for expanded in current_state.values() if expanded)
            
            # Show current state for debugging
            for platform, expanded in current_state.items():
                status = "EXPANDED" if expanded else "collapsed"
            
            # If we have expansions now, save them
            if expanded_count > 0:
                self._last_stable_state = current_state.copy()
                self._persistent_expansion_state = current_state.copy()

            
        except Exception as e:
            print(f"Error stabilizing expansion state: {e}")
            
        return False  # Don't repeat
    
    def _can_update_in_place(self, new_platforms):
        """Check if we can update without any tree structure changes"""
        if not self._last_platforms:
            return False
            
        # Must have same platforms in same order
        old_platform_names = list(self._last_platforms.keys())
        new_platform_names = list(new_platforms.keys())
        
        return old_platform_names == new_platform_names
    
    def _update_in_place(self, new_platforms):
        """Update platform contents with zero visual disruption AND updated counts"""

        # Save current expansion state before any changes
        current_expansion_state = self._get_current_expansion_state()
        expanded_platforms = [name for name, expanded in current_expansion_state.items() if expanded]
        
        if expanded_platforms:
            print(f"💾 Maintaining expansion for: {', '.join(expanded_platforms)}")
        
        # Track which platforms need count updates
        platforms_with_changes = []
        
        # Update platform data first
        for i, platform_name in enumerate(new_platforms.keys()):
            if i < self.root_store.get_n_items():
                platform_item = self.root_store.get_item(i)
                if isinstance(platform_item, PlatformItem):
                    old_game_count = len(platform_item.games)
                    new_games = new_platforms[platform_name]
                    new_game_count = len(new_games)
                    
                    # Update the platform's internal data
                    platform_item.games = new_games
                    
                    # Update child store
                    platform_item.child_store.remove_all()
                    for game in new_games:
                        platform_item.child_store.append(GameItem(game))
                    
                    # Track platforms that need count updates
                    if old_game_count != new_game_count:
                        platforms_with_changes.append((i, platform_name, old_game_count, new_game_count))
                        print(f"📊 {platform_name}: {old_game_count} → {new_game_count} games")
        
        # Now update counts using a flicker-free method
        if platforms_with_changes:
            self._update_platform_counts_flicker_free(platforms_with_changes, expanded_platforms)
        else:
            pass
        

    def _update_platform_counts_flicker_free(self, platforms_with_changes, expanded_platforms):
        """Update platform counts without visual flicker"""
        print(f"🔄 Updating counts for {len(platforms_with_changes)} platforms")
        
        # Save exact expansion state for each item that will be updated
        expansion_backup = {}
        for i, platform_name, old_count, new_count in platforms_with_changes:
            expansion_backup[i] = platform_name in expanded_platforms
        
        # Update each platform individually with immediate restoration
        for i, platform_name, old_count, new_count in platforms_with_changes:
            was_expanded = expansion_backup[i]
            
            # Get tree item before update
            tree_item = None
            if self.tree_model and i < self.tree_model.get_n_items():
                tree_item = self.tree_model.get_item(i)
            
            # Use a minimal items_changed that only affects this one item
            self.root_store.items_changed(i, 1, 1)
            
            # Immediately restore expansion if it was expanded
            if was_expanded:
                def restore_immediately(item_index=i, platform_nm=platform_name):
                    try:
                        if self.tree_model and item_index < self.tree_model.get_n_items():
                            updated_tree_item = self.tree_model.get_item(item_index)
                            if updated_tree_item and updated_tree_item.get_depth() == 0:
                                if not updated_tree_item.get_expanded():
                                    updated_tree_item.set_expanded(True)
                                    # Don't log every restoration to avoid spam
                                # else: already expanded, good
                        return False
                    except:
                        return False
                
                # Multiple immediate restoration attempts
                GLib.idle_add(restore_immediately)
                GLib.timeout_add(1, restore_immediately)  # Very fast follow-up
            
            print(f"✅ Updated count: {platform_name} ({new_count} games)")
        
        # Final verification with minimal delay
        def verify_final_state():
            final_state = self._get_current_expansion_state()
            preserved_count = sum(1 for name in expanded_platforms 
                                if final_state.get(name, False))
            
            if preserved_count == len(expanded_platforms):
                print(f"🎉 Perfect: {preserved_count}/{len(expanded_platforms)} expansions preserved with updated counts")
            else:
                print(f"⚠️ Need emergency restore: {preserved_count}/{len(expanded_platforms)} preserved")
                
                # Emergency restoration for any lost expansions
                for platform_name in expanded_platforms:
                    if not final_state.get(platform_name, False):
                        for i in range(self.tree_model.get_n_items()):
                            item = self.tree_model.get_item(i)
                            if item and item.get_depth() == 0:
                                tree_item_obj = item.get_item()
                                if isinstance(tree_item_obj, PlatformItem):
                                    if tree_item_obj.platform_name == platform_name:
                                        item.set_expanded(True)
                                        print(f"🚑 Emergency restore: {platform_name}")
                                        break
            
            return False
        
        GLib.timeout_add(10, verify_final_state)  # Very fast verification

    def _update_platform_name_display(self, platform_item, platform_name, game_count):
        """Update platform display name without calling items_changed"""
        try:
            # Calculate downloaded count
            downloaded_count = sum(1 for g in platform_item.games if g.get('is_downloaded'))
            
            # Update the platform name property directly
            # This triggers the UI to update the display without rebuilding the tree
            new_display_name = f"{platform_name} ({downloaded_count}/{game_count})"
            
            # Force property notification without items_changed
            # We need to trigger a property change notification
            platform_item.notify('name')
            
            print(f"🏷️ Updated display: {new_display_name}")
            
        except Exception as e:
            print(f"❌ Error updating platform display: {e}")

    def _rebuild_with_preservation(self, new_platforms):
        """Rebuild tree while maintaining expansion states from last stable state"""
        # Clear connections before rebuilding
        self._clear_expansion_connections()
        
        # Clear and rebuild
        self.root_store.remove_all()
        
        # Add all platforms
        for platform_name in sorted(new_platforms.keys()):
            platform_item = PlatformItem(platform_name, new_platforms[platform_name])
            self.root_store.append(platform_item)
        
        # Use the last stable state for restoration
        restoration_state = self._last_stable_state.copy()
        if not restoration_state and self._persistent_expansion_state:
            restoration_state = self._persistent_expansion_state.copy()
            
        if restoration_state:
            expanded_to_restore = sum(1 for exp in restoration_state.values() if exp)
            print(f"🔄 Restoring from stable state: {expanded_to_restore} platforms to expand")
            
            # Show what we're trying to restore
            for platform, should_expand in restoration_state.items():
                if should_expand:
                    print(f"   🔄 Will restore: {platform} = EXPANDED")
            
            self._restore_expansion_from_state(restoration_state)
        else:
            pass
    
    def _restore_expansion_from_state(self, expansion_state):
        """Restore expansion state from a specific state dict"""
        def do_restore(attempt_num):
            try:
                restored_count = 0
                total_items = self.tree_model.get_n_items()
                
                print(f"🔄 Restore attempt {attempt_num}: checking {total_items} items")
                
                for i in range(total_items):
                    item = self.tree_model.get_item(i)
                    if item and item.get_depth() == 0:
                        tree_item_obj = item.get_item()
                        if isinstance(tree_item_obj, PlatformItem):
                            platform_name = tree_item_obj.platform_name
                            should_be_expanded = expansion_state.get(platform_name, False)
                            current_state = item.get_expanded()
                            
                            if should_be_expanded and not current_state:
                                item.set_expanded(True)
                                print(f"   🔄 Attempt {attempt_num}: Expanding {platform_name}")
                                restored_count += 1
                            elif should_be_expanded and current_state:
                                print(f"   ✅ Attempt {attempt_num}: {platform_name} already expanded")
                                restored_count += 1
                
                print(f"✅ Restore attempt {attempt_num}: processed {restored_count} expansions")
                
            except Exception as e:
                print(f"❌ Restore attempt {attempt_num} failed: {e}")
            
            return False  # Don't repeat
        
        # Multiple restoration attempts with different timings
        GLib.idle_add(do_restore, 1)
        GLib.timeout_add(50, do_restore, 2)
        GLib.timeout_add(150, do_restore, 3)

    # Legacy methods for compatibility
    def save_expansion_state(self):
        """Legacy method - now uses manual tracking"""
        current_state = self._get_current_expansion_state()
        return current_state

    def restore_expansion_state(self, expansion_state):
        """Legacy method - now uses stable restoration"""
        if expansion_state:
            # Only update if we have actual expansions
            expanded_count = sum(1 for exp in expansion_state.values() if exp)
            if expanded_count > 0:
                self._last_stable_state = expansion_state.copy()
                self._persistent_expansion_state = expansion_state.copy()
                self._restore_expansion_from_state(expansion_state)

class EnhancedLibrarySection:
    """Enhanced library section with tree view"""
    
    def __init__(self, parent_window):
        self.parent = parent_window
        self.library_model = LibraryTreeModel()
        self.selected_game = None
        self.selected_checkboxes = set()  # Keep this for compatibility
        self.selected_rom_ids = set()     # Add this new tracking
        self.selected_game_keys = set()   # Add this for non-ROM ID games
        self.setup_library_ui()
        self.filtered_games = []
        self.search_text = ""
        self.game_progress = {}  # rom_id -> progress_info
        self.show_downloaded_only = False # Filter state
        self.sort_downloaded_first = False  # Sort mode state

    def apply_filters(self, games):
        """Apply both platform and search filters to games list"""
        filtered_games = games
        
        # Apply platform filter
        selected_index = self.platform_filter.get_selected()
        if selected_index != Gtk.INVALID_LIST_POSITION:
            string_list = self.platform_filter.get_model()
            if string_list:
                selected_platform = string_list.get_string(selected_index)
                if selected_platform != "All Platforms":
                    filtered_games = [game for game in filtered_games 
                                    if game.get('platform', 'Unknown') == selected_platform]
        
        # Apply search filter
        if self.search_text:
            filtered_games = [game for game in filtered_games 
                            if self.search_text in game.get('name', '').lower() or 
                                self.search_text in game.get('platform', '').lower()]
        
        # Apply downloaded filter   
        if self.show_downloaded_only:
            filtered_games = [game for game in filtered_games if game.get('is_downloaded', False)]

        return filtered_games

    def on_toggle_sort(self, button):
        """Toggle between alphabetical and download-status sorting"""
        self.sort_downloaded_first = not self.sort_downloaded_first
        
        if self.sort_downloaded_first:
            button.set_icon_name("view-sort-descending-symbolic")
            button.set_tooltip_text("Sort: Alphabetical")
        else:
            button.set_icon_name("view-sort-ascending-symbolic")
            button.set_tooltip_text("Sort: Downloaded")
        
        # Sort in-place by reordering existing GameItem objects
        for i in range(self.library_model.root_store.get_n_items()):
            platform_item = self.library_model.root_store.get_item(i)
            if isinstance(platform_item, PlatformItem):
                # Get filtered games first (respect current filter state)
                if self.show_downloaded_only:
                    games_to_sort = [g for g in platform_item.games if g.get('is_downloaded', False)]
                else:
                    games_to_sort = platform_item.games

                # Sort the filtered games
                if self.sort_downloaded_first:
                    games_to_sort.sort(key=lambda g: (not g.get('is_downloaded', False), g.get('name', '').lower()))
                else:
                    games_to_sort.sort(key=lambda g: g.get('name', '').lower())

                # Create sorted GameItem list from filtered games
                sorted_game_items = [GameItem(game) for game in games_to_sort]
                
                # Use splice to replace all items at once (prevents scroll reset)
                n_items = platform_item.child_store.get_n_items()
                if n_items > 0:
                    platform_item.child_store.splice(0, n_items, sorted_game_items)
                else:
                    for game_item in sorted_game_items:
                        platform_item.child_store.append(game_item)
        
        # Update filtered_games
        self.filtered_games = []
        for i in range(self.library_model.root_store.get_n_items()):
            platform_item = self.library_model.root_store.get_item(i)
            if isinstance(platform_item, PlatformItem):
                self.filtered_games.extend(platform_item.games)

    def on_toggle_filter(self, button):
            """Toggle between showing all games and only downloaded games with no flicker."""
            # 1. Save the current UI state before making changes
            scroll_position = 0
            if hasattr(self, 'column_view'):
                # Get the parent ScrolledWindow to access its adjustment
                scrolled_window = self.column_view.get_parent()
                if scrolled_window:
                    vadj = scrolled_window.get_vadjustment()
                    if vadj:
                        scroll_position = vadj.get_value()
            
            # Save the expansion state of the tree
            expansion_state = self.library_model._get_current_expansion_state()
            
            # 2. Freeze the UI to prevent intermediate redraws
            # This is the key to preventing flicker.
            self.library_model.root_store.freeze_notify()
            if hasattr(self, 'column_view'):
                self.column_view.freeze_notify()
            
            try:
                # 3. Perform all data and state updates
                self.show_downloaded_only = not self.show_downloaded_only
                
                if self.show_downloaded_only:
                    button.set_icon_name("starred-symbolic") # Use a "filled" icon for active filter
                    button.set_tooltip_text("Show all games")
                else:
                    button.set_icon_name("folder-symbolic") # Use an "outline" icon for inactive
                    button.set_tooltip_text("Show downloaded only")
                
                # Work directly with existing platform items (no redundant filtering)
                for i in range(self.library_model.root_store.get_n_items()):
                    platform_item = self.library_model.root_store.get_item(i)
                    if isinstance(platform_item, PlatformItem):
                        # Apply download filter only
                        if self.show_downloaded_only:
                            filtered_platform_games = [g for g in platform_item.games if g.get('is_downloaded', False)]
                        else:
                            filtered_platform_games = platform_item.games
                        
                        # Apply current sort
                        if self.sort_downloaded_first:
                            filtered_platform_games.sort(key=lambda g: (not g.get('is_downloaded', False), g.get('name', '').lower()))
                        else:
                            filtered_platform_games.sort(key=lambda g: g.get('name', '').lower())
                        
                        # Update child store in-place
                        filtered_game_items = [GameItem(game) for game in filtered_platform_games]
                        n_items = platform_item.child_store.get_n_items()
                        if n_items > 0:
                            platform_item.child_store.splice(0, n_items, filtered_game_items)
                        else:
                            for game_item in filtered_game_items:
                                platform_item.child_store.append(game_item)

                # Update filtered_games for other components
                self.filtered_games = []
                for i in range(self.library_model.root_store.get_n_items()):
                    platform_item = self.library_model.root_store.get_item(i)
                    if isinstance(platform_item, PlatformItem):
                        self.filtered_games.extend(platform_item.games if not self.show_downloaded_only 
                                                else [g for g in platform_item.games if g.get('is_downloaded', False)])
                
            finally:
                # 4. Thaw notifications. This triggers a single, batched UI update.
                # The 'finally' block ensures this runs even if an error occurs.
                self.library_model.root_store.thaw_notify()
                if hasattr(self, 'column_view'):
                    self.column_view.thaw_notify()
            
            # 5. Restore the UI state after the update has been processed
            # We use a short timeout to ensure this runs after the UI has redrawn.
            def restore_state():
                self.library_model._restore_expansion_from_state(expansion_state)
                
                if hasattr(self, 'column_view'):
                    scrolled_window = self.column_view.get_parent()
                    if scrolled_window:
                        vadj = scrolled_window.get_vadjustment()
                        if vadj:
                            # Restore the scroll position smoothly
                            vadj.set_value(scroll_position)
                return False # Ensures the function only runs once
            
            GLib.timeout_add(50, restore_state)

    def sort_games_consistently(self, games):
        """Lightning-fast sorting with key pre-computation"""
        if not games:
            return games
        
        # Check if we should sort by download status first  
        sort_downloaded_first = getattr(self, 'sort_downloaded_first', False)
        
        game_count = len(games)
        
        # For small lists, use simple sorting
        if game_count < 200:
            if sort_downloaded_first:
                return sorted(games, key=lambda game: (
                    game.get('platform', 'ZZZ_Unknown'),
                    not game.get('is_downloaded', False),  # Downloaded first (False sorts before True)
                    game.get('name', '').lower()
                ))
            else:
                return sorted(games, key=lambda game: (
                    game.get('platform', 'ZZZ_Unknown'),
                    game.get('name', '').lower()
                ))
        
        # For large lists, use optimized sorting with download status
        print(f"⚡ Fast-sorting {game_count:,} games...")
        start_time = time.time()
        
        keyed_games = []
        for game in games:
            platform = game.get('platform', 'ZZZ_Unknown')
            name = game.get('name', '')
            name_lower = name.lower() if name else ''
            
            if sort_downloaded_first:
                is_downloaded = game.get('is_downloaded', False)
                sort_key = (platform, not is_downloaded, name_lower)  # Downloaded first
            else:
                sort_key = (platform, name_lower)
            
            keyed_games.append((sort_key, game))
        
        # Sort using pre-computed keys
        keyed_games.sort(key=lambda x: x[0])
        sorted_games = [game for sort_key, game in keyed_games]
        
        elapsed = time.time() - start_time
        print(f"✅ Sorted {game_count:,} games in {elapsed:.2f}s")
        
        return sorted_games

    def update_game_progress(self, rom_id, progress_info):
        """Update progress for a specific game"""
        if progress_info:
            self.game_progress[rom_id] = progress_info
        elif rom_id in self.game_progress:
            del self.game_progress[rom_id]
        
        # Find and update the specific game item
        self._update_game_status_display(rom_id)
        
    def _update_game_status_display(self, rom_id):
        """Start polling for progress updates without items_changed flicker"""
        # Find the game cells and start polling for this ROM
        if not hasattr(self, '_polling_roms'):
            self._polling_roms = set()
        
        if rom_id not in self._polling_roms:
            self._polling_roms.add(rom_id)
            
            def poll_progress():
                progress_info = self.game_progress.get(rom_id)
                if not progress_info or not progress_info.get('downloading'):
                    self._polling_roms.discard(rom_id)
                    return False  # Stop polling
                
                # Force a minimal refresh by updating a dummy property
                model = self.library_model.tree_model
                for i in range(model.get_n_items() if model else 0):
                    tree_item = model.get_item(i)
                    if tree_item and tree_item.get_depth() == 1:  # Game level
                        item = tree_item.get_item()
                        if isinstance(item, GameItem) and item.game_data.get('rom_id') == rom_id:
                            # Trigger a minimal update
                            item.notify('name')  # This forces cell refresh without items_changed
                            break
                
                return True  # Continue polling
            
            GLib.timeout_add(200, poll_progress)

    def on_open_in_romm_clicked(self, button):
        """Opens the selected game or platform page in the default web browser."""
        
        # Check if RomM client is connected
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return
        
        base_url = self.parent.romm_client.base_url
        
        # Check for single row selection first
        if self.selected_game:
            # Individual game selected
            rom_id = self.selected_game.get('rom_id')
            if rom_id:
                game_url = f"{base_url}/rom/{rom_id}"
                try:
                    webbrowser.open(game_url)
                    self.parent.log_message(f"🌐 Opened {self.selected_game.get('name')} in browser.")
                except Exception as e:
                    self.parent.log_message(f"❌ Could not open web page: {e}")
        else:
            # Check if platform is selected via tree selection
            selection_model = self.column_view.get_model()
            selected_positions = []
            for i in range(selection_model.get_n_items()):
                if selection_model.is_selected(i):
                    selected_positions.append(i)
            
            if len(selected_positions) == 1:
                tree_item = selection_model.get_item(selected_positions[0])
                item = tree_item.get_item()
                
                if isinstance(item, PlatformItem):
                    # Platform selected - get platform ID from first game in platform
                    platform_name = item.platform_name
                    platform_id = None
                    
                    # Get platform ID from any game in this platform
                    if item.games:
                        for game in item.games:
                            romm_data = game.get('romm_data')
                            if romm_data and romm_data.get('platform_id'):
                                platform_id = romm_data['platform_id']
                                break
                    
                    if platform_id:
                        platform_url = f"{base_url}/platform/{platform_id}"
                    else:
                        # Fallback to generic platforms page
                        platform_url = f"{base_url}/platforms"
                    
                    try:
                        webbrowser.open(platform_url)
                        self.parent.log_message(f"🌐 Opened {platform_name} platform in browser.")
                    except Exception as e:
                        self.parent.log_message(f"❌ Could not open platform page: {e}")

    def auto_expand_platforms_with_results(self, filtered_games):
        """Automatically expand platforms that contain search results"""
        if not self.search_text:  # No search active, don't auto-expand
            return
            
        # Get platforms that have results
        platforms_with_results = set()
        for game in filtered_games:
            platforms_with_results.add(game.get('platform', 'Unknown'))
        
        def expand_matching_platforms():
            model = self.library_model.tree_model
            if not model:
                return False
                
            for i in range(model.get_n_items()):
                tree_item = model.get_item(i)
                if tree_item and tree_item.get_depth() == 0:  # Platform level
                    platform_item = tree_item.get_item()
                    if isinstance(platform_item, PlatformItem):
                        if platform_item.platform_name in platforms_with_results:
                            tree_item.set_expanded(True)
            
            return False
        
        # Expand after a small delay to ensure tree is updated
        GLib.timeout_add(100, expand_matching_platforms)

    def update_games_library(self, games):
        """Update the tree view with enhanced stable expansion preservation"""
        if getattr(self.parent, '_dialog_open', False):
            return
        
        # Apply current filters (platform + search)
        games = self.apply_filters(games)
        self.filtered_games = games
        
        # Apply current platform filter
        selected_index = self.platform_filter.get_selected()
        if selected_index != Gtk.INVALID_LIST_POSITION:
            string_list = self.platform_filter.get_model()
            if string_list:
                selected_platform = string_list.get_string(selected_index)
                if selected_platform != "All Platforms":
                    games = [game for game in games if game.get('platform', 'Unknown') == selected_platform]
        
        self.filtered_games = games
        
        # Save scroll position
        scroll_position = 0
        if hasattr(self, 'column_view'):
            scrolled_window = self.column_view.get_parent()
            if scrolled_window:
                vadj = scrolled_window.get_vadjustment()
                if vadj:
                    scroll_position = vadj.get_value()
        
        
        def do_update():
            self.library_model.update_library(games)
            self.update_platform_filter(self.parent.available_games)  # Use all games for filter options
            
            if games:
                downloaded_count = sum(1 for g in games if g.get('is_downloaded'))
                total_count = len(games)
        
        # Update with selection preservation
        self.preserve_selections_during_update(do_update)
        
        # Restore scroll position
        def restore_scroll():
            if hasattr(self, 'column_view'):
                scrolled_window = self.column_view.get_parent()
                if scrolled_window:
                    vadj = scrolled_window.get_vadjustment()
                    if vadj:
                        vadj.set_value(scroll_position)
            return False
        
        GLib.timeout_add(400, restore_scroll)

        # Save scroll position
        scroll_position = 0
        if hasattr(self, 'column_view'):
            scrolled_window = self.column_view.get_parent()
            if scrolled_window:
                vadj = scrolled_window.get_vadjustment()
                if vadj:
                    scroll_position = vadj.get_value()
        
        def do_update():
            # The enhanced LibraryTreeModel now handles expansion tracking
            self.library_model.update_library(games)
            self.update_platform_filter(games)
            
            if games:
                downloaded_count = sum(1 for g in games if g.get('is_downloaded'))
                total_count = len(games)
        
        # Update with selection preservation
        self.preserve_selections_during_update(do_update)
        
        # Restore scroll position with delay to avoid interfering with expansion restoration
        def restore_scroll():
            if hasattr(self, 'column_view'):
                scrolled_window = self.column_view.get_parent()
                if scrolled_window:
                    vadj = scrolled_window.get_vadjustment()
                    if vadj:
                        vadj.set_value(scroll_position)
            return False
        
        GLib.timeout_add(400, restore_scroll)

    def _detect_connection_change(self, games):
        """Detect if this update is due to connection state change"""
        # Simple heuristic: if we have games with ROM IDs vs games without ROM IDs
        current_has_rom_ids = any(g.get('rom_id') for g in games) if games else False
        last_had_rom_ids = any(g.get('rom_id') for g in getattr(self.parent, 'available_games', [])) if hasattr(self.parent, 'available_games') else False
        
        return current_has_rom_ids != last_had_rom_ids

    def refresh_all_platform_checkboxes(self):
        """Force refresh all platform checkbox states to match current selections"""
        model = self.library_model.tree_model
        for i in range(model.get_n_items()):
            tree_item = model.get_item(i)
            if tree_item and tree_item.get_depth() == 0:  # Platform level
                item = tree_item.get_item()
                if isinstance(item, PlatformItem):
                    self.update_platform_checkbox_for_game({'platform': item.platform_name})

    def _restore_tree_state_immediate(self, tree_state):
        """Restore tree state immediately for smoother transitions"""
        try:
            # Restore expansion first (immediately)
            expansion_state = tree_state.get('expansion_state', {})
            self.library_model._restore_expansion_immediate(expansion_state)
            
            # Then restore scroll position with minimal delay
            GLib.timeout_add(50, lambda: self._restore_scroll_position(tree_state))
            
        except Exception as e:
            print(f"Error in immediate tree state restore: {e}")
    
    def _restore_scroll_position(self, tree_state):
        """Restore scroll position"""
        try:
            if hasattr(self, 'column_view'):
                scrolled_window = self.column_view.get_parent()
                if scrolled_window:
                    vadj = scrolled_window.get_vadjustment()
                    if vadj:
                        vadj.set_value(tree_state.get('scroll_position', 0))
            return False
        except Exception as e:
            print(f"Error restoring scroll position: {e}")
            return False

    def get_selected_games(self):
        """Get list of selected game data (not GameItem objects)"""
        selected_games = []
        # Use filtered games instead of all available games
        games_to_check = self.filtered_games if hasattr(self, 'filtered_games') else self.parent.available_games
        
        for game in games_to_check:
            identifier_type, identifier_value = self.get_game_identifier(game)
            if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                selected_games.append(game)
            elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                selected_games.append(game)
        return selected_games

    def get_game_identifier(self, game_data):
        """Get unique identifier for a game (ROM ID if available, otherwise name+platform)"""
        rom_id = game_data.get('rom_id')
        if rom_id:
            return ('rom_id', rom_id)
        else:
            name = game_data.get('name', '')
            platform = game_data.get('platform', '')
            return ('game_key', f"{name}|{platform}")

    def _block_selection_updates(self, block=True):
        """Temporarily block selection updates during dialogs"""
        self._selection_blocked = block

    def update_platform_checkbox_states(self):
        """Update platform checkbox states based on their games' selection"""
        model = self.library_model.tree_model
        for i in range(model.get_n_items()):
            tree_item = model.get_item(i)
            if tree_item and tree_item.get_depth() == 0:  # Platform level items
                item = tree_item.get_item()
                if isinstance(item, PlatformItem):
                    # Check how many games in this platform are selected
                    selected_games_in_platform = [
                        game_item for game_item in self.selected_checkboxes 
                        if game_item.game_data in item.games
                    ]
                    
                    # Platform should be checked if all games are selected
                    should_be_checked = len(selected_games_in_platform) == len(item.games) and len(item.games) > 0
                    
                    # This will trigger a UI refresh for the platform checkbox
                    # The bind_checkbox_cell method will handle the visual update

    def update_bulk_action_buttons(self):
        """Update action button states based on selection (no separate bulk buttons)"""
        # SKIP UPDATES DURING DIALOG  
        if getattr(self, '_selection_blocked', False):
            return

        # Count selected games using the dual tracking system
        selected_count = 0
        
        for game in self.parent.available_games:
            identifier_type, identifier_value = self.get_game_identifier(game)
            if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                selected_count += 1
            elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                selected_count += 1
        
        # Update selection label
        if selected_count > 0:
            self.selection_label.set_text(f"{selected_count} selected")
        else:
            self.selection_label.set_text("No selection")

    def on_bulk_delete(self, button):
        """Delete all selected downloaded games"""
        selected_games = self.get_selected_games()
        
        # Filter to only downloaded games
        downloaded_games = [g for g in selected_games if g.get('is_downloaded', False)]
        
        if downloaded_games and hasattr(self.parent, 'delete_multiple_games'):
            self.parent.delete_multiple_games(downloaded_games)

    def on_select_all(self, button):
        """Select all game items (not platforms)"""
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        
        # Add all games to selection tracking
        for game in self.parent.available_games:
            identifier_type, identifier_value = self.get_game_identifier(game)
            if identifier_type == 'rom_id':
                self.selected_rom_ids.add(identifier_value)
            elif identifier_type == 'game_key':
                self.selected_game_keys.add(identifier_value)
        
        self.sync_selected_checkboxes()
        self.update_action_buttons()
        self.update_selection_label()
        # Force immediate checkbox sync instead of full refresh
        GLib.idle_add(self.force_checkbox_sync)
        GLib.idle_add(self.refresh_all_platform_checkboxes)

    def on_select_downloaded(self, button):
        """Select only downloaded games"""
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        
        # Add only downloaded games to selection tracking
        for game in self.parent.available_games:
            if game.get('is_downloaded', False):
                identifier_type, identifier_value = self.get_game_identifier(game)
                if identifier_type == 'rom_id':
                    self.selected_rom_ids.add(identifier_value)
                elif identifier_type == 'game_key':
                    self.selected_game_keys.add(identifier_value)
        
        self.sync_selected_checkboxes()
        self.update_action_buttons()
        self.update_selection_label()
        # Force immediate checkbox sync instead of full refresh
        GLib.idle_add(self.force_checkbox_sync)
        GLib.idle_add(self.refresh_all_platform_checkboxes)

    def on_select_none(self, button):
        """Clear all selections"""
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.update_action_buttons()
        self.update_selection_label()
        # Force immediate checkbox sync instead of full refresh
        GLib.idle_add(self.force_checkbox_sync)
        GLib.idle_add(self.refresh_all_platform_checkboxes)

    def setup_library_ui(self):
        """Create the enhanced library UI with tree view"""
        # Create library group
        self.library_group = Adw.PreferencesGroup()
        self.library_group.set_title("Game Library")
        
        # Create main container
        library_container = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        library_container.set_margin_top(12)
        library_container.set_margin_bottom(12)
        library_container.set_margin_start(12)
        library_container.set_margin_end(12)
        
        # Toolbar with actions
        toolbar = self.create_toolbar()
        library_container.append(toolbar)
        
        # Tree view container
        tree_container = self.create_tree_view()
        library_container.append(tree_container)
        
        # Action buttons
        action_bar = self.create_action_bar()
        library_container.append(action_bar)
        
        # Wrap in ActionRow for proper styling
        library_row = Adw.ActionRow()
        library_row.set_child(library_container)
        self.library_group.add(library_row)
    
    def create_toolbar(self):
        """Create toolbar with search and filters"""
        toolbar_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        
        # Search entry
        self.search_entry = Gtk.SearchEntry()
        self.search_entry.set_placeholder_text("Search games...")
        self.search_entry.set_hexpand(True)
        self.search_entry.connect('search-changed', self.on_search_changed)
        toolbar_box.append(self.search_entry)
        
        # Platform filter dropdown
        self.platform_filter = Gtk.DropDown()
        self.platform_filter.set_tooltip_text("Filter by platform")
        self.platform_filter.connect('notify::selected-item', self.on_platform_filter_changed)
        toolbar_box.append(self.platform_filter)
        
        # View options
        view_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        view_box.add_css_class('linked')
        
        # Expand all button
        expand_btn = Gtk.Button.new_from_icon_name("view-list-symbolic")
        expand_btn.set_tooltip_text("Expand all platforms")
        expand_btn.connect('clicked', self.on_expand_all)
        view_box.append(expand_btn)
        
        # Collapse all button
        collapse_btn = Gtk.Button.new_from_icon_name("go-up-symbolic")
        collapse_btn.set_tooltip_text("Collapse all platforms")
        collapse_btn.connect('clicked', self.on_collapse_all)
        view_box.append(collapse_btn)

        # Sort toggle button (between collapse and filter)
        self.sort_btn = Gtk.Button.new_from_icon_name("view-sort-ascending-symbolic")
        self.sort_btn.set_tooltip_text("Sort: Downloaded")
        self.sort_btn.connect('clicked', self.on_toggle_sort)
        view_box.append(self.sort_btn)

        # Filter toggle button
        self.filter_btn = Gtk.Button.new_from_icon_name("folder-symbolic")
        self.filter_btn.set_tooltip_text("Show downloaded only")
        self.filter_btn.connect('clicked', self.on_toggle_filter)
        view_box.append(self.filter_btn)
                
        # Refresh button
        refresh_btn = Gtk.Button.new_from_icon_name("view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Refresh library")
        refresh_btn.connect('clicked', self.on_refresh_library)
        view_box.append(refresh_btn)
        
        toolbar_box.append(view_box)
        
        return toolbar_box
    
    def create_tree_view(self):
        scrolled = Gtk.ScrolledWindow()
        scrolled.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
        scrolled.set_size_request(-1, 300)
        scrolled.add_css_class('data-table')
        
        # Create MultiSelection model
        selection_model = Gtk.MultiSelection.new(self.library_model.tree_model)
        selection_model.connect('selection-changed', self.on_selection_changed)

        self.column_view = Gtk.ColumnView()
        self.column_view.set_model(selection_model)
        self.column_view.add_css_class('data-table')

        # Make sure the ColumnView can actually be selected
        self.column_view.set_can_focus(True)
        self.column_view.set_focusable(True)

        # Add row activation (double-click)
        self.column_view.connect('activate', self.on_row_activated)

        # Add checkbox column (first column)
        checkbox_factory = Gtk.SignalListItemFactory()
        checkbox_factory.connect('setup', self.setup_checkbox_cell)
        checkbox_factory.connect('bind', self.bind_checkbox_cell)
        checkbox_column = Gtk.ColumnViewColumn.new("", checkbox_factory)
        checkbox_column.set_fixed_width(40)
        self.column_view.append_column(checkbox_column)
        
        # Name column with TreeExpander
        name_factory = Gtk.SignalListItemFactory()
        name_factory.connect('setup', self.setup_name_cell)
        name_factory.connect('bind', self.bind_name_cell)
        name_column = Gtk.ColumnViewColumn.new("Name", name_factory)
        name_column.set_expand(True)
        self.column_view.append_column(name_column)
        
        # Status column
        status_factory = Gtk.SignalListItemFactory()
        status_factory.connect('setup', self.setup_status_cell)
        status_factory.connect('bind', self.bind_status_cell)
        status_column = Gtk.ColumnViewColumn.new("Status", status_factory)
        status_column.set_fixed_width(80)
        self.column_view.append_column(status_column)
        
        # Size column
        size_factory = Gtk.SignalListItemFactory()
        size_factory.connect('setup', self.setup_size_cell)
        size_factory.connect('bind', self.bind_size_cell)
        size_column = Gtk.ColumnViewColumn.new("Size", size_factory)  # Fixed the typo here
        size_column.set_fixed_width(150)
        self.column_view.append_column(size_column)
        
        scrolled.set_child(self.column_view)
        return scrolled

    def on_row_activated(self, column_view, position):
        """Handle row activation (double-click)"""
        selection_model = column_view.get_model()
        tree_item = selection_model.get_item(position)
        
        if tree_item:
            item = tree_item.get_item()
            if isinstance(item, GameItem):
                # Double-click on game: download or launch
                if hasattr(self, 'on_game_action_clicked'):
                    self.selected_game = item.game_data
                    self.on_game_action_clicked(None)
            elif isinstance(item, PlatformItem):
                # Double-click on platform: toggle expansion
                tree_item.set_expanded(not tree_item.get_expanded())

    def create_mission_center_columns(self):
        """Create columns that match Mission Center's layout"""
        
        # Name column with tree structure (like Mission Center's main column)
        name_factory = Gtk.SignalListItemFactory()
        name_factory.connect('setup', self.setup_name_cell_mission_center)
        name_factory.connect('bind', self.bind_name_cell_mission_center)
        
        name_column = Gtk.ColumnViewColumn.new("Name", name_factory)
        name_column.set_expand(True)
        name_column.set_resizable(True)
        self.column_view.append_column(name_column)
        
        # Games count column (like Mission Center's PID column)
        count_factory = Gtk.SignalListItemFactory()
        count_factory.connect('setup', self.setup_count_cell)
        count_factory.connect('bind', self.bind_count_cell)
        
        count_column = Gtk.ColumnViewColumn.new("Games", count_factory)
        count_column.set_fixed_width(80)
        count_column.set_resizable(True)
        self.column_view.append_column(count_column)
        
        # Size column (like Mission Center's Memory column)
        size_factory = Gtk.SignalListItemFactory()
        size_factory.connect('setup', self.setup_size_cell_right_aligned)
        size_factory.connect('bind', self.bind_size_cell_mission_center)
        
        size_column = Gtk.ColumnViewColumn.new("Size", size_factory)
        size_column.set_fixed_width(100)
        size_column.set_resizable(True)
        self.column_view.append_column(size_column)

    def setup_name_cell(self, factory, list_item):
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=2)
        
        expander = Gtk.TreeExpander()
        icon = Gtk.Image()
        icon.set_pixel_size(16)
        label = Gtk.Label()
        label.set_halign(Gtk.Align.START)
        label.set_ellipsize(3)
        
        expander.set_child(icon)
        box.append(expander)
        box.append(label)
        list_item.set_child(box)

    def bind_name_cell(self, factory, list_item):
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        box = list_item.get_child()
        expander = box.get_first_child()
        label = box.get_last_child()
        icon = expander.get_child()
        
        expander.set_list_row(tree_item)
        depth = tree_item.get_depth()
        box.set_margin_start(depth * 0)
        
        if isinstance(item, PlatformItem):
            icon.set_from_icon_name("folder-symbolic")
            # IMPORTANT: Bind the label to the 'name' property instead of setting text directly
            # This means when platform_item.notify('name') is called, the label will automatically update
            item.bind_property('name', label, 'label', GObject.BindingFlags.SYNC_CREATE)
        else:
            icon.set_from_icon_name("object-select-symbolic" if item.is_downloaded else "folder-download-symbolic")
            # For games, we can still set directly since they update via items_changed
            label.set_text(item.name)

    def bind_status_cell(self, factory, list_item):
        """Show percentage during downloads, normal status otherwise"""
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        label = list_item.get_child()
        
        if isinstance(item, PlatformItem):
            item.bind_property('status-text', label, 'label', GObject.BindingFlags.SYNC_CREATE)
        elif isinstance(item, GameItem):
            # Connect to name property changes to update status
            def update_status(*args):
                rom_id = item.game_data.get('rom_id')
                progress_info = self.game_progress.get(rom_id) if rom_id else None
                
                if progress_info and progress_info.get('downloading'):
                    progress = progress_info.get('progress', 0.0)
                    label.set_text(f"{progress*100:.0f}%")
                elif progress_info and progress_info.get('completed'):
                    label.set_text("✅")
                elif progress_info and progress_info.get('failed'):
                    label.set_text("❌")
                else:
                    label.set_text("✅" if item.is_downloaded else "⬇️")
            
            item.connect('notify::name', update_status)
            update_status()  # Initial update

    def bind_size_cell(self, factory, list_item):
        """Show download info with compact format"""
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        label = list_item.get_child()
        
        if isinstance(item, PlatformItem):
            item.bind_property('size-text', label, 'label', GObject.BindingFlags.SYNC_CREATE)
        elif isinstance(item, GameItem):
            def update_size(*args):
                rom_id = item.game_data.get('rom_id')
                progress_info = self.game_progress.get(rom_id) if rom_id else None
                
                if progress_info and progress_info.get('downloading'):
                    downloaded = progress_info.get('downloaded', 0)
                    total = progress_info.get('total', 0)
                    speed = progress_info.get('speed', 0)  # Add this line
                    
                    def format_size_compact(bytes_val):
                        if bytes_val >= 1024**3:
                            return f"{bytes_val / (1024**3):.1f}G"
                        elif bytes_val >= 1024**2:
                            return f"{bytes_val / (1024**2):.0f}M"
                        else:
                            return f"{bytes_val / 1024:.0f}K"
                    
                    if total > 0:
                        size_text = f"{format_size_compact(downloaded)}/{format_size_compact(total)}"
                    else:
                        size_text = format_size_compact(downloaded)
                    
                    # Add speed display back
                    if speed > 0:
                        speed_str = format_size_compact(speed)
                        label.set_text(f"{size_text} @{speed_str}/s")
                    else:
                        label.set_text(f"{size_text} ...")
                else:
                    label.set_text(item.size_text)
            
            item.connect('notify::name', update_size)
            update_size()  # Initial update

    def setup_status_cell(self, factory, list_item):
        """Simple status label - no progress bar needed"""
        label = Gtk.Label()
        label.set_halign(Gtk.Align.CENTER)
        label.add_css_class('numeric')
        list_item.set_child(label)


    def setup_size_cell(self, factory, list_item):
        label = Gtk.Label()
        label.set_halign(Gtk.Align.END)
        label.add_css_class('numeric')
        list_item.set_child(label)
    
    def create_action_bar(self):
        action_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        action_box.set_margin_top(6)
        
        # Original single-item action buttons (left side) - now work on multiple items too
        single_actions = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        single_actions.add_css_class('linked')
        
        # Download/Launch button - now handles multiple selections
        self.action_button = Gtk.Button(label="Download")
        self.action_button.add_css_class('warning')  # Start with warning style for Download
        self.action_button.set_sensitive(False)
        self.action_button.set_size_request(125, -1)  # Fixed width for text
        self.action_button.set_hexpand(False)
        self.action_button.set_halign(Gtk.Align.START)
        self.action_button.connect('clicked', self.on_action_clicked)
        single_actions.append(self.action_button)
        
        # Delete button - now handles multiple selections
        self.delete_button = Gtk.Button.new_from_icon_name("user-trash-symbolic")
        self.delete_button.set_tooltip_text("Delete selected ROM(s)")
        self.delete_button.add_css_class('destructive-action')
        self.delete_button.set_sensitive(False)
        self.delete_button.connect('clicked', self.on_delete_clicked)
        single_actions.append(self.delete_button)

        # --- Create button with RomM Logo ---
        self.open_in_romm_button = Gtk.Button()
        script_dir = os.path.dirname(os.path.abspath(__file__))

        # Try multiple icon locations for AppImage compatibility
        icon_locations = [
            os.path.join(script_dir, 'romm_icon.png'),  # AppImage location
            os.path.join(script_dir, '..', 'assets', 'icons', 'romm_icon.png'),  # Regular install
            'romm_icon.png'  # Fallback
        ]

        romm_icon_path = None
        for location in icon_locations:
            if os.path.exists(location):
                romm_icon_path = location
                break

        if romm_icon_path:
            image = Gtk.Image.new_from_file(romm_icon_path)
            image.set_pixel_size(16)
            self.open_in_romm_button.set_child(image)
        else:
            # Fallback to text if icon not found
            self.open_in_romm_button.set_label("RomM")

        self.open_in_romm_button.set_tooltip_text("Open game/platform page in RomM")
        self.open_in_romm_button.set_sensitive(False)
        self.open_in_romm_button.connect('clicked', self.on_open_in_romm_clicked)
        single_actions.append(self.open_in_romm_button)

        
        action_box.append(single_actions)
        
        # Separator
        separator = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        separator.set_margin_start(6)
        separator.set_margin_end(6)
        action_box.append(separator)
        
        # Bulk selection controls
        bulk_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=3)
        bulk_box.add_css_class('linked')
        
        # Select all buttons
        select_all_btn = Gtk.Button(label="All")
        select_all_btn.connect('clicked', self.on_select_all)
        select_all_btn.set_tooltip_text("Select all games")
        bulk_box.append(select_all_btn)
        
        select_downloaded_btn = Gtk.Button(label="Downloaded")
        select_downloaded_btn.connect('clicked', self.on_select_downloaded)
        select_downloaded_btn.set_tooltip_text("Select downloaded games")
        bulk_box.append(select_downloaded_btn)
        
        select_none_btn = Gtk.Button(label="None")
        select_none_btn.connect('clicked', self.on_select_none)
        select_none_btn.set_tooltip_text("Clear selection")
        bulk_box.append(select_none_btn)
        
        action_box.append(bulk_box)
        
        # Selection info (right side)
        spacer = Gtk.Box()
        spacer.set_hexpand(True)
        spacer.set_size_request(20, -1)  # Minimum width to prevent UI jumping
        action_box.append(spacer)
        
        # Selection label with ellipsization but flexible width
        self.selection_label = Gtk.Label()
        self.selection_label.set_text("No selection")
        self.selection_label.add_css_class('dim-label')
        self.selection_label.set_ellipsize(3)  # PANGO_ELLIPSIZE_END
        self.selection_label.set_xalign(1.0)  # Right-align text
        self.selection_label.set_size_request(200, -1)  # Give it minimum 200px width
        action_box.append(self.selection_label)
        
        return action_box
    
    def update_action_buttons(self):
        """Update action buttons based on selected game(s) or platform"""
        if getattr(self, '_selection_blocked', False):
            return
        
        is_connected = self.parent.romm_client and self.parent.romm_client.authenticated
        
        # Check for checkbox selections first (to determine priority)
        selected_games = []
        for game in self.parent.available_games:
            identifier_type, identifier_value = self.get_game_identifier(game)
            if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                selected_games.append(game)
            elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                selected_games.append(game)
        
        # Priority 1: Check for single row selection (game or platform) 
        # BUT only if there are no checkbox selections
        if self.selected_game and not selected_games:
            # Single game row selected with no checkbox selections
            game = self.selected_game
            is_downloaded = game.get('is_downloaded', False)
            
            if is_downloaded:
                self.action_button.set_label("Launch")
                self.action_button.remove_css_class('warning')
                self.action_button.add_css_class('suggested-action')
            else:
                self.action_button.set_label("Download")
                self.action_button.remove_css_class('suggested-action')
                self.action_button.add_css_class('warning')
            
            self.action_button.set_sensitive(True)
            self.delete_button.set_sensitive(is_downloaded)
            self.open_in_romm_button.set_sensitive(is_connected and game.get('rom_id'))
            return
        
        # Priority 2: Handle checkbox selections (takes precedence when present)
        if selected_games:
            downloaded_games = [g for g in selected_games if g.get('is_downloaded', False)]
            not_downloaded_games = [g for g in selected_games if not g.get('is_downloaded', False)]
            
            if len(selected_games) == 1:
                # Single checkbox selection
                game = selected_games[0]
                is_downloaded = game.get('is_downloaded', False)
                
                if is_downloaded:
                    self.action_button.set_label("Launch")
                    self.action_button.remove_css_class('warning')
                    self.action_button.add_css_class('suggested-action')
                else:
                    self.action_button.set_label("Download")
                    self.action_button.remove_css_class('suggested-action')
                    self.action_button.add_css_class('warning')
                
                self.action_button.set_sensitive(True)
                self.delete_button.set_sensitive(is_downloaded)
                self.open_in_romm_button.set_sensitive(is_connected and game.get('rom_id'))
            else:
                # Multiple checkbox selections
                if not_downloaded_games:
                    self.action_button.set_label(f"Download ({len(not_downloaded_games)})")
                    self.action_button.remove_css_class('suggested-action')
                    self.action_button.add_css_class('warning')
                    self.action_button.set_sensitive(True)
                elif downloaded_games:
                    self.action_button.set_label("Launch")
                    self.action_button.remove_css_class('warning')
                    self.action_button.remove_css_class('suggested-action')
                    self.action_button.set_sensitive(False)
                
                self.delete_button.set_sensitive(len(downloaded_games) > 0)
                self.open_in_romm_button.set_sensitive(False)  # Disable for multi-selection
            return
        
        # Priority 3: Check for single platform row selection (only if no checkboxes and no game row selected)
        selection_model = self.column_view.get_model()
        selected_positions = []
        for i in range(selection_model.get_n_items()):
            if selection_model.is_selected(i):
                selected_positions.append(i)
        
        if len(selected_positions) == 1:
            tree_item = selection_model.get_item(selected_positions[0])
            item = tree_item.get_item()
            
            if isinstance(item, PlatformItem):
                # Platform selected
                self.action_button.set_sensitive(False)
                self.delete_button.set_sensitive(False)
                self.open_in_romm_button.set_sensitive(is_connected)
                return
        
        # No selections - disable all buttons
        self.action_button.set_sensitive(False)
        self.action_button.set_label("Download")
        self.delete_button.set_sensitive(False)
        self.open_in_romm_button.set_sensitive(False)

    def update_platform_filter(self, games):
        """Update platform filter dropdown"""
        platforms = set()
        for game in games:
            platforms.add(game.get('platform', 'Unknown'))
        
        platform_list = ["All Platforms"] + sorted(platforms)
        string_list = Gtk.StringList()
        for platform in platform_list:
            string_list.append(platform)
        
        self.platform_filter.set_model(string_list)
        self.platform_filter.set_selected(0)  # "All Platforms"
    
    def on_selection_changed(self, selection_model, position, n_items):
        """Handle selection changes for both single and multi-selection"""
        # Find selected positions
        selected_positions = []
        for i in range(selection_model.get_n_items()):
            if selection_model.is_selected(i):
                selected_positions.append(i)
        
        if len(selected_positions) == 1:
            # Single item selected
            tree_item = selection_model.get_item(selected_positions[0])
            item = tree_item.get_item()
            
            if isinstance(item, GameItem):
                self.selected_game = item.game_data
                # Clear checkbox selections without full refresh
                if self.selected_checkboxes or self.selected_rom_ids or self.selected_game_keys:
                    self.selected_checkboxes.clear()
                    self.selected_rom_ids.clear()
                    self.selected_game_keys.clear()
                    GLib.idle_add(self.force_checkbox_sync)
                    GLib.idle_add(self.refresh_all_platform_checkboxes)
            elif isinstance(item, PlatformItem):
                self.selected_game = None
            # Update button states for both game and platform selections
            self.update_action_buttons()
        else:
            # Multiple or no row selection
            self.selected_game = None
            # Update button states
            self.update_action_buttons()
        
        # Update selection label
        self.update_selection_label()

    def update_selection_label(self):
        """Update the selection label text"""
        # SKIP UPDATES DURING DIALOG  
        if getattr(self, '_selection_blocked', False):
            return

        # Count selected games using the same logic as action buttons
        selected_count = 0
        for game in self.parent.available_games:
            identifier_type, identifier_value = self.get_game_identifier(game)
            if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                selected_count += 1
            elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                selected_count += 1
        
        if self.selected_game and selected_count == 0:
            # Single row selection, no checkboxes
            game_name = self.selected_game.get('name', 'Unknown')
            self.selection_label.set_text(f"{game_name}")
        elif selected_count > 0:
            # Checkbox selections
            if self.selected_game:
                game_name = self.selected_game.get('name', 'Unknown')
                self.selection_label.set_text(f"Row: {game_name} | {selected_count} checked")
            else:
                self.selection_label.set_text(f"{selected_count} games checked")
        else:
            # No selection
            self.selection_label.set_text("No selection")

    def on_search_changed(self, search_entry):
        """Handle search text changes"""
        self.search_text = search_entry.get_text().lower().strip()
        
        filtered_games = self.apply_filters(self.parent.available_games)
        sorted_games = self.sort_games_consistently(filtered_games)
        self.library_model.update_library(sorted_games)
        self.filtered_games = sorted_games
        
        self.auto_expand_platforms_with_results(sorted_games)
    
    def on_platform_filter_changed(self, dropdown, pspec):
        """Handle platform filter changes"""
        # Apply combined filters
        filtered_games = self.apply_filters(self.parent.available_games)
        self.library_model.update_library(filtered_games)
        self.filtered_games = filtered_games
        
    def on_expand_all(self, button):
        """Expand all tree items - simple and direct approach"""
        def expand_all_platforms():
            model = self.library_model.tree_model
            if not model:
                return False
            
            # Simple approach: just expand everything, multiple times if needed
            expanded_any = False
            
            # Do this multiple times to catch any lazy-loaded items
            for attempt in range(3):  # Try up to 3 times
                current_expanded = 0
                total_items = model.get_n_items()
                
                for i in range(total_items):
                    try:
                        tree_item = model.get_item(i)
                        if tree_item and tree_item.get_depth() == 0:  # Platform items
                            if not tree_item.get_expanded():
                                tree_item.set_expanded(True)
                                expanded_any = True
                                current_expanded += 1
                    except Exception as e:
                        print(f"Error expanding item {i}: {e}")
                        continue
                
                print(f"Expand attempt {attempt + 1}: expanded {current_expanded} platforms")
                
                # If we didn't expand anything this round, we're probably done
                if current_expanded == 0:
                    break
            
            if expanded_any:
                print(f"✅ Expand All completed")
            else:
                print(f"⚠️ No platforms found to expand")
            
            return False
        
        # Run immediately
        GLib.idle_add(expand_all_platforms)

    def on_collapse_all(self, button):
        """Collapse all tree items with proper state saving"""
        model = self.library_model.tree_model
        collapsed_count = 0
        
        for i in range(model.get_n_items()):
            item = model.get_item(i)
            if item and item.get_depth() == 0:  # Top level platform items
                if item.get_expanded():
                    item.set_expanded(False)
                    collapsed_count += 1
        
        print(f"👆 Collapse All: collapsed {collapsed_count} platforms")
        # The expansion tracking will automatically save the state
    
    def on_refresh_library(self, button):
        """Refresh library data"""
        if hasattr(self.parent, 'refresh_games_list'):
            self.parent.refresh_games_list()
    
    def on_action_clicked(self, button):
            """Handle main action button (download/launch) for single or multiple items"""
            selected_games = []
            
            # Priority 1: If there's a row selection (single game clicked), use that exclusively
            if self.selected_game:
                selected_games = [self.selected_game]
            else:
                # Priority 2: Use ROM ID/game key tracking (same as update_action_buttons)
                for game in self.parent.available_games:
                    identifier_type, identifier_value = self.get_game_identifier(game)
                    if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                        selected_games.append(game)
                    elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                        selected_games.append(game)
            
            if not selected_games:
                self.parent.log_message("No games selected")
                return
            
            if len(selected_games) == 1:
                # Single selection - use existing logic
                game = selected_games[0]
                if game.get('is_downloaded', False):
                    # Launch the game
                    if hasattr(self.parent, 'launch_game'):
                        self.parent.launch_game(game)
                else:
                    # Download the game
                    if hasattr(self.parent, 'download_game'):
                        self.parent.download_game(game)
            else:
                # Multiple selection - only download (launching multiple games isn't practical)
                not_downloaded_games = [g for g in selected_games if not g.get('is_downloaded', False)]
                
                if not_downloaded_games:
                    # Download multiple games immediately without confirmation
                    if hasattr(self.parent, 'download_multiple_games'):
                        self.parent.download_multiple_games(not_downloaded_games)

            # Clear checkbox selections after operation
            if len(selected_games) > 1:  # Only clear for multi-selection operations
                GLib.timeout_add(500, self.clear_checkbox_selection)  # Small delay for UI feedback

    def on_delete_clicked(self, button):
        """Handle delete button for single or multiple items"""
        selected_games = []
        
        # Priority 1: If there's a row selection (single game clicked), use that exclusively
        if self.selected_game:
            selected_games = [self.selected_game]
        else:
            # Priority 2: Use ROM ID/game key tracking (same as update_action_buttons)
            for game in self.parent.available_games:
                identifier_type, identifier_value = self.get_game_identifier(game)
                if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                    selected_games.append(game)
                elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                    selected_games.append(game)
        
        downloaded_games = [g for g in selected_games if g.get('is_downloaded', False)]
        
        if not downloaded_games:
            return
        
        if len(downloaded_games) == 1:
            # Single deletion - use existing logic
            if hasattr(self.parent, 'delete_game_file'):
                self.parent.delete_game_file(downloaded_games[0])
        else:
            # Multiple deletion
            if hasattr(self.parent, 'delete_multiple_games'):
                self.parent.delete_multiple_games(downloaded_games)

        # Clear checkbox selections after operation
        if len(downloaded_games) > 1:  # Only clear for multi-selection operations
            GLib.timeout_add(500, self.clear_checkbox_selection)  # Small delay for UI feedback

    def on_download_all_clicked(self, button):
        """Handle download all button for platform"""
        # TODO: Implement bulk download for platform
        print("Download all for platform")

    def update_single_game(self, updated_game_data, skip_platform_update=False):
        """
        Finds and updates a single game and tells the model to refresh just that row
        using the items_changed signal for a reliable update.
        FIXED: Now preserves visual row selection during updates.
        """
        target_rom_id = updated_game_data.get('rom_id')
        target_platform_name = updated_game_data.get('platform')

        if not target_rom_id or not target_platform_name:
            # Fallback for safety if we don't have enough info.
            print("⚠️ Insufficient data for in-place update. Falling back to full refresh.")
            self.update_games_library(self.parent.available_games)
            return

        # 1. Update the master list of game data. This ensures the source of truth is correct.
        game_found_in_source = False
        for game in self.parent.available_games:
            if game.get('rom_id') == target_rom_id:
                game.update(updated_game_data)
                game_found_in_source = True
                break
        
        if not game_found_in_source:
            # This can happen if the game is no longer in the main list. A full refresh is best.
            self.update_games_library(self.parent.available_games)
            return

        # SAVE CURRENT SELECTION STATE BEFORE UPDATING
        saved_selection_position = None
        saved_selected_game = self.selected_game
        
        # Find which position is currently selected in the tree
        if hasattr(self, 'column_view'):
            selection_model = self.column_view.get_model()
            if selection_model:
                for i in range(selection_model.get_n_items()):
                    if selection_model.is_selected(i):
                        tree_item = selection_model.get_item(i)
                        if tree_item:
                            item = tree_item.get_item()
                            if isinstance(item, GameItem) and item.game_data.get('rom_id') == target_rom_id:
                                saved_selection_position = i
                                break

        # 2. Find the corresponding item in the UI model and update both game and platform data
        for i in range(self.library_model.root_store.get_n_items()):
            platform_item = self.library_model.root_store.get_item(i)
            
            # We need to check the platform name from the item's data, not its property,
            # to be sure we find the right one even if the name format changes.
            if platform_item.platform_name == updated_game_data.get('platform'):
                # Find the game item in the platform's child store.
                for j in range(platform_item.child_store.get_n_items()):
                    game_item = platform_item.child_store.get_item(j)
                    
                    if game_item.game_data.get('rom_id') == target_rom_id:
                        # Found the game. Update the underlying 'game_data' dict.
                        game_item.game_data.update(updated_game_data)
                        
                        # IMPORTANT: Also update the corresponding game in platform_item.games
                        # This ensures the platform's aggregated calculations are correct
                        for k, platform_game in enumerate(platform_item.games):
                            if platform_game.get('rom_id') == target_rom_id:
                                platform_item.games[k].update(updated_game_data)
                                break
                        
                        # Tell the child store that the item at position 'j' has changed.
                        platform_item.child_store.items_changed(j, 1, 1)
                        
                        # With proper property binding, platform updates should be automatic
                        print(f"✅ Updated game data for '{game_item.name}' - property binding should handle UI update...")
                        
                        def restore_selection_and_update():
                            try:
                                # Update platform properties
                                if hasattr(platform_item, 'force_property_update'):
                                    platform_item.force_property_update()
                                else:
                                    # Fallback to manual notifications
                                    platform_item.notify('name')
                                    platform_item.notify('status-text')
                                    platform_item.notify('size-text')
                                
                                print(f"🔄 Property notifications sent for {platform_item.platform_name}")
                                
                                # RESTORE VISUAL SELECTION if it was previously selected
                                if (saved_selection_position is not None and 
                                    saved_selected_game and 
                                    saved_selected_game.get('rom_id') == target_rom_id):
                                    
                                    selection_model = self.column_view.get_model()
                                    if selection_model and saved_selection_position < selection_model.get_n_items():
                                        # Clear current selection first
                                        selection_model.unselect_all()
                                        # Restore the selection
                                        selection_model.select_item(saved_selection_position, False)
                                        print(f"🎯 Restored visual selection for position {saved_selection_position}")
                                        
                                        # Make sure the selected_game is still set correctly
                                        self.selected_game = saved_selected_game
                                        
                                        # Update action buttons to reflect the selection
                                        self.update_action_buttons()
                                
                            except Exception as e:
                                print(f"❌ Error in restore selection: {e}")
                            return False
                        
                        # Schedule the restoration with a small delay to ensure the tree is updated
                        GLib.timeout_add(50, restore_selection_and_update)
                        return

        # Fallback if the item wasn't visible/found in the UI model.
        print(f"⚠️ Could not find UI item to signal change. Falling back to full refresh.")
        self.update_games_library(self.parent.available_games)

    def setup_checkbox_cell(self, factory, list_item):
        checkbox = Gtk.CheckButton()
        checkbox.connect('toggled', self.on_checkbox_toggled)
        list_item.set_child(checkbox)

    def bind_checkbox_cell(self, factory, list_item):
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        checkbox = list_item.get_child()
        
        if isinstance(item, GameItem):
            checkbox.set_visible(True)
            checkbox.game_item = item
            checkbox.tree_item = tree_item
            checkbox.is_platform = False
            
            # Check if this game is selected using dual tracking
            game_data = item.game_data
            identifier_type, identifier_value = self.get_game_identifier(game_data)
            
            should_be_active = False  # Define the variable first
            if identifier_type == 'rom_id':
                should_be_active = identifier_value in self.selected_rom_ids
            elif identifier_type == 'game_key':
                should_be_active = identifier_value in self.selected_game_keys
            
            checkbox.set_active(should_be_active)

            
        elif isinstance(item, PlatformItem):
            checkbox.set_visible(True)
            checkbox.platform_item = item
            checkbox.tree_item = tree_item
            checkbox.is_platform = True
            
            # Count selected games using the dual tracking system
            total_games = len(item.games)
            selected_games = 0
            
            for game in item.games:
                identifier_type, identifier_value = self.get_game_identifier(game)
                if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                    selected_games += 1
                elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                    selected_games += 1
            
            if selected_games == 0:
                # No games selected
                checkbox.set_active(False)
                checkbox.set_inconsistent(False)
            elif selected_games == total_games and total_games > 0:
                # All games selected
                checkbox.set_active(True)
                checkbox.set_inconsistent(False)
            else:
                # Some games selected (partial)
                checkbox.set_active(False)
                checkbox.set_inconsistent(True)

    def on_checkbox_toggled(self, checkbox):
        # Prevent recursive calls during updates
        if getattr(checkbox, '_updating', False):
            return
        
        if hasattr(checkbox, 'is_platform') and checkbox.is_platform:
            # Platform checkbox toggled
            platform_item = checkbox.platform_item
            should_select = checkbox.get_active()
            
            # If checkbox was inconsistent, clicking it should select all
            if checkbox.get_inconsistent():
                should_select = True
                checkbox.set_inconsistent(False)
                checkbox.set_active(True)
            
            # Create unique identifiers for games (works with or without ROM IDs)
            platform_game_keys = set()
            for game in platform_item.games:
                game_key = f"{game.get('name', '')}|{game.get('platform', '')}"
                platform_game_keys.add(game_key)
            
            # Update selections
            if should_select:
                # Add all games in this platform
                for game in platform_item.games:
                    identifier_type, identifier_value = self.get_game_identifier(game)
                    if identifier_type == 'rom_id':
                        self.selected_rom_ids.add(identifier_value)
                    else:
                        self.selected_game_keys.add(identifier_value)
            else:
                # Remove all games in this platform
                for game in platform_item.games:
                    identifier_type, identifier_value = self.get_game_identifier(game)
                    if identifier_type == 'rom_id':
                        self.selected_rom_ids.discard(identifier_value)
                    else:
                        self.selected_game_keys.discard(identifier_value)
            
            # CRITICAL: Sync the selected_checkboxes set with the new selections
            self.sync_selected_checkboxes()
            
            # Directly update visible game checkboxes without rebuilding
            self._update_visible_game_checkboxes(platform_game_keys, should_select)

            # Force sync in case direct update didn't work
            GLib.idle_add(self.force_checkbox_sync)
                            
        elif hasattr(checkbox, 'game_item'):
            # Game checkbox toggled - handle both ROM ID and non-ROM ID games
            game_data = checkbox.game_item.game_data
            identifier_type, identifier_value = self.get_game_identifier(game_data)
            
            if checkbox.get_active():
                if identifier_type == 'rom_id':
                    self.selected_rom_ids.add(identifier_value)
                else:
                    self.selected_game_keys.add(identifier_value)
                self.selected_checkboxes.add(checkbox.game_item)
            else:
                if identifier_type == 'rom_id':
                    self.selected_rom_ids.discard(identifier_value)
                else:
                    self.selected_game_keys.discard(identifier_value)
                self.selected_checkboxes.discard(checkbox.game_item)
            
            # Update parent platform checkbox state
            self.update_platform_checkbox_for_game(checkbox.game_item.game_data)
        
        # IMPORTANT: Clear row selection when any checkbox is toggled
        # This ensures checkbox selections take priority over row selections
        if self.selected_game:
            self.selected_game = None
        
        # Clear visual row selection more aggressively
        selection_model = self.column_view.get_model()
        if selection_model:
            try:
                # Force clear all row selections
                selection_model.unselect_all()
                
                # Double-check by manually clearing any remaining selections
                for i in range(selection_model.get_n_items()):
                    if selection_model.is_selected(i):
                        selection_model.unselect_item(i)
            except Exception as e:
                print(f"Error clearing row selection: {e}")
        
        # Update button states and selection label
        self.update_action_buttons()
        self.update_selection_label()

    def preserve_selections_during_update(self, update_func):
        """Wrapper to preserve selections during tree updates"""
        # Save current selections
        saved_rom_ids = self.selected_rom_ids.copy()
        saved_game_keys = self.selected_game_keys.copy()
        saved_checkboxes = self.selected_checkboxes.copy()
        
        # Perform the update
        result = update_func()
        
        # Restore selections
        self.selected_rom_ids = saved_rom_ids
        self.selected_game_keys = saved_game_keys
        self.selected_checkboxes = saved_checkboxes
        
        return result

    def update_platform_checkbox_for_game(self, game_data):
        """Update platform checkbox state when an individual game selection changes"""
        platform_name = game_data.get('platform', '')
        
        # Find the platform checkbox widget directly
        def find_platform_checkbox(widget, target_platform):
            """Recursively find platform checkbox widget"""
            if isinstance(widget, Gtk.CheckButton):
                if (hasattr(widget, 'is_platform') and widget.is_platform and 
                    hasattr(widget, 'platform_item') and 
                    widget.platform_item.platform_name == target_platform):
                    return widget
            
            # Continue searching children
            if hasattr(widget, 'get_first_child'):
                child = widget.get_first_child()
                while child:
                    result = find_platform_checkbox(child, target_platform)
                    if result:
                        return result
                    child = child.get_next_sibling()
            return None
        
        # Find and update the platform checkbox
        platform_checkbox = find_platform_checkbox(self.column_view, platform_name)
        if platform_checkbox and hasattr(platform_checkbox, 'platform_item'):
            platform_item = platform_checkbox.platform_item
            
            # Count selected games in this platform (handle both ROM ID and non-ROM ID)
            total_games = len(platform_item.games)
            selected_games = 0

            for game in platform_item.games:
                identifier_type, identifier_value = self.get_game_identifier(game)
                is_selected = False
                
                if identifier_type == 'rom_id':
                    is_selected = identifier_value in self.selected_rom_ids
                elif identifier_type == 'game_key':
                    is_selected = identifier_value in self.selected_game_keys
                
                if is_selected:
                    selected_games += 1
            
            # Update platform checkbox state
            platform_checkbox._updating = True  # Prevent recursion
            
            if selected_games == 0:
                # No games selected
                platform_checkbox.set_active(False)
                platform_checkbox.set_inconsistent(False)
            elif selected_games == total_games and total_games > 0:
                # All games selected
                platform_checkbox.set_active(True)
                platform_checkbox.set_inconsistent(False)
            else:
                # Some games selected (partial)
                platform_checkbox.set_active(False)
                platform_checkbox.set_inconsistent(True)
            
            platform_checkbox._updating = False

    def _update_visible_game_checkboxes(self, platform_game_keys, should_select):
        """Directly update visible game checkboxes by finding and updating them"""
        updated_count = 0
        
        # Walk through all widgets to find game checkboxes
        def find_and_update_checkboxes(widget):
            nonlocal updated_count
            
            if isinstance(widget, Gtk.CheckButton):
                if (hasattr(widget, 'game_item') and hasattr(widget, 'is_platform') and 
                    not widget.is_platform):  # It's a game checkbox
                    
                    game = widget.game_item.game_data
                    game_key = f"{game.get('name', '')}|{game.get('platform', '')}"
                    
                    if game_key in platform_game_keys:
                        widget._updating = True
                        widget.set_active(should_select)
                        widget._updating = False
                        updated_count += 1
            
            # Continue walking the widget tree
            if hasattr(widget, 'get_first_child'):
                child = widget.get_first_child()
                while child:
                    find_and_update_checkboxes(child)
                    child = child.get_next_sibling()
        
        # Start the search from the column view
        find_and_update_checkboxes(self.column_view)
        
        # If we couldn't find checkboxes (maybe they're not created yet), 
        # force them to be updated when they are created
        if updated_count == 0:
            print("DEBUG: No checkboxes found, will update on next bind")

    def force_checkbox_sync(self):
        """Force all visible checkboxes to match current selection state"""
        def sync_checkboxes(widget):
            if isinstance(widget, Gtk.CheckButton):
                if hasattr(widget, 'game_item') and hasattr(widget, 'is_platform'):
                    if not widget.is_platform:  # Game checkbox
                        game_data = widget.game_item.game_data
                        identifier_type, identifier_value = self.get_game_identifier(game_data)
                        
                        should_be_active = False
                        if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                            should_be_active = True
                        elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                            should_be_active = True
                        
                        if widget.get_active() != should_be_active:
                            widget._updating = True
                            widget.set_active(should_be_active)
                            widget._updating = False
            
            # Continue walking
            if hasattr(widget, 'get_first_child'):
                child = widget.get_first_child()
                while child:
                    sync_checkboxes(child)
                    child = child.get_next_sibling()
        
        sync_checkboxes(self.column_view)

    def _find_checkbox_for_tree_item(self, target_tree_item):
        """Find the checkbox widget for a specific tree item"""
        # This is complex in GTK4, so return None for now
        # The sync_selected_checkboxes() will handle the logic correctly
        return None

    def sync_selected_checkboxes(self):
        """Sync the GameItem set with current selections"""
        self.selected_checkboxes.clear()
        
        # Find all GameItem instances that should be selected
        model = self.library_model.tree_model
        for i in range(model.get_n_items()):
            tree_item = model.get_item(i)
            if tree_item and tree_item.get_depth() == 1:  # Game level items
                item = tree_item.get_item()
                if isinstance(item, GameItem):
                    # Check if this game is selected using dual tracking
                    identifier_type, identifier_value = self.get_game_identifier(item.game_data)
                    
                    is_selected = False
                    if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                        is_selected = True
                    elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                        is_selected = True
                    
                    if is_selected:
                        self.selected_checkboxes.add(item)

    def refresh_checkbox_states(self):
        """Force refresh of all checkbox states to match current selection"""
        def deferred_refresh():
            # Get the checkbox column (first column)
            checkbox_column = self.column_view.get_columns().get_item(0)
            if checkbox_column:
                # Get the factory and force it to rebind all cells
                factory = checkbox_column.get_factory()
                if factory:
                    # Emit items-changed to force rebind of just this column
                    model = self.library_model.tree_model
                    n_items = model.get_n_items()
            return False  # Don't repeat
        
        GLib.idle_add(deferred_refresh)

    def clear_checkbox_selection(self):
        """Clear all checkbox selections"""
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.update_action_buttons()
        self.update_selection_label()
        
        # Force UI refresh to update checkboxes
        GLib.idle_add(lambda: self.update_games_library(self.parent.available_games))

    def clear_checkbox_selections_smooth(self):
        """Clear checkbox selections without full tree refresh"""
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.update_action_buttons()
        self.update_selection_label()
        GLib.idle_add(self.force_checkbox_sync)
        GLib.idle_add(self.refresh_all_platform_checkboxes) 

class SettingsManager:
    """Handle saving and loading application settings"""
    
    def __init__(self):
        self.config_dir = Path.home() / '.config' / 'romm-retroarch-sync'
        self.config_file = self.config_dir / 'settings.ini'
        self.config_dir.mkdir(parents=True, exist_ok=True)

        # Add encryption setup
        self._setup_encryption()

        self.config = configparser.ConfigParser()
        self.load_settings()

    def _setup_encryption(self):
        """Setup encryption key"""
        try:
            from cryptography.fernet import Fernet
            import hashlib
            import getpass
            
            # Create key from username + hostname for basic protection
            key_material = f"{getpass.getuser()}-{socket.gethostname()}".encode()
            key = hashlib.sha256(key_material).digest()
            self.cipher = Fernet(base64.urlsafe_b64encode(key))
        except ImportError:
            print("⚠️ cryptography not available, using plain text storage")
            self.cipher = None

    def _encrypt(self, value):
        """Encrypt sensitive data"""
        if self.cipher and value:
            try:
                return self.cipher.encrypt(value.encode()).decode()
            except:
                pass
        return value

    def _decrypt(self, value):
        """Decrypt sensitive data"""
        if self.cipher and value:
            try:
                return self.cipher.decrypt(value.encode()).decode()
            except:
                pass
        return value

    def load_settings(self):
        """Load settings from file"""
        if self.config_file.exists():
            self.config.read(self.config_file)
        else:
            # Create default settings
            self.config['RomM'] = {
                'url': '',
                'username': '',
                'password': '',
                'remember_credentials': 'false',
                'auto_connect': 'false'
            }
            self.config['Download'] = {
                'rom_directory': str(Path.home() / 'RomMSync' / 'roms'),
                'save_directory': str(Path.home() / 'RomMSync' / 'saves'),
            }
            
            # UPDATE THIS EXISTING SECTION:
            self.config['AutoSync'] = {
                'auto_enable_on_connect': 'false',
                'overwrite_behavior': '0'  # Add this line
            }
            self.save_settings()
    
    def save_settings(self):
        """Save settings to file"""
        with open(self.config_file, 'w') as f:
            self.config.write(f)
    
    def get(self, section, key, fallback=''):
        """Get a setting value with decryption for sensitive data"""
        value = self.config.get(section, key, fallback=fallback)
        
        # Decrypt sensitive fields
        if section == 'RomM' and key in ['username', 'password'] and value:
            value = self._decrypt(value)
        
        return value

    def set(self, section, key, value):
        """Set a setting value with encryption for sensitive data"""
        if section not in self.config:
            self.config[section] = {}
        
        # Encrypt sensitive fields
        if section == 'RomM' and key in ['username', 'password'] and value:
            value = self._encrypt(value)
        
        self.config[section][key] = str(value)
        self.save_settings()

class DownloadProgress:
    """Track download progress with speed and ETA calculations"""
    
    def __init__(self, total_size, filename):
        self.total_size = total_size
        self.filename = filename
        self.downloaded = 0
        self.start_time = time.time()
        self.last_update = self.start_time
        
    def update(self, chunk_size):
        """Update progress with new chunk"""
        self.downloaded += chunk_size
        current_time = time.time()
        
        # Calculate progress percentage
        if self.total_size > 0:
            progress = self.downloaded / self.total_size
        else:
            # For unknown size, show as ongoing (never complete until manually set)
            progress = min(0.9, self.downloaded / (1024 * 1024))  # Approach 90% for 1MB downloaded
        
        # Calculate speed and ETA
        elapsed = current_time - self.start_time
        if elapsed > 0:
            speed = self.downloaded / elapsed  # bytes per second
            if self.total_size > 0:
                remaining = self.total_size - self.downloaded
                eta = remaining / speed if speed > 0 else 0
            else:
                eta = 0  # Unknown for indeterminate progress
        else:
            speed = 0
            eta = 0
            
        return {
            'progress': min(progress, 1.0),  # Cap at 100%
            'downloaded': self.downloaded,
            'total': self.total_size if self.total_size > 0 else self.downloaded,
            'speed': speed,
            'eta': eta,
            'filename': self.filename
        }

class RomMClient:
    """Client for interacting with RomM API"""
    
    def __init__(self, base_url, username=None, password=None):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        
        # Force HTTP/2 and connection reuse
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry
        
        adapter = HTTPAdapter(
            pool_connections=4,
            pool_maxsize=4,
            max_retries=Retry(total=2)
        )
        self.session.mount('http://', adapter)
        self.session.mount('https://', adapter)
        
        # Existing headers + compression
        self.session.headers.update({
            'Accept-Encoding': 'gzip, deflate, br',
            'Accept': 'application/json',
            'User-Agent': 'RomM-RetroArch-Sync/1.0.5',
            'Connection': 'keep-alive',
            'Keep-Alive': 'timeout=30, max=100'  # Keep connections alive
        })
        
        if username and password:
            self.authenticate(username, password)
    
    def authenticate(self, username, password):
        """Authenticate with RomM using Basic Auth or Token endpoint"""
        try:
            # Method 1: Basic Authentication (simpler approach)
            print("Trying Basic Authentication...")
            import base64
            
            # Create basic auth header
            credentials = f"{username}:{password}"
            encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
            
            self.session.headers.update({
                'Authorization': f'Basic {encoded_credentials}'
            })
            
            # Test the authentication by trying to access a protected endpoint
            test_response = self.session.get(
                urljoin(self.base_url, '/api/roms'),
                timeout=10
            )
            
            print(f"Basic auth test response: {test_response.status_code}")
            
            if test_response.status_code == 200:
                print("Basic Authentication successful!")
                self.authenticated = True
                return True
            elif test_response.status_code == 401:
                print("Basic auth failed, trying token endpoint...")
                
                # Method 2: Token-based authentication
                # Remove basic auth header first
                if 'Authorization' in self.session.headers:
                    del self.session.headers['Authorization']
                
                # Request token with required scopes
                token_data = {
                    'username': username,
                    'password': password,
                    'scopes': 'read:roms write:roms read:platforms write:platforms read:saves write:saves read:states write:states'
                }
                
                print("Requesting access token...")
                token_response = self.session.post(
                    urljoin(self.base_url, '/api/token'),
                    json=token_data,
                    timeout=10
                )
                
                print(f"Token response status: {token_response.status_code}")
                
                if token_response.status_code == 200:
                    token_data = token_response.json()
                    access_token = token_data.get('access_token')
                    
                    if access_token:
                        # Use the access token for future requests
                        self.session.headers.update({
                            'Authorization': f'Bearer {access_token}'
                        })
                        
                        # Test the token
                        test_response = self.session.get(
                            urljoin(self.base_url, '/api/roms'),
                            timeout=10
                        )
                        
                        if test_response.status_code == 200:
                            print("Token authentication successful!")
                            self.authenticated = True
                            return True
                
                print(f"Token auth failed: {token_response.text[:200] if token_response else 'No response'}")
            
            print("All authentication methods failed")
            return False
            
        except requests.exceptions.SSLError as e:
            print(f"SSL Error: {e}")
            return False
        except requests.exceptions.ConnectionError as e:
            print(f"Connection Error: {e}")
            return False
        except requests.exceptions.Timeout as e:
            print(f"Timeout Error: {e}")
            return False
        except Exception as e:
            print(f"Authentication error: {e}")
            return False

    def get_roms(self, progress_callback=None, limit=500, offset=0):
        """Get ROMs with pagination support - FIXED to fetch ALL games"""
        if not self.authenticated:
            return [], 0
        
        try:
            # For backward compatibility, if no specific limit is requested, fetch ALL games
            if limit == 500 and offset == 0:
                return self._fetch_all_games_chunked(progress_callback)
            else:
                # Specific pagination request
                response = self.session.get(
                    urljoin(self.base_url, '/api/roms'),
                    params={
                        'limit': limit,
                        'offset': offset,
                        'fields': 'id,name,fs_name,platform_name,platform_slug'
                    },
                    timeout=60
                )
                
                if response.status_code != 200:
                    print(f"❌ RomM API error: HTTP {response.status_code}")
                    return [], 0
                
                data = response.json()
                items = data.get('items', [])
                total = data.get('total', 0)
                
                if progress_callback:
                    progress_callback('batch', {'items': items, 'total': total, 'offset': offset})
                
                return items, total
            
        except Exception as e:
            print(f"❌ Error fetching ROMs: {e}")
            return [], 0

    def _fetch_all_games_chunked(self, progress_callback):
        """Fetch all games using parallel requests"""
        try:
            # First, get total count
            response = self.session.get(
                urljoin(self.base_url, '/api/roms'),
                params={'limit': 1, 'offset': 0, 'fields': 'id'},
                timeout=30
            )
            
            if response.status_code != 200:
                return [], 0
                
            data = response.json()
            total_games = data.get('total', 0)
            
            if total_games == 0:
                return [], 0
                
            chunk_size = 500
            total_chunks = (total_games + chunk_size - 1) // chunk_size
            
            print(f"📚 Fetching {total_games:,} games in {total_chunks} chunks of {chunk_size:,} (parallel)...")
            
            # Use existing parallel fetching
            all_games = self._fetch_pages_parallel(total_games, chunk_size, total_chunks, progress_callback)
            
            return all_games, len(all_games)
            
        except Exception as e:
            print(f"❌ Parallel fetch error: {e}")
            return [], 0
        
    def _find_optimal_page_size(self, total_items, progress_callback):
        # For libraries under 5000, skip all testing
        if total_items <= 5000:
            print(f"⚡ Small library ({total_items:,}), using single request")
            return total_items
        
        # Check cache first
        cached_page_size = getattr(self, '_cached_page_size', None)
        if cached_page_size:
            print(f"⚡ Using cached page size: {cached_page_size:,}")
            return cached_page_size
        
        # Only test for large libraries, and test fewer sizes
        test_sizes = [3000, 2000]  # Reduced from [5000, 3000, 2000, 1000]
        
        for size in test_sizes:
            try:  # ADD THIS LINE
                response = self.session.get(
                    urljoin(self.base_url, '/api/roms'),
                    params={'limit': size, 'offset': 0, 'fields': 'id,name'},
                    timeout=15
                )
                    
                if response.status_code == 200:
                    data = response.json()
                    items = data.get('items', [])
                    
                    expected_items = min(size, total_items)
                    variance = abs(len(items) - expected_items)
                    
                    if variance <= 10 or len(items) >= expected_items:
                        print(f"✅ Optimal page size found: {size:,} (cached for future use)")
                        self._cached_page_size = size
                        return size
                        
                elif response.status_code == 422:
                    print(f"❌ Page size {size:,}: Server limit exceeded")
                    continue
                        
            except Exception as e:
                print(f"❌ Page size {size:,} test failed: {e}")
                continue
        
        # Fallback (MOVE THIS OUTSIDE THE LOOP)
        fallback_size = 1000
        self._cached_page_size = fallback_size
        print(f"⚠️ Using fallback page size: {fallback_size}")
        return fallback_size

    def _fetch_pages_parallel(self, total_items, page_size, pages_needed, progress_callback):
        """Fetch all pages using parallel requests with deduplication"""
        import concurrent.futures
        import threading
        
        all_roms = []
        max_workers = 4  # Conservative to avoid overwhelming server
        completed_pages = 0
        lock = threading.Lock()
        
        def fetch_single_page(page_num):
            """Fetch a single page with optimized parameters"""
            offset = (page_num - 1) * page_size  # Use page_size, not optimal_page_size
            
            if offset >= total_items:
                return page_num, []
            
            try:
                response = self.session.get(
                    urljoin(self.base_url, '/api/roms'),
                    params={
                        'limit': page_size,
                        'offset': offset,
                        'fields': 'id,name,fs_name,platform_name,platform_slug'  # Remove fs_name_no_ext and fs_size_bytes for now
                    },
                    timeout=60
                )
                
                if response.status_code == 200:
                    data = response.json()
                    items = data.get('items', [])
                    
                    # Process in smaller chunks to reduce memory pressure
                    chunk_size = 500
                    processed_items = []
                    
                    for i in range(0, len(items), chunk_size):
                        chunk = items[i:i + chunk_size]
                        processed_items.extend(chunk)
                        
                        # Small yield every 500 items to prevent blocking
                        if i > 0 and i % 1000 == 0:
                            time.sleep(0.001)  # Tiny yield
                    
                    return page_num, processed_items
                else:
                    print(f"❌ Page {page_num} failed: HTTP {response.status_code}")
                    
            except Exception as e:
                print(f"❌ Page {page_num} error: {e}")
            
            return page_num, []
        
        # Process pages in batches to avoid overwhelming the server
        print(f"🚀 Starting parallel fetch: {pages_needed} pages, {max_workers} workers")

        for batch_start in range(1, pages_needed + 1, max_workers):
            batch_end = min(batch_start + max_workers, pages_needed + 1)
            batch_pages = list(range(batch_start, batch_end))
            
            if progress_callback:
                progress_callback('page', f'🔄 Parallel batch {batch_start}-{batch_end-1} pages')
            
            # Fetch this batch in parallel
            with concurrent.futures.ThreadPoolExecutor(max_workers=max_workers) as executor:
                # Submit all pages in this batch
                future_to_page = {
                    executor.submit(fetch_single_page, page): page 
                    for page in batch_pages
                }
                
                # Collect results as they complete
                batch_results = []
                batch_game_count = 0  # Track games in current batch
                
                for future in concurrent.futures.as_completed(future_to_page):
                    page_num, page_roms = future.result()
                    batch_results.append((page_num, page_roms))
                    batch_game_count += len(page_roms)  # Count games in this batch
                    
                    with lock:
                        completed_pages += 1
                        if progress_callback:
                            progress_callback('page', f'🔄 Completed {completed_pages}/{pages_needed} pages ({batch_game_count} games)')
                
                # Sort batch results by page number and add to main collection
                batch_results.sort(key=lambda x: x[0])
                for page_num, page_roms in batch_results:
                    if page_roms:
                        all_roms.extend(page_roms)  # Add to main accumulator

                # Call progress callback ONCE per batch (not per page)
                if progress_callback and all_roms:
                    progress_callback('batch', {
                        'items': batch_results[-1][1] if batch_results else [],  # Last batch's items
                        'total': total_items,
                        'accumulated_games': all_roms,  # Send ALL games accumulated so far
                        'chunk_number': len(range(batch_start, batch_end)),
                        'total_chunks': pages_needed
                    })

            print(f"✅ Batch complete: {len(all_roms):,} ROMs loaded so far ({batch_game_count} in this batch)")
            
        # Remove duplicates by ID (in case API returns duplicates)
        if all_roms:
            seen_ids = set()
            unique_roms = []
            for rom in all_roms:
                rom_id = rom.get('id')
                if rom_id and rom_id not in seen_ids:
                    seen_ids.add(rom_id)
                    unique_roms.append(rom)
                elif not rom_id:  # Keep ROMs without IDs
                    unique_roms.append(rom)
            
            if len(unique_roms) != len(all_roms):
                print(f"🔍 Removed {len(all_roms) - len(unique_roms)} duplicate ROMs")
                all_roms = unique_roms
        
        return all_roms
        
    def download_rom(self, rom_id, rom_name, download_path, progress_callback=None):
        """Download a ROM file with progress tracking"""
        try:
            # First, get detailed ROM info to find the filename
            rom_details_response = self.session.get(
                urljoin(self.base_url, f'/api/roms/{rom_id}'),
                timeout=10
            )
            
            if rom_details_response.status_code != 200:
                return False, f"Could not get ROM details: HTTP {rom_details_response.status_code}"
            
            rom_details = rom_details_response.json()
            print(f"ROM details keys: {list(rom_details.keys())}")
            
            # Try to find the filename in the ROM details
            filename = None
            possible_filename_fields = ['file_name', 'filename', 'fs_name', 'name', 'file', 'path']
            
            for field in possible_filename_fields:
                if field in rom_details and rom_details[field]:
                    filename = rom_details[field]
                    print(f"Found filename in field '{field}': {filename}")
                    break
            
            if not filename:
                # Try to extract from file_name_no_tags or other fields
                for field, value in rom_details.items():
                    if 'file' in field.lower() and isinstance(value, str) and value:
                        filename = value
                        print(f"Using filename from '{field}': {filename}")
                        break
            
            if not filename:
                print(f"Available ROM fields: {rom_details}")
                return False, "Could not find filename in ROM details"
            
            # Now try the correct API endpoint with filename
            api_endpoint = f'/api/roms/{rom_id}/content/{filename}'
            print(f"Trying correct API endpoint: {api_endpoint}")
            
            response = self.session.get(
                urljoin(self.base_url, api_endpoint),
                stream=True,
                timeout=30
            )
            
            print(f"API Response: {response.status_code}")
            print(f"Content-Type: {response.headers.get('content-type', 'unknown')}")
            print(f"Content-Length: {response.headers.get('content-length', 'Not provided')}")
            
            if response.status_code != 200:
                return False, f"API download failed: HTTP {response.status_code}"
            
            # Check if we're getting HTML (error page) instead of a ROM file
            content_type = response.headers.get('content-type', '').lower()
            if 'text/html' in content_type:
                sample = response.content[:200]
                print(f"Got HTML response: {sample}")
                return False, "API returned HTML page instead of ROM file"
            
            # Get total file size from headers
            total_size = int(response.headers.get('content-length', 0))
            print(f"File size from header: {total_size} bytes")
            
            # Create progress tracker
            if progress_callback:
                progress = DownloadProgress(total_size, rom_name)
            
            # Ensure download directory exists
            download_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Download with progress tracking
            actual_downloaded = 0
            start_time = time.time()
            
            with open(download_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        actual_downloaded += len(chunk)
                        
                        # Update progress
                        if progress_callback:
                            if total_size > 0:
                                progress_info = progress.update(len(chunk))
                            else:
                                # Create dynamic progress info
                                elapsed = time.time() - start_time
                                speed = actual_downloaded / elapsed if elapsed > 0 else 0
                                
                                progress_info = {
                                    'progress': min(0.8, actual_downloaded / (10 * 1024 * 1024)),  # Progress toward 10MB
                                    'downloaded': actual_downloaded,
                                    'total': max(actual_downloaded, 1024 * 1024),
                                    'speed': speed,
                                    'eta': 0,
                                    'filename': rom_name
                                }
                            progress_callback(progress_info)
            
            print(f"Successfully downloaded {actual_downloaded} bytes to {download_path}")
            
            # Verify the download
            if actual_downloaded == 0:
                return False, "Downloaded file is empty"
            
            # Final progress update
            if progress_callback:
                final_progress = {
                    'progress': 1.0,
                    'downloaded': actual_downloaded,
                    'total': actual_downloaded,
                    'speed': 0,
                    'eta': 0,
                    'filename': rom_name
                }
                progress_callback(final_progress)
            
            return True, f"Download successful ({actual_downloaded} bytes)"
            
        except Exception as e:
            print(f"Download exception: {e}")
            return False, f"Download error: {e}"
    
    def download_save(self, rom_id, save_type, download_path):
        """Download the latest save or state file for the given ROM"""
        try:
            suffix = download_path.suffix.lower()
            filename = None
            download_url = None
            expected_size = 0

            # Step 1: Get ROM details to check metadata first
            rom_details_url = urljoin(self.base_url, f"/api/roms/{rom_id}")
            rom_response = self.session.get(rom_details_url, timeout=10)
            
            if rom_response.status_code == 200:
                rom_data = rom_response.json()
                metadata_key = 'user_saves' if save_type == 'saves' else 'user_states'
                possible_files = rom_data.get(metadata_key, [])
                
                print(f"ROM metadata key '{metadata_key}':", rom_data.get(metadata_key))
                
                if isinstance(possible_files, list) and possible_files:
                    # Find files with matching extension
                    matching_files = []
                    for f in possible_files:
                        if isinstance(f, dict):
                            file_name = f.get('file_name', '')
                            if file_name.lower().endswith(suffix):
                                matching_files.append(f)
                        elif isinstance(f, str) and f.lower().endswith(suffix):
                            matching_files.append({'file_name': f})
                    
                    if matching_files:
                        # Sort by filename (later timestamps last) and pick the most recent
                        latest_file = sorted(matching_files, key=lambda x: x.get('file_name', ''), reverse=True)[0]
                        filename = latest_file['file_name']
                        expected_size = latest_file.get('file_size_bytes', 0)
                        
                        print(f"Expected file size: {expected_size} bytes")
                        
                        # Check if the metadata provides a direct download_path
                        if 'download_path' in latest_file:
                            # Use the download_path from metadata (it's relative to base_url)
                            download_url = urljoin(self.base_url, latest_file['download_path'])
                            print(f"Using metadata download_path: {download_url}")
                        else:
                            # Fallback to constructed URL
                            download_url = urljoin(self.base_url, f"/api/roms/{rom_id}/{save_type}/{filename}")
                            print(f"Using constructed URL: {download_url}")
                    else:
                        print(f"No {save_type} files with {suffix} extension found in metadata")
                        return False
                else:
                    print(f"No {save_type} files found in ROM metadata")
                    return False
            else:
                print(f"Failed to retrieve ROM metadata: {rom_response.status_code}")
                return False

            # Step 2: Try to download the file with enhanced debugging
            if download_url and filename:
                print(f"Downloading {filename} from {download_url}")
                
                # Make request with detailed logging
                download_response = self.session.get(download_url, stream=True, timeout=30)
                
                print(f"Download response status: {download_response.status_code}")
                print(f"Response headers: {dict(download_response.headers)}")
                
                if download_response.status_code != 200:
                    print(f"Failed to download {filename}: {download_response.status_code}")
                    print(f"Response text: {download_response.text[:500]}")
                    return False

                # Check content type and headers
                content_type = download_response.headers.get('content-type', 'unknown')
                content_length = download_response.headers.get('content-length')
                
                if content_length:
                    reported_size = int(content_length)
                    print(f"Server reports content-length: {reported_size} bytes")
                    if expected_size > 0 and abs(reported_size - expected_size) > 1000:
                        print(f"⚠ WARNING: Size mismatch! Expected {expected_size}, server reports {reported_size}")
                
                # Check if we're getting an error response instead of the file
                if 'text/html' in content_type.lower():
                    print("⚠ WARNING: Got HTML response instead of binary file")
                    sample_content = download_response.content[:500]
                    print(f"Sample content: {sample_content}")
                    return False

                # Ensure download directory exists
                download_path.parent.mkdir(parents=True, exist_ok=True)
                
                # Download with byte counting
                actual_bytes = 0
                chunk_count = 0
                
                try:
                    with open(download_path, 'wb') as f:
                        for chunk in download_response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                actual_bytes += len(chunk)
                                chunk_count += 1
                                
                                # Log progress for large files
                                if chunk_count % 100 == 0:  # Every 100 chunks (800KB)
                                    print(f"Downloaded {actual_bytes} bytes so far...")
                    
                    print(f"Download completed: {actual_bytes} bytes written to disk")
                    
                    # Verify the download
                    if download_path.exists():
                        file_size = download_path.stat().st_size
                        print(f"File verification: {file_size} bytes on disk")
                        
                        if file_size != actual_bytes:
                            print(f"⚠ WARNING: Bytes written ({actual_bytes}) != file size ({file_size})")
                        
                        if expected_size > 0 and abs(file_size - expected_size) > 1000:
                            print(f"⚠ WARNING: Downloaded size ({file_size}) significantly different from expected ({expected_size})")
                            
                            # Try to inspect the file content
                            try:
                                with open(download_path, 'rb') as f:
                                    first_bytes = f.read(100)
                                    print(f"First 100 bytes: {first_bytes}")
                                    
                                    # Check if it might be a text error response
                                    try:
                                        text_content = first_bytes.decode('utf-8', errors='ignore')
                                        if any(error_indicator in text_content.lower() for error_indicator in ['error', 'not found', '404', 'unauthorized', 'html']):
                                            print(f"⚠ File appears to be an error response: {text_content}")
                                            return False
                                    except:
                                        pass
                            except Exception as e:
                                print(f"Could not inspect file content: {e}")
                        
                        if file_size > 0:
                            print(f"Successfully downloaded {filename} ({file_size} bytes)")
                            return True
                        else:
                            print(f"Downloaded file is empty")
                            return False
                    else:
                        print(f"Downloaded file not found after write")
                        return False
                        
                except Exception as write_error:
                    print(f"Error writing file: {write_error}")
                    return False
            else:
                print(f"Could not determine download URL for {save_type}")
                return False

        except Exception as e:
            print(f"Error downloading {save_type} for ROM {rom_id}: {e}")
            return False
    
    def upload_save(self, rom_id, save_type, file_path, emulator=None):
        """Upload save file using RomM naming convention with timestamps"""
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                print(f"Upload error: file not found at {file_path}")
                return False

            file_size = file_path.stat().st_size
            print(f"Uploading {file_path.name} ({file_size} bytes) to ROM {rom_id} as {save_type}")

            # Correct endpoint with rom_id as query parameter
            if emulator:
                endpoint = f'/api/{save_type}?rom_id={rom_id}&emulator={emulator}'
            else:
                endpoint = f'/api/{save_type}?rom_id={rom_id}'
                upload_url = urljoin(self.base_url, endpoint)
                print(f"Using endpoint: {upload_url}")

            # Use correct field names discovered from web interface
            if save_type == 'states':
                file_field_name = 'stateFile'
            elif save_type == 'saves':
                file_field_name = 'saveFile'
            else:
                print(f"Unknown save type: {save_type}")
                return False

            # Generate RomM-style filename with timestamp
            original_basename = file_path.stem  # "Test (USA)"
            file_extension = file_path.suffix   # ".state"
            
            # Generate timestamp in RomM format: [YYYY-MM-DD HH-MM-SS-mmm]
            import datetime
            now = datetime.datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H-%M-%S-%f")[:-3]  # Remove last 3 digits from microseconds
            
            # Create RomM-style filename: "Game Name [2025-07-02 15-30-25-123].state"
            romm_filename = f"{original_basename} [{timestamp}]{file_extension}"
            
            print(f"Original filename: {file_path.name}")
            print(f"RomM filename: {romm_filename}")
            print(f"Using file field: '{file_field_name}'")

            try:
                with open(file_path, 'rb') as f:
                    # Upload with RomM-style filename
                    files = {file_field_name: (romm_filename, f, 'application/octet-stream')}
                    
                    response = self.session.post(
                        upload_url,
                        files=files,
                        timeout=60
                    )
                
                print(f"Response: {response.status_code}")
                
                if response.status_code in [200, 201]:
                    print(f"🎉 SUCCESS! Upload with RomM naming convention!")
                    
                    try:
                        response_data = response.json()
                        
                        # Extract useful info from response
                        if isinstance(response_data, dict):
                            file_id = response_data.get('id', 'unknown')
                            server_filename = response_data.get('file_name', 'unknown')
                            created_at = response_data.get('created_at', 'unknown')
                            download_path = response_data.get('download_path', 'unknown')
                            
                            print(f"✅ Upload successful!")
                            print(f"   File ID: {file_id}")
                            print(f"   Server filename: {server_filename}")
                            print(f"   Created: {created_at}")
                            
                            # Verify the filename was accepted
                            if server_filename == romm_filename:
                                print(f"   ✅ Filename matches RomM convention!")
                            else:
                                print(f"   ⚠️ Server used different filename: {server_filename}")
                            
                            return True
                            
                    except Exception as parse_error:
                        print(f"Response text: {response.text[:200]}")
                        return True
                    
                elif response.status_code == 422:
                    print(f"Validation error (422):")
                    try:
                        error_data = response.json()
                        print(f"  Error details: {error_data}")
                    except:
                        print(f"  Raw error: {response.text[:300]}")
                        
                elif response.status_code == 400:
                    error_text = response.text
                    print(f"Bad request (400): {error_text}")
                        
                else:
                    print(f"Unexpected status {response.status_code}: {response.text[:200]}")
                    
            except Exception as e:
                print(f"Upload exception: {e}")
                
            print(f"❌ Upload failed")
            return False
                
        except Exception as e:
            print(f"Error in upload_save: {e}")
            return False
            
    def upload_save_with_thumbnail(self, rom_id, save_type, file_path, thumbnail_path=None, emulator=None):
        """NEW METHOD: Upload save file with optional thumbnail using separate linked uploads"""
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                print(f"Upload error: file not found at {file_path}")
                return False

            file_size = file_path.stat().st_size
            print(f"🚀 NEW METHOD: Uploading {file_path.name} ({file_size} bytes) to ROM {rom_id} as {save_type}")
            
            if thumbnail_path and thumbnail_path.exists():
                thumbnail_size = thumbnail_path.stat().st_size
                print(f"🖼️ With screenshot: {thumbnail_path.name} ({thumbnail_size} bytes)")
            
            # Step 1: Upload the save state file first and get its ID AND server filename
            print(f"📤 Step 1: Uploading save state file...")
            save_state_id, server_filename = self.upload_save_and_get_id(rom_id, save_type, file_path, emulator)
            
            if not save_state_id:
                print(f"❌ Failed to upload save file or get save state ID, falling back to old method")
                # Fallback to old method if new method fails
                return self.upload_save(rom_id, save_type, file_path)
            
            print(f"✅ Step 1 complete: Save file uploaded with ID: {save_state_id}")
            print(f"📄 Server filename: {server_filename}")
            
            # Step 2: Upload thumbnail and link it to the save state using MATCHING timestamp
            if thumbnail_path and thumbnail_path.exists():
                print(f"📸 Step 2: Uploading screenshot with matching timestamp...")
                
                screenshot_success = self.upload_screenshot_with_matching_timestamp(
                    rom_id, save_state_id, save_type, server_filename, thumbnail_path
                )
                
                if screenshot_success:
                    print(f"🎉 SUCCESS: Save file and screenshot uploaded and linked!")
                    return True
                else:
                    print(f"⚠️ Save file uploaded, but screenshot linking failed")
                    return True  # Still consider it successful since save file worked
            else:
                print(f"✅ Save file uploaded successfully (no screenshot to upload)")
                return True
                
        except Exception as e:
            print(f"Error in new upload method: {e}")
            print(f"Falling back to old method...")
            return self.upload_save(rom_id, save_type, file_path)

    def upload_screenshot_with_matching_timestamp(self, rom_id, save_state_id, save_type, save_state_filename, thumbnail_path):
        """Upload screenshot using the EXACT same timestamp as the save state"""
        try:
            # Extract timestamp from save state filename
            # Example: "Test (USA) [2025-07-03 02-24-20-692].state"
            import re
            
            # Find the timestamp pattern [YYYY-MM-DD HH-MM-SS-mmm]
            timestamp_match = re.search(r'\[([0-9\-\s:]+)\]', save_state_filename)
            if timestamp_match:
                timestamp = timestamp_match.group(1)
                print(f"🕐 Extracted timestamp from save state: {timestamp}")
            else:
                print(f"⚠️ Could not extract timestamp from: {save_state_filename}")
                # Fallback to generating new timestamp
                import datetime
                now = datetime.datetime.now()
                timestamp = now.strftime("%Y-%m-%d %H-%M-%S-%f")[:-3]
                print(f"🕐 Using fallback timestamp: {timestamp}")
            
            # Extract base name (remove timestamp and extension)
            base_name_match = re.match(r'^(.+?)\s*\[', save_state_filename)
            if base_name_match:
                base_name = base_name_match.group(1).strip()
            else:
                # Fallback: use stem of original filename
                base_name = Path(save_state_filename).stem
                base_name = re.sub(r'\s*\[.*?\]', '', base_name)  # Remove any existing timestamps
            
            # Create screenshot filename with MATCHING timestamp
            screenshot_filename = f"{base_name} [{timestamp}].png"
            
            print(f"📸 Screenshot filename with matching timestamp: {screenshot_filename}")
            print(f"🔗 Linking to save state ID: {save_state_id}")
            
            # Upload screenshot with the matching timestamp
            upload_url = urljoin(self.base_url, f'/api/screenshots?rom_id={rom_id}&state_id={save_state_id}')
            
            try:
                with open(thumbnail_path, 'rb') as thumb_f:
                    files = {'screenshotFile': (screenshot_filename, thumb_f.read(), 'image/png')}
                    
                    # Include comprehensive data
                    data = {
                        'rom_id': str(rom_id),
                        'state_id': str(save_state_id),
                        'filename': screenshot_filename,
                        'file_name': screenshot_filename,
                    }
                    
                    response = self.session.post(upload_url, files=files, data=data, timeout=30)
                    
                    print(f"📡 Screenshot upload response: {response.status_code}")
                    
                    if response.status_code in [200, 201]:
                        print(f"🎉 Screenshot with matching timestamp uploaded!")
                        
                        try:
                            response_data = response.json()
                            screenshot_id = response_data.get('id')
                            server_screenshot_filename = response_data.get('file_name')
                            
                            print(f"   Screenshot ID: {screenshot_id}")
                            print(f"   Server screenshot filename: {server_screenshot_filename}")
                            
                            # Verify the timestamps match
                            if timestamp in server_screenshot_filename:
                                print(f"✅ Timestamps match perfectly!")
                            else:
                                print(f"⚠️ Timestamp mismatch: expected {timestamp}")
                            
                            # Verify the link
                            print(f"🔍 Verifying screenshot link with matching timestamp...")
                            verification_success = self.verify_screenshot_link(save_state_id, screenshot_id, save_type)
                            if verification_success:
                                print(f"🎉 PERFECT! Screenshot linked successfully - should appear on RomM!")
                                return True
                            else:
                                print(f"🤔 Screenshot uploaded with matching timestamp but still not linked")
                                return False
                                
                        except Exception as parse_error:
                            print(f"   Response text: {response.text[:200]}")
                            return True  # Uploaded successfully even if we can't parse response
                    else:
                        print(f"❌ Screenshot upload failed: {response.text[:200]}")
                        return False
                        
            except Exception as upload_error:
                print(f"❌ Screenshot upload error: {upload_error}")
                return False
                
        except Exception as e:
            print(f"Error in matching timestamp upload: {e}")
            return False
                
        except Exception as e:
            print(f"Error in new upload method: {e}")
            print(f"Falling back to old method...")
            return self.upload_save(rom_id, save_type, file_path)

    def upload_save_and_get_id(self, rom_id, save_type, file_path, emulator=None):
        """Upload save file using existing server filename for saves, new timestamp for states"""
        try:
            file_path = Path(file_path)
            
            # Build endpoint with emulator if provided
            if emulator:
                endpoint = f'/api/{save_type}?rom_id={rom_id}&emulator={emulator}'
            else:
                endpoint = f'/api/{save_type}?rom_id={rom_id}'
            
            upload_url = urljoin(self.base_url, endpoint)
            
            # Use correct field names
            if save_type == 'states':
                file_field_name = 'stateFile'
            elif save_type == 'saves':
                file_field_name = 'saveFile'
            else:
                return None, None

            original_basename = file_path.stem
            file_extension = file_path.suffix
            
            if save_type == 'saves':
                # REUSE EXISTING SERVER FILENAME FOR SAVES
                existing_filename = self.get_existing_save_filename(rom_id, save_type)
                if existing_filename:
                    romm_filename = existing_filename  # Use exact server filename
                    print(f"♻️ Reusing server filename: {romm_filename}")
                else:
                    # No existing save, create new with timestamp
                    import datetime
                    now = datetime.datetime.now()
                    timestamp = now.strftime("%Y-%m-%d %H-%M-%S-%f")[:-3]
                    romm_filename = f"{original_basename} [{timestamp}]{file_extension}"
            else:
                # States: Always new timestamp
                import datetime
                now = datetime.datetime.now()
                timestamp = now.strftime("%Y-%m-%d %H-%M-%S-%f")[:-3]
                romm_filename = f"{original_basename} [{timestamp}]{file_extension}"
            
            with open(file_path, 'rb') as f:
                files = {file_field_name: (romm_filename, f.read(), 'application/octet-stream')}
                
                response = self.session.post(
                    upload_url,
                    files=files,
                    timeout=60
                )
                
                if response.status_code in [200, 201]:
                    try:
                        response_data = response.json()
                        save_state_id = response_data.get('id')
                        # Get the actual filename used by the server
                        server_filename = response_data.get('file_name', romm_filename)
                        if save_state_id:
                            return save_state_id, server_filename
                        else:
                            print(f"No ID in save upload response: {response_data}")
                            return None, None
                    except:
                        print(f"Could not parse save upload response: {response.text[:200]}")
                        return None, None
                else:
                    print(f"Save upload failed: {response.status_code} - {response.text[:200]}")
                    return None, None
                    
        except Exception as e:
            print(f"Error uploading save and getting ID: {e}")
            return None, None

    def get_existing_save_filename(self, rom_id, save_type):
        """Get filename of existing save/state on server"""
        try:
            response = self.session.get(urljoin(self.base_url, f'/api/roms/{rom_id}'), timeout=5)
            if response.status_code == 200:
                rom_data = response.json()
                files = rom_data.get(f'user_{save_type}', [])
                if files and isinstance(files, list):
                    # Return filename of most recent file
                    latest_file = max(files, key=lambda f: f.get('updated_at', ''), default=None)
                    if latest_file:
                        return latest_file.get('file_name')
        except:
            pass
        return None

    def upload_screenshot_for_save_state(self, rom_id, save_state_id, save_type, save_file_path, thumbnail_path):
        """Upload screenshot and link it to a specific save state"""
        try:
            # Generate matching filename with same timestamp pattern as save file
            original_basename = save_file_path.stem
            
            # Extract timestamp from the uploaded save file name or generate new one
            import datetime
            now = datetime.datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H-%M-%S-%f")[:-3]
            
            screenshot_filename = f"{original_basename} [{timestamp}].png"
            
            print(f"Screenshot filename: {screenshot_filename}")
            print(f"Linking to save state ID: {save_state_id}")
            
            # First, get the save state details to see the expected structure
            try:
                save_state_response = self.session.get(
                    urljoin(self.base_url, f'/api/states/{save_state_id}'),
                    timeout=10
                )
                if save_state_response.status_code == 200:
                    save_state_data = save_state_response.json()
                    print(f"📄 Save state structure: {list(save_state_data.keys())}")
                    # Check if there are any clues about how screenshots should be linked
                    if 'screenshot' in save_state_data:
                        print(f"🖼️ Screenshot field exists: {save_state_data.get('screenshot')}")
            except:
                pass
            
            # Try the approach that worked before, but with more debugging
            success = self.try_standard_screenshot_upload(rom_id, save_state_id, screenshot_filename, thumbnail_path)
            if success:
                return True
            
            # If that failed, try the direct file structure approach
            print("🔄 Trying direct file structure approach...")
            return self.try_direct_file_structure_upload(rom_id, save_state_id, screenshot_filename, thumbnail_path)
            
        except Exception as e:
            print(f"Error uploading screenshot for save state: {e}")
            return False
    
    def try_standard_screenshot_upload(self, rom_id, save_state_id, screenshot_filename, thumbnail_path):
        """Try the standard screenshot upload approach"""
        try:
            # Try screenshot upload endpoints with multiple field names
            screenshot_endpoints = [
                # Most promising: screenshot upload with state linking
                f'/api/screenshots?rom_id={rom_id}&state_id={save_state_id}',
                f'/api/screenshots?rom_id={rom_id}',
                f'/api/roms/{rom_id}/screenshots',
            ]
            
            # Multiple field names to try for the screenshot file
            field_names = ['screenshotFile', 'screenshot', 'file', 'image']
            
            for attempt, endpoint in enumerate(screenshot_endpoints):
                try:
                    upload_url = urljoin(self.base_url, endpoint)
                    print(f"  Screenshot attempt {attempt + 1}: {endpoint}")
                    
                    # Try different field names for this endpoint
                    for field_name in field_names:
                        try:
                            print(f"    Trying field name: '{field_name}'")
                            
                            with open(thumbnail_path, 'rb') as thumb_f:
                                files = {field_name: (screenshot_filename, thumb_f.read(), 'image/png')}
                                
                                # Include comprehensive linking data
                                data = {
                                    'rom_id': str(rom_id),
                                    'filename': screenshot_filename,
                                    'file_name': screenshot_filename,  # Alternative field name
                                }
                                
                                # Add save state linking info if this endpoint supports it
                                if 'state_id' in endpoint:
                                    data['state_id'] = str(save_state_id)
                                    data['states_id'] = str(save_state_id)  # Alternative field name
                                
                                response = self.session.post(
                                    upload_url,
                                    files=files,
                                    data=data,
                                    timeout=30
                                )
                                
                                print(f"      Response: {response.status_code}")
                                
                                if response.status_code in [200, 201]:
                                    print(f"🎉 Screenshot uploaded successfully!")
                                    print(f"   Endpoint: {endpoint}")
                                    print(f"   Field name: {field_name}")
                                    print(f"   Filename: {screenshot_filename}")
                                    
                                    try:
                                        response_data = response.json()
                                        screenshot_id = response_data.get('id')
                                        print(f"   Screenshot ID: {screenshot_id}")
                                        print(f"   Screenshot data: {response_data}")
                                        
                                        # Always verify the linking worked by checking the save state
                                        print(f"🔍 Verifying screenshot link...")
                                        verification_success = self.verify_screenshot_link(save_state_id, screenshot_id, 'states')
                                        if verification_success:
                                            print(f"✅ Screenshot link verified - should appear on RomM!")
                                            return True
                                        else:
                                            print(f"⚠️ Screenshot uploaded but link verification failed")
                                            # Try explicit linking as backup
                                            print(f"🔧 Attempting explicit linking...")
                                            explicit_link = self.link_screenshot_to_save_state(save_state_id, screenshot_id, 'states')
                                            if explicit_link:
                                                print(f"✅ Explicit linking successful!")
                                                return True
                                            else:
                                                print(f"❌ Explicit linking also failed")
                                                # Continue trying other methods rather than return False
                                        
                                    except Exception as parse_error:
                                        print(f"   Response text: {response.text[:200]}")
                                    
                                    # Even if linking failed, screenshot was uploaded, so continue to try other approaches
                                    break  # Break from field names to try next endpoint
                                    
                                elif response.status_code == 400:
                                    error_text = response.text[:200]
                                    print(f"      400 Error with '{field_name}': {error_text}")
                                    
                                    # If we still get "No screenshot file provided", continue to next field
                                    if "No screenshot file provided" in error_text:
                                        continue
                                    else:
                                        # Different error, might be validation issue
                                        continue
                                        
                                elif response.status_code == 404:
                                    # Endpoint doesn't exist, try next endpoint
                                    print(f"      404 - Endpoint not found")
                                    break  # Break from field names, try next endpoint
                                    
                                else:
                                    print(f"      Unexpected {response.status_code}: {response.text[:100]}")
                                    continue
                                    
                        except Exception as field_error:
                            print(f"    Field '{field_name}' error: {field_error}")
                            continue
                            
                except Exception as endpoint_error:
                    print(f"  Endpoint error: {endpoint_error}")
                    continue
            
            return False
            
        except Exception as e:
            print(f"Error in standard screenshot upload: {e}")
            return False
    
    def try_direct_file_structure_upload(self, rom_id, save_state_id, screenshot_filename, thumbnail_path):
        """Try uploading using the direct file structure approach that RomM expects"""
        try:
            print("📁 Attempting direct file structure upload...")
            
            # Get ROM details to determine platform and user structure
            rom_response = self.session.get(urljoin(self.base_url, f'/api/roms/{rom_id}'), timeout=10)
            if rom_response.status_code != 200:
                print("Could not get ROM details")
                return False
            
            rom_data = rom_response.json()
            platform_slug = rom_data.get('platform_slug', 'unknown')
            print(f"Platform: {platform_slug}")
            
            # Try specialized screenshot endpoints that might handle the file structure
            specialized_endpoints = [
                # Try endpoints that might automatically handle the file path structure
                f'/api/raw/assets/screenshots?rom_id={rom_id}&platform={platform_slug}&state_id={save_state_id}',
                f'/api/assets/screenshots?rom_id={rom_id}&platform={platform_slug}&state_id={save_state_id}',
                f'/api/upload/screenshot?rom_id={rom_id}&platform={platform_slug}&state_id={save_state_id}',
                f'/api/screenshots/upload?rom_id={rom_id}&platform={platform_slug}&state_id={save_state_id}',
            ]
            
            for endpoint in specialized_endpoints:
                try:
                    upload_url = urljoin(self.base_url, endpoint)
                    print(f"  Trying specialized endpoint: {endpoint}")
                    
                    with open(thumbnail_path, 'rb') as thumb_f:
                        files = {'screenshotFile': (screenshot_filename, thumb_f.read(), 'image/png')}
                        data = {
                            'rom_id': str(rom_id),
                            'state_id': str(save_state_id),
                            'platform': platform_slug,
                            'filename': screenshot_filename,
                        }
                        
                        response = self.session.post(upload_url, files=files, data=data, timeout=30)
                        print(f"    Response: {response.status_code}")
                        
                        if response.status_code in [200, 201]:
                            print(f"🎉 Specialized upload successful!")
                            try:
                                response_data = response.json()
                                screenshot_id = response_data.get('id')
                                if screenshot_id:
                                    # Verify this approach worked
                                    if self.verify_screenshot_link(save_state_id, screenshot_id, 'states'):
                                        print(f"✅ Specialized upload and link verified!")
                                        return True
                            except:
                                pass
                            return True
                        else:
                            print(f"    Failed: {response.text[:100]}")
                            
                except Exception as e:
                    print(f"  Specialized endpoint error: {e}")
                    continue
            
            print("❌ All specialized upload attempts failed")
            return False
            
        except Exception as e:
            print(f"Error in direct file structure upload: {e}")
            return False

    def upload_screenshot_separately_then_link(self, rom_id, save_state_id, save_type, screenshot_filename, thumbnail_path):
        """Upload screenshot separately, then try to link it to the save state"""
        try:
            print("📸 Attempting separate screenshot upload...")
            
            # Simple screenshot upload without state linking
            upload_url = urljoin(self.base_url, f'/api/screenshots?rom_id={rom_id}')
            
            # Try the most likely field names
            for field_name in ['screenshot', 'file', 'image']:
                try:
                    print(f"  Trying separate upload with field '{field_name}'")
                    
                    with open(thumbnail_path, 'rb') as thumb_f:
                        files = {field_name: (screenshot_filename, thumb_f.read(), 'image/png')}
                        data = {'rom_id': str(rom_id), 'filename': screenshot_filename}
                        
                        response = self.session.post(upload_url, files=files, data=data, timeout=30)
                        
                        if response.status_code in [200, 201]:
                            try:
                                response_data = response.json()
                                screenshot_id = response_data.get('id')
                                
                                if screenshot_id:
                                    print(f"✅ Screenshot uploaded separately! ID: {screenshot_id}")
                                    # Now try to link it
                                    link_success = self.link_screenshot_to_save_state(save_state_id, screenshot_id, save_type)
                                    return link_success
                                    
                            except:
                                print(f"Could not parse screenshot upload response")
                                return False
                                
                except Exception as e:
                    print(f"  Error with field '{field_name}': {e}")
                    continue
            
            print("❌ Separate screenshot upload also failed")
            return False
            
        except Exception as e:
            print(f"Error in separate screenshot upload: {e}")
            return False

    def verify_screenshot_link(self, save_state_id, screenshot_id, save_type):
        """Verify that the screenshot is properly linked to the save state"""
        try:
            print(f"Checking if save state {save_state_id} has screenshot {screenshot_id} linked...")
            
            # Get the save state data to check if screenshot is linked
            response = self.session.get(
                urljoin(self.base_url, f'/api/{save_type}/{save_state_id}'),
                timeout=10
            )
            
            if response.status_code == 200:
                save_state_data = response.json()
                screenshot_data = save_state_data.get('screenshot')
                
                if screenshot_data:
                    linked_screenshot_id = screenshot_data.get('id')
                    if linked_screenshot_id == screenshot_id:
                        print(f"✅ Screenshot {screenshot_id} is properly linked!")
                        return True
                    else:
                        print(f"❌ Wrong screenshot linked: expected {screenshot_id}, got {linked_screenshot_id}")
                        return False
                else:
                    print(f"❌ No screenshot linked to save state {save_state_id}")
                    return False
            else:
                print(f"Could not verify link: HTTP {response.status_code}")
                return False
                
        except Exception as e:
            print(f"Error verifying screenshot link: {e}")
            return False

    def link_screenshot_to_save_state(self, save_state_id, screenshot_id, save_type):
        """Link an uploaded screenshot to a save state using multiple methods"""
        try:
            print(f"Linking screenshot {screenshot_id} to {save_type} {save_state_id}")
            
            # Try different linking methods
            link_methods = [
                # Method 1: PATCH the save state with screenshot_id
                {
                    'method': 'PATCH',
                    'url': f'/api/{save_type}/{save_state_id}',
                    'data': {'screenshot_id': screenshot_id}
                },
                # Method 2: PUT the save state with screenshot_id
                {
                    'method': 'PUT', 
                    'url': f'/api/{save_type}/{save_state_id}',
                    'data': {'screenshot_id': screenshot_id}
                },
                # Method 3: POST to a screenshot link endpoint
                {
                    'method': 'POST',
                    'url': f'/api/{save_type}/{save_state_id}/screenshot',
                    'data': {'screenshot_id': screenshot_id}
                },
                # Method 4: Update screenshot with state reference
                {
                    'method': 'PATCH',
                    'url': f'/api/screenshots/{screenshot_id}',
                    'data': {f'{save_type[:-1]}_id': save_state_id, 'rom_id': 37}
                },
            ]
            
            for i, method_info in enumerate(link_methods):
                try:
                    print(f"  Link attempt {i+1}: {method_info['method']} {method_info['url']}")
                    
                    link_url = urljoin(self.base_url, method_info['url'])
                    
                    if method_info['method'] == 'PATCH':
                        response = self.session.patch(link_url, json=method_info['data'], timeout=10)
                    elif method_info['method'] == 'PUT':
                        response = self.session.put(link_url, json=method_info['data'], timeout=10)
                    else:  # POST
                        response = self.session.post(link_url, json=method_info['data'], timeout=10)
                    
                    print(f"    Response: {response.status_code}")
                    
                    if response.status_code in [200, 201, 204]:
                        print(f"✅ Linking successful with method {i+1}!")
                        # Verify the link worked
                        if self.verify_screenshot_link(save_state_id, screenshot_id, save_type):
                            return True
                        else:
                            print(f"⚠️ Link reported success but verification failed")
                            continue
                    else:
                        error_text = response.text[:200] if response.text else "No error details"
                        print(f"    Failed: {error_text}")
                        continue
                        
                except Exception as e:
                    print(f"    Exception: {e}")
                    continue
            
            print(f"❌ All linking methods failed")
            return False
            
        except Exception as e:
            print(f"Error linking screenshot to save state: {e}")
            return False

class RetroArchInterface:
    """Interface for RetroArch network commands and file monitoring"""
    
    def __init__(self):
        self.save_dirs = self.find_retroarch_dirs()
        self.cores_dir = self.find_cores_directory()
        self.retroarch_executable = self.find_retroarch_executable()
        self.thumbnails_dir = self.find_thumbnails_directory()

        self.host = '127.0.0.1'
        self.port = 55355
        print(f"🔧 RetroArch network settings: {self.host}:{self.port}")

        # Platform to core mapping
        self.platform_core_map = {
            'Super Nintendo Entertainment System': ['snes9x', 'bsnes', 'mesen-s'],
            'PlayStation': ['beetle_psx', 'beetle_psx_hw', 'pcsx_rearmed', 'swanstation'],
            'Nintendo Entertainment System': ['nestopia', 'fceumm', 'mesen'],
            'Game Boy': ['gambatte', 'sameboy', 'tgbdual'],
            'Game Boy Color': ['gambatte', 'sameboy', 'tgbdual'],
            'Game Boy Advance': ['mgba', 'vba_next', 'vbam'],
            'Sega Genesis': ['genesis_plus_gx', 'blastem', 'picodrive'],
            'Nintendo 64': ['mupen64plus_next', 'parallel_n64'],
            'Sega Saturn': ['beetle_saturn', 'kronos'],
            'Arcade': ['mame', 'fbneo', 'fbalpha'],
            'PlayStation 2': ['pcsx2', 'play'],
            'Nintendo GameCube': ['dolphin'],
            'Sega Dreamcast': ['flycast', 'redream'],
            'Atari 2600': ['stella'],
        }
        
        # Mapping from RomM emulator names to RetroArch save directory names
        self.emulator_directory_map = {
            # SNES cores
            'snes9x': 'Snes9x',
            'bsnes': 'bsnes',
            'mesen-s': 'Mesen-S',
            
            # NES cores
            'nestopia': 'Nestopia',
            'fceumm': 'FCEUmm',
            'mesen': 'Mesen',
            
            # PlayStation cores
            'beetle_psx': 'Beetle PSX',
            'beetle_psx_hw': 'Beetle PSX HW',
            'pcsx_rearmed': 'PCSX-ReARMed',
            'swanstation': 'SwanStation',
            'mednafen_psx': 'Beetle PSX',
            'mednafen_psx_hw': 'Beetle PSX HW',
            
            # Game Boy cores
            'gambatte': 'Gambatte',
            'sameboy': 'SameBoy',
            'tgbdual': 'TGB Dual',
            'mgba': 'mGBA',
            'vba_next': 'VBA Next',
            'vbam': 'VBA-M',
            
            # Genesis/Mega Drive cores
            'genesis_plus_gx': 'Genesis Plus GX',
            'blastem': 'BlastEm',
            'picodrive': 'PicoDrive',
            
            # Nintendo 64 cores
            'mupen64plus_next': 'Mupen64Plus-Next',
            'parallel_n64': 'ParaLLEl N64',
            
            # Saturn cores
            'beetle_saturn': 'Beetle Saturn',
            'kronos': 'Kronos',
            'mednafen_saturn': 'Beetle Saturn',
            
            # Arcade cores
            'mame': 'MAME',
            'fbneo': 'FBNeo',
            'fbalpha': 'FB Alpha',
            
            # PlayStation 2 cores
            'pcsx2': 'PCSX2',
            'play': 'Play!',
            
            # GameCube cores
            'dolphin': 'Dolphin',
            
            # Dreamcast cores
            'flycast': 'Flycast',
            'redream': 'Redream',
            
            # Atari cores
            'stella': 'Stella',
            
            # PC Engine cores
            'beetle_pce': 'Beetle PCE',
            'beetle_pce_fast': 'Beetle PCE Fast',
            'mednafen_pce': 'Beetle PCE',
            'mednafen_pce_fast': 'Beetle PCE Fast',
            
            # Neo Geo cores
            'fbneo': 'FBNeo',
            
            # Additional common cores
            'dosbox_pure': 'DOSBox-Pure',
            'scummvm': 'ScummVM',
            'ppsspp': 'PPSSPP',
            'desmume': 'DeSmuME',
            'melonds': 'melonDS',
            'citra': 'Citra',
            'dolphin': 'Dolphin',
            'flycast': 'Flycast',
        }

    def find_retroarch_executable(self):
        """Find RetroArch executable"""
        possible_paths = [
            '/usr/bin/retroarch',
            '/usr/local/bin/retroarch',
            '/opt/retroarch/bin/retroarch',
            '/snap/bin/retroarch',
            'retroarch'  # In PATH
        ]
        
        # Check Flatpak
        try:
            import subprocess
            result = subprocess.run(['flatpak', 'list'], capture_output=True, text=True)
            if 'org.libretro.RetroArch' in result.stdout:
                return 'flatpak run org.libretro.RetroArch'
        except:
            pass
        
        # Check regular paths
        import shutil
        for path in possible_paths:
            if shutil.which(path):
                return path
        
        return None
    
    def find_cores_directory(self):
        """Find RetroArch cores directory"""
        possible_dirs = [
            Path.home() / '.var/app/org.libretro.RetroArch/config/retroarch/cores',
            Path.home() / '.config/retroarch/cores',
            Path('/usr/lib/libretro'),
            Path('/usr/local/lib/libretro'),
            Path('/usr/lib/x86_64-linux-gnu/libretro'),
        ]
        
        for cores_dir in possible_dirs:
            if cores_dir.exists() and any(cores_dir.glob('*.so')):
                return cores_dir
        
        return None
    
    def get_available_cores(self):
        """Get list of available RetroArch cores"""
        if not self.cores_dir:
            return {}
        
        cores = {}
        for core_file in self.cores_dir.glob('*.so'):
            # Remove _libretro.so suffix to get core name
            core_name = core_file.stem.replace('_libretro', '')
            cores[core_name] = str(core_file)
        
        return cores
    
    def suggest_core_for_platform(self, platform_name):
        """Suggest best core for a platform"""
        available_cores = self.get_available_cores()
        suggested_cores = self.platform_core_map.get(platform_name, [])
        
        # Find first available suggested core
        for core in suggested_cores:
            if core in available_cores:
                return core, available_cores[core]
        
        # If no suggested core found, return any available core that might work
        platform_lower = platform_name.lower()
        for core_name in available_cores:
            if any(keyword in core_name.lower() for keyword in ['snes', 'nes', 'psx', 'genesis', 'gameboy', 'gba']):
                if any(keyword in platform_lower for keyword in ['nintendo', 'snes', 'nes', 'playstation', 'psx', 'sega', 'game boy', 'gba']):
                    return core_name, available_cores[core_name]
        
        return None, None

    def send_notification(self, message):
        """Send notification to RetroArch using SHOW_MSG command"""
        try:
            # Use SHOW_MSG instead of NOTIFICATION
            command = f'SHOW_MSG "{message}"'
            print(f"🔔 Sending RetroArch notification: {message}")
            
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            
            message_bytes = command.encode('utf-8')
            sock.sendto(message_bytes, (self.host, self.port))
            sock.close()
            
            print(f"✅ RetroArch notification sent successfully")
            return True
            
        except Exception as e:
            print(f"❌ Failed to send RetroArch notification: {e}")
            return False
        
    def launch_game(self, rom_path, platform_name=None, core_name=None):
        """Launch a game in RetroArch"""
        if not self.retroarch_executable:
            return False, "RetroArch executable not found"
        
        # If no core specified, try to suggest one
        if not core_name and platform_name:
            core_name, core_path = self.suggest_core_for_platform(platform_name)
            if not core_name:
                return False, f"No suitable core found for platform: {platform_name}"
        
        # Get core path
        available_cores = self.get_available_cores()
        if core_name not in available_cores:
            return False, f"Core not found: {core_name}"
        
        core_path = available_cores[core_name]
        
        try:
            import subprocess
            
            # Build RetroArch command
            cmd = [self.retroarch_executable, '-L', core_path, str(rom_path)]
            
            # Handle Flatpak case
            if 'flatpak' in self.retroarch_executable:
                cmd = ['flatpak', 'run', 'org.libretro.RetroArch', '-L', core_path, str(rom_path)]
            
            print(f"Launching RetroArch: {' '.join(cmd)}")
            
            # Launch RetroArch
            subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            
            return True, f"Launched {rom_path.name} with {core_name} core"
            
        except Exception as e:
            return False, f"Launch error: {e}"
    
    def find_retroarch_dirs(self):
        """Find RetroArch save directories"""
        # Common RetroArch locations
        possible_dirs = [
            Path.home() / '.var/app/org.libretro.RetroArch/config/retroarch',
            Path.home() / '.config/retroarch',
            Path('/var/lib/flatpak/app/org.libretro.RetroArch'),
            Path.home() / '.local/share/applications'
        ]
        
        save_dirs = {}
        for base_dir in possible_dirs:
            if base_dir.exists():
                saves_dir = base_dir / 'saves'
                states_dir = base_dir / 'states'
                
                if saves_dir.exists():
                    save_dirs['saves'] = saves_dir
                if states_dir.exists():
                    save_dirs['states'] = states_dir
                    
                if save_dirs:
                    break
        
        return save_dirs
    
    def send_command(self, command):
        """Send UDP command to RetroArch"""
        try:
            print(f"🌐 Connecting to RetroArch at {self.host}:{self.port}")
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(2.0)
            
            message = command.encode('utf-8')
            print(f"📤 Sending command: {command}")
            bytes_sent = sock.sendto(message, (self.host, self.port))
            print(f"📊 Sent {bytes_sent} bytes")
            
            # Don't wait for response on SHOW_MSG commands
            if command.startswith('SHOW_MSG'):
                print(f"📢 Notification sent (no response expected)")
                sock.close()
                return "OK"
            
            # Try to receive response
            try:
                response, addr = sock.recvfrom(1024)
                response_text = response.decode('utf-8').strip()
                print(f"📨 Received: '{response_text}' from {addr}")
                return response_text
            except socket.timeout:
                print(f"⏰ Timeout - no response received")
                return None
            except Exception as recv_e:
                print(f"❌ Receive error: {recv_e}")
                return None
            finally:
                sock.close()
                
        except Exception as e:
            print(f"❌ Socket error: {e}")
            return None

    def get_selected_game(self):
        if hasattr(self, 'library_section'):
            return self.library_section.selected_game
        return None

    def get_status(self):
        """Get RetroArch status"""
        return self.send_command("GET_STATUS")

    def get_retroarch_directory_name(self, romm_emulator_name):
        """Convert RomM emulator name to RetroArch save directory name"""
        if not romm_emulator_name:
            return None
        
        # Direct mapping
        mapped_name = self.emulator_directory_map.get(romm_emulator_name.lower())
        if mapped_name:
            return mapped_name
        
        # Fallback: try some common patterns
        fallback_patterns = {
            'beetle_': 'Beetle ',
            'mednafen_': 'Beetle ',
            '_libretro': '',
            '_': ' ',
        }
        
        fallback_name = romm_emulator_name
        for pattern, replacement in fallback_patterns.items():
            fallback_name = fallback_name.replace(pattern, replacement)
        
        # Capitalize first letter of each word
        fallback_name = ' '.join(word.capitalize() for word in fallback_name.split())
        
        return fallback_name

    def convert_to_retroarch_filename(self, original_filename, save_type, target_directory):
        """
        Convert RomM filename with timestamp to RetroArch expected format
        """
        import re
        from pathlib import Path
        
        # Extract the base filename by removing timestamp brackets
        # Pattern matches: [YYYY-MM-DD HH-MM-SS-mmm] or similar timestamp formats
        timestamp_pattern = r'\s*\[[\d\-\s:]+\]'
        base_name = re.sub(timestamp_pattern, '', Path(original_filename).stem)
        
        # Get the original extension
        original_ext = Path(original_filename).suffix.lower()
        
        if save_type == 'saves':
            # For save files, keep the extension (.srm, .sav, etc.)
            if original_ext in ['.srm', '.sav']:
                target_filename = f"{base_name}{original_ext}"
            else:
                # Default to .srm if unknown save extension
                target_filename = f"{base_name}.srm"
        
        elif save_type == 'states':
            # For save states, we need to determine the slot
            target_filename = self.determine_state_filename(base_name, target_directory)
        
        else:
            # Unknown save type, keep original
            target_filename = original_filename
        
        return target_filename

    def determine_state_filename(self, base_name, target_directory):
        """
        Determine the appropriate state filename based on existing files
        
        RetroArch save state priority:
        1. .state (auto/quick save) - most commonly used
        2. .state1, .state2, etc. (manual save slots)
        """
        target_dir = Path(target_directory)
        
        # Check what state files already exist for this game
        existing_states = []
        if target_dir.exists():
            # Look for existing state files for this game
            patterns = [
                f"{base_name}.state",
                f"{base_name}.state1", 
                f"{base_name}.state2",
                f"{base_name}.state3",
                f"{base_name}.state4",
                f"{base_name}.state5",
                f"{base_name}.state6",
                f"{base_name}.state7",
                f"{base_name}.state8",
                f"{base_name}.state9"
            ]
            
            for pattern in patterns:
                state_file = target_dir / pattern
                if state_file.exists():
                    existing_states.append(pattern)
        
        # Decision logic for state filename
        auto_state = f"{base_name}.state"
        
        if not existing_states:
            # No existing states, use auto state (.state)
            return auto_state
        else:
            # States exist, we have a few options:
            # Option 1: Always overwrite auto state (most common usage)
            # Option 2: Find next available slot
            # 
            # For now, let's use Option 1 (overwrite auto state) since it's most commonly used
            # Users typically want their latest state to be the quick save/load
            return auto_state
            
            # Uncomment below for Option 2 (find next available slot):
            # if auto_state.split('/')[-1] not in existing_states:
            #     return auto_state
            # else:
            #     # Find next available numbered slot
            #     for i in range(1, 10):
            #         slot_state = f"{base_name}.state{i}"
            #         if slot_state.split('/')[-1] not in existing_states:
            #             return slot_state
            #     # All slots taken, overwrite slot 1
            #     return f"{base_name}.state1"

    def get_retroarch_base_filename(self, rom_data):
        """
        Get the base filename that RetroArch would use for saves/states
        This should match the ROM filename without extension
        """
        # Try to get the clean filename from ROM data
        if rom_data and isinstance(rom_data, dict):
            # First try fs_name_no_ext (filename without extension, no tags)
            base_name = rom_data.get('fs_name_no_ext')
            if base_name:
                return base_name
            
            # Fallback to fs_name without extension
            fs_name = rom_data.get('fs_name')
            if fs_name:
                return Path(fs_name).stem
            
            # Fallback to name field
            name = rom_data.get('name')
            if name:
                return name
        
        return None

    def get_save_files(self):
        """Get list of save files in RetroArch directories, including emulator subdirectories"""
        save_files = {}
        
        # Define common save and state extensions
        save_extensions = {'.srm', '.sav'}
        state_extensions = {'.state', '.state1', '.state2', '.state3', '.state4', '.state5', '.state6', '.state7', '.state8', '.state9'}
        
        for save_type, directory in self.save_dirs.items():
            if directory.exists():
                files = []
                
                # Scan both root directory and emulator subdirectories
                directories_to_scan = [directory]
                
                # Add all subdirectories (emulator cores)
                for subdir in directory.iterdir():
                    if subdir.is_dir():
                        directories_to_scan.append(subdir)
                
                for scan_dir in directories_to_scan:
                    for file_path in scan_dir.glob('*'):
                        if file_path.is_file():
                            # Determine emulator from directory structure
                            if file_path.parent == directory:
                                emulator_dir = None  # Root directory
                                retroarch_emulator = None
                            else:
                                emulator_dir = file_path.parent.name  # Subdirectory name
                                retroarch_emulator = emulator_dir  # This is already the RetroArch name
                            
                            # Check file extension
                            if save_type == 'saves' and file_path.suffix.lower() in save_extensions:
                                files.append({
                                    'name': file_path.name,
                                    'path': str(file_path),
                                    'modified': file_path.stat().st_mtime,
                                    'emulator_dir': emulator_dir,
                                    'retroarch_emulator': retroarch_emulator,
                                    'relative_path': str(file_path.relative_to(directory))
                                })
                            elif save_type == 'states' and file_path.suffix.lower() in state_extensions:
                                files.append({
                                    'name': file_path.name,
                                    'path': str(file_path),
                                    'modified': file_path.stat().st_mtime,
                                    'emulator_dir': emulator_dir,
                                    'retroarch_emulator': retroarch_emulator,
                                    'relative_path': str(file_path.relative_to(directory))
                                })

                save_files[save_type] = files
        
        return save_files
    
    def find_thumbnails_directory(self):
        """Find RetroArch thumbnails directory"""
        possible_dirs = [
            Path.home() / '.var/app/org.libretro.RetroArch/config/retroarch/thumbnails',
            Path.home() / '.config/retroarch/thumbnails',
            Path.home() / '.var/app/org.libretro.RetroArch/config/retroarch/states/thumbnails',
        ]
        
        for thumbnails_dir in possible_dirs:
            if thumbnails_dir.exists():
                return thumbnails_dir
        
        return None

    def find_thumbnail_for_save_state(self, state_file_path):
        """Find the thumbnail file corresponding to a save state"""
        state_path = Path(state_file_path)
        thumbnails_dir = self.find_thumbnails_directory()
        
        # RetroArch thumbnail naming patterns
        base_name = state_path.stem  # Remove .state extension
        game_name = base_name
        
        # Remove state slot numbers (.state1, .state2, etc.)
        import re
        game_name = re.sub(r'\.state\d*$', '', game_name)
        
        # Possible thumbnail locations (prioritize same directory first)
        possible_thumbnails = [
            # MOST COMMON: Same directory as save state, appending .png to full filename
            state_path.with_name(state_path.name + '.png'),  # "game.state" -> "game.state.png"
            
            # Same directory, replacing extension
            state_path.with_suffix('.png'),  # "game.state" -> "game.png"
            
            # Same directory with game name variations
            state_path.parent / f"{game_name}.png",
            state_path.parent / f"{base_name}.png",
        ]
        
        # Add RetroArch thumbnails directory paths if available
        if thumbnails_dir:
            possible_thumbnails.extend([
                # Direct thumbnail in thumbnails root
                thumbnails_dir / f"{game_name}.png",
                thumbnails_dir / f"{base_name}.png",
                
                # In core-specific subdirectories
                thumbnails_dir / "savestate_thumbnails" / f"{game_name}.png",
                thumbnails_dir / "savestate_thumbnails" / f"{base_name}.png",
                
                # Boxart/screenshot folders (if RetroArch uses these for states)
                thumbnails_dir / "Named_Boxarts" / f"{game_name}.png",
                thumbnails_dir / "Named_Snaps" / f"{game_name}.png",
            ])
        
        # Find first existing thumbnail with debug logging
        for i, thumbnail_path in enumerate(possible_thumbnails):
            if thumbnail_path.exists():
                file_size = thumbnail_path.stat().st_size
                if file_size > 0:
                    print(f"🖼️ Found thumbnail (option {i+1}): {thumbnail_path} ({file_size} bytes)")
                    return thumbnail_path
                else:
                    print(f"⚠️ Found empty thumbnail file: {thumbnail_path}")
            else:
                # Debug: Show first few failed attempts
                if i < 3:
                    print(f"🔍 Thumbnail not found: {thumbnail_path}")
        
        print(f"❌ No thumbnail found for {state_path.name}")
        return None

class SyncWindow(Gtk.ApplicationWindow):
    """Main application window"""
    
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        
        # Set window icon directly
        import os
        script_dir = os.path.dirname(os.path.abspath(__file__))
        custom_icon_path = os.path.join(script_dir, 'romm_icon.png')
        
        if os.path.exists(custom_icon_path):
            try:
                from gi.repository import GdkPixbuf
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(custom_icon_path)
                # Try different GTK4 methods
                if hasattr(self, 'set_icon'):
                    self.set_icon(pixbuf)
                elif hasattr(self, 'set_default_icon'):
                    self.set_default_icon(pixbuf)
                print(f"Set window icon: {custom_icon_path}")
            except Exception as e:
                print(f"Failed to set window icon: {e}")

        # Auto-integrate AppImage on first run
        self.integrate_appimage()

        # Set application identity FIRST
        self.set_application_identity()
        
        # Debug icon loading
        self.debug_icon_loading()
        
        self.romm_client = None

        self.retroarch = RetroArchInterface()
        self.settings = SettingsManager()

        self.game_cache = GameDataCache(self.settings)        
        
        # Progress tracking
        self.download_queue = []
        self.available_games = []  # Initialize games list
        
        self.download_progress = {}
        self._last_progress_update = {}  # rom_id -> timestamp
        self._progress_update_interval = 0.1  # Update UI every 100ms max

        self.setup_ui()
        self.connect('close-request', self.on_window_close_request)
        self.load_saved_settings()

        # Add about action
        about_action = Gio.SimpleAction.new("about", None)
        about_action.connect("activate", self.on_about)
        self.add_action(about_action)

        # Initialize log view early so log_message() works from the start
        self.log_view = Gtk.TextView()
        self.log_view.set_editable(False)
        self.log_view.set_cursor_visible(False)

        # Add logs action (ADD THIS)
        logs_action = Gio.SimpleAction.new("logs", None)
        logs_action.connect("activate", lambda action, param: self.on_show_logs_dialog(None))
        self.add_action(logs_action)

        self.tray = TrayIcon(self.get_application(), self)

        self._pending_refresh = False
        
        # Initialize RetroArch info and attempt to load games list
        try:
            self.refresh_retroarch_info()
            # Try to refresh games list (will show local games if not connected to RomM)
            self.refresh_games_list()
        except Exception as e:
            print(f"Initial setup error: {e}")

        # Initialize auto-sync (add after other initializations)
        self.auto_sync = AutoSyncManager(
            romm_client=None,  # Will be set when connected
            retroarch=self.retroarch,
            settings=self.settings,
            log_callback=self.log_message,
            get_games_callback=lambda: getattr(self, 'available_games', []),
            parent_window=self
        )

        # ADD AUTO-CONNECT LOGIC:
        GLib.timeout_add(50, self.try_auto_connect)

    def try_auto_connect(self):
        """Try to auto-connect if enabled"""
        auto_connect_enabled = self.settings.get('RomM', 'auto_connect')
        remember_enabled = self.settings.get('RomM', 'remember_credentials')
        url = self.settings.get('RomM', 'url')
        username = self.settings.get('RomM', 'username')
        password = self.settings.get('RomM', 'password')
        
        self.log_message(f"🔍 Auto-connect check: auto={auto_connect_enabled}, remember={remember_enabled}")
        self.log_message(f"🔍 Credentials: url={bool(url)}, user={bool(username)}, pass={bool(password)}")
        
        if (auto_connect_enabled == 'true' and remember_enabled == 'true'):
            if url and username and password:
                self.log_message("🔄 Auto-connecting to RomM...")
                self.connection_enable_switch.set_active(True)
            else:
                self.log_message("⚠️ Auto-connect enabled but credentials incomplete")
        else:
            self.log_message("⚠️ Auto-connect or remember credentials disabled")
        
        return False

    def load_remaining_games(self, download_dir, total_games):
        """Load remaining games in background batches"""
        def load_in_background():
            batch_size = 1000
            current_games = list(self.available_games)  # Copy current list
            
            for offset in range(500, total_games, batch_size):
                try:
                    batch, _ = self.romm_client.get_roms(limit=batch_size, offset=offset)
                    if not batch:
                        break
                    
                    # Process this batch
                    new_games = self.process_rom_batch(batch, download_dir)
                    current_games.extend(new_games)
                    
                    # Update UI every batch
                    def update_ui(games_so_far=len(current_games), total=total_games):
                        self.available_games = current_games
                        if hasattr(self, 'library_section'):
                            self.library_section.update_games_library(current_games)
                        self.log_message(f"📚 Loaded {games_so_far}/{total} games...")
                    
                    GLib.idle_add(update_ui)
                    time.sleep(0.1)  # Small delay between batches
                    
                except Exception as e:
                    self.log_message(f"Background loading error: {e}")
                    break
            
            # Final update
            GLib.idle_add(lambda: self.log_message(f"✅ All {len(current_games)} games loaded!"))
            
            # Cache final result
            threading.Thread(target=lambda: self.game_cache.save_games_data(current_games), daemon=True).start()
        
        threading.Thread(target=load_in_background, daemon=True).start()

    def process_rom_batch(self, rom_batch, download_dir):
        """Process a batch of ROMs"""
        games = []
        for rom in rom_batch:
            rom_id = rom.get('id')
            platform = rom.get('platform_name', 'Unknown')
            file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
            
            platform_dir = download_dir / platform
            local_path = platform_dir / file_name
            is_downloaded = local_path.exists() and local_path.stat().st_size > 1024
            
            display_name = Path(file_name).stem if file_name else rom.get('name', 'Unknown')
            
            games.append({
                'name': display_name,
                'rom_id': rom_id,
                'platform': platform,
                'file_name': file_name,
                'is_downloaded': is_downloaded,
                'local_path': str(local_path) if is_downloaded else None,
                'local_size': local_path.stat().st_size if is_downloaded else 0,
                'romm_data': rom
            })
        
        return self.library_section.sort_games_consistently(games)

    def set_application_identity(self):
        """Set proper application identity for dock/taskbar"""
        try:
            # Set WM_CLASS to match desktop file
            import gi
            gi.require_version('Gdk', '4.0')
            from gi.repository import Gdk, GLib
            
            # Set application name first
            GLib.set_application_name("RomM - RetroArch Sync")
            
            # Get the surface and set WM_CLASS
            surface = self.get_surface()
            if surface and hasattr(surface, 'set_title'):
                surface.set_title("RomM - RetroArch Sync")
            
            # Set window class name
            self.set_title("RomM - RetroArch Sync")
            
            # Force the WM_CLASS for X11 systems
            display = self.get_display()
            if display and hasattr(display, 'get_name'):
                display_name = display.get_name() 
                if 'x11' in display_name.lower():
                    # For X11, we need to set the class hint
                    self.set_wmclass("romm-sync", "RomM - RetroArch Sync")
            
            print("✅ Set application identity")
            
        except Exception as e:
            print(f"❌ Failed to set application identity: {e}")

    def apply_delta_update(self, delta_items, download_dir, current_total, current_signature):
        """Apply delta changes with improved handling"""
        start_time = time.time()
        
        # Load existing games from cache
        existing_games = list(self.game_cache.cached_games)
        games_by_id = {g.get('rom_id'): g for g in existing_games if g.get('rom_id')}
        
        changes_applied = 0
        new_games_added = 0
        existing_games_updated = 0
        
        # Process delta items
        for delta_rom in delta_items:
            rom_id = delta_rom.get('id')
            if not rom_id:
                continue
                
            # Process the delta item
            updated_game = self.process_single_rom(delta_rom, download_dir)
            
            if rom_id in games_by_id:
                # Update existing game
                old_game = games_by_id[rom_id]
                games_by_id[rom_id] = updated_game
                
                # Replace in the main list
                for i, game in enumerate(existing_games):
                    if game.get('rom_id') == rom_id:
                        existing_games[i] = updated_game
                        break
                
                existing_games_updated += 1
            else:
                # New game - add it in the right position to maintain sort order
                existing_games.append(updated_game)
                games_by_id[rom_id] = updated_game
                new_games_added += 1
            
            changes_applied += 1
        
        # Re-sort to maintain consistency
        updated_games = self.library_section.sort_games_consistently(existing_games)
        
        # Update UI immediately
        def update_ui():
            self.available_games = updated_games
            if hasattr(self, 'library_section'):
                self.library_section.update_games_library(updated_games)
            GLib.idle_add(lambda: self.update_connection_ui("connected"))
            
            elapsed = time.time() - start_time
            change_summary = []
            if new_games_added > 0:
                change_summary.append(f"{new_games_added} new")
            if existing_games_updated > 0:
                change_summary.append(f"{existing_games_updated} updated")
            
            summary = ", ".join(change_summary) if change_summary else "processed"
            self.log_message(f"⚡ Delta sync: {summary} in {elapsed:.2f}s")
        
        GLib.idle_add(update_ui)
        
        # Save updated cache and metadata (pass the games for signature)
        content_hash = hash(str(len(updated_games)) + str(updated_games[0].get('rom_id', '') if updated_games else ''))
        
        # Background save
        threading.Thread(target=lambda: [
            self.game_cache.save_games_data(updated_games),
        ], daemon=True).start()

    def handle_offline_mode(self):
        """Handle when not connected to RomM - show only downloaded games"""
        download_dir = Path(self.rom_dir_row.get_text())
        
        self.log_message(f"🔍 DEBUG: Offline mode - download_dir: {download_dir}")
        self.log_message(f"🔍 DEBUG: Cache valid: {self.game_cache.is_cache_valid()}")
        self.log_message(f"🔍 DEBUG: Cached games count: {len(self.game_cache.cached_games)}")
        
        if self.game_cache.is_cache_valid():
            # Use cached data but FILTER to only show downloaded games
            cached_games = list(self.game_cache.cached_games)
            local_games = self.filter_to_downloaded_games_only(cached_games, download_dir)
            
            def update_ui():
                self.available_games = local_games
                if hasattr(self, 'library_section'):
                    self.library_section.update_games_library(local_games)
                self.update_connection_ui("disconnected")
                
                if local_games:
                    self.log_message(f"📂 Offline mode: {len(local_games)} downloaded games (from cache)")
                else:
                    self.log_message(f"📂 Offline mode: No downloaded games found")
            
            GLib.idle_add(update_ui)
        else:
            # No cache - scan local files only
            local_games = self.scan_local_games_only(download_dir)
            
            def update_ui():
                self.available_games = local_games
                if hasattr(self, 'library_section'):
                    self.library_section.update_games_library(local_games)
                self.update_connection_ui("disconnected")
                self.log_message(f"📂 Offline mode: {len(local_games)} local games found")
            
            GLib.idle_add(update_ui)

    def filter_to_downloaded_games_only(self, cached_games, download_dir):
        """Filter cached games to only show those that are actually downloaded"""
        downloaded_games = []
        
        for game in cached_games:
            # Use platform_slug instead of platform for directory name
            platform_slug = game.get('platform_slug') or game.get('platform', 'Unknown')
            file_name = game.get('file_name', '')
            
            if not file_name:
                continue
                
            # Build expected local path using platform_slug
            platform_dir = download_dir / platform_slug  # Use slug, not full name
            local_path = platform_dir / file_name
            
            # Check if file exists and is not empty
            if local_path.exists() and local_path.stat().st_size > 1024:
                # Update game data with current local info
                game_copy = game.copy()
                game_copy['is_downloaded'] = True
                game_copy['local_path'] = str(local_path)
                game_copy['local_size'] = local_path.stat().st_size
                downloaded_games.append(game_copy)
        
        return self.library_section.sort_games_consistently(downloaded_games)

    def scan_and_merge_local_changes(self, cached_games):
        """Merge local file changes with cached RomM data - FILTER to downloaded only"""
        download_dir = Path(self.rom_dir_row.get_text())
        
        # Filter to only downloaded games instead of showing all
        downloaded_games = self.filter_to_downloaded_games_only(cached_games, download_dir)
        
        def update_ui():
            self.available_games = downloaded_games
            if hasattr(self, 'library_section'):
                self.library_section.update_games_library(downloaded_games)
            self.update_connection_ui("disconnected")
            
            if downloaded_games:
                total_cached = len(cached_games)
                self.log_message(f"📂 Offline: {len(downloaded_games)} downloaded games (of {total_cached} in cache)")
            else:
                self.log_message(f"📂 Offline: No downloaded games found")
        
        GLib.idle_add(update_ui)

    def use_cached_data_as_fallback(self):
        """Emergency fallback to cached data"""
        if self.game_cache.is_cache_valid():
            self.log_message("🛡️ Using cached data as fallback")
            self.scan_and_merge_local_changes(list(self.game_cache.cached_games))
        else:
            self.log_message("⚠️ No valid cache available")
            self.handle_offline_mode()

    def process_single_rom(self, rom, download_dir):
        """Process a single ROM with short directory names but full display names"""
        rom_id = rom.get('id')
        platform_display_name = rom.get('platform_name', 'Unknown')  # Full name for tree view
        platform_slug = rom.get('platform_slug', platform_display_name)  # Short name for directories
        file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
        
        # Use short platform slug for local directory structure
        platform_dir = download_dir / platform_slug  # e.g., "snes" instead of "Super Nintendo Entertainment System"
        local_path = platform_dir / file_name
        is_downloaded = local_path.exists() and local_path.stat().st_size > 1024
        
        display_name = Path(file_name).stem if file_name else rom.get('name', 'Unknown')
        
        return {
            'name': display_name,
            'rom_id': rom_id,
            'platform': platform_display_name,  # Tree view shows full name
            'platform_slug': platform_slug,     # Keep slug for reference
            'file_name': file_name,
            'is_downloaded': is_downloaded,
            'local_path': str(local_path) if is_downloaded else None,
            'local_size': local_path.stat().st_size if is_downloaded else 0,
            'romm_data': rom
        }

    def on_auto_connect_changed(self, switch_row, pspec):
        """Handle auto-connect setting change"""
        self.settings.set('RomM', 'auto_connect', str(switch_row.get_active()).lower())

    def on_about(self, action, param):
        """Show about dialog"""
        about = Adw.AboutWindow(
            transient_for=self,
            application_name="RomM - RetroArch Sync",
            application_icon="com.romm.retroarch.sync",
            version="1.0.5",
            developer_name='Hector Eduardo "Covin" Silveri',
            copyright="© 2025 Hector Eduardo Silveri",
            license_type=Gtk.License.GPL_3_0
        )
        about.set_website("https://github.com/Covin90/romm-retroarch-sync")
        about.set_issue_url("hhttps://github.com/Covin90/romm-retroarch-sync/issues")
        about.present()

    def get_overwrite_behavior(self):
        """Get user's preferred overwrite behavior"""
        if hasattr(self, 'auto_overwrite_row'):
            selected = self.auto_overwrite_row.get_selected()
            behaviors = [
                "Smart (prefer newer)",
                "Always prefer local", 
                "Always download from server",
                "Ask each time"
            ]
            if 0 <= selected < len(behaviors):
                return behaviors[selected]
        
        return "Smart (prefer newer)"  # Default

    def on_overwrite_behavior_changed(self, combo_row, pspec):
        """Save overwrite behavior setting"""
        selected = combo_row.get_selected()
        self.settings.set('AutoSync', 'overwrite_behavior', str(selected))


    def debug_icon_loading(self):
        """Set application icon for GTK4 correctly"""
        import os
        from pathlib import Path
        
        print("=== Setting GTK4 Application Icon ===")
        
        # Get the script directory (src/) and go up to project root
        script_dir = os.path.dirname(os.path.abspath(__file__))
        project_root = os.path.dirname(script_dir)
        
        # Find the icon in the new structure
        icon_locations = [
            # New structure paths
            os.path.join(project_root, 'assets', 'icons', 'romm_icon.png'),
            # AppImage paths (for when running from AppImage)
            os.path.join(os.environ.get('APPDIR', ''), 'usr/bin/romm_icon.png'),
            os.path.join(os.environ.get('APPDIR', ''), 'romm-sync.png'),
            # Fallback: same directory as script
            os.path.join(script_dir, 'romm_icon.png'),
            "romm_icon.png"
        ]
        
        icon_path = None
        for location in icon_locations:
            if location and Path(location).exists():
                icon_path = location
                print(f"✓ Using icon: {icon_path}")
                break
        
        if icon_path:
            try:
                # GTK4 approach: Set via GLib and icon theme
                from gi.repository import GLib, Gtk, Gio
                import shutil
                import tempfile
                
                # Create temp icon directory
                temp_dir = Path(tempfile.gettempdir()) / 'romm-sync-icons'
                temp_dir.mkdir(exist_ok=True)
                
                # Copy with application ID as filename
                app_icon_path = temp_dir / 'com.romm.retroarch.sync.png'
                shutil.copy2(icon_path, app_icon_path)
                
                # Add to default icon theme
                icon_theme = Gtk.IconTheme.get_for_display(self.get_display())
                icon_theme.add_search_path(str(temp_dir))
                
                # Set as default icon for all windows
                Gtk.Window.set_default_icon_name('com.romm.retroarch.sync')
                
                print("✅ Set application icon via GTK4 method")
                    
            except Exception as e:
                print(f"❌ Failed to set application icon: {e}")
        else:
            print("❌ No icon found in any location")
        
    print("================================")

    def set_window_icon(self):
        """Set window icon using custom romm_icon.png only"""
        try:
            import os
            from gi.repository import GdkPixbuf
            
            # Try custom icon only
            script_dir = os.path.dirname(os.path.abspath(__file__))
            custom_icon_path = os.path.join(script_dir, 'romm_icon.png')
            
            if os.path.exists(custom_icon_path):
                pixbuf = GdkPixbuf.Pixbuf.new_from_file(custom_icon_path)
                self.set_icon(pixbuf)
                print(f"Using custom window icon: {custom_icon_path}")
            else:
                print("Custom icon not found, using system default")
                
        except Exception as e:
            print(f"Failed to set window icon: {e}")

    def integrate_appimage(self):
        """Set up icon theme for AppImage without desktop file creation"""
        try:
            import os
            import shutil
            import subprocess
            from pathlib import Path
            
            # Check if running from AppImage
            appimage_path = os.environ.get('APPIMAGE')
            if not appimage_path:
                return
            
            print("Setting up AppImage icon theme...")
            
            # Copy icon to user icon directory for proper display
            icon_source = os.path.join(os.environ.get('APPDIR', ''), 'usr/bin/romm_icon.png')
            if Path(icon_source).exists():
                # Copy to multiple icon sizes for better scaling
                icon_sizes = [16, 22, 24, 32, 48, 64, 96, 128, 256]
                for size in icon_sizes:
                    size_dir = Path.home() / '.local/share/icons/hicolor' / f'{size}x{size}' / 'apps'
                    size_dir.mkdir(parents=True, exist_ok=True)
                    icon_dest = size_dir / 'com.romm.retroarch.sync.png'
                    shutil.copy2(icon_source, icon_dest)
                
                print(f"✅ Copied icon to {len(icon_sizes)} different sizes")
                
                # Update icon cache
                subprocess.run(['gtk-update-icon-cache', str(Path.home() / '.local/share/icons/hicolor')], 
                            capture_output=True)
                
                print("✅ Icon theme updated")
            else:
                print("⚠️ Icon source not found")
            
        except Exception as e:
            print(f"Icon setup failed: {e}")

    def load_saved_settings(self):
        """Load saved settings into UI"""
        self.url_row.set_text(self.settings.get('RomM', 'url'))
        
        if self.settings.get('RomM', 'remember_credentials') == 'true':
            self.username_row.set_text(self.settings.get('RomM', 'username'))
            self.password_row.set_text(self.settings.get('RomM', 'password'))
            self.remember_switch.set_active(True)
        
        self.auto_connect_switch.set_active(self.settings.get('RomM', 'auto_connect') == 'true')

    def setup_ui(self):
            """Set up the user interface with actually working wider layout"""
            self.set_title("RomM - RetroArch Sync")
            self.set_default_size(800, 900)  # Increased width
            
            # Header bar with menu button
            header = Adw.HeaderBar()
            self.set_titlebar(header)

            # Add menu button to header bar
            menu_button = Gtk.MenuButton()
            menu_button.set_icon_name("open-menu-symbolic")
            menu_button.set_tooltip_text("Menu")

            # Create simple menu
            menu = Gio.Menu()
            menu.append("Logs / Advanced", "win.logs")  # ADD THIS LINE
            menu.append("About", "win.about")
            menu_button.set_menu_model(menu)
            header.pack_end(menu_button)
            
            # Add custom CSS - using very specific targeting
            css_provider = Gtk.CssProvider()
            css_provider.load_from_data(b"""
            /* Mission Center-inspired styling with system font */
            .data-table {
                background: @view_bg_color;
                font-family: -gtk-system-font;
                font-size: 1em;
            }

            /* Target the ScrolledWindow that contains the tree view */
            scrolledwindow.data-table {
                border: 1px solid @borders;
                border-radius: 10px;
                background: @view_bg_color;
            }

            .data-table columnview {
                border: none;  /* Remove border since ScrolledWindow has it now */
                border-radius: 10px;
            }

            /* Make sure the listview inside respects the rounded corners */
            .data-table columnview > listview {
                border-radius: 0px;
            }

            .data-table row {
                min-height: 36px;
                border-bottom: 1px solid alpha(@borders, 0.25);
                transition: all 150ms ease;
                background: @view_bg_color;
            }

            /* Round the corners of first and last rows */
            .data-table row:first-child {
                border-top-left-radius: 0px;
                border-top-right-radius: 0px;
            }

            .data-table row:last-child {
                border-bottom-left-radius: 0px;
                border-bottom-right-radius: 0px;
                border-bottom: none;
            }

            .data-table row:nth-child(even) {
                background: alpha(@window_bg_color, 0.5);
            }

            .data-table row:nth-child(odd) {
                background: alpha(@card_bg_color, 0.4);
            }

            .data-table row:hover {
                background: alpha(@accent_color, 0.1);
            }

            /* Simple selection without rounded corners */
            columnview > listview > row:selected {
                background: alpha(@accent_bg_color, 0.3);
                color: @window_fg_color;
            }

            columnview > listview > row:selected > cell {
                background: alpha(@accent_bg_color, 0.3);
                color: @window_fg_color;
            }
                                        
            .numeric {
                font-family: -gtk-system-font;
                font-size: 1em;
                color: @dim_label_color;
            }
            """)
            Gtk.StyleContext.add_provider_for_display(
                self.get_display(),
                css_provider,
                Gtk.STYLE_PROVIDER_PRIORITY_USER  # Higher priority than APPLICATION
            )
            
            # Use Adw.PreferencesPage for scrollable layout
            self.preferences_page = Adw.PreferencesPage()
            
            # Main scrolled content
            scrolled_window = Gtk.ScrolledWindow()
            scrolled_window.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
            scrolled_window.set_child(self.preferences_page)
            
            # Set as main content
            self.set_child(scrolled_window)
            
            # Hook to force width after the widget is realized
            def on_preferences_realize(widget):
                # Get the clamp widget and force it wider
                try:
                    def find_clamp_widget(widget):
                        """Recursively find clamp widgets"""
                        if hasattr(widget, 'get_css_name') and widget.get_css_name() == 'clamp':
                            return widget
                        if hasattr(widget, 'get_first_child'):
                            child = widget.get_first_child()
                            while child:
                                result = find_clamp_widget(child)
                                if result:
                                    return result
                                child = child.get_next_sibling()
                        return None
                    
                    clamp_widget = find_clamp_widget(self.preferences_page)
                    if clamp_widget:
                        # Force the clamp widget to be wider
                        clamp_widget.set_maximum_size(1000)
                        print("✅ Found and modified clamp widget")
                    else:
                        print("❌ Could not find clamp widget")
                except Exception as e:
                    print(f"Error modifying clamp: {e}")
            
            # Connect to realize signal
            self.preferences_page.connect('realize', on_preferences_realize)
            
            # Create sections (no changes needed)
            self.create_connection_section()  # Combined server & status section
            self.create_library_section()     # Game library tree view
            self.create_settings_section()    # Settings including auto-sync with upload saves

    def create_connection_section(self):
        """Create combined server section with RomM connection and RetroArch status"""
        connection_group = Adw.PreferencesGroup()
        connection_group.set_title("Server &amp; Status")
        
        # RomM Connection expander (keep as is)
        self.connection_expander = Adw.ExpanderRow()
        self.connection_expander.set_title("RomM Connection")
        self.connection_expander.set_subtitle("Not connected - expand to configure")
        
        # Add toggle switch as suffix to enable/disable connection
        self.connection_enable_switch = Gtk.Switch()
        self.connection_enable_switch.set_valign(Gtk.Align.CENTER)
        self.connection_enable_switch.connect('notify::active', self.on_connection_toggle)
        self.connection_expander.add_suffix(self.connection_enable_switch)
        
        # RomM Connection settings inside the expander (only shown when expanded)
        
        # RomM URL entry
        self.url_row = Adw.EntryRow()
        self.url_row.set_title("Server URL")
        self.url_row.set_text("")
        self.connection_expander.add_row(self.url_row)
        
        # Username entry
        self.username_row = Adw.EntryRow()
        self.username_row.set_title("Username")
        self.connection_expander.add_row(self.username_row)
        
        # Password entry
        self.password_row = Adw.PasswordEntryRow()
        self.password_row.set_title("Password")
        self.connection_expander.add_row(self.password_row)
        
        # Remember credentials switch
        self.remember_switch = Adw.SwitchRow()
        self.remember_switch.set_title("Remember credentials")
        self.remember_switch.set_subtitle("Save login details locally")
        self.connection_expander.add_row(self.remember_switch)

        # Auto-connect switch
        self.auto_connect_switch = Adw.SwitchRow()
        self.auto_connect_switch.set_title("Auto-connect on startup")
        self.auto_connect_switch.set_subtitle("Automatically connect when app starts")
        self.auto_connect_switch.connect('notify::active', self.on_auto_connect_changed)
        self.connection_expander.add_row(self.auto_connect_switch)

        # Connection status row
        self.connection_status_row = Adw.ActionRow()
        self.connection_status_row.set_title("Status")
        self.connection_status_row.set_subtitle("🔴 Disconnected")
        self.connection_expander.add_row(self.connection_status_row)
        
        connection_group.add(self.connection_expander)
        
        # RetroArch section - simplified without status monitoring
        self.retroarch_expander = Adw.ExpanderRow()
        self.retroarch_expander.set_title("RetroArch")
        self.retroarch_expander.set_subtitle("Installation and core information")

        # Refresh button
        refresh_container = Gtk.Box()
        refresh_container.set_size_request(-1, 18)
        refresh_container.set_valign(Gtk.Align.CENTER)

        refresh_button = Gtk.Button(label="Refresh")
        refresh_button.connect('clicked', self.on_refresh_retroarch_info)
        refresh_button.set_size_request(80, 18)
        refresh_button.set_hexpand(False)
        refresh_button.set_vexpand(False)
        refresh_button.set_valign(Gtk.Align.CENTER)
        refresh_container.append(refresh_button)

        self.retroarch_expander.add_suffix(refresh_container)

        # Installation info row
        self.retroarch_info_row = Adw.ActionRow()
        self.retroarch_info_row.set_title("Installation")
        self.retroarch_info_row.set_subtitle("Checking...")
        self.retroarch_expander.add_row(self.retroarch_info_row)

        # Cores directory row
        self.cores_info_row = Adw.ActionRow()
        self.cores_info_row.set_title("Cores Directory")
        self.cores_info_row.set_subtitle("Checking...")
        self.retroarch_expander.add_row(self.cores_info_row)

        # Available cores count row
        self.core_count_row = Adw.ActionRow()
        self.core_count_row.set_title("Available Cores")
        self.core_count_row.set_subtitle("Checking...")
        self.retroarch_expander.add_row(self.core_count_row)

        # Add this new connection status row
        self.retroarch_connection_row = Adw.ActionRow()
        self.retroarch_connection_row.set_title("Network Connection")
        self.retroarch_connection_row.set_subtitle("Checking...")
        self.retroarch_expander.add_row(self.retroarch_connection_row)

        connection_group.add(self.retroarch_expander)
        
        self.preferences_page.add(connection_group)

    def on_clear_cache(self, button):
        """Clear cached game data"""
        if hasattr(self, 'game_cache'):
            self.game_cache.clear_cache()
            self.log_message("🗑️ Game data cache cleared")
            self.log_message("💡 Reconnect to RomM to rebuild cache")
        else:
            self.log_message("❌ No cache to clear")

    def on_check_cache_status(self, button):
        """Check cache status and report"""
        if hasattr(self, 'game_cache'):
            cache = self.game_cache
            
            if cache.is_cache_valid():
                game_count = len(cache.cached_games)
                platform_count = len(cache.platform_mapping)
                filename_count = len(cache.filename_mapping)
                
                self.log_message(f"📂 Cache Status: VALID")
                self.log_message(f"   Games: {game_count}")
                self.log_message(f"   Platform mappings: {platform_count}")
                self.log_message(f"   Filename mappings: {filename_count}")
                
                # Show some examples
                if platform_count > 0:
                    sample_platforms = list(cache.platform_mapping.items())[:3]
                    self.log_message(f"   Platform examples:")
                    for dir_name, platform_name in sample_platforms:
                        self.log_message(f"     {dir_name} → {platform_name}")
            else:
                self.log_message(f"📭 Cache Status: EMPTY or EXPIRED")
                self.log_message(f"   Connect to RomM to populate cache")
        else:
            self.log_message(f"❌ Cache system not initialized")

    def on_connection_toggle(self, switch_row, pspec):
            """Handle connection enable/disable toggle"""
            if switch_row.get_active():
                # User wants to connect
                url = self.url_row.get_text()
                username = self.username_row.get_text()
                password = self.password_row.get_text()
                
                if not url or not username or not password:
                    self.log_message("⚠️ Please fill in all connection details first")
                    switch_row.set_active(False)
                    return
                
                # Start connection
                self.start_connection(url, username, password)
                
            else:
                # User wants to disconnect
                self.disconnect_from_romm()

    def refresh_games_list_batched(self):
        """Batch multiple refresh requests to reduce jarring transitions"""
        if self._pending_refresh:
            return
            
        self._pending_refresh = True
        
        # Small delay to batch multiple rapid refresh requests
        GLib.timeout_add(100, self._do_batched_refresh)
    
    def _do_batched_refresh(self):
        """Execute the actual refresh"""
        self._pending_refresh = False
        self.refresh_games_list()
        return False  # Don't repeat
    
    def start_connection(self, url, username, password):
        """Modified to use batched refresh"""
        remember = self.remember_switch.get_active()
        
        # Save settings
        self.settings.set('RomM', 'url', url)
        self.settings.set('RomM', 'remember_credentials', str(remember).lower())
        
        if remember:
            self.settings.set('RomM', 'username', username)
            self.settings.set('RomM', 'password', password)
        else:
            self.settings.set('RomM', 'username', '')
            self.settings.set('RomM', 'password', '')
        
        def connect():
            self.log_message(f"Connecting to {url}...")
            GLib.idle_add(lambda: self.update_connection_ui("connecting"))
            
            self.romm_client = RomMClient(url, username, password)
            
            def update_ui():
                if self.romm_client.authenticated:
                    self.update_connection_ui("loading")
                    self.log_message("Successfully connected to RomM")
                    
                    if hasattr(self, 'auto_sync'):
                        self.auto_sync.romm_client = self.romm_client
                    
                    # Clear selections when connecting
                    if hasattr(self, 'library_section'):
                        self.library_section.clear_checkbox_selections_smooth()
                    
                    # Auto-enable sync if setting is enabled
                    if self.settings.get('AutoSync', 'auto_enable_on_connect') == 'true':
                        self.autosync_enable_switch.set_active(True)
                        self.log_message("🔄 Auto-sync enabled automatically")
                    
                    # Use batched refresh to reduce jarring transitions
                    self.refresh_games_list_batched()
                else:
                    self.update_connection_ui("failed")
                    self.connection_enable_switch.set_active(False)
                    # Turn off auto-sync on connection failure
                    if hasattr(self, 'autosync_enable_switch'):
                        self.autosync_enable_switch.set_active(False)
                        self.autosync_status_row.set_subtitle("🔴 Disabled - connection failed")
                    self.log_message("Failed to connect to RomM")
            
            GLib.idle_add(update_ui)
        
        threading.Thread(target=connect, daemon=True).start()

    def disconnect_from_romm(self):
        """Disconnect and switch to local-only view"""
        self.romm_client = None
        
        # Clear selections when disconnecting  
        if hasattr(self, 'library_section'):
            self.library_section.clear_checkbox_selections_smooth()
        
        if hasattr(self, 'auto_sync'):
            self.auto_sync.stop_auto_sync()
            # Turn off auto-sync switch when disconnected
            self.autosync_enable_switch.set_active(False)
            self.autosync_status_row.set_subtitle("🔴 Disabled - not connected to RomM")
        
        self.update_connection_ui("disconnected")
        self.log_message("Disconnected from RomM")
        
        # Switch to local-only view immediately
        self.handle_offline_mode()

    def update_connection_ui(self, state):
        """Update connection UI based on state"""
        if state == "connecting":
            self.connection_expander.set_subtitle("🟡 Connecting...")
            self.connection_status_row.set_subtitle("🟡 Connecting...")
            
        elif state == "loading":
            self.connection_expander.set_subtitle("🔄 Loading games...")
            self.connection_status_row.set_subtitle("🔄 Loading games...")
            
        elif state == "connected":
            # Add game count when connected
            game_count = len(getattr(self, 'available_games', []))
            if game_count > 0:
                subtitle = f"🟢 Connected - {game_count:,} Games"
            else:
                subtitle = "🟢 Connected"
            self.connection_expander.set_subtitle(subtitle)
            self.connection_status_row.set_subtitle(subtitle)
                
        elif state == "failed":
            self.connection_expander.set_subtitle("🔴 Connection failed")
            self.connection_status_row.set_subtitle("🔴 Connection failed")
            
        elif state == "disconnected":
            self.connection_expander.set_subtitle("🔴 Disconnected")
            self.connection_status_row.set_subtitle("🔴 Disconnected")

    def update_connection_ui_with_message(self, message):
        """Update connection UI with custom message"""
        self.connection_expander.set_subtitle(message)
        self.connection_status_row.set_subtitle(message)       

    def create_status_section(self):
        """Status section is now combined with connection section, so this is empty"""
        # This method is now empty since we moved everything to create_connection_section
        pass

    def create_library_section(self):
        """Create the enhanced library section with tree view (moved from quick actions)"""
        # Create enhanced library section with tree view
        self.library_section = EnhancedLibrarySection(self)
        self.preferences_page.add(self.library_section.library_group)

    def on_autosync_toggle(self, switch_row, pspec):
        """Handle auto-sync enable/disable"""
        if switch_row.get_active():
            if self.romm_client and self.romm_client.authenticated:
                # Update auto-sync settings
                self.auto_sync.romm_client = self.romm_client
                self.auto_sync.upload_enabled = self.autoupload_row.get_active()
                self.auto_sync.download_enabled = self.autodownload_row.get_active()
                self.auto_sync.upload_delay = int(self.sync_delay_row.get_value())
                
                # Start auto-sync
                self.auto_sync.start_auto_sync()
                self.autosync_status_row.set_subtitle("🟢 Active - monitoring for changes")
                
                self.log_message("🔄 Auto-sync enabled")
            else:
                self.log_message("⚠️ Please connect to RomM before enabling auto-sync")
                self.autosync_status_row.set_subtitle("🔴 Disabled - not connected to RomM")
                switch_row.set_active(False)
        else:
            self.auto_sync.stop_auto_sync()
            self.autosync_status_row.set_subtitle("🔴 Disabled")
            self.log_message("⏹️ Auto-sync disabled")

    def get_selected_game(self):
        """Get currently selected game from tree view"""
        if hasattr(self, 'library_section'):
            return self.library_section.selected_game
        return None

    def on_auto_enable_sync_changed(self, switch_row, pspec):
        """Handle auto-enable sync setting change"""
        self.settings.set('AutoSync', 'auto_enable_on_connect', str(switch_row.get_active()).lower())

    def create_quick_actions_section(self):
        """Create the enhanced library section with tree view (quick actions removed)"""
        # Create enhanced library section with tree view
        self.library_section = EnhancedLibrarySection(self)
        self.preferences_page.add(self.library_section.library_group)

    def create_settings_section(self):
        """Create settings section with merged download and sync settings"""
        # Combined Download & Sync settings
        download_sync_group = Adw.PreferencesGroup()
        download_sync_group.set_title("Download &amp; Sync")

        # ROM directory
        self.rom_dir_expander = Adw.ExpanderRow()
        self.rom_dir_expander.set_title("Library Directory")
        self.rom_dir_expander.set_subtitle(self.settings.get('Download', 'rom_directory'))

        # Directory chooser button wrapped in container
        dir_button_container = Gtk.Box()
        dir_button_container.set_size_request(-1, 18)
        dir_button_container.set_valign(Gtk.Align.CENTER)

        choose_dir_button = Gtk.Button(label="Browse")
        choose_dir_button.connect('clicked', self.on_choose_directory)
        choose_dir_button.set_hexpand(False)
        choose_dir_button.set_vexpand(False)
        choose_dir_button.set_valign(Gtk.Align.CENTER)
        dir_button_container.append(choose_dir_button)

        self.rom_dir_expander.add_suffix(dir_button_container)

        # ROM Directory Path entry (nested under expander)
        self.rom_dir_row = Adw.EntryRow()
        self.rom_dir_row.set_title("Directory Path")
        self.rom_dir_row.set_text(self.settings.get('Download', 'rom_directory'))
        self.rom_dir_expander.add_row(self.rom_dir_row)

        # Open Download Folder (nested under ROM Directory)
        browse_row = Adw.ActionRow()
        browse_row.set_title("Open Download Folder")
        browse_row.set_subtitle("View downloaded files in file manager")

        browse_button_container = Gtk.Box()
        browse_button_container.set_size_request(-1, 18)
        browse_button_container.set_valign(Gtk.Align.CENTER)

        browse_button = Gtk.Button(label="Open")
        browse_button.connect('clicked', self.on_browse_downloads)
        browse_button.set_hexpand(False)
        browse_button.set_vexpand(False)
        browse_button.set_valign(Gtk.Align.CENTER)
        browse_button_container.append(browse_button)

        browse_row.add_suffix(browse_button_container)
        self.rom_dir_expander.add_row(browse_row)

        download_sync_group.add(self.rom_dir_expander)
        
        # Auto-Sync expander with built-in toggle switch
        self.autosync_expander = Adw.ExpanderRow()
        self.autosync_expander.set_title("Auto-Sync")
        self.autosync_expander.set_subtitle("Monitor and sync save files automatically")
        
        # Add toggle switch as suffix to the expander
        self.autosync_enable_switch = Gtk.Switch()
        self.autosync_enable_switch.set_valign(Gtk.Align.CENTER)
        self.autosync_enable_switch.connect('notify::active', self.on_autosync_toggle)
        self.autosync_expander.add_suffix(self.autosync_enable_switch)
        
        # Auto-upload toggle
        self.autoupload_row = Adw.SwitchRow()
        self.autoupload_row.set_title("Auto-Upload")
        self.autoupload_row.set_subtitle("Upload saves to RomM when files change")
        self.autoupload_row.set_active(True)
        self.autosync_expander.add_row(self.autoupload_row)
        
        # Auto-download toggle  
        self.autodownload_row = Adw.SwitchRow()
        self.autodownload_row.set_title("Auto-Download")
        self.autodownload_row.set_subtitle("Download saves from RomM before launching games")
        self.autodownload_row.set_active(True)
        self.autosync_expander.add_row(self.autodownload_row)
        
        # Upload delay setting
        self.sync_delay_row = Adw.SpinRow()
        self.sync_delay_row.set_title("Upload Delay")
        self.sync_delay_row.set_subtitle("Seconds to wait after file changes")
        adjustment = Gtk.Adjustment(value=3, lower=1, upper=30, step_increment=1)
        self.sync_delay_row.set_adjustment(adjustment)
        self.autosync_expander.add_row(self.sync_delay_row)

        # Auto-enable on connection toggle
        self.auto_enable_sync_row = Adw.SwitchRow()
        self.auto_enable_sync_row.set_title("Auto-Enable on Connect")
        self.auto_enable_sync_row.set_subtitle("Automatically turn on auto-sync when connecting to RomM")
        self.auto_enable_sync_row.set_active(self.settings.get('AutoSync', 'auto_enable_on_connect') == 'true')
        self.auto_enable_sync_row.connect('notify::active', self.on_auto_enable_sync_changed)
        self.autosync_expander.add_row(self.auto_enable_sync_row)

        # ADD THIS NEW SECTION HERE:
        # Auto-overwrite behavior setting
        self.auto_overwrite_row = Adw.ComboRow()
        self.auto_overwrite_row.set_title("Auto-Sync Behaviour")
        self.auto_overwrite_row.set_subtitle("How to handle conflicts between local and server saves")

        overwrite_options = Gtk.StringList()
        overwrite_options.append("Smart (prefer newer)")  # Default
        overwrite_options.append("Always prefer local")
        overwrite_options.append("Always download from server")
        overwrite_options.append("Ask each time")

        self.auto_overwrite_row.set_model(overwrite_options)
        self.auto_overwrite_row.set_selected(0)  # Default to "Smart"
        
        # Connect the setting change handler
        self.auto_overwrite_row.connect('notify::selected', self.on_overwrite_behavior_changed)
        
        # Load saved setting
        saved_behavior = int(self.settings.get('AutoSync', 'overwrite_behavior', '0'))
        self.auto_overwrite_row.set_selected(saved_behavior)
        
        self.autosync_expander.add_row(self.auto_overwrite_row)

        # Manual Upload Saves row
        upload_saves_row = Adw.ActionRow()
        upload_saves_row.set_title("Manual Upload")
        upload_saves_row.set_subtitle("Upload all local saves to RomM now")
        
        # Upload button wrapped in container
        upload_container = Gtk.Box()
        upload_container.set_size_request(-1, 18)
        upload_container.set_valign(Gtk.Align.CENTER)
        
        upload_button = Gtk.Button(label="Upload Saves")
        upload_button.connect('clicked', self.on_sync_to_romm)
        upload_button.set_hexpand(False)
        upload_button.set_vexpand(False)
        upload_button.set_valign(Gtk.Align.CENTER)
        upload_container.append(upload_button)
        
        upload_saves_row.add_suffix(upload_container)
        self.autosync_expander.add_row(upload_saves_row)
        
        # Status indicator
        self.autosync_status_row = Adw.ActionRow()
        self.autosync_status_row.set_title("Status")
        self.autosync_status_row.set_subtitle("🔴 Disabled")
        self.autosync_expander.add_row(self.autosync_status_row)
        
        download_sync_group.add(self.autosync_expander)
        self.preferences_page.add(download_sync_group)

    def on_autosync_toggle(self, switch_row, pspec):
        """Handle auto-sync enable/disable"""
        if switch_row.get_active():
            if self.romm_client and self.romm_client.authenticated:
                # Update auto-sync settings
                self.auto_sync.romm_client = self.romm_client
                self.auto_sync.upload_enabled = self.autoupload_row.get_active()
                self.auto_sync.download_enabled = self.autodownload_row.get_active()
                self.auto_sync.upload_delay = int(self.sync_delay_row.get_value())
                
                # Start auto-sync
                self.auto_sync.start_auto_sync()
                self.autosync_status_row.set_subtitle("🟢 Active - monitoring for changes")
                
                self.log_message("🔄 Auto-sync enabled")
            else:
                self.log_message("⚠️ Please connect to RomM before enabling auto-sync")
                switch_row.set_active(False)
        else:
            self.auto_sync.stop_auto_sync()
            self.autosync_status_row.set_subtitle("🔴 Disabled")
            self.log_message("⏹️ Auto-sync disabled")
    
    def on_show_logs_dialog(self, button):
        """Show logs and advanced tools dialog"""
        dialog = Adw.PreferencesWindow()
        dialog.set_title("Logs & Advanced Tools")
        dialog.set_default_size(600, 500)
        dialog.set_transient_for(self)
        dialog.set_modal(False)
        
        # Activity Log
        log_group = Adw.PreferencesGroup()
        log_group.set_title("Activity Log")
        
        # Create dialog log view that SHARES the same buffer
        dialog_log_view = Gtk.TextView()
        dialog_log_view.set_editable(False)
        dialog_log_view.set_cursor_visible(False)
        dialog_log_view.set_buffer(self.log_view.get_buffer())  # SHARE the buffer
        
        scrolled_log = Gtk.ScrolledWindow()
        scrolled_log.set_child(dialog_log_view)
        scrolled_log.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        scrolled_log.set_size_request(-1, 200)
        
        log_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        log_box.append(scrolled_log)
        
        log_row = Adw.ActionRow()
        log_row.set_child(log_box)
        log_group.add(log_row)
        
        # Advanced Tools  
        advanced_group = Adw.PreferencesGroup()
        advanced_group.set_title("Advanced Tools")
        
        # Debug API
        debug_row = Adw.ActionRow()
        debug_row.set_title("Debug RomM API")
        debug_row.set_subtitle("Test API endpoints and authentication")
        debug_btn = Gtk.Button(label="Debug")
        debug_btn.set_valign(Gtk.Align.CENTER)  # CHANGE: Use valign instead
        debug_btn.set_size_request(80, -1)      # CHANGE: Only set width
        debug_btn.connect('clicked', self.on_debug_api)
        debug_row.add_suffix(debug_btn)
        advanced_group.add(debug_row)

        # Inspect Files
        inspect_row = Adw.ActionRow()
        inspect_row.set_title("Inspect Files")
        inspect_row.set_subtitle("Check downloaded file integrity")
        inspect_btn = Gtk.Button(label="Inspect")
        inspect_btn.set_valign(Gtk.Align.CENTER)  # CHANGE: Use valign instead
        inspect_btn.set_size_request(80, -1)      # CHANGE: Only set width
        inspect_btn.connect('clicked', self.on_inspect_downloads)
        inspect_row.add_suffix(inspect_btn)
        advanced_group.add(inspect_row)

        # Cache Management
        cache_row = Adw.ActionRow()
        cache_row.set_title("Game Data Cache")
        cache_row.set_subtitle("Local storage management")

        cache_box = Gtk.Box(spacing=6)
        cache_box.set_valign(Gtk.Align.CENTER)    # CHANGE: Align the box
        check_btn = Gtk.Button(label="Check")
        check_btn.set_size_request(70, -1)        # CHANGE: Only set width
        check_btn.connect('clicked', self.on_check_cache_status)
        clear_btn = Gtk.Button(label="Clear")
        clear_btn.set_size_request(70, -1)        # CHANGE: Only set width
        clear_btn.add_css_class('destructive-action')
        clear_btn.connect('clicked', self.on_clear_cache)
        cache_box.append(check_btn)
        cache_box.append(clear_btn)
        cache_row.add_suffix(cache_box)
        advanced_group.add(cache_row)
        
        # Create page and add groups
        page = Adw.PreferencesPage()
        page.add(log_group)
        page.add(advanced_group)
        dialog.add(page)
        
        dialog.present()

    def log_message(self, message):
        """Add message to log view"""
        def update_ui():
            buffer = self.log_view.get_buffer()
            end_iter = buffer.get_end_iter()
            buffer.insert(end_iter, f"{message}\n")
            
            # Auto-scroll to bottom
            mark = buffer.get_insert()
            self.log_view.scroll_mark_onscreen(mark)
        
        GLib.idle_add(update_ui)
    
    def on_choose_directory(self, button):
        """Choose download directory"""
        def on_response(dialog, response):
            if response == Gtk.ResponseType.ACCEPT:
                file = dialog.get_file()
                if file:
                    path = file.get_path()
                    self.rom_dir_row.set_text(path)
                    self.rom_dir_expander.set_subtitle(path)  # Update expander subtitle too
                    self.settings.set('Download', 'rom_directory', path)
                    self.log_message(f"Download directory set to: {path}")
        
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose ROM Download Directory")
        dialog.select_folder(self, None, on_response)
    
    def update_download_progress(self, progress_info):
        """Update progress for specific game in tree view only"""
        rom_id = getattr(self, '_current_download_rom_id', None)
        if not rom_id:
            return
        
        # Only update tree view progress data
        current_time = time.time()
        last_update = self._last_progress_update.get(rom_id, 0)
        
        if rom_id in self.download_progress:
            self.download_progress[rom_id].update({
                'progress': progress_info['progress'],
                'speed': progress_info['speed'],
                'downloaded': progress_info['downloaded'],
                'total': progress_info['total'],
                'downloading': True
            })
        
        # Throttled tree view updates only
        if (current_time - last_update >= self._progress_update_interval or
            progress_info.get('progress', 0) >= 1.0):
            self._last_progress_update[rom_id] = current_time
            
            if hasattr(self, 'library_section'):
                GLib.idle_add(lambda: self._safe_progress_update(rom_id))

    def _safe_progress_update(self, rom_id):
        """Safely update progress in main thread"""
        try:
            if (hasattr(self, 'library_section') and 
                rom_id in self.download_progress):
                self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
        except Exception as e:
            print(f"Safe progress update error: {e}")
        return False  # Don't repeat
            
    def set_action_row_icon(self, action_row, icon_name):
        """Helper method to set icon on ActionRow using modern GTK4 approach"""
        # Remove existing prefix if any
        prefix = action_row.get_prefix()
        if prefix:
            action_row.remove_prefix(prefix)
        
        # Create and configure new icon
        icon = Gtk.Image.new_from_icon_name(icon_name)
        icon.set_pixel_size(16)
        icon.set_valign(Gtk.Align.CENTER)  # Important for proper alignment
        icon.set_visible(True)  # Ensure it's visible
        
        # Add the icon as prefix
        action_row.add_prefix(icon)

    def refresh_retroarch_info(self):
        """Update RetroArch information in UI"""
        def update_info():
            # Check RetroArch executable
            if hasattr(self, 'retroarch_info_row'):
                if self.retroarch.retroarch_executable:
                    self.retroarch_info_row.set_subtitle(f"Found: {self.retroarch.retroarch_executable}")
                    self.retroarch_expander.set_subtitle("🟢 RetroArch installed")
                else:
                    self.retroarch_info_row.set_subtitle("Not found - install RetroArch")
                    self.retroarch_expander.set_subtitle("🔴 RetroArch not installed")
            
            # Check cores directory
            if hasattr(self, 'cores_info_row'):
                if self.retroarch.cores_dir:
                    self.cores_info_row.set_subtitle(f"Found: {self.retroarch.cores_dir}")
                    
                    # Count cores
                    cores = self.retroarch.get_available_cores()
                    core_count = len(cores)
                    
                    if hasattr(self, 'core_count_row'):
                        self.core_count_row.set_subtitle(f"{core_count} cores available")
                    
                    if core_count > 0:
                        # Show some example cores
                        example_cores = list(cores.keys())[:3]
                        examples = ", ".join(example_cores)
                        if len(cores) > 3:
                            examples += f" + {len(cores) - 3} more"
                        self.log_message(f"Available cores: {examples}")
                        
                        if self.retroarch.retroarch_executable:
                            self.retroarch_expander.set_subtitle("🟢 Ready")
                    else:
                        self.retroarch_expander.set_subtitle("⚠️ No cores available")
                else:
                    if hasattr(self, 'cores_info_row'):
                        self.cores_info_row.set_subtitle("Cores directory not found")
                    if hasattr(self, 'core_count_row'):
                        self.core_count_row.set_subtitle("0 cores available")
                    self.retroarch_expander.set_subtitle("🔴 Cores directory not found")
        
            # Test RetroArch network connection
            if hasattr(self, 'retroarch_connection_row'):
                if self.retroarch.retroarch_executable:
                    # Test connection
                    try:
                        status = self.retroarch.send_command("GET_STATUS")
                        if status:
                            self.retroarch_connection_row.set_subtitle("🟢 Connected - notifications will work")
                        else:
                            self.retroarch_connection_row.set_subtitle("🔴 Not responding - turn ON Settings - Network - Network Commands to get notifications")
                    except Exception as e:
                        self.retroarch_connection_row.set_subtitle("🔴 Connection failed")
                else:
                    self.retroarch_connection_row.set_subtitle("⚠️ RetroArch not found")
        
        update_info()
        
    def on_refresh_retroarch_info(self, button):
        """Refresh RetroArch information"""
        self.log_message("Refreshing RetroArch information...")
        
        # Re-initialize RetroArch interface
        self.retroarch = RetroArchInterface()
        self.refresh_retroarch_info()
        
        self.log_message("RetroArch information updated")

    def refresh_games_list(self):
        """Smart sync with comprehensive change detection"""
        if getattr(self, '_dialog_open', False):
            return

        def smart_sync():
            if not (self.romm_client and self.romm_client.authenticated):
                self.handle_offline_mode()
                return
                
            try:
                download_dir = Path(self.rom_dir_row.get_text())
                server_url = self.romm_client.base_url
                
                self.log_message(f"🔄 Syncing with server: {server_url}")
                self.perform_full_sync(download_dir, server_url)
                
            except Exception as e:
                self.log_message(f"❌ Sync error: {e}")
                self.use_cached_data_as_fallback()
        
        threading.Thread(target=smart_sync, daemon=True).start()

    def apply_no_changes_update(self):
        """Handle the perfect case where nothing changed"""
        start_time = time.time()
        
        def update_ui():
            # Use existing cached data
            cached_games = list(self.game_cache.cached_games)
            self.available_games = cached_games
            
            if hasattr(self, 'library_section'):
                self.library_section.update_games_library(cached_games)
            
            GLib.idle_add(lambda: self.update_connection_ui("connected"))
            
            elapsed = time.time() - start_time
            self.log_message(f"⚡ Lightning sync: No changes (cached data) in {elapsed:.3f}s")
        
        GLib.idle_add(update_ui)

    def perform_full_sync(self, download_dir, server_url):
        """Perform full sync with live updates"""
        try:
            start_time = time.time()
            
            def progress_handler(stage, data):
                if stage in ['chunk', 'page']:
                    # Update connection status with chunk progress
                    GLib.idle_add(lambda msg=data: self.update_connection_ui_with_message(msg))
                elif stage == 'batch':
                    # Process and show games after each chunk
                    chunk_games = data.get('accumulated_games', [])
                    chunk_num = data.get('chunk_number', 0)
                    total_chunks = data.get('total_chunks', 0)
                    
                    if chunk_games:
                        # Process games
                        processed_games = []
                        for rom in chunk_games:
                            processed_game = self.process_single_rom(rom, download_dir)
                            processed_games.append(processed_game)
                        
                        # Sort games
                        processed_games = self.library_section.sort_games_consistently(processed_games)
                        
                        # Update UI with current progress
                        def update_ui():
                            self.available_games = processed_games
                            if hasattr(self, 'library_section'):
                                self.library_section.update_games_library(processed_games)
                        
                        GLib.idle_add(update_ui)
            
            # Fetch with progress handler
            romm_result = self.romm_client.get_roms(progress_callback=progress_handler)
            
            if not romm_result or len(romm_result) != 2:
                self.log_message("Failed to fetch games from RomM")
                return
                
            final_games, total_count = romm_result
            
            # Final processing and UI update
            games = []
            for rom in final_games:
                processed_game = self.process_single_rom(rom, download_dir)
                games.append(processed_game)
            
            games = self.library_section.sort_games_consistently(games)
            
            def final_update():
                self.available_games = games
                if hasattr(self, 'library_section'):
                    self.library_section.update_games_library(games)
                
                elapsed = time.time() - start_time
                
                # Show completion message first
                completion_msg = f"✅ Full sync complete: {len(games):,} games in {elapsed:.2f}s"
                self.update_connection_ui_with_message(completion_msg)
                self.log_message(completion_msg)
                
                # After 3 seconds, show connected status
                def show_connected():
                    self.update_connection_ui("connected")
                    return False  # Don't repeat
                
                GLib.timeout_add(5000, show_connected)  # 3 second delay
            
            GLib.idle_add(final_update)
            
            # Save cache in background
            content_hash = hash(str(len(games)) + str(games[0].get('rom_id', '') if games else ''))
            threading.Thread(target=lambda: self.game_cache.save_games_data(games), daemon=True).start()

        except Exception as e:
            self.log_message(f"Full sync error: {e}")

    def scan_local_games_only(self, download_dir):
        """Enhanced local game scanning that handles both slug and full platform names"""
        games = []
        
        self.log_message(f"🔍 DEBUG: Scanning {download_dir}")
        self.log_message(f"🔍 DEBUG: Directory exists: {download_dir.exists()}")
        
        if not download_dir.exists():
            return games
        
        rom_extensions = {'.zip', '.7z', '.rar', '.bin', '.cue', '.iso', '.chd', '.sfc', '.smc', '.nes', '.gba', '.gb', '.gbc', '.md', '.gen', '.n64', '.z64'}
        
        for file_path in download_dir.rglob('*'):
            if file_path.is_file() and file_path.suffix.lower() in rom_extensions:
                directory_name = file_path.parent.name if file_path.parent != download_dir else "Unknown"
                
                game_name = file_path.stem
                
                # Use cache to get proper platform name (handles both slug and full names)
                platform_display_name = self.game_cache.get_platform_name(directory_name)
                
                # Try to get additional ROM data from cache
                game_info = self.game_cache.get_game_info(file_path.name)
                
                if game_info:
                    platform_display_name = game_info['platform']  # Use cached full platform name
                    rom_id = game_info['rom_id']
                    romm_data = game_info['romm_data']
                else:
                    rom_id = None
                    romm_data = None
                
                games.append({
                    'name': game_name,
                    'rom_id': rom_id,
                    'platform': platform_display_name,  # Full name for tree view
                    'platform_slug': directory_name,    # Actual directory name used
                    'file_name': file_path.name,
                    'is_downloaded': True,
                    'local_path': str(file_path),
                    'local_size': file_path.stat().st_size,
                    'romm_data': romm_data
                })
        
        return self.library_section.sort_games_consistently(games)

    def on_refresh_games_list(self, button):
        """Refresh games list button handler"""
        self.log_message("Refreshing games list...")
        self.refresh_games_list()
    
    def on_delete_game_clicked(self, button):
        """Delete a downloaded game file"""
        selected_game = self.get_selected_game()
        if not selected_game:
            self.log_message("No game selected")
            return
        
        if not selected_game['is_downloaded']:
            self.log_message("Game is not downloaded")
            return
        
        # Create confirmation dialog
        def on_response(dialog, response):
            if response == "delete":
                self.delete_game_file(selected_game)
        
        dialog = Adw.MessageDialog.new(self)
        dialog.set_heading("Delete Game?")
        dialog.set_body(f"Are you sure you want to delete '{selected_game['name']}'? This will permanently remove the ROM file from your computer.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect('response', on_response)
        dialog.present()

    def download_multiple_games(self, games):
        """Download multiple games immediately without confirmation"""
        count = len(games)
        games_to_download = list(games)
        
        # Filter to only games that aren't already downloaded
        not_downloaded = [g for g in games_to_download if not g.get('is_downloaded', False)]
        
        if not not_downloaded:
            self.log_message("All selected games are already downloaded")
            return
        
        # Update count to reflect actual games to download
        download_count = len(not_downloaded)
        
        # CAPTURE SELECTION STATE BEFORE BLOCKING (this is the key fix!)
        if hasattr(self, 'library_section'):
            # Create a set of ROM IDs that we're downloading for tracking
            self._downloading_rom_ids = set()
            for game in not_downloaded:
                identifier_type, identifier_value = self.library_section.get_game_identifier(game)
                if identifier_type == 'rom_id':
                    self._downloading_rom_ids.add(identifier_value)
            
            print(f"🔍 DEBUG: Captured downloading ROM IDs: {self._downloading_rom_ids}")
        
        # BLOCK TREE REFRESHES DURING BULK OPERATION (prevents visible collapsing)
        self._dialog_open = True
        if hasattr(self, 'library_section'):
            self.library_section._block_selection_updates(True)
        
        # Track completion - starts at download count, decrements as each completes
        self._bulk_download_remaining = download_count
        
        # Start downloads immediately
        self.log_message(f"🚀 Starting bulk download of {download_count} games...")
        for game in not_downloaded:
            self.download_game(game, is_bulk_operation=True)
        
        # Check for completion periodically
        def check_completion():
            if hasattr(self, '_bulk_download_remaining') and self._bulk_download_remaining <= 0:
                # All downloads completed and selections updated
                self._dialog_open = False
                if hasattr(self, 'library_section'):
                    self.library_section._block_selection_updates(False)
                    
                    # Clean up any remaining selections for downloaded ROMs
                    if hasattr(self, '_downloading_rom_ids'):
                        for rom_id in self._downloading_rom_ids:
                            self.library_section.selected_rom_ids.discard(rom_id)
                        self.library_section.sync_selected_checkboxes()
                        self.library_section.update_action_buttons()
                        self.library_section.update_selection_label()
                        self.library_section.refresh_all_platform_checkboxes()
                        delattr(self, '_downloading_rom_ids')
                    
                self.log_message(f"✅ Bulk download complete ({download_count} games)")
                delattr(self, '_bulk_download_remaining')  # Clean up
                return False  # Stop checking
            return True  # Continue checking
        
        # Start checking after a short delay to allow initial setup
        GLib.timeout_add(500, check_completion)

    def delete_multiple_games(self, games):
        """Delete multiple games with confirmation"""
        count = len(games)
        games_to_delete = list(games)
        
        # SAVE SELECTION STATE BEFORE DIALOG
        if hasattr(self, 'library_section'):
            saved_rom_ids = self.library_section.selected_rom_ids.copy()
            saved_game_keys = self.library_section.selected_game_keys.copy()
            saved_checkboxes = self.library_section.selected_checkboxes.copy()
            saved_selected_game = self.library_section.selected_game
        
        # BLOCK ALL UPDATES
        self._dialog_open = True
        if hasattr(self, 'library_section'):
            self.library_section._block_selection_updates(True)
        
        dialog = Adw.MessageDialog.new(self)
        dialog.set_heading(f"Delete {count} Games?")
        dialog.set_body(f"Are you sure you want to delete {count} selected games?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete Selected")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        
        def on_response(dialog, response):
            self._dialog_open = False
            if hasattr(self, 'library_section'):
                self.library_section._block_selection_updates(False)
                
                if response == "delete":
                    for game in games_to_delete:
                        self.delete_game_file(game, is_bulk_operation=True)
                    self.library_section.clear_checkbox_selections_smooth()
                else:
                    # RESTORE SELECTION STATE ON CANCEL
                    self.library_section.selected_rom_ids = saved_rom_ids
                    self.library_section.selected_game_keys = saved_game_keys
                    self.library_section.selected_checkboxes = saved_checkboxes
                    self.library_section.selected_game = saved_selected_game
                    
                    # UPDATE UI TO REFLECT RESTORED STATE
                    self.library_section.update_action_buttons()
                    self.library_section.update_selection_label()
        
        dialog.connect('response', on_response)
        dialog.present()

    def delete_game_file(self, game, is_bulk_operation=False):
        """Actually delete the game file"""
        def delete():
            try:
                # Get game name early for logging
                game_name = game.get('name', 'Unknown Game')
                local_path = game.get('local_path')
                
                if not local_path:
                    GLib.idle_add(lambda n=game_name: 
                                self.log_message(f"No local path for {n}"))
                    return
                    
                game_path = Path(local_path)
                
                GLib.idle_add(lambda n=game_name: 
                            self.log_message(f"Deleting {n}..."))
                
                if game_path.exists():
                    game_path.unlink()  # Delete the file
                    
                    # Update game data
                    game['is_downloaded'] = False
                    game['local_path'] = None
                    game['local_size'] = 0
                    
                    GLib.idle_add(lambda n=game_name: 
                                self.log_message(f"✓ Deleted {n}"))
                    
                    # If not connected to RomM, remove the game entirely from the list
                    if not (self.romm_client and self.romm_client.authenticated):
                        def remove_from_list():
                            try:
                                if hasattr(self, 'available_games') and game in self.available_games:
                                    self.available_games.remove(game)
                                
                                # Refresh the entire library to remove the item
                                if hasattr(self, 'library_section'):
                                    self.library_section.update_games_library(self.available_games)
                            except Exception as e:
                                print(f"Error removing game from list: {e}")
                        
                        GLib.idle_add(remove_from_list)
                    else:
                        # Connected to RomM - just update the single item
                        GLib.idle_add(lambda: self.library_section.update_single_game(game) if hasattr(self, 'library_section') else self.refresh_games_list())

                    # Only clear selections after an individual (non-bulk) deletion.
                    if not is_bulk_operation:
                        GLib.idle_add(lambda: self.library_section.clear_checkbox_selections_smooth() if hasattr(self, 'library_section') else None)
                    
                    # Try to remove empty platform directory
                    try:
                        platform_dir = game_path.parent
                        if platform_dir.exists() and not any(platform_dir.iterdir()):
                            platform_dir.rmdir()
                            GLib.idle_add(lambda d=platform_dir.name: 
                                        self.log_message(f"Removed empty directory: {d}"))
                    except:
                        pass  # Directory not empty or other error, ignore
                        
                else:
                    GLib.idle_add(lambda n=game_name: 
                                self.log_message(f"File not found: {n}"))
                
            except Exception as e:
                # Make sure game_name is available here too
                name = game.get('name', 'Unknown Game')
                GLib.idle_add(lambda err=str(e), n=name: 
                            self.log_message(f"Error deleting {n}: {err}"))
        
        threading.Thread(target=delete, daemon=True).start()
    
    def on_game_action_clicked(self, button):
        """Handle download or launch action based on game status"""
        selected_game = self.get_selected_game()
        if not selected_game:
            self.log_message("No game selected")
            return
        
        if selected_game['is_downloaded']:
            # Launch the game
            self.launch_game(selected_game)
        else:
            # Download the game
            self.download_game(selected_game)
    
    def launch_game(self, game):
        """Launch a downloaded game in RetroArch"""
        def launch():
            try:
                game_name = game['name']
                game_path = Path(game['local_path'])
                platform = game['platform']
                
                GLib.idle_add(lambda n=game_name, p=platform: 
                             self.log_message(f"Launching {n} ({p})..."))
                
                if (hasattr(self, 'auto_sync') and 
                    self.auto_sync.enabled and 
                    self.auto_sync.download_enabled):
                    self.auto_sync.sync_before_launch(game)

                # Launch the game
                success, message = self.retroarch.launch_game(game_path, platform)
                
                if success:
                    GLib.idle_add(lambda m=message: self.log_message(f"✓ {m}"))
                else:
                    GLib.idle_add(lambda m=message: self.log_message(f"✗ Launch failed: {m}"))
                
            except Exception as e:
                GLib.idle_add(lambda err=str(e): self.log_message(f"Launch error: {err}"))
        
        threading.Thread(target=launch, daemon=True).start()

    def on_window_close(self):
        """Clean up when window closes"""
        if hasattr(self, 'auto_sync'):
            self.auto_sync.stop_auto_sync()
        return False  # Allow window to close

    def on_window_close_request(self, _window):
        """Overrides the default window close action.
        
        Instead of quitting, this will just hide the window to the tray.
        The actual quit logic is now handled by the StatusNotifierItem class.
        """
        self.set_visible(False)
        # Return True to prevent the window from being destroyed
        return True

    def download_game(self, game, is_bulk_operation=False):     
        """Download a single game from RomM and its saves (with throttled progress)"""
        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("Please connect to RomM first")
            return
        
        def download():
            try:
                rom_name = game['name']
                rom_id = game['rom_id']
                platform = game['platform']
                platform_slug = game.get('platform_slug', platform)
                file_name = game['file_name']
                
                # Track current download for progress updates
                self._current_download_rom_id = rom_id
                
                # Initialize progress and throttling for this game
                self.download_progress[rom_id] = {
                    'progress': 0.0,
                    'downloading': True,
                    'filename': rom_name,
                    'speed': 0,
                    'downloaded': 0,
                    'total': 0
                }
                self._last_progress_update[rom_id] = 0  # Reset throttling
                
                # Update tree view to show download starting
                GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id]) 
                            if hasattr(self, 'library_section') else None)
                
                GLib.idle_add(lambda n=rom_name, p=platform: 
                            self.log_message(f"Downloading {n} ({p})..."))
                
                # Get download directory and create platform directory
                download_dir = Path(self.rom_dir_row.get_text())
                platform_dir = download_dir / platform_slug
                platform_dir.mkdir(parents=True, exist_ok=True)
                download_path = platform_dir / file_name
                
                # Log file size for large downloads
                try:
                    # Try to get file size from ROM data
                    romm_data = game.get('romm_data', {})
                    expected_size = romm_data.get('fs_size_bytes', 0)
                    if expected_size > 1024 * 1024 * 1024:  # > 1GB
                        size_gb = expected_size / (1024 * 1024 * 1024)
                        GLib.idle_add(lambda s=size_gb: 
                                    self.log_message(f"⚠️ Large file download: {s:.1f} GB - this may take a while"))
                except:
                    pass
                
                # Download with throttled progress tracking
                success, message = self.romm_client.download_rom(
                    rom_id, rom_name, download_path, self.update_download_progress
                )
                
                if success:
                    # Mark download complete
                    file_size = download_path.stat().st_size if download_path.exists() else 0
                    self.download_progress[rom_id] = {
                        'progress': 1.0,
                        'downloading': False,
                        'completed': True,
                        'filename': rom_name,
                        'downloaded': file_size,
                        'total': file_size
                    }
                    
                    # Force final update
                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id]) 
                                if hasattr(self, 'library_section') else None)
                    
                    # Rest of success handling...
                    if download_path.exists():
                        size_str = f"{file_size / (1024*1024*1024):.1f} GB" if file_size > 1024*1024*1024 else f"{file_size / (1024*1024):.1f} MB" if file_size > 1024*1024 else f"{file_size / 1024:.1f} KB"
                        
                        GLib.idle_add(lambda n=rom_name, s=size_str: 
                                    self.log_message(f"✓ Downloaded {n} ({s})"))
                        
                        # Update game data
                        game['is_downloaded'] = True
                        game['local_path'] = str(download_path)
                        game['local_size'] = file_size
                        
                        # Update UI
                        GLib.idle_add(lambda: self.library_section.update_single_game(game, skip_platform_update=is_bulk_operation) if hasattr(self, 'library_section') else self.refresh_games_list())

                        # Bulk operation handling
                        if is_bulk_operation and hasattr(self, 'library_section'):
                            def update_bulk_progress():
                                if hasattr(self, '_bulk_download_remaining'):
                                    self._bulk_download_remaining -= 1
                                    remaining = self._bulk_download_remaining
                                    
                                    if remaining > 0:
                                        GLib.idle_add(lambda r=remaining: 
                                            self.library_section.selection_label.set_text(f"{r} downloads remaining") 
                                            if hasattr(self.library_section, 'selection_label') else None)
                                    else:
                                        GLib.idle_add(lambda: 
                                            self.library_section.selection_label.set_text("Downloads complete") 
                                            if hasattr(self.library_section, 'selection_label') else None)
                            
                            GLib.idle_add(update_bulk_progress)

                        # Clear checkbox selections for individual downloads, but preserve row selections
                        if not is_bulk_operation and hasattr(self, 'library_section'):
                            def clear_only_checkboxes():
                                # Only clear checkbox selections if there's no row selection
                                # If user clicked on a row and downloaded, they probably want to keep it selected to launch
                                if not self.library_section.selected_game:
                                    self.library_section.clear_checkbox_selections_smooth()
                                else:
                                    # Just clear checkboxes but keep the row selection
                                    self.library_section.selected_checkboxes.clear()
                                    self.library_section.selected_rom_ids.clear()
                                    self.library_section.selected_game_keys.clear()
                                    # Update UI to reflect cleared checkboxes but keep row selection
                                    self.library_section.update_action_buttons()
                                    self.library_section.update_selection_label()
                                    GLib.idle_add(self.library_section.force_checkbox_sync)
                            
                            GLib.idle_add(clear_only_checkboxes)

                        # Download saves
                        self.download_saves_for_game(game)
                        
                        if file_size >= 1024:
                            GLib.idle_add(lambda n=rom_name: self.log_message(f"✓ {n} ready to play"))
                
                else:
                    # Mark download failed
                    self.download_progress[rom_id] = {
                        'progress': 0.0,
                        'downloading': False,
                        'failed': True,
                        'filename': rom_name
                    }
                    
                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id]) 
                                if hasattr(self, 'library_section') else None)
                    
                    GLib.idle_add(lambda n=rom_name, m=message: 
                                self.log_message(f"✗ Failed to download {n}: {m}"))
                
                # Clean up progress and throttling data
                def cleanup_progress():
                    time.sleep(3)  # Show completed/failed state for 3 seconds
                    if rom_id in self.download_progress:
                        del self.download_progress[rom_id]
                    if rom_id in self._last_progress_update:
                        del self._last_progress_update[rom_id]
                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, None)
                                if hasattr(self, 'library_section') else None)
                
                threading.Thread(target=cleanup_progress, daemon=True).start()
                
            except Exception as e:
                # Handle error state
                if hasattr(self, '_current_download_rom_id'):
                    rom_id = self._current_download_rom_id
                    self.download_progress[rom_id] = {
                        'progress': 0.0,
                        'downloading': False,
                        'failed': True,
                        'filename': game.get('name', 'Unknown')
                    }
                    # Clean up throttling data on error
                    if rom_id in self._last_progress_update:
                        del self._last_progress_update[rom_id]
                        
                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                                if hasattr(self, 'library_section') else None)
                
                GLib.idle_add(lambda err=str(e), n=game['name']: 
                            self.log_message(f"Download error for {n}: {err}"))
        
        threading.Thread(target=download, daemon=True).start()

    def remove_game_from_selection(self, game):
        """Remove a specific game from all selection tracking structures"""
        if not hasattr(self, 'library_section'):
            return
        
        library_section = self.library_section
        
        # Get the game's identifier for tracking removal
        identifier_type, identifier_value = library_section.get_game_identifier(game)
        
        # Remove from ROM ID or game key tracking
        if identifier_type == 'rom_id':
            library_section.selected_rom_ids.discard(identifier_value)
        elif identifier_type == 'game_key':
            library_section.selected_game_keys.discard(identifier_value)
        
        # Remove from GameItem tracking (find matching GameItem)
        items_to_remove = []
        for game_item in library_section.selected_checkboxes:
            if game_item.game_data.get('rom_id') == game.get('rom_id') and game.get('rom_id'):
                items_to_remove.append(game_item)
            elif (game_item.game_data.get('name') == game.get('name') and 
                game_item.game_data.get('platform') == game.get('platform')):
                items_to_remove.append(game_item)
        
        for item in items_to_remove:
            library_section.selected_checkboxes.discard(item)
        
        # Update UI to reflect new selection state
        library_section.update_action_buttons()
        library_section.update_selection_label()
        
        # Decrement bulk download counter if it exists
        if hasattr(self, '_bulk_download_remaining'):
            self._bulk_download_remaining -= 1

    def download_saves_for_game(self, game):
        """Downloads save file and state for a specific game after ROM download"""
        rom_id = game['rom_id']
        rom_data = game.get('romm_data', {})
        
        # Get ROM details to find save/state files with emulator info
        try:
            rom_details_response = self.romm_client.session.get(
                urljoin(self.romm_client.base_url, f'/api/roms/{rom_id}'),
                timeout=10
            )
            
            if rom_details_response.status_code != 200:
                GLib.idle_add(lambda: self.log_message(f"Could not get ROM details for save download"))
                return
            
            rom_details = rom_details_response.json()
            
            # Try to download saves
            if 'saves' in self.retroarch.save_dirs:
                save_base_dir = self.retroarch.save_dirs['saves']
                user_saves = rom_details.get('user_saves', [])
                
                if user_saves:
                    GLib.idle_add(lambda: self.log_message(f"Found {len(user_saves)} save file(s)"))
                    
                    for save_file in user_saves:
                        if isinstance(save_file, dict):
                            original_filename = save_file.get('file_name', '')
                            romm_emulator = save_file.get('emulator', 'unknown')
                            
                            if original_filename:
                                # Convert RomM emulator name to RetroArch directory name
                                retroarch_emulator_dir = self.retroarch.get_retroarch_directory_name(romm_emulator)
                                
                                if retroarch_emulator_dir:
                                    # Create RetroArch-compatible emulator directory
                                    emulator_save_dir = save_base_dir / retroarch_emulator_dir
                                    emulator_save_dir.mkdir(parents=True, exist_ok=True)
                                    
                                    # Convert filename to RetroArch format
                                    retroarch_filename = self.retroarch.convert_to_retroarch_filename(
                                        original_filename, 'saves', emulator_save_dir
                                    )
                                    
                                    # Download to temporary location first
                                    temp_path = emulator_save_dir / original_filename
                                    final_path = emulator_save_dir / retroarch_filename
                                    
                                    GLib.idle_add(lambda f=original_filename, rf=retroarch_filename, e=romm_emulator: 
                                                self.log_message(f"Downloading save: {f} → {rf} ({e})"))
                                    
                                    # Download with original filename
                                    if self.romm_client.download_save(rom_id, 'saves', temp_path):
                                        # Rename to RetroArch format
                                        try:
                                            if temp_path != final_path:
                                                if final_path.exists():
                                                    final_path.unlink()  # Remove existing file
                                                temp_path.rename(final_path)
                                                
                                            final_relative_path = final_path.relative_to(save_base_dir)
                                            GLib.idle_add(lambda p=str(final_relative_path): 
                                                        self.log_message(f"✓ Save file ready: {p}"))
                                        except Exception as e:
                                            GLib.idle_add(lambda err=str(e): 
                                                        self.log_message(f"✗ Failed to rename save file: {err}"))
                                    else:
                                        GLib.idle_add(lambda f=original_filename: 
                                                    self.log_message(f"✗ Failed to download save file: {f}"))
                                else:
                                    GLib.idle_add(lambda e=romm_emulator: 
                                                self.log_message(f"⚠ Unknown emulator mapping for '{e}'"))
                else:
                    GLib.idle_add(lambda: self.log_message(f"No save files found on server"))

            # Try to download states
            if 'states' in self.retroarch.save_dirs:
                state_base_dir = self.retroarch.save_dirs['states']
                user_states = rom_details.get('user_states', [])
                
                if user_states:
                    GLib.idle_add(lambda: self.log_message(f"Found {len(user_states)} save state(s)"))
                    
                    for state_file in user_states:
                        if isinstance(state_file, dict):
                            original_filename = state_file.get('file_name', '')
                            romm_emulator = state_file.get('emulator', 'unknown')
                            
                            if original_filename:
                                # Convert RomM emulator name to RetroArch directory name
                                retroarch_emulator_dir = self.retroarch.get_retroarch_directory_name(romm_emulator)
                                
                                if retroarch_emulator_dir:
                                    # Create RetroArch-compatible emulator directory
                                    emulator_state_dir = state_base_dir / retroarch_emulator_dir
                                    emulator_state_dir.mkdir(parents=True, exist_ok=True)
                                    
                                    # Convert filename to RetroArch format
                                    retroarch_filename = self.retroarch.convert_to_retroarch_filename(
                                        original_filename, 'states', emulator_state_dir
                                    )
                                    
                                    # Download to temporary location first
                                    temp_path = emulator_state_dir / original_filename
                                    final_path = emulator_state_dir / retroarch_filename
                                    
                                    GLib.idle_add(lambda f=original_filename, rf=retroarch_filename, e=romm_emulator: 
                                                self.log_message(f"Downloading state: {f} → {rf} ({e})"))
                                    
                                    # Download with original filename
                                    if self.romm_client.download_save(rom_id, 'states', temp_path):
                                        # Rename to RetroArch format
                                        try:
                                            if temp_path != final_path:
                                                if final_path.exists():
                                                    # Backup existing state
                                                    backup_path = final_path.with_suffix(final_path.suffix + '.backup')
                                                    if backup_path.exists():
                                                        backup_path.unlink()
                                                    final_path.rename(backup_path)
                                                    GLib.idle_add(lambda: self.log_message(f"  Backed up existing state"))
                                                
                                                temp_path.rename(final_path)
                                                
                                            final_relative_path = final_path.relative_to(state_base_dir)
                                            GLib.idle_add(lambda p=str(final_relative_path): 
                                                        self.log_message(f"✓ Save state ready: {p}"))
                                        except Exception as e:
                                            GLib.idle_add(lambda err=str(e): 
                                                        self.log_message(f"✗ Failed to rename state file: {err}"))
                                    else:
                                        GLib.idle_add(lambda f=original_filename: 
                                                    self.log_message(f"✗ Failed to download state file: {f}"))
                                else:
                                    GLib.idle_add(lambda e=romm_emulator: 
                                                self.log_message(f"⚠ Unknown emulator mapping for '{e}'"))
                else:
                    GLib.idle_add(lambda: self.log_message(f"No save states found on server"))
                    
        except Exception as e:
            GLib.idle_add(lambda err=str(e): self.log_message(f"Error downloading saves/states: {err}"))
    
    def on_sync_to_romm(self, button):
        """Upload local saves from RetroArch to RomM using NEW method."""
        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("Please connect to RomM first")
            return
        
        if not self.available_games:
            self.log_message("Game library not loaded. Cannot match saves. Please refresh.")
            return

        def sync():
            try:
                GLib.idle_add(lambda: self.log_message("🚀 Starting upload using NEW thumbnail method..."))
                
                # Create mapping from 'fs_name_no_ext' to rom_id for more reliable matching.
                rom_map = {}
                for game in self.available_games:
                    if game.get('rom_id') and game.get('romm_data'):
                        basename = game['romm_data'].get('fs_name_no_ext')
                        if basename:
                            rom_map[basename] = game['rom_id']

                if not rom_map:
                    GLib.idle_add(lambda: self.log_message("Could not create a map of games from RomM library."))
                    return

                local_saves = self.retroarch.get_save_files()
                total_files = sum(len(files) for files in local_saves.values())
                
                if total_files == 0:
                    GLib.idle_add(lambda: self.log_message("No local save files found to upload."))
                    return

                GLib.idle_add(lambda: self.log_message(f"Found {total_files} local save/state files to check."))
                
                uploaded_count = 0
                unmatched_count = 0

                for save_type, files in local_saves.items(): # 'saves' or 'states'
                    for save_file in files:
                        save_name = save_file['name']
                        save_path = save_file['path']
                        emulator = save_file.get('emulator', 'unknown')
                        relative_path = save_file.get('relative_path', save_name)
                        
                        # Match by filename stem (e.g., "Test.srm" -> "Test")
                        save_basename = Path(save_name).stem
                        
                        # Try to extract a cleaner basename by removing timestamps and brackets
                        import re
                        clean_basename = re.sub(r'\s*\[.*?\]', '', save_basename)  # Remove [timestamp] parts
                        
                        rom_id = rom_map.get(save_basename) or rom_map.get(clean_basename)
                        
                        if rom_id:
                            # Look for thumbnail if it's a save state
                            thumbnail_path = None
                            if save_type == 'states':
                                thumbnail_path = self.retroarch.find_thumbnail_for_save_state(save_path)
                            
                            # Always use the new upload method (with or without thumbnail)
                            if emulator:
                                GLib.idle_add(lambda n=save_name, e=emulator: 
                                            self.log_message(f"  📤 Uploading {n} ({e}) using NEW method..."))
                            else:
                                GLib.idle_add(lambda n=save_name: 
                                            self.log_message(f"  📤 Uploading {n} using NEW method..."))
                            
                            # Use NEW method for all uploads
                            success = self.romm_client.upload_save_with_thumbnail(rom_id, save_type, save_path, thumbnail_path, emulator)
                            
                            if success:
                                if thumbnail_path:
                                    if emulator:
                                        GLib.idle_add(lambda n=save_name, e=emulator: 
                                                    self.log_message(f"  ✅ Successfully uploaded {n} with screenshot 📸 ({e})"))
                                    else:
                                        GLib.idle_add(lambda n=save_name: 
                                                    self.log_message(f"  ✅ Successfully uploaded {n} with screenshot 📸"))
                                else:
                                    if emulator:
                                        GLib.idle_add(lambda n=save_name, e=emulator: 
                                                    self.log_message(f"  ✅ Successfully uploaded {n} ({e})"))
                                    else:
                                        GLib.idle_add(lambda n=save_name: 
                                                    self.log_message(f"  ✅ Successfully uploaded {n}"))
                                uploaded_count += 1
                            else:
                                GLib.idle_add(lambda n=save_name: 
                                            self.log_message(f"  ❌ Failed to upload {n}"))
                        else:
                            unmatched_count += 1
                            location_info = f" ({relative_path})" if relative_path != save_name else ""
                            GLib.idle_add(lambda n=save_name, loc=location_info: 
                                        self.log_message(f"  - Could not match local file '{n}'{loc}, skipping."))
                
                GLib.idle_add(lambda: self.log_message("-" * 20))
                GLib.idle_add(lambda u=uploaded_count, t=total_files, m=unmatched_count:
                            self.log_message(f"Sync complete. Uploaded {u}/{t-m} matched files. ({m} unmatched)"))

            except Exception as e:
                GLib.idle_add(lambda err=str(e): self.log_message(f"An error occurred during save sync: {err}"))

        threading.Thread(target=sync, daemon=True).start()

    def on_clear_cache(self, button):
        """Clear cached game data"""
        if hasattr(self, 'game_cache'):
            self.game_cache.clear_cache()
            self.log_message("🗑️ Game data cache cleared")
            self.log_message("💡 Reconnect to RomM to rebuild cache")
        else:
            self.log_message("❌ No cache to clear")

    def on_debug_api(self, button):
        """Debug RomM API endpoints and structure, including save/state investigation"""
        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("Please connect to RomM first")
            return
        
        def debug():
            try:
                self.log_message("=== Debugging RomM API & Save States ===")
                
                # First, find a ROM with saves/states to investigate
                roms = self.romm_client.get_roms()
                test_rom_id = None
                
                if roms and len(roms) > 0:
                    # Look for ROM with ID 37 or first available
                    for rom in roms:
                        if rom.get('id') == 37:
                            test_rom_id = 37
                            break
                    
                    if not test_rom_id:
                        test_rom_id = roms[0].get('id')
                    
                    GLib.idle_add(lambda id=test_rom_id: 
                                 self.log_message(f"🎮 Testing with ROM ID: {id}"))
                    
                    # Check what save/state data looks like
                    save_endpoints = [
                        f'/api/roms/{test_rom_id}/states',
                        f'/api/roms/{test_rom_id}/saves', 
                        f'/api/states?rom_id={test_rom_id}',
                        f'/api/saves?rom_id={test_rom_id}',
                        f'/api/roms/{test_rom_id}',  # Full ROM details
                    ]
                    
                    for endpoint in save_endpoints:
                        try:
                            response = self.romm_client.session.get(
                                urljoin(self.romm_client.base_url, endpoint),
                                timeout=10
                            )
                            
                            GLib.idle_add(lambda ep=endpoint, code=response.status_code: 
                                         self.log_message(f"📡 {ep} -> {code}"))
                            
                            if response.status_code == 200:
                                try:
                                    data = response.json()
                                    
                                    if isinstance(data, list) and data:
                                        # List of saves/states
                                        save_item = data[0]  # Look at first item
                                        GLib.idle_add(lambda: self.log_message(f"📄 First save/state structure:"))
                                        
                                        # Look for thumbnail-related fields
                                        thumbnail_fields = []
                                        for key, value in save_item.items():
                                            if any(thumb_keyword in key.lower() for thumb_keyword in ['thumb', 'image', 'screenshot', 'preview', 'picture']):
                                                thumbnail_fields.append(f"{key}: {value}")
                                            GLib.idle_add(lambda k=key, v=str(value)[:100]: 
                                                         self.log_message(f"  {k}: {v}"))
                                        
                                        if thumbnail_fields:
                                            GLib.idle_add(lambda: self.log_message("🖼️ Thumbnail-related fields found:"))
                                            for field in thumbnail_fields:
                                                GLib.idle_add(lambda f=field: self.log_message(f"  📸 {f}"))
                                        else:
                                            GLib.idle_add(lambda: self.log_message("❌ No thumbnail fields found in save data"))
                                    
                                    elif isinstance(data, dict):
                                        # Single object or wrapper
                                        if 'user_states' in data or 'user_saves' in data:
                                            # ROM details with nested saves
                                            states = data.get('user_states', [])
                                            saves = data.get('user_saves', [])
                                            
                                            GLib.idle_add(lambda s=len(states), sv=len(saves): 
                                                         self.log_message(f"📄 ROM has {s} states, {sv} saves"))
                                            
                                            if states:
                                                state = states[0]
                                                GLib.idle_add(lambda: self.log_message("🎮 First state structure:"))
                                                for key, value in state.items():
                                                    GLib.idle_add(lambda k=key, v=str(value)[:100]: 
                                                                 self.log_message(f"  {k}: {v}"))
                                        else:
                                            # Unknown structure
                                            GLib.idle_add(lambda keys=list(data.keys()): 
                                                         self.log_message(f"📄 Data keys: {keys}"))
                                
                                except Exception as json_error:
                                    GLib.idle_add(lambda ep=endpoint, err=str(json_error): 
                                                 self.log_message(f"❌ JSON parse error for {ep}: {err}"))
                                    GLib.idle_add(lambda text=response.text[:200]: 
                                                 self.log_message(f"Raw response: {text}"))
                            
                        except Exception as e:
                            GLib.idle_add(lambda ep=endpoint, err=str(e): 
                                         self.log_message(f"❌ {ep} -> Error: {err}"))
                
                # Check OpenAPI for save/state schemas
                try:
                    GLib.idle_add(lambda: self.log_message("📋 Checking OpenAPI schemas..."))
                    openapi_response = self.romm_client.session.get(
                        urljoin(self.romm_client.base_url, '/openapi.json'),
                        timeout=10
                    )
                    
                    if openapi_response.status_code == 200:
                        openapi_spec = openapi_response.json()
                        components = openapi_spec.get('components', {})
                        schemas = components.get('schemas', {})
                        
                        # Look for save/state schemas
                        relevant_schemas = {}
                        for schema_name, schema_def in schemas.items():
                            if any(keyword in schema_name.lower() for keyword in ['save', 'state', 'thumbnail', 'image']):
                                relevant_schemas[schema_name] = schema_def
                        
                        if relevant_schemas:
                            GLib.idle_add(lambda: self.log_message("📋 Found relevant schemas:"))
                            for schema_name, schema_def in relevant_schemas.items():
                                properties = schema_def.get('properties', {})
                                GLib.idle_add(lambda name=schema_name: self.log_message(f"  📄 {name}:"))
                                
                                for prop_name, prop_def in properties.items():
                                    prop_type = prop_def.get('type', 'unknown')
                                    GLib.idle_add(lambda pn=prop_name, pt=prop_type: 
                                                 self.log_message(f"    {pn}: {pt}"))
                        else:
                            GLib.idle_add(lambda: self.log_message("❌ No save/state schemas found in OpenAPI"))
                
                except Exception as e:
                    GLib.idle_add(lambda err=str(e): self.log_message(f"OpenAPI check failed: {err}"))
                
                GLib.idle_add(lambda: self.log_message("=== Debug complete ==="))
                GLib.idle_add(lambda: self.log_message("💡 Check browser DevTools Network tab when viewing saves page"))
                
            except Exception as e:
                GLib.idle_add(lambda err=str(e): self.log_message(f"Debug error: {err}"))
        
        threading.Thread(target=debug, daemon=True).start()
    
    def on_browse_downloads(self, button):
        """Open the download directory in file manager"""
        download_dir = Path(self.rom_dir_row.get_text())
        
        if download_dir.exists():
            import subprocess
            try:
                # Try to open with default file manager
                subprocess.run(['xdg-open', str(download_dir)], check=True)
                self.log_message(f"Opened download directory: {download_dir}")
            except Exception as e:
                self.log_message(f"Could not open directory: {e}")
                self.log_message(f"Download directory: {download_dir}")
        else:
            self.log_message(f"Download directory does not exist: {download_dir}")
            self.log_message("Try downloading some ROMs first!")
    
    def on_inspect_downloads(self, button):
        """Inspect downloaded files to check if they're legitimate"""
        download_dir = Path(self.rom_dir_row.get_text())
        
        def inspect():
            try:
                self.log_message("=== Inspecting Downloaded Files ===")
                
                if not download_dir.exists():
                    GLib.idle_add(lambda: self.log_message("Download directory does not exist"))
                    return
                
                file_count = 0
                total_size = 0
                
                # Recursively find all files
                for file_path in download_dir.rglob('*'):
                    if file_path.is_file():
                        file_count += 1
                        file_size = file_path.stat().st_size
                        total_size += file_size
                        
                        # Format size
                        if file_size > 1024 * 1024:
                            size_str = f"{file_size / (1024 * 1024):.1f} MB"
                        elif file_size > 1024:
                            size_str = f"{file_size / 1024:.1f} KB"
                        else:
                            size_str = f"{file_size} bytes"
                        
                        relative_path = file_path.relative_to(download_dir)
                        GLib.idle_add(lambda p=str(relative_path), s=size_str: 
                                     self.log_message(f"  {p} - {s}"))
                        
                        # Check if suspiciously small
                        if file_size < 1024:
                            try:
                                # Try to read as text to see if it's an error page
                                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                                    content = f.read()[:200]  # First 200 chars
                                    
                                if any(keyword in content.lower() for keyword in ['html', 'error', '404', 'not found', 'unauthorized']):
                                    GLib.idle_add(lambda p=str(relative_path): 
                                                 self.log_message(f"    ⚠ {p} appears to be an error page"))
                                    GLib.idle_add(lambda c=content[:100]: 
                                                 self.log_message(f"    Content: {c}..."))
                                else:
                                    GLib.idle_add(lambda p=str(relative_path): 
                                                 self.log_message(f"    ✓ {p} appears to be binary data"))
                            except:
                                GLib.idle_add(lambda p=str(relative_path): 
                                             self.log_message(f"    ✓ {p} is binary (good sign)"))
                
                # Summary
                if file_count > 0:
                    total_mb = total_size / (1024 * 1024)
                    GLib.idle_add(lambda c=file_count, s=total_mb: 
                                 self.log_message(f"Total: {c} files, {s:.1f} MB"))
                else:
                    GLib.idle_add(lambda: self.log_message("No files found in download directory"))
                
                GLib.idle_add(lambda: self.log_message("=== Inspection complete ==="))
                
            except Exception as e:
                GLib.idle_add(lambda err=str(e): self.log_message(f"Inspection error: {err}"))
        
        threading.Thread(target=inspect, daemon=True).start()

class SyncApp(Adw.Application):
    """Main application class"""
    
    def __init__(self):
        super().__init__(application_id='com.romm.retroarch.sync')  # Also update app ID
        self.connect('activate', self.on_activate)
        self.connect('shutdown', self.on_shutdown)  # Add this line
    
    def on_activate(self, app):
        """Application activation handler"""
        # Only create window if it doesn't exist
        windows = self.get_windows()
        if windows:
            windows[0].present()
        else:
            win = SyncWindow(application=app)
            win.present()
    
    def on_shutdown(self, app):  # Add this method
        """Clean up before shutdown"""
        print("🚪 Application shutting down...")
        for window in self.get_windows():
            if hasattr(window, 'tray'):
                window.tray.cleanup()

class AutoSyncManager:
    """Manages automatic synchronization of saves/states between RetroArch and RomM"""
    
    def __init__(self, romm_client, retroarch, settings, log_callback, get_games_callback, parent_window=None):
        self.romm_client = romm_client
        self.retroarch = retroarch
        self.settings = settings
        self.log = log_callback
        self.get_games = get_games_callback  # Function to get current games list
        self.parent_window = parent_window
        
        # Auto-sync state
        self.enabled = False
        self.upload_enabled = True
        self.download_enabled = True
        self.upload_delay = 3  # Configurable delay
        
        # File monitoring
        self.observer = None
        self.upload_queue = queue.Queue()
        self.upload_debounce = defaultdict(float)  # file_path -> last_change_time
        
        # Game session tracking
        self.current_game = None
        self.last_sync_time = {}  # game_id -> timestamp
        self.should_stop = threading.Event()
        
        # Upload worker thread
        self.upload_worker = None
        
    def start_auto_sync(self):
        """Start all auto-sync components"""
        if self.enabled:
            self.log("Auto-sync already running")
            return
            
        self.enabled = True
        self.should_stop.clear()
        
        try:
            # Start file system monitoring
            self.start_file_monitoring()
            
            # Start upload worker
            self.start_upload_worker()
            
            self.log("🔄 Auto-sync started (file monitoring + RetroArch status)")
            
        except Exception as e:
            self.log(f"❌ Failed to start auto-sync: {e}")
            self.stop_auto_sync()

    def stop_auto_sync(self):
        """Stop all auto-sync components"""
        if not self.enabled:
            return
            
        self.enabled = False
        self.should_stop.set()
        
        # Stop file monitoring
        if self.observer:
            self.observer.stop()
            self.observer.join()
            self.observer = None
        
        # Stop upload worker
        if self.upload_worker and self.upload_worker.is_alive():
            self.upload_worker.join(timeout=2)
        
        self.log("⏹️ Auto-sync stopped")
    
    def start_file_monitoring(self):
        """Start monitoring RetroArch save directories for file changes"""
        if not self.retroarch.save_dirs:
            self.log("⚠️ No RetroArch save directories found")
            return
        
        self.observer = Observer()
        
        for save_type, directory in self.retroarch.save_dirs.items():
            if directory.exists():
                handler = SaveFileHandler(self.on_save_file_changed, save_type)
                self.observer.schedule(handler, str(directory), recursive=True)
                self.log(f"📁 Monitoring {save_type}: {directory}")
        
        self.observer.start()
    
    def start_upload_worker(self):
        """Start background thread to process upload queue"""
        def upload_worker():
            while not self.should_stop.is_set():
                try:
                    # Process pending uploads with debouncing
                    current_time = time.time()
                    uploads_to_process = []
                    
                    for file_path, change_time in list(self.upload_debounce.items()):
                        # If file hasn't changed for upload_delay seconds, upload it
                        if current_time - change_time >= self.upload_delay:
                            uploads_to_process.append(file_path)
                            del self.upload_debounce[file_path]
                    
                    for file_path in uploads_to_process:
                        self.process_save_upload(file_path)
                    
                    time.sleep(1)  # Check every second
                    
                except Exception as e:
                    self.log(f"Upload worker error: {e}")
                    time.sleep(5)  # Back off on error
        
        self.upload_worker = threading.Thread(target=upload_worker, daemon=True)
        self.upload_worker.start()
    
    def on_save_file_changed(self, file_path, save_type):
        """Handle save file change detected by file system monitor"""
        if not self.upload_enabled or not self.romm_client or not self.romm_client.authenticated:
            return
            
        # Update debounce time
        self.upload_debounce[file_path] = time.time()
        
        # Log the detection (but not the upload yet - that's debounced)
        relative_path = Path(file_path).name
        self.log(f"📝 Detected change: {relative_path}")
    
    def process_save_upload(self, file_path):
        """Process a queued save file upload with smart timestamp comparison using NEW method"""
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                return
            
            # Find matching ROM for this save file
            rom_id = self.find_rom_id_for_save_file(file_path)
            if not rom_id:
                self.log(f"⚠️ No matching ROM found for {file_path.name}")
                return
            
            # Determine save type
            if file_path.suffix.lower() in ['.srm', '.sav']:
                save_type = 'saves'
                thumbnail_path = None  # Save files typically don't have thumbnails
            elif 'state' in file_path.suffix.lower():
                save_type = 'states'
                # Look for thumbnail for save states
                thumbnail_path = self.retroarch.find_thumbnail_for_save_state(file_path)
            else:
                self.log(f"⚠️ Unknown save file type: {file_path.suffix}")
                return

            # SMART TIMESTAMP COMPARISON
            local_mtime = file_path.stat().st_mtime
            server_timestamp = self.get_server_save_timestamp(rom_id, save_type)
            
            if server_timestamp and server_timestamp > local_mtime + 60:  # Server is >1min newer
                self.log(f"⏭️ Server has newer {file_path.name}, skipping upload")
                return
            elif server_timestamp:
                time_diff = local_mtime - server_timestamp
                if time_diff > 60:  # Local is >1min newer
                    self.log(f"📊 Local {file_path.name} is {time_diff:.1f}s newer, uploading...")
                else:
                    self.log(f"⚖️ {file_path.name} timestamps similar, uploading anyway...")

            # Find emulator from file path
            emulator = None
            if file_path.parent.name not in ['saves', 'states']:
                # File is in emulator subdirectory - use the directory name directly
                emulator = file_path.parent.name.lower().replace(' ', '_')
            
            # Upload the file with thumbnail and emulator info
            if thumbnail_path:
                self.log(f"⬆️ Auto-uploading {file_path.name} with screenshot...")
            else:
                self.log(f"⬆️ Auto-uploading {file_path.name}...")
                
            success = self.romm_client.upload_save_with_thumbnail(rom_id, save_type, file_path, thumbnail_path, emulator)
            
            if success:
                if thumbnail_path:
                    msg = f"✅ Auto-uploaded {file_path.name} with screenshot 📸"
                    self.log(msg)
                    self.retroarch.send_notification(f"Save uploaded: {file_path.name}")
                else:
                    msg = f"✅ Auto-uploaded {file_path.name}"
                    self.log(msg)
                    self.retroarch.send_notification(f"Save uploaded: {file_path.name}")
            else:
                msg = f"❌ Failed to auto-upload {file_path.name}"
                self.log(msg)
                self.retroarch.send_notification(f"Upload failed: {file_path.name}")
                
        except Exception as e:
            self.log(f"❌ Upload error for {file_path}: {e}")

    def get_server_save_timestamp(self, rom_id, save_type):
        """Get timestamp of latest save/state on server"""
        try:
            from urllib.parse import urljoin
            import datetime
            
            response = self.romm_client.session.get(
                urljoin(self.romm_client.base_url, f'/api/roms/{rom_id}'),
                timeout=5
            )
            if response.status_code == 200:
                rom_data = response.json()
                files = rom_data.get(f'user_{save_type}', [])
                if files and isinstance(files, list):
                    # Get latest file timestamp
                    latest_timestamp = 0
                    for file_item in files:
                        if isinstance(file_item, dict):
                            for field in ['updated_at', 'created_at']:
                                if field in file_item:
                                    try:
                                        # Parse ISO timestamp
                                        timestamp_str = file_item[field]
                                        if timestamp_str.endswith('Z'):
                                            timestamp_str = timestamp_str.replace('Z', '+00:00')
                                        dt = datetime.datetime.fromisoformat(timestamp_str)
                                        if dt.tzinfo is None:
                                            dt = dt.replace(tzinfo=datetime.timezone.utc)
                                        ts = dt.timestamp()
                                        if ts > latest_timestamp:
                                            latest_timestamp = ts
                                    except:
                                        pass
                    return latest_timestamp if latest_timestamp > 0 else None
        except:
            pass
        return None

    def find_rom_id_for_save_file(self, file_path):
        """Find ROM ID by matching save filename to game library"""
        try:
            games = self.get_games()
            if not games:
                return None
            
            save_basename = file_path.stem
            
            # Remove timestamps and clean up filename
            import re
            clean_basename = re.sub(r'\s*\[.*?\]', '', save_basename)
            
            # Try to match against game library
            for game in games:
                if not game.get('rom_id') or not game.get('romm_data'):
                    continue
                
                # Try exact match with fs_name_no_ext
                rom_data = game['romm_data']
                fs_name_no_ext = rom_data.get('fs_name_no_ext', '')
                
                if fs_name_no_ext and (fs_name_no_ext == save_basename or fs_name_no_ext == clean_basename):
                    return game['rom_id']
                
                # Try fuzzy match (remove region tags)
                clean_game_name = re.sub(r'\s*\(.*?\)', '', fs_name_no_ext).strip()
                clean_save_name = re.sub(r'\s*\(.*?\)', '', clean_basename).strip()
                
                if clean_game_name and clean_game_name.lower() == clean_save_name.lower():
                    return game['rom_id']
            
            return None
            
        except Exception as e:
            self.log(f"ROM matching error: {e}")
            return None
    
    def upload_saves_for_game_session(self, game_name):
        """Upload saves for a game that was just closed"""
        # TODO: Find and upload recent save files for this game
        self.log(f"📤 Checking for saves to upload for {game_name}")
    
    def sync_before_launch(self, game):
        """Sync saves before launching a specific game"""
        if not self.download_enabled or not self.romm_client or not self.romm_client.authenticated:
            return

        try:
            game_name = game.get('name', 'Unknown')
            rom_id = game.get('rom_id')
            
            if rom_id:
                self.log(f"🔄 Pre-launch sync for {game_name}...")
                self.download_saves_for_specific_game(game)
                self.log(f"✅ Pre-launch sync complete for {game_name}")
            else:
                self.log(f"⚠️ No ROM ID available for pre-launch sync of {game_name}")
        
        except Exception as e:
            self.log(f"❌ Pre-launch sync failed for {game.get('name', 'Unknown')}: {e}")

    def download_saves_for_specific_game(self, game):
        """Download only the LATEST saves/states for a specific game from RomM with smart overwrite protection"""
        from gi.repository import GLib, Adw
        
        try:
            from urllib.parse import urljoin
            import datetime
            rom_id = game['rom_id']
            game_name = game.get('name', 'Unknown')
            
            # Get user preference for overwrite behavior
            overwrite_behavior = self.parent_window.get_overwrite_behavior() if self.parent_window else "Smart (prefer newer)"
                
            # Get ROM details
            rom_details_response = self.romm_client.session.get(
                urljoin(self.romm_client.base_url, f'/api/roms/{rom_id}'),
                timeout=10
            )
            
            if rom_details_response.status_code != 200:
                self.log(f"Could not get ROM details for {game_name}")
                return
            
            rom_details = rom_details_response.json()
            downloads_successful = 0
            downloads_attempted = 0
            conflicts_detected = 0
            
            # Helper function to safely parse timestamps
            def parse_timestamp(timestamp_str):
                """Parse various timestamp formats from RomM and return UTC timestamp - FIXED VERSION"""
                if not timestamp_str:
                    return None
                    
                try:
                    import datetime
                    
                    # Parse ISO format with timezone info
                    if timestamp_str.endswith('Z'):
                        clean_timestamp = timestamp_str.replace('Z', '+00:00')
                    else:
                        clean_timestamp = timestamp_str
                        
                    dt = datetime.datetime.fromisoformat(clean_timestamp)
                    
                    # FIXED: Ensure we're working with UTC timestamps consistently
                    if dt.tzinfo is None:
                        # If naive datetime, assume UTC (as most servers store in UTC)
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    
                    # Convert to UTC timestamp
                    return dt.timestamp()
                    
                except Exception as e:
                    self.log(f"🔍 DEBUG: Failed to parse timestamp '{timestamp_str}': {e}")
                    pass
                    
                # Try alternative parsing for RomM filename timestamps
                try:
                    import re
                    import datetime
                    
                    # Extract timestamp from filename like [2025-07-19 13-01-39-957]
                    if '[' in timestamp_str and ']' in timestamp_str:
                        timestamp_match = re.search(r'\[([0-9\-\s:]+)\]', timestamp_str)
                        if timestamp_match:
                            timestamp_str = timestamp_match.group(1)
                    
                    # Convert "2025-07-01 20-32-00-547" format
                    parts = timestamp_str.split()
                    if len(parts) >= 2:
                        date_part = parts[0]  # 2025-07-01
                        time_part = parts[1].replace('-', ':')  # 20:32:00
                        
                        # Handle milliseconds if present
                        if len(parts) > 2:
                            ms_part = parts[2]
                            time_part += f".{ms_part}"
                        
                        full_timestamp = f"{date_part} {time_part}"
                        # FIXED: Parse as UTC time consistently
                        dt = datetime.datetime.strptime(full_timestamp, "%Y-%m-%d %H:%M:%S.%f")
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                        return dt.timestamp()
                        
                except Exception as e:
                    self.log(f"🔍 DEBUG: Failed to parse filename timestamp '{timestamp_str}': {e}")
                    pass
                    
                return None

            def should_download_file(local_path, server_file, file_type):
                """Determine if we should download based on metadata timestamps only"""
                if not local_path.exists():
                    return True, f"Local {file_type} doesn't exist"
                
                if overwrite_behavior == "Always prefer local":
                    return False, f"User preference: always prefer local {file_type}"
                
                if overwrite_behavior == "Always download from server":
                    return True, f"User preference: always download from server"
                
                # Get local file timestamp
                local_mtime = local_path.stat().st_mtime
                local_dt = datetime.datetime.fromtimestamp(local_mtime, tz=datetime.timezone.utc)
                
                # Get server timestamp from API metadata ONLY (ignore filename)
                server_timestamp = None
                for field in ['updated_at', 'created_at', 'modified_at']:
                    if field in server_file and server_file[field]:
                        try:
                            timestamp_str = server_file[field]
                            if timestamp_str.endswith('Z'):
                                timestamp_str = timestamp_str.replace('Z', '+00:00')
                            server_dt = datetime.datetime.fromisoformat(timestamp_str)
                            if server_dt.tzinfo is None:
                                server_dt = server_dt.replace(tzinfo=datetime.timezone.utc)
                            server_timestamp = server_dt.timestamp()
                            break
                        except:
                            continue
                
                if not server_timestamp:
                    self.log(f"  ⚠️ No server metadata timestamp for {file_type} - skipping")
                    return False, f"No server timestamp available"
                
                server_dt = datetime.datetime.fromtimestamp(server_timestamp, tz=datetime.timezone.utc)
                time_diff = (local_dt - server_dt).total_seconds()
                
                local_str = local_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                server_str = server_dt.strftime("%Y-%m-%d %H:%M:%S UTC")
                
                self.log(f"  📊 {file_type.title()} timestamp comparison:")
                self.log(f"     Local:  {local_str}")
                self.log(f"     Server: {server_str}")
                
                if overwrite_behavior == "Smart (prefer newer)":
                    if time_diff >= -60:  # Local is newer or within 1 minute
                        self.log(f"     → Local is newer/equivalent, keeping local")
                        return False, f"Local {file_type} is newer ({time_diff:.1f}s difference)"
                    else:
                        self.log(f"     → Server is newer, downloading")
                        return True, f"Server {file_type} is newer ({-time_diff:.1f}s newer)"
                            
                elif overwrite_behavior == "Ask each time":

                    # Ask user in main thread
                    import threading
                    user_choice = threading.Event()
                    download_choice = [False]  # Use list to modify from nested function
                    
                    def ask_user():
                        dialog = Adw.MessageDialog.new(self.parent_window)
                        dialog.set_heading(f"{file_type.title()} Conflict Detected")
                        dialog.set_body(f"Local {file_type}: {local_str}\nServer {file_type}: {server_str}\n\nWhich version do you want to keep?")
                        dialog.add_response("local", "Keep Local")
                        dialog.add_response("server", "Download Server")
                        dialog.set_default_response("local")
                        
                        def on_response(dialog, response):
                            download_choice[0] = (response == "server")
                            user_choice.set()
                        
                        dialog.connect('response', on_response)
                        dialog.present()
                    
                    GLib.idle_add(ask_user)
                    user_choice.wait()  # Wait for user response
                    
                    if download_choice[0]:
                        self.log(f"     → User chose to download server {file_type}")
                        return True, f"User chose server {file_type}"
                    else:
                        self.log(f"     → User chose to keep local {file_type}")
                        return False, f"User chose local {file_type}"

            # Helper function to get the latest file from a list
            def get_latest_file(files_list, file_type_name):
                if not files_list:
                    return None
                    
                def get_file_timestamp(file_item):
                    if isinstance(file_item, dict):
                        # Try timestamp fields
                        for time_field in ['updated_at', 'created_at', 'modified_at', 'timestamp']:
                            if time_field in file_item and file_item[time_field]:
                                timestamp = parse_timestamp(file_item[time_field])
                                if timestamp:
                                    return timestamp
                        
                        # Try filename
                        filename = file_item.get('file_name', '')
                        if filename:
                            timestamp = parse_timestamp(filename)
                            if timestamp:
                                return timestamp
                    
                    return 0  # Default if no timestamp found
                
                sorted_files = sorted(files_list, key=get_file_timestamp, reverse=True)
                latest_file = sorted_files[0]
                
                total_count = len(files_list)
                if total_count > 1:
                    print(f"  📋 Found {total_count} {file_type_name} revisions, selecting latest")
                else:
                    print(f"  📋 Found 1 {file_type_name} file")
                    
                return latest_file

            # Process saves
            if 'saves' in self.retroarch.save_dirs:
                save_base_dir = self.retroarch.save_dirs['saves']
                user_saves = rom_details.get('user_saves', [])
                
                latest_save = get_latest_file(user_saves, "save")
                
                if latest_save:
                    downloads_attempted += 1
                    original_filename = latest_save.get('file_name', '')
                    romm_emulator = latest_save.get('emulator', 'unknown')
                    
                    if original_filename:
                        retroarch_emulator_dir = self.retroarch.get_retroarch_directory_name(romm_emulator)
                        
                        if retroarch_emulator_dir:
                            emulator_save_dir = save_base_dir / retroarch_emulator_dir
                            emulator_save_dir.mkdir(parents=True, exist_ok=True)
                            
                            retroarch_filename = self.retroarch.convert_to_retroarch_filename(
                                original_filename, 'saves', emulator_save_dir
                            )
                            
                            final_path = emulator_save_dir / retroarch_filename
                            
                            # Enhanced conflict detection
                            should_download, reason = should_download_file(final_path, latest_save, "save")
                            
                            if not should_download:
                                if final_path.exists():
                                    conflicts_detected += 1
                                self.log(f"  ⏭️ {reason}")
                            else:
                                # Create backup if overwriting
                                if final_path.exists():
                                    conflicts_detected += 1
                                    backup_path = final_path.with_suffix(final_path.suffix + '.backup')
                                    if backup_path.exists():
                                        backup_path.unlink()
                                    final_path.rename(backup_path)
                                    self.log(f"  💾 Backed up existing save as {backup_path.name}")
                                
                                temp_path = emulator_save_dir / original_filename
                                self.log(f"  📥 {reason} - downloading: {original_filename} → {retroarch_filename}")
                                
                                if self.romm_client.download_save(rom_id, 'saves', temp_path):
                                    try:
                                        if temp_path != final_path:
                                            temp_path.rename(final_path)
                                        downloads_successful += 1
                                        self.log(f"  ✅ Save ready: {retroarch_filename}")
                                    except Exception as e:
                                        self.log(f"  ❌ Failed to rename save: {e}")

            # Process states (similar logic)
            if 'states' in self.retroarch.save_dirs:
                state_base_dir = self.retroarch.save_dirs['states']
                user_states = rom_details.get('user_states', [])
                
                latest_state = get_latest_file(user_states, "state")
                
                if latest_state:
                    downloads_attempted += 1
                    original_filename = latest_state.get('file_name', '')
                    romm_emulator = latest_state.get('emulator', 'unknown')
                    
                    if original_filename:
                        retroarch_emulator_dir = self.retroarch.get_retroarch_directory_name(romm_emulator)
                        
                        if retroarch_emulator_dir:
                            emulator_state_dir = state_base_dir / retroarch_emulator_dir
                            emulator_state_dir.mkdir(parents=True, exist_ok=True)
                            
                            retroarch_filename = self.retroarch.convert_to_retroarch_filename(
                                original_filename, 'states', emulator_state_dir
                            )
                            
                            final_path = emulator_state_dir / retroarch_filename
                            
                            # Enhanced conflict detection
                            should_download, reason = should_download_file(final_path, latest_state, "state")
                            
                            if not should_download:
                                if final_path.exists():
                                    conflicts_detected += 1
                                self.log(f"  ⏭️ {reason}")
                            else:
                                # Create backup if overwriting
                                if final_path.exists():
                                    conflicts_detected += 1
                                    backup_path = final_path.with_suffix(final_path.suffix + '.backup')
                                    if backup_path.exists():
                                        backup_path.unlink()
                                    final_path.rename(backup_path)
                                    self.log(f"  💾 Backed up existing state as {backup_path.name}")
                                
                                temp_path = emulator_state_dir / original_filename
                                self.log(f"  📥 {reason} - downloading: {original_filename} → {retroarch_filename}")
                                
                                if self.romm_client.download_save(rom_id, 'states', temp_path):
                                    try:
                                        if temp_path != final_path:
                                            temp_path.rename(final_path)
                                        downloads_successful += 1
                                        self.log(f"  ✅ State ready: {retroarch_filename}")
                                        
                                        # Download screenshot if available
                                        screenshot_data = latest_state.get('screenshot')
                                        if screenshot_data and isinstance(screenshot_data, dict):
                                            screenshot_url = screenshot_data.get('download_path')
                                            if screenshot_url:
                                                screenshot_filename = f"{final_path.name}.png"
                                                screenshot_path = final_path.parent / screenshot_filename
                                                
                                                try:
                                                    full_screenshot_url = urljoin(self.romm_client.base_url, screenshot_url)
                                                    screenshot_response = self.romm_client.session.get(full_screenshot_url, timeout=30)
                                                    
                                                    if screenshot_response.status_code == 200:
                                                        with open(screenshot_path, 'wb') as f:
                                                            f.write(screenshot_response.content)
                                                        self.log(f"  📸 Downloaded screenshot: {screenshot_filename}")
                                                            
                                                except Exception as screenshot_error:
                                                    self.log(f"  ❌ Screenshot download error: {screenshot_error}")
                                    
                                    except Exception as e:
                                        self.log(f"  ❌ Failed to rename state: {e}")
            
            # Enhanced summary
            if downloads_attempted > 0:
                status_parts = []
                if downloads_successful > 0:
                    status_parts.append(f"{downloads_successful} downloaded")
                if conflicts_detected > 0:
                    skipped = conflicts_detected - downloads_successful
                    if skipped > 0:
                        status_parts.append(f"{skipped} local files preserved")
                
                status = ", ".join(status_parts) if status_parts else "no changes needed"
                self.log(f"📊 Sync summary for {game_name}: {status}")
                
                if downloads_successful > 0:
                    self.log(f"🎮 {game_name} updated with latest server saves/states")
                elif conflicts_detected > 0:
                    self.log(f"🛡️ {game_name} local saves/states protected from overwrite")
                else:
                    self.log(f"✅ {game_name} saves/states already up to date")
            else:
                self.log(f"📭 No saves/states found on server for {game_name}")
                    
        except Exception as e:
            self.log(f"❌ Error downloading saves/states for {game.get('name', 'Unknown')}: {e}")

class SaveFileHandler(FileSystemEventHandler):
    """File system event handler for save file changes"""
    
    def __init__(self, callback, save_type):
        self.callback = callback
        self.save_type = save_type
        
        # Define file extensions to monitor
        if save_type == 'saves':
            self.extensions = {'.srm', '.sav'}
        elif save_type == 'states':
            self.extensions = {'.state', '.state1', '.state2', '.state3', '.state4', 
                             '.state5', '.state6', '.state7', '.state8', '.state9'}
        else:
            self.extensions = set()
    
    def on_modified(self, event):
        # Only process file events, not directory events
        if not event.is_directory and self.is_save_file(event.src_path):
            self.callback(event.src_path, self.save_type)
    
    def on_created(self, event):
        # Only process file events, not directory events
        if not event.is_directory and self.is_save_file(event.src_path):
            self.callback(event.src_path, self.save_type)
    
    def is_save_file(self, file_path):
        """Check if the file is a save file we should monitor"""
        try:
            path = Path(file_path)
            return path.suffix.lower() in self.extensions
        except:
            return False
        
def main():
    """Main entry point"""
    print("🚀 Starting RomM-RetroArch Sync...")
    
    # Check desktop environment
    desktop = os.environ.get('XDG_CURRENT_DESKTOP', 'unknown').lower()
    print(f"🖥️ Desktop environment: {desktop}")
    
    # Check for AppIndicator availability
    try:
        gi.require_version('AppIndicator3', '0.1')
        from gi.repository import AppIndicator3
        print("✅ AppIndicator3 available")
    except Exception as e:
        print(f"⚠️ AppIndicator3 not available: {e}")
        print("💡 Install libappindicator3-dev for better tray support")
    
    app = SyncApp()
    return app.run()

if __name__ == '__main__':
    main()