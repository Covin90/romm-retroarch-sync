#!/usr/bin/env python3

import gi
import requests
import json
import os
import shutil
import threading
import pickle
import time
import logging
from pathlib import Path
from urllib.parse import urljoin, quote
import socket
import configparser
import html
import webbrowser
import base64
import datetime
from datetime import timezone
import psutil
import stat
import re

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import queue
from collections import defaultdict

# Fix SSL certificate path for AppImage environment
# Use system certificates instead of bundled certifi
import ssl
os.environ['REQUESTS_CA_BUNDLE'] = '/etc/ssl/certs/ca-certificates.crt'
os.environ['SSL_CERT_FILE'] = '/etc/ssl/certs/ca-certificates.crt'

gi.require_version('Gtk', '4.0')

# Try to load Adw, fallback to Gtk if not available (e.g., on SteamOS)
try:
    gi.require_version('Adw', '1')
    from gi.repository import Gtk, Adw, GLib, Gio, GObject
    HAS_ADW = True
except ValueError:
    # libadwaita not available, use Gtk only
    from gi.repository import Gtk, GLib, Gio, GObject
    HAS_ADW = False

    # Create a mock Adw module with Gtk-based fallbacks
    class MockAdw:
        class Application(Gtk.Application):
            pass

        class ApplicationWindow(Gtk.ApplicationWindow):
            pass

        class PreferencesWindow(Gtk.Window):
            def __init__(self):
                super().__init__()
                self.set_modal(True)
                self.set_default_size(800, 600)

        class PreferencesPage(Gtk.Box):
            def __init__(self):
                super().__init__(orientation=Gtk.Orientation.VERTICAL)
                self.set_margin_top(12)
                self.set_margin_bottom(12)
                self.set_margin_start(12)
                self.set_margin_end(12)
                self.set_spacing(12)

            def add(self, child):
                super().append(child)

        class PreferencesGroup(Gtk.Box):
            def __init__(self):
                super().__init__(orientation=Gtk.Orientation.VERTICAL)
                self.set_spacing(0)
                self._title_label = None

            def set_title(self, title):
                if self._title_label is None:
                    self._title_label = Gtk.Label()
                    self._title_label.set_halign(Gtk.Align.START)
                    self._title_label.set_margin_top(12)
                    self._title_label.set_margin_bottom(6)
                    self._title_label.set_margin_start(12)
                    self.prepend(self._title_label)
                # Handle HTML entities in title - decode and escape for markup
                import html as html_module
                decoded_title = html_module.unescape(title)
                escaped_title = decoded_title.replace('&', '&amp;')
                try:
                    self._title_label.set_markup(f"<b>{escaped_title}</b>")
                except:
                    # Fallback to plain text if markup fails
                    self._title_label.set_text(decoded_title)

            def add(self, child):
                super().append(child)

        class HeaderBar(Gtk.HeaderBar):
            pass

        class ToolbarView(Gtk.Box):
            def __init__(self):
                super().__init__(orientation=Gtk.Orientation.VERTICAL)
                self._header = None
                self._content = None

            def add_top_bar(self, header):
                if self._header is None:
                    self._header = header
                    self.prepend(header)
                else:
                    # Replace existing header
                    self.remove(self._header)
                    self._header = header
                    self.prepend(header)

            def set_content(self, content):
                if self._content is not None:
                    self.remove(self._content)
                self._content = content
                self.append(content)

        class ActionRow(Gtk.Box):
            def __init__(self):
                super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
                self.set_spacing(12)
                self.set_margin_top(6)
                self.set_margin_bottom(6)
                self.set_margin_start(12)
                self.set_margin_end(12)
                self._title_box = None
                self._title_label = None
                self._subtitle_label = None

            def set_title(self, title):
                if self._title_box is None:
                    self._title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                    self._title_box.set_hexpand(True)
                    self._title_label = Gtk.Label(label=title)
                    self._title_label.set_halign(Gtk.Align.START)
                    self._title_box.append(self._title_label)
                    self.prepend(self._title_box)
                else:
                    self._title_label.set_text(title)

            def set_subtitle(self, subtitle):
                if self._title_box is None:
                    self.set_title("")  # Initialize title box
                if self._subtitle_label is None:
                    self._subtitle_label = Gtk.Label(label=subtitle)
                    self._subtitle_label.set_halign(Gtk.Align.START)
                    self._subtitle_label.add_css_class("dim-label")
                    self._title_box.append(self._subtitle_label)
                else:
                    self._subtitle_label.set_text(subtitle)

            def add_suffix(self, widget):
                self.append(widget)

            def add_prefix(self, widget):
                if self._title_box is None:
                    self.set_title("")  # Initialize title box
                self.prepend(widget)

            def set_child(self, widget):
                # Simply append the widget - ActionRow with set_child replaces content
                self.append(widget)

        class SwitchRow(Gtk.Box):
            def __init__(self):
                super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
                self.set_spacing(12)
                self.set_margin_top(6)
                self.set_margin_bottom(6)
                self.set_margin_start(12)
                self.set_margin_end(12)
                self._title_box = None
                self._title_label = None
                self._subtitle_label = None
                self.switch = Gtk.Switch()
                self.switch.set_valign(Gtk.Align.CENTER)
                self.append(self.switch)

            def set_title(self, title):
                if self._title_box is None:
                    self._title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                    self._title_box.set_hexpand(True)
                    self._title_label = Gtk.Label(label=title)
                    self._title_label.set_halign(Gtk.Align.START)
                    self._title_box.append(self._title_label)
                    self.prepend(self._title_box)
                else:
                    self._title_label.set_text(title)

            def set_subtitle(self, subtitle):
                if self._title_box is None:
                    self.set_title("")  # Initialize title box
                if self._subtitle_label is None:
                    self._subtitle_label = Gtk.Label(label=subtitle)
                    self._subtitle_label.set_halign(Gtk.Align.START)
                    self._subtitle_label.add_css_class("dim-label")
                    self._title_box.append(self._subtitle_label)
                else:
                    self._subtitle_label.set_text(subtitle)

            def get_active(self):
                return self.switch.get_active()

            def set_active(self, active):
                self.switch.set_active(active)

        class EntryRow(Gtk.Box):
            def __init__(self):
                super().__init__(orientation=Gtk.Orientation.VERTICAL)
                self.set_spacing(6)
                self.set_margin_top(6)
                self.set_margin_bottom(6)
                self.set_margin_start(12)
                self.set_margin_end(12)
                self.entry = Gtk.Entry()
                self.append(self.entry)

            def set_title(self, title):
                label = Gtk.Label(label=title)
                label.set_halign(Gtk.Align.START)
                self.prepend(label)

            def get_text(self):
                return self.entry.get_text()

            def set_text(self, text):
                self.entry.set_text(text)

            def connect(self, signal_name, callback):
                if signal_name in ('activate', 'entry-activated'):
                    # Forward to the internal entry widget's 'activate' signal
                    # (Adw.EntryRow uses 'entry-activated', Gtk.Entry uses 'activate')
                    return self.entry.connect('activate', callback)
                else:
                    return super().connect(signal_name, callback)

        class PasswordEntryRow(EntryRow):
            def __init__(self):
                super().__init__()
                self.entry.set_visibility(False)

        class ExpanderRow(Gtk.Box):
            def __init__(self):
                super().__init__(orientation=Gtk.Orientation.VERTICAL)
                self.set_spacing(0)

                # Header box to hold title, prefix, and suffix
                self.header_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                self.header_box.set_spacing(6)
                self.header_box.set_margin_top(6)
                self.header_box.set_margin_bottom(6)
                self.header_box.set_margin_start(12)
                self.header_box.set_margin_end(12)

                # Prefix box (left side)
                self.prefix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                self.prefix_box.set_spacing(6)
                self.header_box.append(self.prefix_box)

                # Title and subtitle box (center)
                self.title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                self.title_box.set_hexpand(True)
                self.header_box.append(self.title_box)

                # Suffix box (right side)
                self.suffix_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
                self.suffix_box.set_spacing(6)
                self.header_box.append(self.suffix_box)

                # Create expander with custom header
                self.expander = Gtk.Expander()
                self.expander.set_label_widget(self.header_box)
                self.append(self.expander)

                # Content box
                self.content_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                self.expander.set_child(self.content_box)

                self._title_label = None
                self._subtitle_label = None

            def set_title(self, title):
                if self._title_label is None:
                    self._title_label = Gtk.Label()
                    self._title_label.set_halign(Gtk.Align.START)
                    self.title_box.append(self._title_label)
                self._title_label.set_text(title)

            def set_subtitle(self, subtitle):
                if self._subtitle_label is None:
                    self._subtitle_label = Gtk.Label()
                    self._subtitle_label.set_halign(Gtk.Align.START)
                    self._subtitle_label.add_css_class("dim-label")
                    self.title_box.append(self._subtitle_label)
                self._subtitle_label.set_text(subtitle)

            def add_prefix(self, widget):
                self.prefix_box.append(widget)

            def add_suffix(self, widget):
                self.suffix_box.append(widget)

            def add_row(self, row):
                self.content_box.append(row)

        class SpinRow(Gtk.Box):
            def __init__(self):
                super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
                self.set_spacing(12)
                self.set_margin_top(6)
                self.set_margin_bottom(6)
                self.set_margin_start(12)
                self.set_margin_end(12)
                self._title_box = None
                self._title_label = None
                self._subtitle_label = None
                self.spin = Gtk.SpinButton()
                self.spin.set_valign(Gtk.Align.CENTER)
                self.append(self.spin)

            def set_title(self, title):
                if self._title_box is None:
                    self._title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                    self._title_box.set_hexpand(True)
                    self._title_label = Gtk.Label(label=title)
                    self._title_label.set_halign(Gtk.Align.START)
                    self._title_box.append(self._title_label)
                    self.prepend(self._title_box)
                else:
                    self._title_label.set_text(title)

            def set_subtitle(self, subtitle):
                if self._title_box is None:
                    self.set_title("")  # Initialize title box
                if self._subtitle_label is None:
                    self._subtitle_label = Gtk.Label(label=subtitle)
                    self._subtitle_label.set_halign(Gtk.Align.START)
                    self._subtitle_label.add_css_class("dim-label")
                    self._title_box.append(self._subtitle_label)
                else:
                    self._subtitle_label.set_text(subtitle)

            def get_value(self):
                return self.spin.get_value()

            def set_value(self, value):
                self.spin.set_value(value)

            def set_range(self, min_val, max_val):
                self.spin.set_range(min_val, max_val)

            def set_adjustment(self, adjustment):
                self.spin.set_adjustment(adjustment)

        class ComboRow(Gtk.Box):
            def __init__(self):
                super().__init__(orientation=Gtk.Orientation.HORIZONTAL)
                self.set_spacing(12)
                self.set_margin_top(6)
                self.set_margin_bottom(6)
                self.set_margin_start(12)
                self.set_margin_end(12)
                self._title_box = None
                self._title_label = None
                self._subtitle_label = None
                self.combo = Gtk.ComboBoxText()
                self.combo.set_valign(Gtk.Align.CENTER)
                super().append(self.combo)  # Use super().append() to avoid conflict

            def set_title(self, title):
                if self._title_box is None:
                    self._title_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
                    self._title_box.set_hexpand(True)
                    self._title_label = Gtk.Label(label=title)
                    self._title_label.set_halign(Gtk.Align.START)
                    self._title_box.append(self._title_label)
                    self.prepend(self._title_box)
                else:
                    self._title_label.set_text(title)

            def set_subtitle(self, subtitle):
                if self._title_box is None:
                    self.set_title("")  # Initialize title box
                if self._subtitle_label is None:
                    self._subtitle_label = Gtk.Label(label=subtitle)
                    self._subtitle_label.set_halign(Gtk.Align.START)
                    self._subtitle_label.add_css_class("dim-label")
                    self._title_box.append(self._subtitle_label)
                else:
                    self._subtitle_label.set_text(subtitle)

            def append(self, id, label):
                # Forward to the combo widget
                self.combo.append(id, label)

            def get_active_id(self):
                return self.combo.get_active_id()

            def set_active_id(self, id):
                self.combo.set_active_id(id)

            def set_model(self, model):
                # Adw.ComboRow uses set_model with a StringList
                # We'll convert it to ComboBoxText compatible format
                # Clear existing items
                while self.combo.get_active() >= 0 or self.combo.get_has_entry():
                    try:
                        self.combo.remove(0)
                    except:
                        break

                # Add new items
                for i in range(model.get_n_items()):
                    item = model.get_string(i)
                    self.combo.append(str(i), item)

            def get_selected(self):
                active = self.combo.get_active()
                return active if active >= 0 else 0

            def set_selected(self, index):
                if index >= 0:
                    self.combo.set_active(index)

        class AlertDialog(Gtk.Dialog):
            @staticmethod
            def new(title, message):
                dialog = AlertDialog()
                dialog._title = title
                dialog._message = message
                dialog.set_title(title)

                # Create content area with message
                content = dialog.get_content_area()
                label = Gtk.Label(label=message)
                label.set_wrap(True)
                label.set_margin_top(12)
                label.set_margin_bottom(12)
                label.set_margin_start(12)
                label.set_margin_end(12)
                content.append(label)

                return dialog

            def add_response(self, response_id, label):
                self.add_button(label, response_id)

            def set_response_appearance(self, response_id, appearance):
                pass  # Not supported in Gtk-only mode

            def set_close_response(self, response_id):
                self.set_default_response(response_id)

            def choose(self, parent, cancellable, callback, user_data=None):
                # Adw.AlertDialog uses async choose(), but Gtk.Dialog uses run()
                # We need to convert this to the callback pattern
                self.set_transient_for(parent)
                self.set_modal(True)

                def on_response(dialog, response):
                    callback(dialog, None)  # GAsyncResult is None for sync operations

                self.connect('response', on_response)
                self.present()

        class ResponseAppearance:
            DESTRUCTIVE = None

        class AboutWindow(Gtk.AboutDialog):
            def __init__(self, **kwargs):
                super().__init__()
                # Map Adw.AboutWindow parameters to Gtk.AboutDialog
                if 'transient_for' in kwargs:
                    self.set_transient_for(kwargs['transient_for'])
                if 'application_name' in kwargs:
                    self.set_program_name(kwargs['application_name'])
                if 'application_icon' in kwargs:
                    self.set_logo_icon_name(kwargs['application_icon'])
                if 'version' in kwargs:
                    self.set_version(kwargs['version'])
                if 'developer_name' in kwargs:
                    self.set_authors([kwargs['developer_name']])
                if 'copyright' in kwargs:
                    self.set_copyright(kwargs['copyright'])
                if 'license_type' in kwargs:
                    self.set_license_type(kwargs['license_type'])

            def set_website(self, url):
                super().set_website(url)

            def set_issue_url(self, url):
                # Gtk.AboutDialog doesn't have set_issue_url, ignore it
                pass

    Adw = MockAdw()

# Custom exception for download cancellation

from sync_core import *

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
        # Initialize child store for multi-disc games
        self.child_store = Gio.ListStore()
        self.rebuild_children()

    def __eq__(self, other):
        """Enable proper equality comparison for GameItem objects"""
        if not isinstance(other, GameItem):
            return False
        return self.game_data.get('rom_id') == other.game_data.get('rom_id')

    def __hash__(self):
        """Enable GameItem to be used in sets"""
        return hash(self.game_data.get('rom_id', id(self.game_data)))

    def rebuild_children(self):
        """Rebuild child items (discs) if this is a multi-disc game"""
        self.child_store.remove_all()
        if self.game_data.get('is_multi_disc', False):
            discs = self.game_data.get('discs', [])
            for disc in discs:
                disc_item = DiscItem(disc, parent_game=self.game_data)
                self.child_store.append(disc_item)

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
            if size > 1024**3:
                return f"{size / (1024**3):.1f} GB"
            elif size > 1024**2:
                return f"{size / (1024**2):.1f} MB"
            elif size > 1024:
                return f"{size / 1024:.1f} KB"
            return f"{size} bytes"
        return "Not downloaded"

class DiscItem(GObject.Object):
    """Represents an individual disc in a multi-disc game"""
    def __init__(self, disc_data, parent_game=None):
        super().__init__()
        self.disc_data = disc_data
        self.parent_game = parent_game  # Reference to parent game data

    @GObject.Property(type=str, default='Unknown')
    def name(self):
        full_name = self.disc_data.get('name', 'Unknown')
        # Remove file extension from disc name for cleaner display
        if full_name != 'Unknown':
            from pathlib import Path
            return Path(full_name).stem
        return full_name

    @GObject.Property(type=bool, default=False)
    def is_downloaded(self):
        return self.disc_data.get('is_downloaded', False)

    @GObject.Property(type=str, default='Not downloaded')
    def size_text(self):
        if self.disc_data.get('is_downloaded'):
            disc_path = self.disc_data.get('path')
            if disc_path:
                disc_path_obj = Path(disc_path)

                # Try without extension if path doesn't exist (multi-file disc in folder)
                if not disc_path_obj.exists():
                    disc_path_obj = disc_path_obj.parent / disc_path_obj.stem

                if disc_path_obj.exists():
                    if disc_path_obj.is_dir():
                        size = sum(f.stat().st_size for f in disc_path_obj.rglob('*') if f.is_file())
                    else:
                        base_name = disc_path_obj.stem
                        parent = disc_path_obj.parent
                        matching = [f for f in parent.iterdir() if f.is_file() and f.stem == base_name]
                        size = sum(f.stat().st_size for f in matching) or disc_path_obj.stat().st_size
                else:
                    size = self.disc_data.get('size', 0)
            else:
                size = self.disc_data.get('size', 0)

            if size > 1024**3:
                return f"{size / (1024**3):.1f} GB"
            elif size > 1024**2:
                return f"{size / (1024**2):.1f} MB"
            elif size > 1024:
                return f"{size / 1024:.1f} KB"
            return f"{size} bytes"
        return "Not downloaded"

class PlatformItem(GObject.Object):
    def __init__(self, platform_name, games, loading=False, sync_status=None):
        super().__init__()
        self.platform_name = platform_name
        self.games = games
        self.loading = loading  # Flag to show loading state
        self.sync_status = sync_status  # Sync status for collections: 'synced', 'syncing', 'disabled'
        self.child_store = Gio.ListStore()
        self.rebuild_children()
    
    def update_games(self, new_games, loading=False, sync_status=None):
        self.games = new_games
        self.loading = loading  # Update loading state
        if sync_status is not None:
            self.sync_status = sync_status
        self.rebuild_children()
        # Notify all properties changed
        self.notify('name')
        self.notify('status-text')
        self.notify('size-text')
        self.notify('sync-status-text')
    
    def rebuild_children(self):
        """Optimized: Batch append game items instead of one-by-one"""
        self.child_store.remove_all()

        # Batch create GameItems for better performance
        if self.games:
            game_items = [GameItem(game) for game in self.games]
            # Use splice for batched insertion (much faster than individual appends)
            self.child_store.splice(0, 0, game_items)


    @GObject.Property(type=str, default='Unknown Platform')
    def name(self):
        # Just return the platform name without counts (counts are shown in status column)
        return self.platform_name
    
    @GObject.Property(type=str, default='0/0')
    def status_text(self):
        if self.loading:
            return "..."
        downloaded = sum(1 for g in self.games if g.get('is_downloaded'))
        result = f"{downloaded}/{len(self.games)}"
        return result
    
    @GObject.Property(type=bool, default=False)
    def is_downloaded(self):
        return False  # Platforms don't have download status
    
    @GObject.Property(type=str, default='0 KB')
    def size_text(self):
        if self.loading:
            return "Loading..."
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

    @GObject.Property(type=str, default='')
    def sync_status_text(self):
        """Return sync status indicator for collections"""
        if self.loading:
            return "loading"
        if self.sync_status is None:
            return ""  # Platforms don't have sync status

        # Return status string for visual rendering
        return self.sync_status  # 'synced', 'syncing', or 'disabled'

    def force_property_update(self):
        """Manually force property updates - for debugging"""
        print(f"🔄 Forcing property update for {self.platform_name}")

        # Use notify with property names (this should work)
        self.notify('name')
        self.notify('status-text')
        self.notify('size-text')
        self.notify('sync-status-text')
        
        # Alternative approach: get the current values and use freeze/thaw
        try:
            current_name = self.name
            current_status = self.status_text
            current_size = self.size_text

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
            False,
            False,
            self.create_child_model
        )
        self._platforms = {}
        
    def create_child_model(self, item):
        """Create child model for tree items

        Returns:
            - For PlatformItem: return child_store containing games
            - For GameItem (multi-disc): return child_store containing discs
            - For DiscItem: return None (discs have no children)
        """
        if isinstance(item, PlatformItem):
            return item.child_store
        elif isinstance(item, GameItem):
            # Check if this game has discs (multi-disc game)
            is_multi = item.game_data.get('is_multi_disc', False)
            child_count = len(item.child_store)
            if is_multi and child_count > 0:
                return item.child_store
        return None

    def _get_current_expansion_state(self):
        """Get the current expansion state of all platform items"""
        expansion_state = {}
        for i in range(self.tree_model.get_n_items()):
            item = self.tree_model.get_item(i)
            if item and item.get_depth() == 0:
                platform = item.get_item()
                if isinstance(platform, PlatformItem):
                    expansion_state[platform.platform_name] = item.get_expanded()
        return expansion_state

    def _restore_expansion_from_state(self, expansion_state):
        """Restore expansion state for all platform items"""
        for i in range(self.tree_model.get_n_items()):
            item = self.tree_model.get_item(i)
            if item and item.get_depth() == 0:
                platform = item.get_item()
                if isinstance(platform, PlatformItem):
                    should_expand = expansion_state.get(platform.platform_name, False)
                    if should_expand:
                        item.set_expanded(True)
                    else:
                        item.set_expanded(False)

    def _restore_expansion_immediate(self, expansion_state):
        """Restore expansion state immediately (used by search)"""
        self._restore_expansion_from_state(expansion_state)

    def update_library(self, games, group_by='platform', loading=False, sync_status_map=None):
        overall_start = time.time()

        # Save expansion state before update
        save_exp_start = time.time()
        expansion_state = {}
        for i in range(self.tree_model.get_n_items()):
            item = self.tree_model.get_item(i)
            if item and item.get_depth() == 0:
                platform = item.get_item()
                if isinstance(platform, PlatformItem):
                    expansion_state[platform.platform_name] = item.get_expanded()

        # Group games
        group_start = time.time()
        groups = {}
        for game in games:
            key = game.get(group_by, 'Unknown')
            groups.setdefault(key, []).append(game)

        # Build a map of existing platform items to reuse them
        map_start = time.time()
        existing_platforms = {}
        for i in range(self.root_store.get_n_items()):
            platform_item = self.root_store.get_item(i)
            existing_platforms[platform_item.platform_name] = platform_item

        # Clear the store and rebuild in sorted order
        clear_start = time.time()
        self.root_store.remove_all()

        # Add platforms in alphabetical order
        rebuild_start = time.time()
        for name, game_list in sorted(groups.items()):
            # Get sync status for this collection/platform
            sync_status = sync_status_map.get(name) if sync_status_map else None

            # Debug: check for multi-disc games in this group
            multi_count = sum(1 for g in game_list if g.get('is_multi_disc', False))
            if name in existing_platforms:
                # Reuse existing platform item (preserves state)
                platform = existing_platforms[name]
                platform.update_games(game_list, loading=loading, sync_status=sync_status)
            else:
                # Create new platform item
                platform = PlatformItem(name, game_list, loading=loading, sync_status=sync_status)
            self.root_store.append(platform)

        # Restore expansion after a delay
        def restore():
            for i in range(self.tree_model.get_n_items()):
                item = self.tree_model.get_item(i)
                if item and item.get_depth() == 0:
                    platform = item.get_item()
                    if isinstance(platform, PlatformItem):
                        if expansion_state.get(platform.platform_name, False):
                            item.set_expanded(True)
            return False
        GLib.timeout_add(100, restore)

class EnhancedLibrarySection:
    """Enhanced library section with tree view"""
    
    def __init__(self, parent_window):
        self.parent = parent_window
        self.library_model = LibraryTreeModel()
        self.selected_game = None
        self.selected_disc = None  # Track selected disc for launching
        self.selected_collection = None  # Track selected collection for deletion
        self.selected_checkboxes = set()  # Keep this for compatibility
        self.selected_rom_ids = set()     # Add this new tracking
        self.selected_game_keys = set()   # Add this for non-ROM ID games
        self.setup_library_ui()
        self.filtered_games = []
        self.search_text = ""
        self.game_progress = {}  # rom_id -> progress_info
        self.show_downloaded_only = False # Filter state
        self.sort_downloaded_first = False  # Sort mode state
        self.current_view_mode = 'platform'
        self.collections_games = []
        self.collections_cache_time = 0
        self.collections_cache_duration = 300
        self.view_mode_generation = 0  # Track view mode switches to prevent race conditions
        # Store selections for each view mode
        self.platform_view_selection = set()  # Store platform view row selections
        self.collection_view_selection = set()  # Store collection view row selections
        # Store checkbox and game selections for each view mode
        self.platform_view_checkboxes = set()
        self.platform_view_rom_ids = set()
        self.platform_view_game_keys = set()
        self.platform_view_selected_game = None
        self.platform_view_expanded = set()  # Store expanded platform names
        self.collection_view_checkboxes = set()
        self.collection_view_rom_ids = set()
        self.collection_view_game_keys = set()
        self.collection_view_selected_game = None
        self.collection_view_expanded = set()  # Store expanded collection names
        # Collection auto-sync attributes
        self.selected_collections_for_sync = set()  # UI selection state
        self.actively_syncing_collections = set()   # Auto-sync enabled state (persistent)
        self.currently_downloading_collections = set()  # Currently downloading state (temporary)
        self.completed_sync_collections = set()  # Collections that have completed sync (shows green immediately)
        self.collection_auto_sync_enabled = False
        self.collection_sync_thread = None
        self.collection_sync_interval = 120
        self.load_selected_collections()

    def is_path_validly_downloaded(self, path):
        """Check if a path (file or folder) is validly downloaded

        Args:
            path: Path object or string to check

        Returns:
            bool: True if path is validly downloaded (folder with content or file with size > 1024)
        """
        path = Path(path)
        if not path.exists():
            return False

        if path.is_dir():
            # For folders, check if directory has content
            try:
                return any(path.iterdir())
            except (PermissionError, OSError):
                return False
        elif path.is_file():
            # For files, check if file has reasonable size
            return path.stat().st_size > 1024

        return False

    def get_actual_file_size(self, path):
        """Get actual size - sum all files for directories, file size for files"""
        path = Path(path)
        if path.is_dir():
            size = sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
            return size
        elif path.is_file():
            return path.stat().st_size
        else:
            return 0

    def get_collections_for_autosync(self):
        """Get collections selected for auto-sync (either checked or row-selected)"""
        collections_for_sync = set()
        
        # Add checkbox-selected collections
        collections_for_sync.update(self.selected_collections_for_sync)
        
        # Add row-selected collections (if in collections view)
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            selection_model = self.column_view.get_model()
            if selection_model:
                for i in range(selection_model.get_n_items()):
                    if selection_model.is_selected(i):
                        tree_item = selection_model.get_item(i)
                        if tree_item and tree_item.get_depth() == 0:  # Collection level
                            item = tree_item.get_item()
                            if isinstance(item, PlatformItem):
                                collections_for_sync.add(item.platform_name)
        
        return collections_for_sync

    def remove_orphaned_games_on_startup(self):
        """Remove games that are no longer in any synced collections"""
        if not self.actively_syncing_collections:
            return
        
        def check_and_remove():
            try:
                # Get current collection contents
                all_collections = self.parent.romm_client.get_collections()
                all_synced_rom_ids = set()
                
                for collection in all_collections:
                    if collection.get('name') in self.actively_syncing_collections:
                        collection_roms = self.parent.romm_client.get_collection_roms(collection.get('id'))
                        all_synced_rom_ids.update(rom.get('id') for rom in collection_roms if rom.get('id'))
                
                # Check local games - ONLY check games that were previously in synced collections
                # Don't touch games downloaded outside of collection sync!
                removed_count = 0
                for game in self.parent.available_games:
                    if (game.get('is_downloaded') and
                        game.get('rom_id') and
                        game.get('rom_id') not in all_synced_rom_ids):

                        # Check if this game was actually part of a synced collection before
                        # by checking if it has collection metadata
                        if game.get('collection'):
                            # This game WAS in a synced collection but is no longer
                            GLib.idle_add(lambda g=game: self.parent.delete_game_file(g, is_bulk_operation=True))
                            removed_count += 1
                
                if removed_count > 0:
                    GLib.idle_add(lambda count=removed_count: 
                                self.parent.log_message(f"🗑️ Auto-sync startup: removed {count} orphaned games"))
                
            except Exception as e:
                GLib.idle_add(lambda err=str(e): 
                            self.parent.log_message(f"❌ Startup cleanup error: {err}"))
        
        threading.Thread(target=check_and_remove, daemon=True).start()

    def download_all_actively_syncing_games(self):
        """Download all non-downloaded games in actively syncing collections"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return

        def download_all():
            try:
                all_collections = self.parent.romm_client.get_collections()
                total_to_download = 0
                collections_with_downloads = []

                for collection in all_collections:
                    collection_name = collection.get('name', '')
                    if collection_name not in self.actively_syncing_collections:
                        continue

                    collection_id = collection.get('id')
                    collection_roms = self.parent.romm_client.get_collection_roms(collection_id)

                    download_dir = Path(self.parent.rom_dir_row.get_text())
                    games_to_download = []

                    for rom in collection_roms:
                        processed_game = self.parent.process_single_rom(rom, download_dir)

                        if not processed_game.get('is_downloaded'):
                            games_to_download.append(processed_game)

                    if games_to_download:
                        total_to_download += len(games_to_download)
                        collections_with_downloads.append(collection_name)

                        # Mark this collection as currently downloading
                        self.currently_downloading_collections.add(collection_name)

                        GLib.idle_add(lambda name=collection_name, count=len(games_to_download):
                                    self.parent.log_message(f"⬇️ Auto-sync: downloading {count} games from '{name}'"))

                        # Update UI to show orange indicator
                        GLib.idle_add(lambda name=collection_name: self.update_collection_sync_status(name))

                        for game in games_to_download:
                            GLib.idle_add(lambda g=game:
                                        self.parent.download_game(g, is_bulk_operation=True))

                if total_to_download > 0:
                    GLib.idle_add(lambda count=total_to_download:
                                self.parent.log_message(f"🎯 Auto-sync restored: started download of {count} total games"))

                    # Wait for downloads to complete, then update UI
                    def wait_and_update():
                        time.sleep(5)  # Wait for downloads to start
                        while self.parent.download_progress:
                            time.sleep(2)  # Check every 2 seconds

                        # All downloads complete - remove downloading status
                        for collection_name in collections_with_downloads:
                            self.currently_downloading_collections.discard(collection_name)
                            GLib.idle_add(lambda name=collection_name: self.update_collection_sync_status(name))

                    threading.Thread(target=wait_and_update, daemon=True).start()
                else:
                    GLib.idle_add(lambda:
                                self.parent.log_message(f"✅ Auto-sync restored: all collections already complete"))

            except Exception as e:
                GLib.idle_add(lambda err=str(e):
                            self.parent.log_message(f"❌ Auto-sync restore error: {err}"))

        threading.Thread(target=download_all, daemon=True).start()

    def update_collection_sync_status(self, collection_name):
        """Update the visual indicator for a collection's sync status"""
        import time
        entry_time = time.time()
        self.parent.log_message(f"[DEBUG] update_collection_sync_status called for {collection_name}")

        if not hasattr(self, 'library_model') or not self.library_model.tree_model:
            self.parent.log_message(f"[DEBUG] No library_model, returning")
            return

        def update_ui():
            update_start = time.time()
            self.parent.log_message(f"[DEBUG] update_ui callback started ({update_start - entry_time:.3f}s after call)")
            model = self.library_model.tree_model
            for i in range(model.get_n_items() if model else 0):
                tree_item = model.get_item(i)
                if tree_item and tree_item.get_depth() == 0:  # Collection/Platform level
                    item = tree_item.get_item()
                    if isinstance(item, PlatformItem) and item.platform_name == collection_name:
                        # Determine the new sync status
                        # First check if collection is marked as completed
                        if hasattr(self, 'completed_sync_collections') and collection_name in self.completed_sync_collections:
                            new_status = 'synced'  # Green dot - collection sync complete
                        elif collection_name in self.currently_downloading_collections:
                            new_status = 'syncing'  # Orange dot - currently downloading
                        elif collection_name in self.actively_syncing_collections:
                            # Check if all games are downloaded
                            all_downloaded = all(g.get('is_downloaded', False) for g in item.games)
                            new_status = 'synced' if all_downloaded else 'disabled'  # Green if synced, grey if not
                        else:
                            new_status = 'disabled'  # Grey dot (not enabled)

                        self.parent.log_message(f"[DEBUG] Setting status to {new_status} for {collection_name}")
                        # Update the sync_status and notify
                        old_status = item.sync_status
                        item.sync_status = new_status
                        item.notify('sync-status-text')
                        item.notify('name')
                        self.parent.log_message(f"[DEBUG] Status changed from {old_status} to {new_status} ({time.time() - entry_time:.3f}s total)")
                        break
            return False

        # Use HIGH priority to execute ASAP
        GLib.idle_add(update_ui, priority=GLib.PRIORITY_HIGH)

    def download_game_directly(self, game):
        """Download game directly WITH progress tracking"""
        try:
            rom_id = game['rom_id']
            rom_name = game['name']
            platform_slug = game.get('platform_slug', game.get('platform', 'Unknown'))
            file_name = game['file_name']

            # Get download directory
            download_dir = Path(self.parent.rom_dir_row.get_text())

            # Use platform slug directly (RomM and RetroDECK now use the same slugs)
            platform_dir = download_dir / platform_slug
            platform_dir.mkdir(parents=True, exist_ok=True)
            download_path = platform_dir / file_name

            # Skip if already downloaded (handles both files and folders)
            if self.parent.is_path_validly_downloaded(download_path):
                return True

            # Initialize progress tracking
            self.parent.download_progress[rom_id] = {
                'progress': 0.0,
                'downloading': True,
                'filename': rom_name,
                'speed': 0,
                'downloaded': 0,
                'total': 0
            }

            # Update UI to show download starting
            GLib.idle_add(lambda: self.update_game_progress(rom_id, self.parent.download_progress[rom_id]))

            # Download using RomM client with progress callback
            def progress_callback(progress_info):
                # Update progress tracking
                self.parent.download_progress[rom_id].update({
                    'progress': progress_info.get('progress', 0),
                    'speed': progress_info.get('speed', 0),
                    'downloaded': progress_info.get('downloaded', 0),
                    'total': progress_info.get('total', 0),
                    'downloading': True
                })
                # Update UI
                GLib.idle_add(lambda: self.update_game_progress(rom_id, self.parent.download_progress[rom_id]))

            # Download using RomM client
            success, message = self.parent.romm_client.download_rom(
                rom_id, rom_name, download_path, progress_callback
            )

            # Mark as completed or failed
            if success:
                self.parent.download_progress[rom_id] = {
                    'progress': 1.0,
                    'downloading': False,
                    'completed': True,
                    'filename': rom_name
                }
            else:
                self.parent.download_progress[rom_id] = {
                    'progress': 0.0,
                    'downloading': False,
                    'failed': True,
                    'filename': rom_name
                }

            # Final UI update
            GLib.idle_add(lambda: self.update_game_progress(rom_id, self.parent.download_progress[rom_id]))

            # Clean up progress after a delay
            def cleanup():
                import time
                time.sleep(2)
                if rom_id in self.parent.download_progress:
                    del self.parent.download_progress[rom_id]
            threading.Thread(target=cleanup, daemon=True).start()

            return success

        except Exception as e:
            print(f"❌ Direct download error: {e}")
            # Mark as failed
            if rom_id in self.parent.download_progress:
                self.parent.download_progress[rom_id] = {
                    'progress': 0.0,
                    'downloading': False,
                    'failed': True,
                    'filename': rom_name
                }
                GLib.idle_add(lambda: self.update_game_progress(rom_id, self.parent.download_progress[rom_id]))
            return False

    def restore_collection_auto_sync_on_connect(self):
        """Restore collection auto-sync when connection is established"""
        try:
            if not self.actively_syncing_collections or not self.collection_auto_sync_enabled:
                return
                
            self.parent.log_message(f"🔄 Restoring collection auto-sync for {len(self.actively_syncing_collections)} collections")
            
            # Download missing games
            self.download_all_actively_syncing_games()
            
            # Remove orphaned games (if auto-delete enabled)
            self.remove_orphaned_games_on_startup()
            
            # Start background monitoring
            self.start_collection_auto_sync()
            self.update_sync_button_state()
            
            self.parent.log_message("✅ Collection auto-sync restored successfully")
            
        except Exception as e:
            self.parent.log_message(f"⚠️ Failed to restore collection auto-sync: {e}")

    def get_collection_sync_status(self, collection_name, games):
        """Determine sync status of a collection"""
        if not games:
            return 'empty'
        
        downloaded_count = sum(1 for game in games if game.get('is_downloaded', False))
        total_count = len(games)
        
        if downloaded_count == 0:
            return 'none'
        elif downloaded_count == total_count:
            return 'complete'
        else:
            return 'partial'
        
    def refresh_collection_checkboxes(self):
        """Refresh collection display after auto-sync state changes"""
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            # Force rebind of all collection rows
            model = self.library_model.tree_model
            if model:
                for i in range(model.get_n_items()):
                    tree_item = model.get_item(i)
                    if tree_item and tree_item.get_depth() == 0:
                        # Trigger name cell rebind
                        self.library_model.root_store.items_changed(i, 1, 1)

    def save_selected_collections(self):
        """Save both UI selection and active sync states"""
        if hasattr(self.parent, 'settings'):
            # UI selection state
            collections_str = '|'.join(self.selected_collections_for_sync)
            self.parent.settings.set('Collections', 'selected_for_sync', collections_str)
            
            # Active sync state (what's actually running)
            active_str = '|'.join(self.actively_syncing_collections)
            self.parent.settings.set('Collections', 'actively_syncing', active_str)
            
            self.parent.settings.set('Collections', 'auto_sync_enabled', str(self.collection_auto_sync_enabled).lower())

    def load_selected_collections(self):
        """Load settings and restore actively syncing collections"""
        if hasattr(self.parent, 'settings'):
            # Restore actively syncing collections (not UI selections)
            actively_syncing_str = self.parent.settings.get('Collections', 'actively_syncing', '')
            if actively_syncing_str:
                self.actively_syncing_collections = set(actively_syncing_str.split('|'))
            
            # Keep UI selections empty on startup
            self.selected_collections_for_sync = set()
            
            # Load other settings
            interval = int(self.parent.settings.get('Collections', 'sync_interval', '120'))
            self.collection_sync_interval = interval
            
            auto_sync_enabled = self.parent.settings.get('Collections', 'auto_sync_enabled', 'false') == 'true'
            self.collection_auto_sync_enabled = auto_sync_enabled

    def start_collection_auto_sync(self):
        """Start background collection sync and download all non-downloaded games"""
        if not self.actively_syncing_collections:
            self.parent.log_message(f"🚫 No collections selected for sync")
            return
        
        # Don't exit if thread exists - restart it instead
        if self.collection_sync_thread and self.collection_sync_thread.is_alive():
            self.parent.log_message(f"🔄 Collection sync already running")
        else:
            self.parent.log_message(f"🎯 Starting collection sync for {len(self.actively_syncing_collections)} collections...")
            
            # Download all existing games in selected collections first
            # Don't send notifications during startup - only for user-initiated actions
            self.download_all_collection_games(send_notifications=False)

            # Initialize ROM caches for selected collections
            self.initialize_collection_caches()
            
            self.collection_auto_sync_enabled = True
            
            def sync_worker():
                self.parent.log_message(f"🚀 Collection sync worker thread started")
                while self.collection_auto_sync_enabled:
                    try:
                        self.check_actively_syncing_collections()
                        time.sleep(self.collection_sync_interval)
                    except Exception as e:
                        self.parent.log_message(f"❌ Collection sync error: {e}")
                        time.sleep(60)
                self.parent.log_message(f"🛑 Collection sync worker stopped")
            
            self.collection_sync_thread = threading.Thread(target=sync_worker, daemon=True)
            self.collection_sync_thread.start()
            
        self.refresh_collection_checkboxes()

    def download_all_collection_games(self, send_notifications=True):
        """Download all non-downloaded games in selected collections (respecting concurrency limit)"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return

        def download_all():
            try:
                all_collections = self.parent.romm_client.get_collections()
                total_to_download = 0
                all_games_to_download = []
                collections_data = {}  # Track per-collection data

                for collection in all_collections:
                    collection_name = collection.get('name', '')
                    if (collection_name not in self.selected_collections_for_sync and
                        collection_name not in getattr(self, 'actively_syncing_collections', set())):
                        continue

                    collection_id = collection.get('id')
                    collection_roms = self.parent.romm_client.get_collection_roms(collection_id)

                    GLib.idle_add(lambda name=collection_name, count=len(collection_roms):
                                self.parent.log_message(f"📋 Collection '{name}': {count} total games"))

                    download_dir = Path(self.parent.rom_dir_row.get_text())

                    # Track collection-specific data
                    collections_data[collection_name] = {
                        'total': len(collection_roms),
                        'to_download': 0,
                        'already_downloaded': 0
                    }

                    for rom in collection_roms:
                        processed_game = self.parent.process_single_rom(rom, download_dir)

                        if not processed_game.get('is_downloaded'):
                            # Tag game with collection name for tracking
                            processed_game['_sync_collection'] = collection_name
                            all_games_to_download.append(processed_game)
                            total_to_download += 1
                            collections_data[collection_name]['to_download'] += 1
                        else:
                            collections_data[collection_name]['already_downloaded'] += 1

                if total_to_download > 0:
                    # Use bulk download method with collection tracking
                    GLib.idle_add(lambda games=all_games_to_download, cdata=collections_data:
                                self.parent.download_multiple_games_with_collection_tracking(games, cdata))
                else:
                    # All collections are already synced - update their status to green
                    def update_all_collection_statuses():
                        for collection_name in collections_data.keys():
                            self.update_collection_sync_status(collection_name)
                        return False
                    GLib.idle_add(update_all_collection_statuses)

                    # Send per-collection notifications (only if requested)
                    if send_notifications:
                        for collection_name, data in collections_data.items():
                            def send_collection_synced_notification(name=collection_name, total=data['total']):
                                self.parent.send_desktop_notification(
                                    f"✅ {name} - Sync Complete",
                                    f"{total}/{total} ROMs synced"
                                )
                                return False
                            GLib.idle_add(send_collection_synced_notification)

                    GLib.idle_add(lambda:
                                self.parent.log_message(f"✅ Collection sync: all selected collections already complete"))

            except Exception as e:
                GLib.idle_add(lambda err=str(e):
                            self.parent.log_message(f"❌ Collection download error: {err}"))

        threading.Thread(target=download_all, daemon=True).start()

    def initialize_collection_caches(self):
        """Initialize ROM ID caches for actively syncing collections"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return

        # Set a flag to indicate caches are being initialized
        self._cache_initialization_complete = False

        def init_caches():
            try:
                all_collections = self.parent.romm_client.get_collections()
                for collection in all_collections:
                    collection_name = collection.get('name', '')
                    # Only cache actively syncing collections
                    if collection_name in self.actively_syncing_collections:
                        collection_id = collection.get('id')
                        collection_roms = self.parent.romm_client.get_collection_roms(collection_id)

                        # Store current ROM IDs
                        current_rom_ids = {rom.get('id') for rom in collection_roms if rom.get('id')}
                        cache_key = f'_collection_roms_{collection_name}'
                        setattr(self, cache_key, current_rom_ids)

                        GLib.idle_add(lambda name=collection_name, count=len(current_rom_ids):
                                    self.parent.log_message(f"🔋 Initialized cache for '{name}': {count} games"))

                # Mark initialization as complete
                self._cache_initialization_complete = True
                GLib.idle_add(lambda: self.parent.log_message(f"✅ Collection cache initialization complete"))

            except Exception as e:
                self._cache_initialization_complete = True  # Set to True even on error to avoid blocking
                GLib.idle_add(lambda err=str(e):
                            self.parent.log_message(f"❌ Cache initialization error: {err}"))

        threading.Thread(target=init_caches, daemon=True).start()

    def stop_collection_auto_sync(self):
        """Stop background collection sync"""
        self.collection_auto_sync_enabled = False
        self.collection_sync_thread = None
        self.refresh_collection_checkboxes()

    def check_actively_syncing_collections(self):
        """Check actively syncing collections for changes"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return

        # Wait for cache initialization to complete before checking
        if not getattr(self, '_cache_initialization_complete', False):
            self.parent.log_message(f"⏳ Waiting for cache initialization to complete...")
            return

        # ADD THIS LOGGING
        if hasattr(self, '_sync_check_count'):
            self._sync_check_count += 1
        else:
            self._sync_check_count = 1

        # ADD THIS - ALWAYS LOG
        self.parent.log_message(f"🔄 Collection autosync running: checking {len(self.actively_syncing_collections)} collections...")

        try:
            all_collections = self.parent.romm_client.get_collections()
            changes_detected = False
            
            for collection in all_collections:
                collection_name = collection.get('name', '')
                # Check actively syncing collections, not UI selected ones
                if collection_name not in self.actively_syncing_collections:
                    continue
                    
                collection_id = collection.get('id')
                collection_roms = self.parent.romm_client.get_collection_roms(collection_id)
                
                # Get current ROM IDs in this collection
                current_rom_ids = {rom.get('id') for rom in collection_roms if rom.get('id')}
                
                # Get previously stored ROM IDs
                cache_key = f'_collection_roms_{collection_name}'
                previous_rom_ids = getattr(self, cache_key, set())
                
                if previous_rom_ids != current_rom_ids:
                    changes_detected = True

                    # Find added and removed games
                    added_rom_ids = current_rom_ids - previous_rom_ids
                    removed_rom_ids = previous_rom_ids - current_rom_ids

                    if added_rom_ids:
                        # Don't log here - let handle_added_games decide if logging is appropriate
                        self.handle_added_games(collection_roms, added_rom_ids, collection_name)
                    
                    if removed_rom_ids:
                        GLib.idle_add(lambda name=collection_name, count=len(removed_rom_ids): 
                            self.parent.log_message(f"🗑️ Collection '{name}': {count} games removed"))
                        self.handle_removed_games(removed_rom_ids, collection_name)
                    
                # MAKE SURE THIS LINE IS OUTSIDE THE IF BLOCKS AND ALWAYS EXECUTES:
                setattr(self, cache_key, current_rom_ids)  # This must happen after handling changes
            
            # At the end of the method, after the for loop
            if not changes_detected and len(self.actively_syncing_collections) > 0:
                self.parent.log_message(f"✅ Collection check complete: no changes detected in {len(self.actively_syncing_collections)} collections")

            # Note: We no longer reload the entire collections view after changes
            # because handle_added_games and handle_removed_games now do in-place updates
                    
        except Exception as e:
            print(f"Collection change check error: {e}")

    def handle_added_games(self, collection_roms, added_rom_ids, collection_name):
        """Automatically download newly added games"""
        def download_new_games():
            try:
                download_dir = Path(self.parent.rom_dir_row.get_text())
                downloaded_count = 0
                already_downloaded_count = 0

                # Create a stable reference to collection name
                current_collection = str(collection_name)

                # First pass: check how many are already downloaded
                for rom in collection_roms:
                    if rom.get('id') not in added_rom_ids:
                        continue
                    processed_game = self.parent.process_single_rom(rom, download_dir)
                    if processed_game.get('is_downloaded'):
                        already_downloaded_count += 1

                # Only log the "games added" message if not all are already downloaded
                total_added = len(added_rom_ids)
                if already_downloaded_count < total_added:
                    GLib.idle_add(lambda name=current_collection, count=total_added:
                        self.parent.log_message(f"�� Collection '{name}': {count} games added"))

                for rom in collection_roms:
                    if rom.get('id') not in added_rom_ids:
                        continue

                    # Process the new ROM
                    processed_game = self.parent.process_single_rom(rom, download_dir)

                    # Skip if already downloaded (already counted in first pass)
                    if processed_game.get('is_downloaded'):
                        continue

                    # Log with stable collection reference
                    game_name = processed_game.get('name')
                    current_rom_id = rom.get('id')
                    processed_game['collection'] = current_collection

                    # ADD GAME TO TREE BEFORE DOWNLOADING so user can see it appear
                    def add_game_to_tree():
                        # Update available_games
                        for i, game in enumerate(self.parent.available_games):
                            if game.get('rom_id') == current_rom_id:
                                self.parent.available_games[i] = processed_game
                                break
                        else:
                            self.parent.available_games.append(processed_game)

                        # Update collections_games cache
                        if hasattr(self, 'collections_games'):
                            found = False
                            for i, collection_game in enumerate(self.collections_games):
                                if collection_game.get('rom_id') == current_rom_id:
                                    self.collections_games[i] = processed_game
                                    found = True
                                    break
                            if not found:
                                self.collections_games.append(processed_game)

                        # Add to tree view
                        if self.current_view_mode == 'collection':
                            for i in range(self.library_model.root_store.get_n_items()):
                                platform_item = self.library_model.root_store.get_item(i)
                                if platform_item.platform_name == current_collection:
                                    game_exists = any(g.get('rom_id') == current_rom_id for g in platform_item.games)
                                    if not game_exists:
                                        platform_item.games.append(processed_game)
                                        # Sort games alphabetically
                                        if self.sort_downloaded_first:
                                            platform_item.games.sort(key=lambda g: (not g.get('is_downloaded', False), g.get('name', '').lower()))
                                        else:
                                            platform_item.games.sort(key=lambda g: g.get('name', '').lower())
                                        platform_item.rebuild_children()
                                        platform_item.notify('status-text')
                                        platform_item.notify('size-text')
                                    break
                        return False

                    GLib.idle_add(add_game_to_tree)

                    self.parent.log_message(f"  ⬇️ Auto-downloading {game_name} from '{current_collection}'...")

                    # Respect concurrent download limit for auto-sync
                    max_concurrent = int(self.parent.settings.get('Download', 'max_concurrent', '3'))
                    active_downloads = sum(1 for p in self.parent.download_progress.values()
                                        if p.get('downloading', False))

                    if active_downloads < max_concurrent:
                        # Use direct download if under limit
                        if self.download_game_directly(processed_game):
                            downloaded_count += 1
                            self.parent.log_message(f"  ✅ {game_name} downloaded from '{current_collection}'")

                            # CRITICAL: Update the game's download status IMMEDIATELY after download
                            platform_slug = processed_game.get('platform_slug', 'Unknown')
                            file_name = processed_game.get('file_name', '')
                            download_dir = Path(self.parent.rom_dir_row.get_text())

                            # Use platform slug directly (RomM and RetroDECK now use the same slugs)
                            local_path = download_dir / platform_slug / file_name

                            # Check download status (handle both files and folders)
                            is_valid_download = False
                            if local_path.is_dir():
                                # For folders, check if directory exists and has content
                                is_valid_download = any(local_path.iterdir())
                            elif local_path.is_file():
                                # For files, check if file exists and has reasonable size
                                is_valid_download = local_path.stat().st_size > 1024

                            if is_valid_download:
                                processed_game['is_downloaded'] = True
                                processed_game['local_path'] = str(local_path)
                                processed_game['local_size'] = self.parent.get_actual_file_size(local_path)

                            # Update available_games
                            for i, game in enumerate(self.parent.available_games):
                                if game.get('rom_id') == current_rom_id:
                                    self.parent.available_games[i] = processed_game
                                    break

                            # Update collections_games cache
                            if hasattr(self, 'collections_games'):
                                for i, collection_game in enumerate(self.collections_games):
                                    if collection_game.get('rom_id') == current_rom_id:
                                        self.collections_games[i] = processed_game
                                        break

                            # Update UI to show download completed (game already exists in tree)
                            def update_download_status():
                                self.update_single_game(processed_game)
                                return False

                            GLib.idle_add(update_download_status)
                    else:
                        self.parent.log_message(f"  ❌ Failed to download {game_name} from '{current_collection}'")
                
                # Send notifications about collection changes
                total_added = len(added_rom_ids)
                if total_added > 0:
                    if downloaded_count > 0:
                        self.parent.log_message(f"🎯 Auto-downloaded {downloaded_count} new games from '{current_collection}'")

                        # Send RetroArch notification
                        if self.parent.retroarch:
                            if downloaded_count == total_added:
                                self.parent.retroarch.send_notification(f"'{current_collection}': Downloaded {downloaded_count} new game{'s' if downloaded_count != 1 else ''}")
                            else:
                                self.parent.retroarch.send_notification(f"'{current_collection}': {total_added} added ({downloaded_count} downloaded)")

                        # Send desktop notification
                        def send_desktop_notif():
                            if downloaded_count == total_added:
                                self.parent.send_desktop_notification(
                                    "Collection Synced",
                                    f"'{current_collection}': Downloaded {downloaded_count} new game{'s' if downloaded_count != 1 else ''}"
                                )
                            else:
                                self.parent.send_desktop_notification(
                                    "Collection Synced",
                                    f"'{current_collection}': {total_added} game{'s' if total_added != 1 else ''} added, {downloaded_count} downloaded"
                                )
                            return False
                        GLib.idle_add(send_desktop_notif)
                    elif already_downloaded_count > 0 and already_downloaded_count < total_added:
                        # Some games were already downloaded, but not all
                        self.parent.log_message(f"  ℹ️ {already_downloaded_count} of {total_added} games already downloaded")

                        if self.parent.retroarch:
                            self.parent.retroarch.send_notification(f"'{current_collection}': {total_added} game{'s' if total_added != 1 else ''} added ({already_downloaded_count} already downloaded)")

                        def send_desktop_notif_partial():
                            self.parent.send_desktop_notification(
                                "Collection Updated",
                                f"'{current_collection}': {total_added} game{'s' if total_added != 1 else ''} added ({already_downloaded_count} already downloaded)"
                            )
                            return False
                        GLib.idle_add(send_desktop_notif_partial)
                    # If all games were already downloaded (already_downloaded_count == total_added),
                    # don't send any notification - this is likely cache initialization

            except Exception as e:
                self.parent.log_message(f"❌ Auto-download error: {e}")
        
        # Run downloads in background
        threading.Thread(target=download_new_games, daemon=True).start()

    def handle_removed_games(self, removed_rom_ids, collection_name):
        """Handle removed games - always delete if not in other synced collections"""
        download_dir = Path(self.parent.rom_dir_row.get_text())
        deleted_count = 0
        
        # Find and delete removed games
        for game in self.parent.available_games:
            if game.get('rom_id') in removed_rom_ids and game.get('is_downloaded'):
                # Check if game exists in other synced collections
                found_in_other = False
                for other_collection in self.actively_syncing_collections:
                    if other_collection != collection_name:
                        # Check if ROM ID exists in other collection's cache
                        other_cache = getattr(self, f'_collection_roms_{other_collection}', set())
                        if game.get('rom_id') in other_cache:
                            found_in_other = True
                            break
                
                if not found_in_other:
                    local_path = Path(game.get('local_path', ''))
                    if local_path.exists():
                        try:
                            local_path.unlink()
                            self.parent.log_message(f"  🗑️ Deleted {game.get('name')}")
                            deleted_count += 1
                        except Exception as e:
                            self.parent.log_message(f"  ❌ Failed to delete {game.get('name')}: {e}")
        
        if deleted_count > 0:
            self.parent.log_message(f"Auto-deleted {deleted_count} games removed from '{collection_name}'")

            # Send RetroArch notification
            if self.parent.retroarch:
                self.parent.retroarch.send_notification(f"Collection '{collection_name}': Removed {deleted_count} game{'s' if deleted_count != 1 else ''}")

            # Send desktop notification
            def send_removal_notification():
                self.parent.send_desktop_notification(
                    "Collection Synced",
                    f"'{collection_name}': Removed {deleted_count} game{'s' if deleted_count != 1 else ''}"
                )
                return False
            GLib.idle_add(send_removal_notification)

            def update_ui_after_deletion():
                # Update master games list - mark as not downloaded
                for game in self.parent.available_games:
                    if game.get('rom_id') in removed_rom_ids:
                        game['is_downloaded'] = False
                        game['local_path'] = None
                        game['local_size'] = 0
                
                # Force tree view refresh regardless of view mode
                if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                    # Remove games entirely from collections cache
                    if hasattr(self, 'collections_games'):
                        self.collections_games = [
                            game for game in self.collections_games
                            if game.get('rom_id') not in removed_rom_ids
                        ]

                    # Update the view directly WITHOUT invalidating cache
                    # This prevents showing "Loading..." placeholder
                    self.library_model.update_library(self.collections_games, group_by='collection')
                else:
                    # Platform view - full refresh
                    self.update_games_library(self.parent.available_games)
                
                return False
            
            GLib.idle_add(update_ui_after_deletion)

    def on_collection_checkbox_changed(self, checkbox, collection_name):
        """Handle collection selection (visual state only)"""
        if checkbox.get_active():
            self.selected_collections_for_sync.add(collection_name)
        else:
            self.selected_collections_for_sync.discard(collection_name)
        
        self.save_selected_collections()
        self.update_sync_button_state()

    def download_single_collection_games(self, collection_name):
        """Queue all non-downloaded games from a collection using bulk download"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return

        def queue_downloads():
            try:
                all_collections = self.parent.romm_client.get_collections()

                for collection in all_collections:
                    if collection.get('name', '') != collection_name:
                        continue

                    collection_id = collection.get('id')
                    collection_roms = self.parent.romm_client.get_collection_roms(collection_id)

                    download_dir = Path(self.parent.rom_dir_row.get_text())
                    games_to_download = []

                    for rom in collection_roms:
                        processed_game = self.parent.process_single_rom(rom, download_dir)
                        if not processed_game.get('is_downloaded'):
                            processed_game['collection'] = collection_name
                            games_to_download.append(processed_game)

                    if games_to_download:
                        # Mark this collection as currently downloading
                        self.currently_downloading_collections.add(collection_name)

                        GLib.idle_add(lambda: self.parent.log_message(
                            f"📥 Starting download of {len(games_to_download)} games from '{collection_name}'"))

                        # Update UI to show orange indicator
                        GLib.idle_add(lambda name=collection_name: self.update_collection_sync_status(name))

                        # Tag games with collection name for tracking
                        for game in games_to_download:
                            game['_sync_collection'] = collection_name

                        # Prepare collections_data for tracking
                        collections_data = {
                            collection_name: {
                                'total': len(collection_roms),
                                'to_download': len(games_to_download),
                                'already_downloaded': len(collection_roms) - len(games_to_download)
                            }
                        }

                        # Use bulk download method with collection tracking
                        GLib.idle_add(lambda games=games_to_download, cdata=collections_data:
                                    self.parent.download_multiple_games_with_collection_tracking(games, cdata))

                        # Wait for downloads to complete, then update UI
                        def wait_and_update():
                            time.sleep(5)  # Wait for downloads to start
                            while self.parent.download_progress:
                                time.sleep(2)  # Check every 2 seconds

                            # All downloads complete - remove downloading status
                            self.currently_downloading_collections.discard(collection_name)
                            GLib.idle_add(lambda name=collection_name: self.update_collection_sync_status(name))

                        threading.Thread(target=wait_and_update, daemon=True).start()
                    else:
                        GLib.idle_add(lambda: self.parent.log_message(
                            f"✅ Collection '{collection_name}': all games already downloaded"))
                        # Update status to 'synced' (green) since all games are already downloaded
                        GLib.idle_add(lambda name=collection_name: self.update_collection_sync_status(name) or False)
                        # Send notification that collection is already synced
                        total_games = len(collection_roms)
                        def send_sync_complete_notif(name=collection_name, total=total_games):
                            self.parent.send_desktop_notification(
                                f"✅ {name} - Sync Complete",
                                f"{total}/{total} ROMs synced"
                            )
                            return False
                        GLib.idle_add(send_sync_complete_notif)
                    break

            except Exception as e:
                GLib.idle_add(lambda: self.parent.log_message(f"❌ Error: {e}"))

        threading.Thread(target=queue_downloads, daemon=True).start()

    def on_toggle_collection_auto_sync(self, toggle_button):
        """Toggle collection auto-sync on/off"""
        import time
        start_time = time.time()
        self.parent.log_message(f"[DEBUG] Toggle activated at {start_time}")

        if toggle_button.get_active():
            # Check both checkbox selections AND row selection
            selected_collections = self.selected_collections_for_sync.copy()
            self.parent.log_message(f"[DEBUG] Got selected collections ({time.time() - start_time:.3f}s)")

            # Add currently selected row if in collections view
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                selection_model = self.column_view.get_model()
                for i in range(selection_model.get_n_items()):
                    if selection_model.is_selected(i):
                        tree_item = selection_model.get_item(i)
                        item = tree_item.get_item()
                        if isinstance(item, PlatformItem):
                            selected_collections.add(item.platform_name)
            self.parent.log_message(f"[DEBUG] Checked row selection ({time.time() - start_time:.3f}s)")

            if selected_collections:
                self.actively_syncing_collections = selected_collections
                self.parent.log_message(f"[DEBUG] Set actively_syncing_collections ({time.time() - start_time:.3f}s)")

                # Update collection labels to show sync status BEFORE starting sync thread
                for collection_name in selected_collections:
                    # Add to currently_downloading_collections to show orange status immediately
                    self.currently_downloading_collections.add(collection_name)
                    self.parent.log_message(f"[DEBUG] Added {collection_name} to currently_downloading ({time.time() - start_time:.3f}s)")
                    self.update_collection_sync_status(collection_name)
                    self.parent.log_message(f"[DEBUG] Updated status for {collection_name} ({time.time() - start_time:.3f}s)")

                self.start_collection_auto_sync()
                self.parent.log_message(f"[DEBUG] Started auto sync ({time.time() - start_time:.3f}s)")
                toggle_button.set_label("Auto-Sync: ON")
                self.parent.log_message(f"🟡 Collection auto-sync enabled for {len(selected_collections)} collections")
                self.parent.log_message(f"[DEBUG] TOTAL TIME: {time.time() - start_time:.3f}s")

                # Clear UI selections after enabling
                self.selected_collections_for_sync.clear()
                self.save_selected_collections()
                self.refresh_collection_checkboxes()
                
        else:
            # Save collections to update before clearing
            collections_to_update = self.actively_syncing_collections.copy()

            self.stop_collection_auto_sync()
            self.actively_syncing_collections.clear()
            self.currently_downloading_collections.clear()

            # Update collection labels to remove sync indicators
            for collection_name in collections_to_update:
                self.update_collection_sync_status(collection_name)

            # Clear UI selections after disabling
            self.selected_collections_for_sync.clear()
            self.save_selected_collections()
            self.refresh_collection_checkboxes()

            toggle_button.set_label("Auto-Sync: OFF")
            self.parent.log_message("🔴 Collection auto-sync disabled")

            # Save the disabled state
            self.save_selected_collections()

    def bind_checkbox_cell_with_sync_status(self, factory, list_item):
        """Enhanced checkbox binding with visual sync status indicators"""
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        checkbox = list_item.get_child()
        
        if isinstance(item, GameItem):
            # Game-level checkboxes (existing logic)
            checkbox.set_visible(True)
            checkbox.game_item = item
            checkbox.tree_item = tree_item
            checkbox.is_platform = False
            
        elif isinstance(item, PlatformItem):
            checkbox.set_visible(True)
            checkbox.platform_item = item
            checkbox.tree_item = tree_item
            checkbox.is_platform = True

            # In collections view, show sync selection (no status colors)
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                # Show different visual states:
                collection_name = item.platform_name
                is_selected = collection_name in self.selected_collections_for_sync
                is_syncing = is_selected and self.collection_auto_sync_enabled

                if is_syncing:
                    tooltip = f"{collection_name} - Selected & Auto-syncing"
                elif is_selected:
                    tooltip = f"{collection_name} - Selected (click button to start sync)"
                else:
                    tooltip = f"{collection_name} - Not selected"

                checkbox.set_tooltip_text(tooltip)
                
                # Connect handler
                def on_collection_sync_toggle(cb):
                    if not getattr(cb, '_updating', False):
                        self.on_collection_checkbox_changed(cb, collection_name)
                
                if not hasattr(checkbox, '_sync_handler_connected'):
                    checkbox.connect('toggled', on_collection_sync_toggle)
                    checkbox._sync_handler_connected = True
            else:
                # Platform view - existing logic
                pass

    def get_collection_sync_status(self, collection_name, games):
        """Determine sync status of a collection"""
        if not games:
            return 'empty'
        
        downloaded_count = sum(1 for game in games if game.get('is_downloaded', False))
        total_count = len(games)
        
        if downloaded_count == 0:
            return 'none'
        elif downloaded_count == total_count:
            return 'complete'
        else:
            return 'partial'

    def update_sync_button_state(self):
        """Update sync state - now using toggle switches instead of button"""
        # Also restore UI state on collections view load
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            # Check if auto-sync should be restored
            saved_state = self.parent.settings.get('Collections', 'auto_sync_enabled', 'false') == 'true'
            if saved_state and self.selected_collections_for_sync and not self.collection_auto_sync_enabled:
                # Trigger restore if not already running
                GLib.timeout_add(1000, self.restore_auto_sync_state)

    def restore_auto_sync_state(self):
        """Restore auto-sync state after app restart"""
        try:
            if (self.parent.romm_client and 
                self.parent.romm_client.authenticated and 
                self.selected_collections_for_sync and
                not self.collection_auto_sync_enabled):

                # 🚫 Clear previous selections
                self.selected_collections_for_sync.clear()

                self.parent.log_message("⚠️ Skipping collection selection restore (auto-sync only)")

                # Just enable the global auto-sync flag
                self.collection_auto_sync_enabled = True

                self.parent.log_message("✅ Auto-sync restored without restoring selections")

        except Exception as e:
            self.parent.log_message(f"⚠️ Failed to restore auto-sync: {e}")

        return False

    def apply_filters(self, games):
        """Apply both platform and search filters to games list"""
        # Debug: count multi-disc games before filtering
        multi_before = sum(1 for g in games if g.get('is_multi_disc', False))

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

        # Debug: count multi-disc games after filtering
        multi_after = sum(1 for g in filtered_games if g.get('is_multi_disc', False))

        return filtered_games

    def on_toggle_selected_collection_auto_sync(self, button):
        """Toggle auto-sync and then clear selections"""
        current_selections = self.get_collections_for_autosync()
        
        if not current_selections:
            self.parent.log_message("Please select collections first")
            return
        
        # Check if selected collections are actively syncing
        actively_syncing = current_selections.intersection(self.actively_syncing_collections)
        
        if actively_syncing:
            # STOP: Remove selected collections from active sync
            self.actively_syncing_collections -= current_selections

            # Stop global sync if no collections left
            if not self.actively_syncing_collections:
                self.stop_collection_auto_sync()

            # Update collection labels to remove sync indicators
            for collection_name in actively_syncing:
                self.update_collection_sync_status(collection_name)

            self.parent.log_message(f"Stopped auto-sync for {len(actively_syncing)} collections")
        else:
            # START: Add selected collections to active sync
            self.actively_syncing_collections.update(current_selections)
            
            # Update selected_collections_for_sync to persist the selection
            self.selected_collections_for_sync.update(current_selections)
            
            # Download missing games for newly selected collections immediately
            for collection_name in current_selections:
                self.download_single_collection_games(collection_name)
                self.parent.log_message(f"📥 Downloading missing games for '{collection_name}'")
            
            # Initialize collection caches for new collections
            def init_new_collections():
                try:
                    all_collections = self.parent.romm_client.get_collections()
                    for collection in all_collections:
                        if collection.get('name') in current_selections:
                            collection_id = collection.get('id')
                            collection_roms = self.parent.romm_client.get_collection_roms(collection_id)
                            cache_key = f'_collection_roms_{collection.get("name")}'
                            setattr(self, cache_key, {rom.get('id') for rom in collection_roms if rom.get('id')})
                except Exception as e:
                    print(f"Error initializing collection cache: {e}")
            
            threading.Thread(target=init_new_collections, daemon=True).start()
            
            # Start global sync if not already running
            if not self.collection_auto_sync_enabled or not self.collection_sync_thread:
                self.start_collection_auto_sync()
            else:
                # Just log that we added to existing sync
                self.collection_auto_sync_enabled = True

            self.parent.log_message(f"Started auto-sync for {len(current_selections)} collections")

            # Update collection status indicators with a delay to ensure data is loaded
            def update_new_collection_statuses():
                for collection_name in current_selections:
                    self.update_collection_sync_status(collection_name)
                return False
            GLib.timeout_add(1000, update_new_collection_statuses)
        
        # Save persistent state and clear UI selections
        self.save_selected_collections()

        # Clear UI selections after any toggle operation
        self.selected_collections_for_sync.clear()
        self.refresh_collection_checkboxes()
        self.update_sync_button_state()

    def disable_autosync_for_collections(self, collections_to_disable):
        """Disable autosync for specific collections (used when deleting ROMs)"""
        if not collections_to_disable:
            return

        # Remove from actively syncing collections
        self.actively_syncing_collections -= collections_to_disable

        # Update collection labels to remove sync indicators
        for collection_name in collections_to_disable:
            self.update_collection_sync_status(collection_name)

        # Stop global sync if no collections left
        if not self.actively_syncing_collections:
            self.stop_collection_auto_sync()

        # Save persistent state
        self.save_selected_collections()

        # Refresh UI
        self.refresh_collection_checkboxes()
        self.update_sync_button_state()

    def on_toggle_sort(self, button):
        """Toggle between alphabetical and download-status sorting"""
        # 1. Save the current UI state before making changes
        scroll_position = 0
        if hasattr(self, 'column_view'):
            scrolled_window = self.column_view.get_parent()
            if scrolled_window:
                vadj = scrolled_window.get_vadjustment()
                if vadj:
                    scroll_position = vadj.get_value()

        # Save the expansion state of the tree
        expansion_state = self.library_model._get_current_expansion_state()

        # 2. Freeze the UI to prevent intermediate redraws
        self.library_model.root_store.freeze_notify()
        if hasattr(self, 'column_view'):
            self.column_view.freeze_notify()

        try:
            # 3. Toggle sort mode
            self.sort_downloaded_first = not self.sort_downloaded_first

            if self.sort_downloaded_first:
                button.set_icon_name("view-sort-descending-symbolic")
                button.set_tooltip_text("Sort: Alphabetical")
            else:
                button.set_icon_name("view-sort-ascending-symbolic")
                button.set_tooltip_text("Sort: Downloaded")

            # 4. Apply sorting to all platform items
            for i in range(self.library_model.root_store.get_n_items()):
                platform_item = self.library_model.root_store.get_item(i)
                if isinstance(platform_item, PlatformItem):
                    # Get filtered games (respect current filter state)
                    if self.show_downloaded_only:
                        filtered_games = [g for g in platform_item.games if g.get('is_downloaded', False)]
                    else:
                        filtered_games = platform_item.games.copy()  # Make a copy to avoid modifying original

                    # Sort the filtered games
                    if self.sort_downloaded_first:
                        filtered_games.sort(key=lambda g: (not g.get('is_downloaded', False), g.get('name', '').lower()))
                    else:
                        filtered_games.sort(key=lambda g: g.get('name', '').lower())

                    # Replace all items in the child store
                    platform_item.child_store.remove_all()
                    for game in filtered_games:
                        platform_item.child_store.append(GameItem(game))

            # Update filtered_games
            self.filtered_games = []
            for i in range(self.library_model.root_store.get_n_items()):
                platform_item = self.library_model.root_store.get_item(i)
                if isinstance(platform_item, PlatformItem):
                    if self.show_downloaded_only:
                        self.filtered_games.extend([g for g in platform_item.games if g.get('is_downloaded', False)])
                    else:
                        self.filtered_games.extend(platform_item.games)

        finally:
            # 5. Thaw notifications - triggers a single batched UI update
            self.library_model.root_store.thaw_notify()
            if hasattr(self, 'column_view'):
                self.column_view.thaw_notify()

        # 6. Restore the UI state after the update
        def restore_state():
            self.library_model._restore_expansion_from_state(expansion_state)

            if hasattr(self, 'column_view'):
                scrolled_window = self.column_view.get_parent()
                if scrolled_window:
                    vadj = scrolled_window.get_vadjustment()
                    if vadj:
                        vadj.set_value(scroll_position)
            return False

        GLib.timeout_add(50, restore_state)

    def has_active_downloads(self):
        """Check if any downloads are currently in progress"""
        if not hasattr(self.parent, 'download_progress'):
            return False
        return any(
            progress.get('downloading', False) 
            for progress in self.parent.download_progress.values()
        )

    def should_cache_collections_at_startup(self):
        """Determine if collections should be cached at startup based on usage patterns"""
        # Only cache if user has used collections view recently
        try:
            last_collection_use = self.parent.settings.get('Collections', 'last_used', '0')
            last_use_time = float(last_collection_use)
            current_time = time.time()
            
            # Cache if used in last 7 days, or if auto-sync is enabled
            recent_use = (current_time - last_use_time) < (7 * 24 * 60 * 60)
            auto_sync_enabled = self.parent.settings.get('Collections', 'auto_sync_enabled', 'false') == 'true'
            
            return recent_use or auto_sync_enabled
        except:
            return False

    def cache_collections_data(self):
        """Cache collections with full processed game caching"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            return
        
        def load_collections_optimized():
            try:
                import time, json
                start_time = time.time()
                
                # Cache file paths
                cache_dir = Path.home() / '.cache' / 'romm_launcher'
                cache_dir.mkdir(parents=True, exist_ok=True)
                server_hash = self.parent.romm_client.base_url.replace('http://', '').replace('https://', '').replace(':', '_').replace('/', '_')
                
                roms_cache_file = cache_dir / f'collections_{server_hash}.json'
                games_cache_file = cache_dir / f'games_{server_hash}.json'
                
                # Try to load processed games cache first (fastest path)
                if games_cache_file.exists():
                    try:
                        cache_age = time.time() - games_cache_file.stat().st_mtime
                        if cache_age < 3600:  # 1 hour
                            with open(games_cache_file, 'r') as f:
                                cached_games = json.load(f)

                            # Update download status by checking filesystem
                            download_dir = Path(self.parent.rom_dir_row.get_text())
                            downloaded_count = 0
                            for game in cached_games:
                                # Use platform_slug if available, otherwise fallback to platform name
                                platform_slug = game.get('platform_slug') or game.get('platform', 'Unknown')
                                file_name = game.get('file_name')
                                if file_name:
                                    local_path = download_dir / platform_slug / file_name
                                    is_downloaded = self.is_path_validly_downloaded(local_path)
                                    game['is_downloaded'] = is_downloaded
                                    game['local_path'] = str(local_path) if is_downloaded else None
                                    # Update size from local file if downloaded
                                    if is_downloaded:
                                        game['local_size'] = self.get_actual_file_size(local_path)
                                        downloaded_count += 1
                                    elif 'local_size' not in game:
                                        game['local_size'] = 0
                                    # Ensure romm_data exists for size display
                                    if 'romm_data' not in game:
                                        game['romm_data'] = {'fs_size_bytes': game.get('local_size', 0)}
                                else:
                                    game['is_downloaded'] = False
                                    game['local_path'] = None
                                    if 'local_size' not in game:
                                        game['local_size'] = 0
                                    if 'romm_data' not in game:
                                        game['romm_data'] = {'fs_size_bytes': 0}

                            print(f"📊 Updated download status: {downloaded_count}/{len(cached_games)} games downloaded")
                            self.collections_games = cached_games
                            self.collections_cache_time = time.time()  # Mark cache as valid
                            print(f"⚡ Loaded {len(self.collections_games)} games from cache in {time.time()-start_time:.2f}s")
                            print(f"✅ Collections ready for instant loading (cache valid for {self.collections_cache_duration}s)")
                            return
                    except:
                        pass
                
                # Load ROM cache if no games cache
                if roms_cache_file.exists():
                    try:
                        with open(roms_cache_file, 'r') as f:
                            self._collections_rom_cache = json.load(f)
                        print(f"📁 Loaded {len(self._collections_rom_cache)} collections from disk")
                    except:
                        self._collections_rom_cache = {}
                else:
                    self._collections_rom_cache = {}
                
                # Get current collections and fetch any new ones
                all_collections = self.parent.romm_client.get_collections()
                custom_collections = [c for c in all_collections if not c.get('is_auto_generated', False)]
                
                collections_to_fetch = []
                for collection in custom_collections:
                    cache_key = f"{collection.get('id')}:{collection.get('name')}"
                    if cache_key not in self._collections_rom_cache:
                        collections_to_fetch.append(collection)
                
                if collections_to_fetch:
                    print(f"⚡ Fetching {len(collections_to_fetch)} new collections")
                    for collection in collections_to_fetch:
                        roms = self.parent.romm_client.get_collection_roms(collection.get('id'))
                        cache_key = f"{collection.get('id')}:{collection.get('name')}"
                        self._collections_rom_cache[cache_key] = roms
                    
                    # Save ROM cache
                    with open(roms_cache_file, 'w') as f:
                        json.dump(self._collections_rom_cache, f)
                
                # Build games list with download status check
                all_collection_games = []
                download_dir = Path(self.parent.rom_dir_row.get_text())

                for collection in custom_collections:
                    cache_key = f"{collection.get('id')}:{collection.get('name')}"
                    for rom in self._collections_rom_cache.get(cache_key, []):
                        # Check if file is actually downloaded
                        platform_slug = rom.get('platform_slug') or rom.get('platform_name', 'Unknown')
                        file_name = rom.get('fs_name')
                        local_path = download_dir / platform_slug / file_name if file_name else None
                        is_downloaded = local_path and self.is_path_validly_downloaded(local_path)

                        # Get file size from local file if downloaded, otherwise from ROM metadata
                        local_size = 0
                        if is_downloaded and local_path:
                            local_size = self.get_actual_file_size(local_path)
                        elif rom.get('fs_size_bytes'):
                            local_size = rom.get('fs_size_bytes')

                        # Store romm_data for total size calculation (used by size_text property)
                        romm_data = {
                            'fs_size_bytes': rom.get('fs_size_bytes', 0) or local_size
                        }

                        game = {
                            'name': Path(rom.get('fs_name', 'unknown')).stem,
                            'rom_id': rom.get('id'),
                            'platform': rom.get('platform_name', 'Unknown'),
                            'platform_slug': platform_slug,  # Store slug for future use
                            'file_name': rom.get('fs_name'),
                            'is_downloaded': is_downloaded,
                            'local_path': str(local_path) if is_downloaded else None,
                            'local_size': local_size,  # Add size info
                            'romm_data': romm_data,  # Add romm_data for total size display
                            'collection': collection.get('name')
                        }
                        all_collection_games.append(game)
                
                # Save processed games cache
                try:
                    with open(games_cache_file, 'w') as f:
                        json.dump(all_collection_games, f)
                except:
                    pass
                
                self.collections_games = all_collection_games
                self.collections_cache_time = time.time()  # Mark cache as valid
                print(f"✅ Ready in {time.time()-start_time:.2f}s ({len(all_collection_games)} games)")
                print(f"✅ Collections ready for instant loading (cache valid for {self.collections_cache_duration}s)")

            except Exception as e:
                print(f"Error: {e}")

        threading.Thread(target=load_collections_optimized, daemon=True).start()

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
                            filtered_platform_games = platform_item.games.copy()  # Make a copy to avoid modifying original

                        # Apply current sort
                        if self.sort_downloaded_first:
                            filtered_platform_games.sort(key=lambda g: (not g.get('is_downloaded', False), g.get('name', '').lower()))
                        else:
                            filtered_platform_games.sort(key=lambda g: g.get('name', '').lower())

                        # Update child store by removing all and re-adding
                        platform_item.child_store.remove_all()
                        for game in filtered_platform_games:
                            platform_item.child_store.append(GameItem(game))

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
            start_time = time.time()
            if sort_downloaded_first:
                result = sorted(games, key=lambda game: (
                    game.get('platform', 'ZZZ_Unknown'),
                    not game.get('is_downloaded', False),  # Downloaded first (False sorts before True)
                    game.get('name', '').lower()
                ))
            else:
                result = sorted(games, key=lambda game: (
                    game.get('platform', 'ZZZ_Unknown'),
                    game.get('name', '').lower()
                ))
            return result

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
        """Update game status display by directly updating cells"""

        # Find and update the GameItem cells directly
        def update_cells():
            model = self.library_model.tree_model
            updated_any = False
            selected_collection = None

            # In collections view, try to determine which collection is currently selected
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                if self.selected_game and self.selected_game.get('rom_id') == rom_id:
                    selected_collection = self.selected_game.get('collection')

            games_found = 0
            for i in range(model.get_n_items() if model else 0):
                tree_item = model.get_item(i)
                if tree_item and tree_item.get_depth() == 1:  # Game level
                    item = tree_item.get_item()
                    if isinstance(item, GameItem):
                        games_found += 1
                        if item.game_data.get('rom_id') == rom_id:
                            # In collections view, prioritize the selected collection
                            if selected_collection and item.game_data.get('collection') != selected_collection:
                                continue

                            item.notify('is-downloaded')
                            item.notify('size-text')
                            item.notify('name')
                            updated_any = True

                            # If we found the selected collection, stop here
                            if selected_collection:
                                break

            # If no selected collection or not found, update all instances
            if not updated_any:
                for i in range(model.get_n_items() if model else 0):
                    tree_item = model.get_item(i)
                    if tree_item and tree_item.get_depth() == 1:
                        item = tree_item.get_item()
                        if isinstance(item, GameItem) and item.game_data.get('rom_id') == rom_id:
                            item.notify('is-downloaded')
                            item.notify('size-text')
                            item.notify('name')

            return False

        GLib.idle_add(update_cells)

    def update_disc_progress(self, rom_id, disc_name, progress_info):
        """Update progress for a specific disc in a multi-disc game"""
        disc_key = f"{rom_id}:{disc_name}"

        if progress_info:
            # Store disc progress separately
            if not hasattr(self, 'disc_progress'):
                self.disc_progress = {}
            self.disc_progress[disc_key] = progress_info
        elif hasattr(self, 'disc_progress') and disc_key in self.disc_progress:
            del self.disc_progress[disc_key]

        # Find and update the specific disc item
        self._update_disc_status_display(rom_id, disc_name)

    def _update_disc_status_display(self, rom_id, disc_name):
        """Update disc status display by directly updating cells"""
        def update_cells():
            model = self.library_model.tree_model

            # Find the game item first
            for i in range(model.get_n_items() if model else 0):
                tree_item = model.get_item(i)
                if tree_item and tree_item.get_depth() == 0:  # Platform level
                    platform_item = tree_item.get_item()
                    if isinstance(platform_item, PlatformItem):
                        # Look through child items (games)
                        for j in range(platform_item.child_store.get_n_items()):
                            game_item = platform_item.child_store.get_item(j)
                            if isinstance(game_item, GameItem) and game_item.game_data.get('rom_id') == rom_id:
                                # Found the game, now look through its discs
                                if hasattr(game_item, 'child_store') and game_item.child_store:
                                    for k in range(game_item.child_store.get_n_items()):
                                        disc_item = game_item.child_store.get_item(k)
                                        if isinstance(disc_item, DiscItem) and disc_item.disc_data.get('name') == disc_name:
                                            # Trigger property notifications to update UI
                                            # This will call the update functions in bind_size_cell and bind_status_cell
                                            disc_item.notify('is-downloaded')
                                            disc_item.notify('size-text')

                                            # Also queue a redraw to ensure visual updates
                                            if hasattr(self, 'column_view'):
                                                self.column_view.queue_draw()
                                            return False
            return False

        GLib.idle_add(update_cells)

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
        multi_count = sum(1 for g in games if g.get('is_multi_disc', False))

        # Debug: show stack trace to see who's calling this
        if len(games) < 20 or multi_count == 0:
            import traceback
            for line in traceback.format_stack()[-5:-1]:
                pass

        with PerformanceTimer(f"update_games_library called with {len(games)} games") as timer:
            if getattr(self.parent, '_dialog_open', False):
                return

            current_mode = getattr(self, 'current_view_mode', 'platform')

            # Apply current filters (platform + search)
            filter_start = time.time()
            games = self.apply_filters(games)
            timer.checkpoint(f"apply_filters: {time.time() - filter_start:.2f}s")
            self.filtered_games = games

            # Apply current platform filter
            plat_filter_start = time.time()
            selected_index = self.platform_filter.get_selected()
            if selected_index != Gtk.INVALID_LIST_POSITION:
                string_list = self.platform_filter.get_model()
                if string_list:
                    selected_platform = string_list.get_string(selected_index)
                    if selected_platform != "All Platforms":
                        games = [game for game in games if game.get('platform', 'Unknown') == selected_platform]
            timer.checkpoint(f"platform filter: {time.time() - plat_filter_start:.2f}s")

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
                update_start = time.time()
                self.library_model.update_library(games)

                group_filter_start = time.time()
                self.update_group_filter(games)  # Use filtered games, not all games

                if games:
                    downloaded_count = sum(1 for g in games if g.get('is_downloaded'))
                    total_count = len(games)

            # Update with selection preservation
            preserve_start = time.time()
            self.preserve_selections_during_update(do_update)
            timer.checkpoint(f"preserve_selections_during_update: {time.time() - preserve_start:.2f}s")

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
        selected_games = []
        selection_model = self.column_view.get_model()

        # Get row selections
        for i in range(selection_model.get_n_items()):
            if selection_model.is_selected(i):
                tree_item = selection_model.get_item(i)
                if tree_item and tree_item.get_depth() == 1:
                    item = tree_item.get_item()
                    if isinstance(item, GameItem):
                        selected_games.append(item.game_data)

        # Add checkbox selections
        for rom_id in self.selected_rom_ids:
            for game in self.parent.available_games:
                if game.get('rom_id') == rom_id and game not in selected_games:
                    selected_games.append(game)
                    break

        return selected_games

    def get_selected_discs(self):
        """Get selected discs from multi-disc games"""
        selected_discs = []
        for key in self.selected_game_keys:
            if key.startswith('disc:'):
                # Parse disc key: disc:{rom_id}:{disc_name}
                parts = key.split(':', 2)
                if len(parts) == 3:
                    rom_id = int(parts[1])
                    disc_name = parts[2]

                    # Find the game and disc
                    for game in self.parent.available_games:
                        if game.get('rom_id') == rom_id and game.get('is_multi_disc'):
                            for disc in game.get('discs', []):
                                if disc.get('name') == disc_name:
                                    selected_discs.append({
                                        'game': game,
                                        'disc': disc
                                    })
                                    break
                            break
        return selected_discs

    def get_game_identifier(self, game_data):
        """Get unique identifier for a game (ROM ID if available, otherwise name+platform)"""
        rom_id = game_data.get('rom_id')
        if rom_id:
            return ('rom_id', rom_id)
        else:
            name = game_data.get('name', '')
            platform = game_data.get('platform', '')
            return ('game_key', f"{name}|{platform}")

    def is_game_in_autosync_collection(self, game_data):
        """Check if a game is in any collection that has autosync enabled"""
        rom_id = game_data.get('rom_id')

        # If no rom_id, check the current collection only
        if not rom_id:
            collection_name = game_data.get('collection', '')
            return collection_name in self.actively_syncing_collections

        # Check all collections that contain this rom_id
        if hasattr(self, 'collections_games'):
            for collection_game in self.collections_games:
                if collection_game.get('rom_id') == rom_id:
                    collection_name = collection_game.get('collection', '')
                    if collection_name in self.actively_syncing_collections:
                        return True

        return False

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

        # Collection/Platform toggle - round toggle group
        toggle_group_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=0)
        toggle_group_box.add_css_class('linked')
        toggle_group_box.add_css_class('pill')

        self.platforms_toggle_btn = Gtk.ToggleButton(label="Platforms")
        self.platforms_toggle_btn.set_active(True)  # Start with Platforms view
        self.platforms_toggle_btn.connect('toggled', self.on_platforms_toggle)
        toggle_group_box.append(self.platforms_toggle_btn)

        self.collections_toggle_btn = Gtk.ToggleButton(label="Collections")
        self.collections_toggle_btn.set_group(self.platforms_toggle_btn)  # Link them as a group
        self.collections_toggle_btn.connect('toggled', self.on_collections_toggle)
        toggle_group_box.append(self.collections_toggle_btn)

        toolbar_box.append(toggle_group_box)

        # Store reference for backward compatibility
        self.view_mode_toggle = self.collections_toggle_btn
        
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
        refresh_btn.set_tooltip_text("Refresh library from server")
        refresh_btn.connect('clicked', self.on_refresh_library)
        view_box.append(refresh_btn)
        
        toolbar_box.append(view_box)

        # Collection sync controls removed - now using toggle switches directly
        
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
        checkbox_column.set_fixed_width(60)  # Increased width to accommodate toggle switches
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

        # Sync Status column (for collections only)
        sync_status_factory = Gtk.SignalListItemFactory()
        sync_status_factory.connect('setup', self.setup_sync_status_cell)
        sync_status_factory.connect('bind', self.bind_sync_status_cell)
        self.sync_status_column = Gtk.ColumnViewColumn.new("Sync", sync_status_factory)
        self.sync_status_column.set_fixed_width(50)
        self.sync_status_column.set_visible(False)  # Hidden by default (platform view)
        self.column_view.append_column(self.sync_status_column)

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

    def save_current_selection(self):
        """Save current row selections, checkbox states, and expanded states based on view mode"""
        selection_model = self.column_view.get_model()
        if not selection_model:
            return

        selected_indices = set()
        expanded_items = set()

        # Save both selections and expanded states
        for i in range(selection_model.get_n_items()):
            tree_item = selection_model.get_item(i)
            if tree_item:
                # Save selection
                if selection_model.is_selected(i):
                    selected_indices.add(i)
                # Save expanded state for top-level items (platforms/collections)
                if tree_item.get_depth() == 0 and tree_item.get_expanded():
                    item = tree_item.get_item()
                    if isinstance(item, PlatformItem):
                        expanded_items.add(item.platform_name)

        if self.current_view_mode == 'platform':
            self.platform_view_selection = selected_indices
            self.platform_view_checkboxes = self.selected_checkboxes.copy()
            self.platform_view_rom_ids = self.selected_rom_ids.copy()
            self.platform_view_game_keys = self.selected_game_keys.copy()
            self.platform_view_selected_game = self.selected_game
            self.platform_view_expanded = expanded_items
        else:
            self.collection_view_selection = selected_indices
            self.collection_view_checkboxes = self.selected_checkboxes.copy()
            self.collection_view_rom_ids = self.selected_rom_ids.copy()
            self.collection_view_game_keys = self.selected_game_keys.copy()
            self.collection_view_selected_game = self.selected_game
            self.collection_view_expanded = expanded_items

    def restore_saved_selection(self):
        """Restore saved row selections, checkbox states, and expanded states based on view mode"""
        selection_model = self.column_view.get_model()
        if not selection_model:
            return

        if self.current_view_mode == 'platform':
            saved_selection = self.platform_view_selection
            saved_expanded = self.platform_view_expanded
            self.selected_checkboxes = self.platform_view_checkboxes.copy()
            self.selected_rom_ids = self.platform_view_rom_ids.copy()
            self.selected_game_keys = self.platform_view_game_keys.copy()
            self.selected_game = self.platform_view_selected_game
        else:
            saved_selection = self.collection_view_selection
            saved_expanded = self.collection_view_expanded
            self.selected_checkboxes = self.collection_view_checkboxes.copy()
            self.selected_rom_ids = self.collection_view_rom_ids.copy()
            self.selected_game_keys = self.collection_view_game_keys.copy()
            self.selected_game = self.collection_view_selected_game

        # Restore expanded state for platforms/collections ONLY if they were expanded before
        if saved_expanded:
            n_items = selection_model.get_n_items()
            for i in range(n_items):
                tree_item = selection_model.get_item(i)
                if tree_item and tree_item.get_depth() == 0:
                    item = tree_item.get_item()
                    if isinstance(item, PlatformItem):
                        if item.platform_name in saved_expanded:
                            tree_item.set_expanded(True)

        # Restore row selection for saved indices
        if saved_selection:
            n_items = selection_model.get_n_items()
            for index in saved_selection:
                if index < n_items:
                    selection_model.select_item(index, False)  # False = don't unselect others

        # Update UI to reflect restored selections
        self.update_selection_label()
        self.update_action_buttons()

    def on_platforms_toggle(self, toggle_button):
        """Handle Platforms button toggle"""
        if toggle_button.get_active():
            self.switch_to_platform_view()

    def on_collections_toggle(self, toggle_button):
        """Handle Collections button toggle"""
        if toggle_button.get_active():
            self.switch_to_collection_view()

    def switch_to_collection_view(self):
        """Switch to collection view"""
        # Save current selection before switching
        self.save_current_selection()

        # Increment generation counter to invalidate any pending background loads
        self.view_mode_generation += 1

        self.current_view_mode = 'collection'

        # Hide platform filter in collections view
        if hasattr(self, 'platform_filter'):
            self.platform_filter.set_visible(False)

        # IMMEDIATELY clear the tree to remove platform view data
        self.library_model.root_store.remove_all()

        # Clear all selections when switching views
        selection_model = self.column_view.get_model()
        if selection_model:
            selection_model.unselect_all()
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.selected_game = None
        self.selected_collection = None
        # Update UI immediately to reflect cleared selections
        self.update_selection_label()
        self.update_action_buttons()

        # Force GTK to render the empty tree NOW
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

        # Collection sync controls removed - using toggle switches now

        # Show sync status column (only for collections)
        if hasattr(self, 'sync_status_column'):
            self.sync_status_column.set_visible(True)

        self.load_collections_view()

        # Restore saved selection and refresh checkboxes after the view is loaded
        def restore_and_refresh():
            self.restore_saved_selection()
            # Force checkbox sync after data is loaded and expanded
            GLib.timeout_add(100, self.force_checkbox_sync)
            GLib.timeout_add(200, self.force_checkbox_sync)
            GLib.timeout_add(300, self.force_checkbox_sync)
            return False
        GLib.idle_add(restore_and_refresh)

    def switch_to_platform_view(self):
        """Switch to platform view"""
        # Save current selection before switching
        self.save_current_selection()

        # Increment generation counter to invalidate any pending background loads
        self.view_mode_generation += 1

        self.current_view_mode = 'platform'

        # Show platform filter in platform view
        if hasattr(self, 'platform_filter'):
            self.platform_filter.set_visible(True)

        # Collection sync controls removed - using toggle switches now

        # Hide sync status column (not needed for platforms)
        if hasattr(self, 'sync_status_column'):
            self.sync_status_column.set_visible(False)

        # Clear all selections when switching views
        selection_model = self.column_view.get_model()
        if selection_model:
            selection_model.unselect_all()
        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.selected_game = None
        self.selected_collection = None
        # Update UI immediately to reflect cleared selections
        self.update_selection_label()
        self.update_action_buttons()

        original_games = self.parent.available_games.copy()
        self.library_model.update_library(original_games, group_by='platform')

        # Restore saved selection and refresh checkboxes after the view is loaded
        def restore_and_refresh():
            self.restore_saved_selection()
            # Force checkbox sync after data is loaded and expanded
            GLib.timeout_add(100, self.force_checkbox_sync)
            GLib.timeout_add(200, self.force_checkbox_sync)
            GLib.timeout_add(300, self.force_checkbox_sync)
            return False
        GLib.idle_add(restore_and_refresh)

    def on_view_mode_toggled(self, toggle_button):
        """Switch between platform and collection view"""
        # Save current selection before switching
        self.save_current_selection()

        # Increment generation counter to invalidate any pending background loads
        self.view_mode_generation += 1

        if toggle_button.get_active():
            # Collections view
            self.current_view_mode = 'collection'

            # IMMEDIATELY clear the tree to remove platform view data
            self.library_model.root_store.remove_all()

            # Clear all selections when switching views
            selection_model = self.column_view.get_model()
            if selection_model:
                selection_model.unselect_all()
            self.selected_checkboxes.clear()
            self.selected_rom_ids.clear()
            self.selected_game_keys.clear()
            self.selected_game = None
            self.selected_collection = None
            # Update UI immediately to reflect cleared selections
            self.update_selection_label()
            self.update_action_buttons()

            # Force GTK to render the empty tree NOW
            context = GLib.MainContext.default()
            while context.pending():
                context.iteration(False)

            # Collection sync controls removed - using toggle switches now

            # Show sync status column (only for collections)
            if hasattr(self, 'sync_status_column'):
                self.sync_status_column.set_visible(True)

            self.load_collections_view()

            # Restore saved selection and refresh checkboxes after the view is loaded
            def restore_and_refresh():
                self.restore_saved_selection()
                # Force checkbox sync after data is loaded and expanded
                GLib.timeout_add(100, self.force_checkbox_sync)
                GLib.timeout_add(200, self.force_checkbox_sync)
                GLib.timeout_add(300, self.force_checkbox_sync)
                return False
            GLib.idle_add(restore_and_refresh)
        else:
            # Platform view
            toggle_button.set_label("Collections")
            self.current_view_mode = 'platform'

            # Collection sync controls removed - using toggle switches now

            # Hide sync status column (not needed for platforms)
            if hasattr(self, 'sync_status_column'):
                self.sync_status_column.set_visible(False)

            # Clear all selections when switching views
            selection_model = self.column_view.get_model()
            if selection_model:
                selection_model.unselect_all()
            self.selected_checkboxes.clear()
            self.selected_rom_ids.clear()
            self.selected_game_keys.clear()
            self.selected_game = None
            self.selected_collection = None
            # Update UI immediately to reflect cleared selections
            self.update_selection_label()
            self.update_action_buttons()

            original_games = self.parent.available_games.copy()
            self.library_model.update_library(original_games, group_by='platform')

            # Restore saved selection and refresh checkboxes after the view is loaded
            def restore_and_refresh():
                self.restore_saved_selection()
                # Force checkbox sync after data is loaded and expanded
                GLib.timeout_add(100, self.force_checkbox_sync)
                GLib.timeout_add(200, self.force_checkbox_sync)
                GLib.timeout_add(300, self.force_checkbox_sync)
                return False
            GLib.idle_add(restore_and_refresh)

    def load_collections_view(self):
        """Load and display custom collections only"""
        if not (self.parent.romm_client and self.parent.romm_client.authenticated):
            self.parent.log_message("Please connect to RomM to view collections")
            return

        # FIXED: More robust cache check
        import time
        current_time = time.time()

        # Initialize cache attributes if missing
        if not hasattr(self, 'collections_games'):
            self.collections_games = []
        if not hasattr(self, 'collections_cache_time'):
            self.collections_cache_time = 0
        if not hasattr(self, 'collections_cache_duration'):
            self.collections_cache_duration = 300

        cache_valid = (
            self.collections_games and  # Has cached data
            current_time - self.collections_cache_time < self.collections_cache_duration
        )

        # If cache is valid, show it immediately without placeholder (no flicker)
        if cache_valid:
            # Build sync status map for cached data
            cached_sync_status = {}
            collection_games_map = {}  # Group games by collection

            # Group games by collection
            for game in self.collections_games:
                collection_name = game.get('collection', 'Unknown')
                if collection_name not in collection_games_map:
                    collection_games_map[collection_name] = []
                collection_games_map[collection_name].append(game)

            # Calculate sync status for each collection
            for collection_name, games in collection_games_map.items():
                is_syncing = collection_name in self.actively_syncing_collections
                if is_syncing:
                    # Check if all games are downloaded
                    downloaded_count = sum(1 for game in games if game.get('is_downloaded', False))
                    total_count = len(games)
                    all_downloaded = downloaded_count == total_count
                    cached_sync_status[collection_name] = 'synced' if all_downloaded else 'syncing'
                else:
                    cached_sync_status[collection_name] = 'disabled'

            self.library_model.update_library(self.collections_games, group_by='collection', sync_status_map=cached_sync_status)
            return

        # Show loading placeholder for cases where we need to load data
        self.parent.log_message("Loading collections...")
        placeholder_games = [{
            'name': 'Loading...',
            'rom_id': 'placeholder_loading',
            'collection': 'Loading collections...',
            'is_downloaded': False,
            'platform': '',
            'file_name': ''
        }]
        self.library_model.update_library(placeholder_games, group_by='collection', loading=True)

        # CRITICAL: Force GTK to process pending events and render the placeholder
        context = GLib.MainContext.default()
        while context.pending():
            context.iteration(False)

        # Capture the current generation to check if this load is still valid when it completes
        expected_generation = self.view_mode_generation

        def load_collections():
            try:
                # Get collection list first
                all_collections = self.parent.romm_client.get_collections()

                # Filter to only custom collections
                custom_collections = []
                for collection in all_collections:
                    is_custom = (
                        not collection.get('is_auto_generated', False) and
                        collection.get('type') != 'auto' and
                        'auto' not in collection.get('name', '').lower()
                    )
                    if is_custom:
                        custom_collections.append(collection)

                if not custom_collections:
                    GLib.idle_add(lambda: self.parent.log_message("No custom collections found"))
                    GLib.idle_add(lambda: self.library_model.update_library([], group_by='collection'))
                    return

                # Update placeholders with actual collection names (still loading games)
                def show_collection_placeholders():
                    # Create placeholder for each collection with real name
                    placeholder_games = []
                    for collection in custom_collections:
                        placeholder_game = {
                            'name': 'Loading...',
                            'rom_id': f'placeholder_{collection.get("id")}',
                            'collection': collection.get('name', 'Unknown Collection'),
                            'is_downloaded': False,
                            'platform': '',
                            'file_name': ''
                        }
                        placeholder_games.append(placeholder_game)

                    # Update tree with named placeholders (shows "Loading..." in status/size)
                    self.library_model.update_library(placeholder_games, group_by='collection', loading=True)
                    return False

                GLib.idle_add(show_collection_placeholders)

                # Create lookup map of existing games by ROM ID for download status
                existing_games_map = {}
                for game in self.parent.available_games:
                    rom_id = game.get('rom_id')
                    if rom_id:
                        existing_games_map[rom_id] = game
                
                all_collection_games = []
                collection_sync_status = {}  # Map collection name to sync status

                for collection in custom_collections:
                    collection_id = collection.get('id')
                    collection_name = collection.get('name', 'Unknown Collection')

                    collection_roms = self.parent.romm_client.get_collection_roms(collection_id)

                    # Determine sync status for this collection
                    is_syncing = collection_name in self.actively_syncing_collections
                    if is_syncing:
                        # Check if fully synced
                        downloaded_count = 0
                        download_dir = Path(self.parent.rom_dir_row.get_text())

                        for rom in collection_roms:
                            platform_slug = rom.get('platform_slug', 'Unknown')
                            file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
                            platform_dir = download_dir / platform_slug
                            local_path = platform_dir / file_name
                            if self.parent.is_path_validly_downloaded(local_path):
                                downloaded_count += 1

                        if downloaded_count == len(collection_roms) and len(collection_roms) > 0:
                            collection_sync_status[collection_name] = 'synced'
                        else:
                            collection_sync_status[collection_name] = 'syncing'
                    else:
                        collection_sync_status[collection_name] = 'disabled'

                    for rom in collection_roms:
                        # First process the ROM normally
                        processed_game = self.parent.process_single_rom(rom, Path(self.parent.rom_dir_row.get_text()))

                        # Then merge with existing game data to preserve download status
                        rom_id = rom.get('id')
                        if rom_id and rom_id in existing_games_map:
                            existing_game = existing_games_map[rom_id]
                            # Preserve critical download info from existing game
                            processed_game['is_downloaded'] = existing_game.get('is_downloaded', False)
                            processed_game['local_path'] = existing_game.get('local_path')
                            processed_game['local_size'] = existing_game.get('local_size', 0)

                        # Add collection info
                        processed_game['collection'] = collection_name
                        all_collection_games.append(processed_game)
                
                # Store collections games separately AND update the instance variable
                all_collection_games_copy = []
                for game in all_collection_games:
                    all_collection_games_copy.append(game.copy())

                def update_collections_data():
                    # Check if this load is still valid (view mode hasn't changed)
                    if self.view_mode_generation != expected_generation:
                        print(f"⚠️ Discarding stale collections load (generation mismatch: {expected_generation} vs {self.view_mode_generation})")
                        return False

                    # Only update if we're still in collections view
                    if self.current_view_mode != 'collection':
                        print(f"⚠️ Discarding collections load (no longer in collections view)")
                        return False

                    import time
                    self.collections_games = all_collection_games_copy
                    self.collections_cache_time = time.time()  # Update cache timestamp
                    self.library_model.update_library(self.collections_games, group_by='collection', sync_status_map=collection_sync_status)
                    self.parent.log_message(f"Loaded {len(custom_collections)} custom collections with {len(all_collection_games)} games")
                    return False

                GLib.idle_add(update_collections_data)

            except Exception as e:
                def log_error():
                    # Only log error if still in the same view generation
                    if self.view_mode_generation == expected_generation:
                        self.parent.log_message(f"Failed to load collections: {e}")
                    return False
                GLib.idle_add(log_error)
        
        threading.Thread(target=load_collections, daemon=True).start()

        # Check if auto-sync should be restored when switching to collections view
        self.update_sync_button_state()        

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

            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                # Dynamic label update for collections - just show the name, status dots will show the state
                def update_collection_label(*args):
                    collection_name = item.platform_name
                    label.set_text(collection_name)

                # Connect to name property changes to trigger label updates
                item.connect('notify::name', update_collection_label)
                update_collection_label()  # Initial update
            else:
                # For platforms view, use simple binding
                item.bind_property('name', label, 'label', GObject.BindingFlags.SYNC_CREATE)
        elif isinstance(item, DiscItem):
            # For disc items, show media-optical icon
            icon.set_from_icon_name("media-optical-symbolic")

            def update_disc_label(*args):
                label.set_text(item.name)

            item.connect('notify::name', update_disc_label)
            update_disc_label()
        else:
            # For games (GameItem), set up dynamic icon updates
            def update_icon_and_name(*args):
                # Update icon based on download status or multi-disc
                if item.game_data.get('is_multi_disc', False):
                    # Multi-disc game - show optical disc icon
                    icon.set_from_icon_name("media-optical-symbolic")
                elif item.game_data.get('is_downloaded', False):
                    icon.set_from_icon_name("object-select-symbolic")
                else:
                    icon.set_from_icon_name("folder-download-symbolic")

                # Update label
                label.set_text(item.name)

            # Connect to property changes that might affect the icon
            item.connect('notify::name', update_icon_and_name)
            item.connect('notify::is-downloaded', update_icon_and_name)

            # Initial update
            update_icon_and_name()

    def bind_status_cell(self, factory, list_item):
        """Show percentage/icons using Cairo drawing"""
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        box = list_item.get_child()

        # Get the drawing area and label from the box
        drawing_area = box.get_first_child()
        label = drawing_area.get_next_sibling()

        if isinstance(item, PlatformItem):
            # For platforms, show text status
            drawing_area.set_visible(False)
            label.set_visible(True)
            item.bind_property('status-text', label, 'label', GObject.BindingFlags.SYNC_CREATE)
        elif isinstance(item, DiscItem):
            # For discs, show download status with progress support
            label.set_visible(False)
            drawing_area.set_visible(True)

            def update_disc_status(*args):
                # Check for disc download progress
                rom_id = item.parent_game.get('rom_id') if item.parent_game else None
                disc_name = item.disc_data.get('name')
                disc_key = f"{rom_id}:{disc_name}" if rom_id and disc_name else None

                progress_info = None
                if disc_key and hasattr(self, 'disc_progress'):
                    progress_info = self.disc_progress.get(disc_key)

                if progress_info and progress_info.get('downloading'):
                    # Show percentage using Cairo (orange)
                    progress = progress_info.get('progress', 0.0)
                    self.parent.draw_download_status_icon(drawing_area, 'downloading', progress)
                elif progress_info and progress_info.get('completed'):
                    # Show green checkmark icon
                    self.parent.draw_download_status_icon(drawing_area, 'completed')
                elif item.is_downloaded:
                    # Downloaded disc
                    self.parent.draw_download_status_icon(drawing_area, 'downloaded')
                else:
                    # Not downloaded
                    self.parent.draw_download_status_icon(drawing_area, 'not_downloaded')

            # Connect to property changes
            item.connect('notify::is-downloaded', update_disc_status)
            update_disc_status()
        elif isinstance(item, GameItem):
            # Connect to name property changes to update status
            def update_status(*args):
                rom_id = item.game_data.get('rom_id')
                progress_info = self.parent.download_progress.get(rom_id) if rom_id else None

                # Always hide label and use drawing area for games
                label.set_visible(False)
                drawing_area.set_visible(True)

                if progress_info and progress_info.get('downloading'):
                    # Show percentage using Cairo (orange)
                    progress = progress_info.get('progress', 0.0)
                    self.parent.draw_download_status_icon(drawing_area, 'downloading', progress)
                elif progress_info and progress_info.get('completed'):
                    # Show green checkmark icon
                    self.parent.draw_download_status_icon(drawing_area, 'completed')
                elif progress_info and progress_info.get('failed'):
                    # Show red X icon
                    self.parent.draw_download_status_icon(drawing_area, 'failed')
                else:
                    # Show download status icon
                    status_type = 'downloaded' if item.is_downloaded else 'not_downloaded'
                    self.parent.draw_download_status_icon(drawing_area, status_type)

            item.connect('notify::name', update_status)
            update_status()  # Initial update

    def bind_size_cell(self, factory, list_item):
        """Show download info with compact format"""
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        label = list_item.get_child()
        
        if isinstance(item, PlatformItem):
            item.bind_property('size-text', label, 'label', GObject.BindingFlags.SYNC_CREATE)
        elif isinstance(item, DiscItem):
            # For discs, show size with progress support
            def update_disc_size(*args):
                rom_id = item.parent_game.get('rom_id') if item.parent_game else None
                disc_name = item.disc_data.get('name')
                disc_key = f"{rom_id}:{disc_name}" if rom_id and disc_name else None

                progress_info = None
                if disc_key and hasattr(self, 'disc_progress'):
                    progress_info = self.disc_progress.get(disc_key)

                if progress_info and progress_info.get('downloading'):
                    downloaded = progress_info.get('downloaded', 0)
                    total = progress_info.get('total', 0)
                    speed = progress_info.get('speed', 0)

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

                    if speed > 0:
                        speed_str = format_size_compact(speed)
                        final_text = f"{size_text} @{speed_str}/s"
                    else:
                        final_text = f"{size_text} ..."

                    label.set_text(final_text)
                else:
                    size_text = item.size_text
                    label.set_text(size_text)

            item.connect('notify::size-text', update_disc_size)
            item.connect('notify::is-downloaded', update_disc_size)
            update_disc_size()  # Initial update
        elif isinstance(item, GameItem):
            def update_size(*args):
                rom_id = item.game_data.get('rom_id')
                progress_info = self.parent.download_progress.get(rom_id) if rom_id else None
                
                if progress_info and progress_info.get('downloading'):
                    downloaded = progress_info.get('downloaded', 0)
                    total = progress_info.get('total', 0)
                    speed = progress_info.get('speed', 0)
                    
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
                    
                    if speed > 0:
                        speed_str = format_size_compact(speed)
                        final_text = f"{size_text} @{speed_str}/s"
                    else:
                        final_text = f"{size_text} ..."
                        
                    label.set_text(final_text)
                else:
                    size_text = item.size_text
                    label.set_text(size_text)
            
            item.connect('notify::name', update_size)
            update_size()  # Initial update

    def setup_status_cell(self, factory, list_item):
        """Cairo-drawn status icons for download status"""
        # Create a box to hold either a drawing area or label (for percentage)
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        # Create drawing area for icons and text (wider to accommodate percentage)
        drawing_area = Gtk.DrawingArea()
        drawing_area.set_size_request(50, 16)
        drawing_area.set_halign(Gtk.Align.CENTER)
        drawing_area.set_valign(Gtk.Align.CENTER)
        box.append(drawing_area)

        # Create label for percentage text (hidden by default)
        label = Gtk.Label()
        label.set_halign(Gtk.Align.CENTER)
        label.add_css_class('numeric')
        label.set_visible(False)
        box.append(label)

        list_item.set_child(box)


    def setup_size_cell(self, factory, list_item):
        label = Gtk.Label()
        label.set_halign(Gtk.Align.END)
        label.add_css_class('numeric')
        list_item.set_child(label)

    def setup_sync_status_cell(self, factory, list_item):
        """Setup sync status indicator for collections"""
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        # Create a drawing area for the colored dot
        drawing_area = Gtk.DrawingArea()
        drawing_area.set_size_request(10, 10)
        drawing_area.set_halign(Gtk.Align.CENTER)
        drawing_area.set_valign(Gtk.Align.CENTER)

        box.append(drawing_area)
        list_item.set_child(box)

    def bind_sync_status_cell(self, factory, list_item):
        """Bind sync status for collections"""
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        box = list_item.get_child()
        drawing_area = box.get_first_child()

        if isinstance(item, PlatformItem):
            def update_status(*args):
                status = item.sync_status_text

                def draw_func(area, cr, width, height):
                    # Determine color based on status
                    if status == 'synced':
                        cr.set_source_rgb(0.29, 0.86, 0.50)  # Green (#4ade80)
                    elif status == 'syncing':
                        cr.set_source_rgb(0.98, 0.57, 0.24)  # Orange (#fb923c)
                    elif status == 'disabled':
                        cr.set_source_rgb(0.42, 0.45, 0.50)  # Grey (#6b7280)
                    elif status == 'loading':
                        cr.set_source_rgb(0.6, 0.6, 0.6)  # Light grey
                    else:
                        return  # Don't draw anything for empty status

                    # Draw a filled circle
                    radius = min(width, height) / 2.0
                    cr.arc(width / 2.0, height / 2.0, radius - 1, 0, 2 * 3.14159)
                    cr.fill()

                drawing_area.set_draw_func(draw_func)
                drawing_area.queue_draw()

            item.connect('notify::sync-status-text', update_status)
            update_status()  # Initial draw
        elif isinstance(item, GameItem):
            # Games don't have sync status - don't draw anything
            drawing_area.set_draw_func(lambda *args: None)

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
        self._action_button_handler_id = self.action_button.connect('clicked', self.on_action_clicked)
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
        # Allow button updates during bulk downloads to show Cancel state
        # but still block during other dialogs
        is_bulk_download = self.parent._bulk_download_in_progress if hasattr(self, 'parent') else False
        if getattr(self, '_selection_blocked', False) and not is_bulk_download:
            return

        # Priority 0: Check for selected discs first
        selected_discs = self.get_selected_discs()
        if selected_discs:
            # Check if any disc is not downloaded
            not_downloaded_discs = [d for d in selected_discs if not d['disc'].get('is_downloaded', False)]
            is_connected = self.parent.romm_client and self.parent.romm_client.authenticated

            # Clear all button style classes first
            self.action_button.remove_css_class('warning')
            self.action_button.remove_css_class('suggested-action')
            self.action_button.remove_css_class('destructive-action')

            if not_downloaded_discs and is_connected:
                self.action_button.set_label(f"Download ({len(not_downloaded_discs)})")
                self.action_button.add_css_class('warning')
                self.action_button.set_sensitive(True)
            else:
                self.action_button.set_label("Download")
                self.action_button.set_sensitive(False)

            # Enable delete if any disc is downloaded
            downloaded_discs = [d for d in selected_discs if d['disc'].get('is_downloaded', False)]
            self.delete_button.set_sensitive(len(downloaded_discs) > 0)
            self.open_in_romm_button.set_sensitive(False)
            return  # Exit early

        # ADD THIS BLOCK HERE:
        # Priority 1: Check for single row selection first
        if self.selected_game:
            # Check if a specific disc is selected
            if self.selected_disc:
                # Individual disc selected - show Launch button if downloaded
                is_disc_downloaded = self.selected_disc.get('is_downloaded', False)
                is_connected = self.parent.romm_client and self.parent.romm_client.authenticated

                # Clear all button style classes first
                self.action_button.remove_css_class('warning')
                self.action_button.remove_css_class('suggested-action')
                self.action_button.remove_css_class('destructive-action')

                if is_disc_downloaded:
                    self.action_button.set_label("Launch")
                    self.action_button.add_css_class('suggested-action')
                    self.action_button.set_sensitive(True)
                else:
                    # Disc not downloaded - disable action button (cannot download individual discs)
                    self.action_button.set_label("Download")
                    self.action_button.set_sensitive(False)

                # Disable delete and open for individual discs
                self.delete_button.set_sensitive(False)
                self.open_in_romm_button.set_sensitive(False)
                return  # Exit early

            # Game selected (not a disc)
            is_downloaded = self.selected_game.get('is_downloaded', False)
            is_connected = self.parent.romm_client and self.parent.romm_client.authenticated
            rom_id = self.selected_game.get('rom_id')

            # Check if download is in progress FOR THIS SPECIFIC GAME
            is_downloading = (rom_id and rom_id in self.parent.download_progress and
                            self.parent.download_progress[rom_id].get('downloading', False))

            # Check if this is part of a bulk download
            is_bulk_download = self.parent._bulk_download_in_progress

            # Clear all button style classes first
            self.action_button.remove_css_class('warning')
            self.action_button.remove_css_class('suggested-action')
            self.action_button.remove_css_class('destructive-action')

            if is_downloading:
                # Always show "Cancel" for single row selection
                # (bulk downloads are handled in the multiple checkbox selection case)
                self.action_button.set_label("Cancel")
                self.action_button.add_css_class('destructive-action')
            elif is_downloaded:
                self.action_button.set_label("Launch")
                self.action_button.add_css_class('suggested-action')
            else:
                self.action_button.set_label("Download")
                self.action_button.add_css_class('warning')

            self.action_button.set_sensitive(True)
            # Check if game is in autosync collection - disable delete if so
            is_in_autosync = self.is_game_in_autosync_collection(self.selected_game)
            self.delete_button.set_sensitive(is_downloaded and not is_in_autosync)
            self.open_in_romm_button.set_sensitive(is_connected and self.selected_game.get('rom_id'))
            return  # Exit early, don't check other selections
        
        selected_games = self.get_selected_games()
        
        is_connected = self.parent.romm_client and self.parent.romm_client.authenticated
        
        # Priority 2: Check for checkbox selections first (to determine priority)
        selected_games = []

        # FIX: Use correct games source based on view mode
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            games_to_check = getattr(self, 'collections_games', [])
        else:
            games_to_check = self.parent.available_games

        for game in games_to_check:  # CHANGED: was self.parent.available_games
            # Handle collection mode differently
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                rom_id = game.get('rom_id')
                collection_name = game.get('collection', '')
                if rom_id and collection_name:
                    collection_key = f"collection:{rom_id}:{collection_name}"
                    if collection_key in self.selected_game_keys:
                        selected_games.append(game)
            else:
                # Standard platform mode logic
                identifier_type, identifier_value = self.get_game_identifier(game)
                if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                    selected_games.append(game)
                elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                    selected_games.append(game)
        
        # Priority 2: Handle checkbox selections (takes precedence when present)
        if selected_games:
            downloaded_games = [g for g in selected_games if g.get('is_downloaded', False)]
            # Exclude games that are currently downloading from not_downloaded list
            downloading_rom_ids = set(rom_id for rom_id in self.parent.download_progress.keys()
                                     if self.parent.download_progress[rom_id].get('downloading', False))
            not_downloaded_games = [g for g in selected_games
                                   if not g.get('is_downloaded', False)
                                   and g.get('rom_id') not in downloading_rom_ids]

            if len(selected_games) == 1:
                # Single checkbox selection
                game = selected_games[0]
                is_downloaded = game.get('is_downloaded', False)
                rom_id = game.get('rom_id')

                # ADD THIS CHECK for collections view:
                if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                    # In collections, ensure we check the actual download status
                    if rom_id:
                        # Cross-reference with main games list for accurate download status
                        for main_game in self.parent.available_games:
                            if main_game.get('rom_id') == rom_id:
                                is_downloaded = main_game.get('is_downloaded', False)
                                break

                # Check if download is in progress FOR THIS SPECIFIC GAME
                is_downloading = (rom_id and rom_id in self.parent.download_progress and
                                self.parent.download_progress[rom_id].get('downloading', False))

                # Check if this is part of a bulk download
                is_bulk_download = self.parent._bulk_download_in_progress

                # Clear all button style classes first
                self.action_button.remove_css_class('warning')
                self.action_button.remove_css_class('suggested-action')
                self.action_button.remove_css_class('destructive-action')

                if is_downloading:
                    # Always show "Cancel" for single selection
                    # (bulk downloads are handled in the multiple selection case)
                    self.action_button.set_label("Cancel")
                    self.action_button.add_css_class('destructive-action')
                elif is_downloaded:
                    self.action_button.set_label("Launch")
                    self.action_button.add_css_class('suggested-action')
                else:
                    self.action_button.set_label("Download")
                    self.action_button.add_css_class('warning')

                self.action_button.set_sensitive(True)
                # Check if game is in autosync collection - disable delete if so
                is_in_autosync = self.is_game_in_autosync_collection(game)
                self.delete_button.set_sensitive(is_downloaded and not is_in_autosync)
                self.open_in_romm_button.set_sensitive(is_connected and game.get('rom_id'))
            else:
                # Multiple checkbox selections
                # Check if this is part of a bulk download
                is_bulk_download = self.parent._bulk_download_in_progress

                # Check if any selected games are currently downloading
                downloading_games = [g for g in selected_games
                                   if g.get('rom_id') and g.get('rom_id') in self.parent.download_progress
                                   and self.parent.download_progress[g.get('rom_id')].get('downloading', False)]

                # Clear all button style classes first
                self.action_button.remove_css_class('warning')
                self.action_button.remove_css_class('suggested-action')
                self.action_button.remove_css_class('destructive-action')

                # Prioritize bulk download state - show Cancel All even if individual downloads haven't started yet
                if is_bulk_download:
                    self.action_button.set_label("Cancel All")
                    self.action_button.add_css_class('destructive-action')
                    self.action_button.set_sensitive(True)
                elif downloading_games:
                    # Multiple individual downloads (not part of bulk)
                    self.action_button.set_label(f"Cancel ({len(downloading_games)})")
                    self.action_button.add_css_class('destructive-action')
                    self.action_button.set_sensitive(True)
                elif not_downloaded_games:
                    self.action_button.set_label(f"Download ({len(not_downloaded_games)})")
                    self.action_button.add_css_class('warning')
                    self.action_button.set_sensitive(True)
                elif downloaded_games:
                    self.action_button.set_label("Launch")
                    self.action_button.set_sensitive(False)

                # Check if any selected games are in autosync collections
                has_autosync_game = any(self.is_game_in_autosync_collection(g) for g in selected_games)
                self.delete_button.set_sensitive(len(downloaded_games) > 0 and not has_autosync_game)
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
                # Check if this is a collection (not a platform)
                is_collection_view = hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection'

                if is_collection_view:
                    # Collection selected - enable delete button
                    collection_name = item.platform_name
                    has_downloaded_games = any(g.get('is_downloaded', False) for g in item.games)
                    self.action_button.set_sensitive(False)
                    self.delete_button.set_sensitive(has_downloaded_games)
                    self.delete_button.set_tooltip_text(f"Delete downloaded games from '{collection_name}'")
                    self.open_in_romm_button.set_sensitive(is_connected)
                    # Store the selected collection for delete handler
                    self.selected_collection = collection_name
                    return
                else:
                    # Platform selected - disable delete
                    self.action_button.set_sensitive(False)
                    self.delete_button.set_sensitive(False)
                    self.open_in_romm_button.set_sensitive(is_connected)
                    self.selected_collection = None
                    return

        # No selections - disable all buttons
        # Clear all button style classes first
        self.action_button.remove_css_class('warning')
        self.action_button.remove_css_class('suggested-action')
        self.action_button.remove_css_class('destructive-action')
        self.action_button.set_sensitive(False)
        self.action_button.set_label("Download")
        self.delete_button.set_sensitive(False)
        self.open_in_romm_button.set_sensitive(False)

    def update_group_filter(self, games, group_by='platform'):
        """Update filter dropdown for platforms or collections"""
        groups = set()
        group_key = 'collection' if group_by == 'collection' else 'platform'

        for game in games:
            groups.add(game.get(group_key, 'Unknown'))

        prefix = "All Collections" if group_by == 'collection' else "All Platforms"
        group_list = [prefix] + sorted(groups)

        string_list = Gtk.StringList()
        for group in group_list:
            string_list.append(group)

        # Block the notify::selected-item signal while updating to prevent duplicate update_library calls
        try:
            # Use a flag to prevent recursive calls
            if not hasattr(self, '_updating_filter'):
                self._updating_filter = False

            if self._updating_filter:
                return  # Already updating, skip

            self._updating_filter = True
            self.platform_filter.set_model(string_list)
            self.platform_filter.set_selected(0)
            self._updating_filter = False
        except Exception as e:
            self._updating_filter = False
            print(f"Error updating group filter: {e}")
    
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
                self.selected_disc = None  # Clear disc selection
                self.selected_collection = None  # Clear collection selection
                # Clear checkbox selections without full refresh
                if self.selected_checkboxes or self.selected_rom_ids or self.selected_game_keys:
                    self.selected_checkboxes.clear()
                    self.selected_rom_ids.clear()
                    self.selected_game_keys.clear()
                    GLib.idle_add(self.force_checkbox_sync)
                    GLib.idle_add(self.refresh_all_platform_checkboxes)
            elif isinstance(item, DiscItem):
                # Disc selected - store both the disc and its parent game
                self.selected_disc = item.disc_data
                self.selected_game = item.parent_game  # Store parent game for context
                self.selected_collection = None  # Clear collection selection
                # Clear checkbox selections without full refresh
                if self.selected_checkboxes or self.selected_rom_ids or self.selected_game_keys:
                    self.selected_checkboxes.clear()
                    self.selected_rom_ids.clear()
                    self.selected_game_keys.clear()
                    GLib.idle_add(self.force_checkbox_sync)
                    GLib.idle_add(self.refresh_all_platform_checkboxes)
            elif isinstance(item, PlatformItem):
                self.selected_game = None
                self.selected_disc = None  # Clear disc selection
                # Note: selected_collection is set in update_action_buttons for collection rows
            # Update button states for both game and platform selections
            self.update_action_buttons()
        else:
            # Multiple or no row selection
            self.selected_game = None
            self.selected_disc = None  # Clear disc selection
            self.selected_collection = None  # Clear collection selection
            # Update button states
            self.update_action_buttons()

        # Update selection label
        self.update_selection_label()

        # Update collection auto-sync button if in collections view
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            self.update_sync_button_state()

    def update_selection_label(self):
        """Update the selection label text"""
        # SKIP UPDATES DURING DIALOG
        if getattr(self, '_selection_blocked', False):
            return

        # Count selected discs
        disc_count = len(self.get_selected_discs())

        # Count selected games using the same logic as action buttons
        selected_count = 0

        # FIX: Use correct games source based on view mode
        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
            games_to_check = getattr(self, 'collections_games', [])
        else:
            games_to_check = self.parent.available_games

        for game in games_to_check:  # CHANGED: was self.parent.available_games
            # Handle collection mode differently
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                rom_id = game.get('rom_id')
                collection_name = game.get('collection', '')
                if rom_id and collection_name:
                    collection_key = f"collection:{rom_id}:{collection_name}"
                    if collection_key in self.selected_game_keys:
                        selected_count += 1
            else:
                # Standard platform mode logic
                identifier_type, identifier_value = self.get_game_identifier(game)
                if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                    selected_count += 1
                elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                    selected_count += 1

        # Rest of the method unchanged...
        if disc_count > 0:
            self.selection_label.set_text(f"{disc_count} disc{'s' if disc_count != 1 else ''} checked")
        elif self.selected_disc:
            # Show disc name when a disc is selected
            game_name = self.selected_game.get('name', 'Unknown')
            disc_name = self.selected_disc.get('name', 'Unknown Disc')
            self.selection_label.set_text(f"{game_name} - {disc_name}")
        elif self.selected_game and selected_count == 0:
            game_name = self.selected_game.get('name', 'Unknown')
            self.selection_label.set_text(f"{game_name}")
        elif selected_count > 0:
            if self.selected_game:
                game_name = self.selected_game.get('name', 'Unknown')
                self.selection_label.set_text(f"Row: {game_name} | {selected_count} checked")
            else:
                self.selection_label.set_text(f"{selected_count} games checked")
        else:
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
        """Handle platform/collection filter changes"""
        # Skip if we're programmatically updating the filter
        if getattr(self, '_updating_filter', False):
            return

        # Apply combined filters
        filtered_games = self.apply_filters(self.parent.available_games)

        # Determine current view mode
        group_by = 'collection' if hasattr(self, 'view_mode_toggle') and self.view_mode_toggle.get_active() else 'platform'

        self.library_model.update_library(filtered_games, group_by=group_by)
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
        """Refresh library data based on current view mode"""
        if self.current_view_mode == 'collection':
            # Clear collections cache and reload
            self.collections_cache_time = 0
            self.load_collections_view()
        else:
            # Regular platform refresh
            if hasattr(self.parent, 'refresh_games_list'):
                self.parent.refresh_games_list()
    
    def on_action_clicked(self, button):
        """Handle main action button (download/launch/cancel) for single or multiple items"""

        # Check if bulk download is in progress first - this takes priority
        if self.parent._bulk_download_in_progress:
            # Cancel the bulk download by passing any rom_id
            # Get the first available rom_id from selected games or download progress
            rom_id = None
            if self.selected_game:
                rom_id = self.selected_game.get('rom_id')
            if not rom_id and self.parent.download_progress:
                rom_id = next(iter(self.parent.download_progress.keys()), None)

            if rom_id and hasattr(self.parent, 'cancel_download'):
                self.parent.cancel_download(rom_id)
            return

        # Check for selected discs first
        selected_discs = self.get_selected_discs()
        if selected_discs:
            # Individual disc downloads are disabled due to RomM API limitations
            # RomM always downloads all discs when requesting any disc from a multi-disc game
            game_name = selected_discs[0]['game'].get('name', 'this game')
            self.parent.log_message(
                f"⚠️ Cannot download individual discs. Please select and download '{game_name}' "
                f"to get all discs at once. (RomM API limitation)"
            )
            # Clear selections
            GLib.timeout_add(100, self.clear_checkbox_selections_smooth)
            return

        # ADD THIS BLOCK FIRST:
        # Priority: Handle single row selection directly
        if self.selected_game:
            game = self.selected_game
            rom_id = game.get('rom_id')

            # Check if a specific disc is selected
            if self.selected_disc:
                # Launch the specific disc
                if self.selected_disc.get('is_downloaded', False):
                    if hasattr(self.parent, 'launch_disc'):
                        self.parent.launch_disc(game, self.selected_disc)
                    else:
                        self.parent.log_message("⚠️ Disc launching not implemented")
                return  # Exit early

            # Check if download is in progress - if so, cancel it
            is_downloading = (rom_id and rom_id in self.parent.download_progress and
                            self.parent.download_progress[rom_id].get('downloading', False))

            if is_downloading:
                # Cancel the download
                if hasattr(self.parent, 'cancel_download'):
                    self.parent.cancel_download(rom_id)
            elif game.get('is_downloaded', False):
                # Launch the game
                if hasattr(self.parent, 'launch_game'):
                    self.parent.launch_game(game)
            else:
                # Download the game
                if hasattr(self.parent, 'download_game'):
                    self.parent.download_game(game)
            return  # Exit early, don't process checkbox logic
        selected_games = []
        
        # Priority 1: If there's a row selection (single game clicked), use that exclusively
        if self.selected_game:
            selected_games = [self.selected_game]
        else:
            # FIX: Use correct games source based on view mode
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                games_to_check = getattr(self, 'collections_games', [])
            else:
                games_to_check = self.parent.available_games
            
            for game in games_to_check:  # CHANGED: was self.parent.available_games
                # Handle collection mode differently
                if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                    rom_id = game.get('rom_id')
                    collection_name = game.get('collection', '')
                    if rom_id and collection_name:
                        collection_key = f"collection:{rom_id}:{collection_name}"
                        if collection_key in self.selected_game_keys:
                            selected_games.append(game)
                else:
                    # Standard platform mode logic
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
                rom_id = game.get('rom_id')

                # Check if download is in progress - if so, cancel it
                is_downloading = (rom_id and rom_id in self.parent.download_progress and
                                self.parent.download_progress[rom_id].get('downloading', False))

                if is_downloading:
                    # Cancel the download
                    if hasattr(self.parent, 'cancel_download'):
                        self.parent.cancel_download(rom_id)
                elif game.get('is_downloaded', False):
                    # Launch the game
                    if hasattr(self.parent, 'launch_game'):
                        self.parent.launch_game(game)
                else:
                    # Download the game
                    if hasattr(self.parent, 'download_game'):
                        self.parent.download_game(game)
            else:
                # Multiple selection
                # Check if this is a bulk download that should be cancelled
                is_bulk_download = self.parent._bulk_download_in_progress
                if is_bulk_download:
                    # Cancel the bulk download - pick any downloading game and cancel it
                    # This will trigger bulk cancellation
                    for game in selected_games:
                        rom_id = game.get('rom_id')
                        if rom_id and rom_id in self.parent.download_progress:
                            if self.parent.download_progress[rom_id].get('downloading', False):
                                if hasattr(self.parent, 'cancel_download'):
                                    self.parent.cancel_download(rom_id)
                                break
                else:
                    # Not a bulk download - check for download action
                    not_downloaded_games = [g for g in selected_games if not g.get('is_downloaded', False)]

                    if not_downloaded_games:
                        # Download multiple games immediately without confirmation
                        if hasattr(self.parent, 'download_multiple_games'):
                            self.parent.download_multiple_games(not_downloaded_games)

            # Clear checkbox selections after operation
            # Don't clear if bulk download started - selections will be cleared when bulk completes
            if len(selected_games) > 1 and not self.parent._bulk_download_in_progress:
                GLib.timeout_add(500, self.clear_checkbox_selection)  # Small delay for UI feedback

    def on_delete_clicked(self, button):
        """Handle delete button for single or multiple items, including collections"""
        # Check if a collection row is selected (not individual games)
        if hasattr(self, 'selected_collection') and self.selected_collection:
            # Collection deletion
            self.delete_collection(self.selected_collection)
            return

        # Check for selected discs first
        selected_discs = self.get_selected_discs()
        if selected_discs:
            # Delete selected discs
            for item in selected_discs:
                self.parent.delete_disc(item['game'], item['disc'])
            # Clear selections
            GLib.timeout_add(500, self.clear_checkbox_selection)
            return

        selected_games = []

        # Priority 1: If there's a row selection (single game clicked), use that exclusively
        if self.selected_game:
            selected_games = [self.selected_game]
        else:
            # FIX: Use correct games source based on view mode
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                games_to_check = getattr(self, 'collections_games', [])
            else:
                games_to_check = self.parent.available_games

            for game in games_to_check:  # CHANGED: was self.parent.available_games
                # Handle collection mode differently
                if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                    rom_id = game.get('rom_id')
                    collection_name = game.get('collection', '')
                    if rom_id and collection_name:
                        collection_key = f"collection:{rom_id}:{collection_name}"
                        if collection_key in self.selected_game_keys:
                            selected_games.append(game)
                else:
                    # Standard platform mode logic
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

    def delete_collection(self, collection_name):
        """Delete downloaded games from a collection with safety checks"""
        # Get all games in this collection
        collection_games = [g for g in self.collections_games if g.get('collection') == collection_name]
        downloaded_games = [g for g in collection_games if g.get('is_downloaded', False)]

        if not downloaded_games:
            self.parent.log_message(f"No downloaded games in '{collection_name}' to delete")
            return

        # Determine which games can be safely deleted
        # (only delete games that are NOT in other autosync-enabled collections)
        games_to_delete = []
        games_protected = []

        for game in downloaded_games:
            rom_id = game.get('rom_id')
            if not rom_id:
                # No ROM ID, can't check other collections, so include for deletion
                games_to_delete.append(game)
                continue

            # Check if this game exists in other autosync collections
            found_in_other_autosync = False
            for other_collection_game in self.collections_games:
                other_collection = other_collection_game.get('collection', '')
                # Skip if it's the same collection we're deleting from
                if other_collection == collection_name:
                    continue
                # Check if this is the same game and the other collection has autosync
                if other_collection_game.get('rom_id') == rom_id:
                    if other_collection in self.actively_syncing_collections:
                        found_in_other_autosync = True
                        games_protected.append((game, other_collection))
                        break

            if not found_in_other_autosync:
                games_to_delete.append(game)

        # Build confirmation message
        total_count = len(downloaded_games)
        delete_count = len(games_to_delete)
        protected_count = len(games_protected)

        # Prepare dialog message
        if protected_count > 0:
            # Some games are protected
            protected_list = []
            # Group by collection for cleaner message
            protected_by_collection = {}
            for game, other_coll in games_protected:
                if other_coll not in protected_by_collection:
                    protected_by_collection[other_coll] = []
                protected_by_collection[other_coll].append(game.get('name', 'Unknown'))

            protected_details = "\n".join(
                f"  • {coll}: {len(games)} game(s)"
                for coll, games in protected_by_collection.items()
            )

            message_body = (
                f"Found {total_count} downloaded game(s) in '{collection_name}':\n\n"
                f"  • {delete_count} will be deleted\n"
                f"  • {protected_count} will be kept (in other autosync collections)\n\n"
                f"Protected games are in:\n{protected_details}\n\n"
                f"Auto-sync for '{collection_name}' will be disabled."
            )
        else:
            # All games will be deleted
            message_body = (
                f"This will delete all {delete_count} downloaded game(s) from '{collection_name}'.\n\n"
                f"Auto-sync for this collection will be disabled."
            )

        # Show confirmation dialog
        dialog = Adw.AlertDialog.new(f"Delete Collection: {collection_name}?", message_body)
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", f"Delete {delete_count} Game(s)")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(dialog, response):
            if response == "cancel":
                # User cancelled - clear selection to avoid stale state
                self.selected_collection = None
                # Clear the row selection
                def clear_selection():
                    try:
                        selection_model = self.column_view.get_model()
                        if selection_model:
                            selection_model.unselect_all()
                        self.update_action_buttons()
                    except:
                        pass
                    return False
                GLib.idle_add(clear_selection)
            elif response == "delete":
                # Disable autosync for this collection first
                if collection_name in self.actively_syncing_collections:
                    self.actively_syncing_collections.discard(collection_name)
                    self.selected_collections_for_sync.discard(collection_name)

                    # Stop global sync if no collections left
                    if not self.actively_syncing_collections:
                        self.stop_collection_auto_sync()

                    # Save the updated settings
                    self.save_selected_collections()
                    self.parent.log_message(f"🔄 Auto-sync disabled for '{collection_name}'")

                # Delete the games
                if games_to_delete:
                    self.parent.log_message(f"🗑️ Deleting {len(games_to_delete)} game(s) from '{collection_name}'...")
                    for game in games_to_delete:
                        self.parent.delete_game_file(game, is_bulk_operation=True)

                    # Refresh the collections view after deletion
                    GLib.timeout_add(1000, lambda: self.load_collections_view() or False)

                    if protected_count > 0:
                        self.parent.log_message(
                            f"✅ Deleted {delete_count} game(s), kept {protected_count} game(s) "
                            f"(protected by other autosync collections)"
                        )
                    else:
                        self.parent.log_message(f"✅ Deleted {delete_count} game(s) from '{collection_name}'")
                else:
                    self.parent.log_message(f"All games in '{collection_name}' are protected by other autosync collections")

                # Clear the selected collection and row selection
                self.selected_collection = None

                # Clear the row selection to prevent lingering UI state
                def clear_selection():
                    try:
                        selection_model = self.column_view.get_model()
                        if selection_model:
                            selection_model.unselect_all()
                        # Also update button states
                        self.update_action_buttons()
                    except:
                        pass
                    return False

                GLib.idle_add(clear_selection)

        dialog.connect('response', on_response)
        dialog.present()

    def update_single_game(self, updated_game_data, skip_platform_update=False):
        """Update a single game in the tree without rebuilding - preserves expansion state"""
        rom_id = updated_game_data.get('rom_id')

        # Update master list
        for i, game in enumerate(self.parent.available_games):
            if game.get('rom_id') == rom_id:
                self.parent.available_games[i] = updated_game_data
                break

        # Try to update in-place first, avoiding full rebuild
        updated = False

        # In collection view, also update the collections_games cache
        if self.current_view_mode == 'collection' and hasattr(self, 'collections_games'):
            for i, collection_game in enumerate(self.collections_games):
                if collection_game.get('rom_id') == rom_id:
                    # Preserve the collection field when updating
                    updated_collection_game = updated_game_data.copy()
                    updated_collection_game['collection'] = collection_game.get('collection')
                    self.collections_games[i] = updated_collection_game

        model = self.library_model.tree_model
        for i in range(model.get_n_items()):
            tree_item = model.get_item(i)
            if tree_item and tree_item.get_depth() == 0:  # Platform/Collection level
                platform_item = tree_item.get_item()
                if isinstance(platform_item, PlatformItem):
                    # Update the game data in platform's games list
                    for j, game in enumerate(platform_item.games):
                        if game.get('rom_id') == rom_id:
                            # Preserve collection field if in collection view
                            if self.current_view_mode == 'collection':
                                updated_game_with_collection = updated_game_data.copy()
                                updated_game_with_collection['collection'] = game.get('collection')
                                platform_item.games[j] = updated_game_with_collection
                            else:
                                platform_item.games[j] = updated_game_data

                            # Update the corresponding GameItem in child_store
                            for k in range(platform_item.child_store.get_n_items()):
                                game_item = platform_item.child_store.get_item(k)
                                if isinstance(game_item, GameItem) and game_item.game_data.get('rom_id') == rom_id:
                                    # Deep copy game data to avoid reference issues with disc arrays
                                    import copy
                                    if self.current_view_mode == 'collection':
                                        updated_game_with_collection = copy.deepcopy(updated_game_data)
                                        updated_game_with_collection['collection'] = game_item.game_data.get('collection')
                                        game_item.game_data = updated_game_with_collection
                                    else:
                                        game_item.game_data = copy.deepcopy(updated_game_data)
                                    # Rebuild children for multi-disc games to update disc status
                                    if game_item.game_data.get('is_multi_disc', False):
                                        game_item.rebuild_children()
                                    # Trigger property notifications to refresh UI
                                    game_item.notify('name')
                                    game_item.notify('is-downloaded')
                                    game_item.notify('size-text')
                                    break

                            # Update platform properties (status and size text)
                            platform_item.notify('status-text')
                            platform_item.notify('size-text')
                            updated = True
                            break

                if updated:
                    break

        # If in-place update failed, fall back to full refresh
        if not updated:
            if self.current_view_mode == 'collection':
                self.load_collections_view()
            else:
                self.update_games_library(self.parent.available_games)

    def setup_checkbox_cell(self, factory, list_item):
        # Create a box to hold either a checkbox or a switch
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.set_halign(Gtk.Align.CENTER)
        box.set_valign(Gtk.Align.CENTER)

        # We'll add the checkbox/switch dynamically in bind_checkbox_cell
        # since we need to know if it's a collection or a game
        list_item.set_child(box)

    def bind_checkbox_cell(self, factory, list_item):
        tree_item = list_item.get_item()
        item = tree_item.get_item()
        box = list_item.get_child()

        # Clear the box first
        while box.get_first_child():
            box.remove(box.get_first_child())

        if isinstance(item, DiscItem):
            # For individual discs, use checkboxes (disabled - for visual consistency only)
            checkbox = Gtk.CheckButton()
            checkbox.connect('toggled', self.on_checkbox_toggled)
            box.append(checkbox)

            checkbox.set_visible(True)
            checkbox.disc_item = item
            checkbox.tree_item = tree_item
            checkbox.is_disc = True

            # Disable disc checkboxes - users cannot check individual discs
            # but can still select the row for launching
            checkbox.set_sensitive(False)
            checkbox.set_tooltip_text("Individual discs cannot be selected. Select the parent game instead.")

            # Check if this disc is selected
            disc_key = f"disc:{item.parent_game.get('rom_id')}:{item.disc_data.get('name')}"
            should_be_active = disc_key in self.selected_game_keys
            checkbox.set_active(should_be_active)

        elif isinstance(item, GameItem):
            # For games, use checkboxes
            checkbox = Gtk.CheckButton()
            checkbox.connect('toggled', self.on_checkbox_toggled)
            box.append(checkbox)

            checkbox.set_visible(True)
            checkbox.game_item = item
            checkbox.tree_item = tree_item
            checkbox.is_platform = False

            # Check if this game is selected using collection-aware tracking
            game_data = item.game_data

            should_be_active = False
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                # Collections view: use collection-aware identifier
                rom_id = game_data.get('rom_id')
                collection_name = game_data.get('collection', '')
                game_name = game_data.get('name', 'NO_NAME')

                # Check if collection has autosync enabled - disable checkbox if so
                if collection_name in self.actively_syncing_collections:
                    checkbox.set_sensitive(False)
                    checkbox.set_tooltip_text(f"Cannot select - '{collection_name}' has auto-sync enabled")
                else:
                    checkbox.set_sensitive(True)
                    checkbox.set_tooltip_text("")

                if rom_id and collection_name:
                    collection_key = f"collection:{rom_id}:{collection_name}"
                    should_be_active = collection_key in self.selected_game_keys
                else:
                    # Fallback for games without ROM ID
                    name_key = f"collection:{game_data.get('name', '')}:{game_data.get('platform', '')}:{collection_name}"
                    should_be_active = name_key in self.selected_game_keys
            else:
                # Platform view: use standard identifier
                identifier_type, identifier_value = self.get_game_identifier(game_data)
                if identifier_type == 'rom_id':
                    should_be_active = identifier_value in self.selected_rom_ids
                elif identifier_type == 'game_key':
                    should_be_active = identifier_value in self.selected_game_keys

            checkbox.set_active(should_be_active)

            # Force immediate visual update for collections
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                def force_checkbox_update():
                    try:
                        checkbox._updating = True
                        checkbox.set_active(should_be_active)
                        checkbox._updating = False
                        return False
                    except:
                        return False

                GLib.timeout_add(1, force_checkbox_update)

        elif isinstance(item, PlatformItem):
            # In collections view, use a switch for collections
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                switch = Gtk.Switch()
                switch.set_valign(Gtk.Align.CENTER)
                switch.set_halign(Gtk.Align.CENTER)
                # Make switch smaller
                switch.add_css_class('compact-switch')
                box.append(switch)

                switch.set_visible(True)
                switch.platform_item = item
                switch.tree_item = tree_item
                switch.is_platform = True

                collection_name = item.platform_name

                # Check if this collection has autosync enabled (persistent state)
                is_actively_syncing = collection_name in self.actively_syncing_collections

                # Apply color and icon based on auto-sync status
                if is_actively_syncing and self.collection_auto_sync_enabled:
                    switch.add_css_class('collection-synced')  # Green
                    status_text = "Auto-sync active"
                elif is_actively_syncing:
                    switch.add_css_class('collection-partial-sync')  # Orange
                    status_text = "Selected (auto-sync paused)"
                else:
                    switch.add_css_class('collection-not-synced')  # Red
                    status_text = "Not selected"

                # Switch shows persistent autosync state (ON if collection has autosync enabled)
                switch._updating = True
                switch.set_active(is_actively_syncing)
                switch._updating = False

                tooltip = f"{collection_name} - {status_text}"
                switch.set_tooltip_text(tooltip)

                # Connect handler
                def on_collection_sync_toggle(sw, pspec):
                    if not getattr(sw, '_updating', False):
                        self.on_switch_toggled(sw, collection_name)

                if not hasattr(switch, '_sync_handler_connected'):
                    switch.connect('notify::active', on_collection_sync_toggle)
                    switch._sync_handler_connected = True
                return
            else:
                # For platform view, use checkboxes
                checkbox = Gtk.CheckButton()
                checkbox.connect('toggled', self.on_checkbox_toggled)
                box.append(checkbox)

                checkbox.set_visible(True)
                checkbox.platform_item = item
                checkbox.tree_item = tree_item
                checkbox.is_platform = True

                # Normal platform view logic (existing game selection)
                # Count selected games using the dual tracking system
                total_games = len(item.games)
                selected_games = 0

                for game_data in item.games:  # Fixed: use game_data instead of game
                    identifier_type, identifier_value = self.get_game_identifier(game_data)
                    if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                        selected_games += 1
                    elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                        selected_games += 1

                # Set platform checkbox state
                if selected_games == 0:
                    checkbox.set_active(False)
                    checkbox.set_inconsistent(False)
                elif selected_games == total_games and total_games > 0:
                    checkbox.set_active(True)
                    checkbox.set_inconsistent(False)
                else:
                    checkbox.set_active(False)
                    checkbox.set_inconsistent(True)

                pass

    def on_checkbox_toggled(self, checkbox):
        """Handle checkbox toggle with debugging"""
        if hasattr(checkbox, 'is_disc') and checkbox.is_disc:
            # Disc checkbox toggled
            disc_item = checkbox.disc_item
            disc_key = f"disc:{disc_item.parent_game.get('rom_id')}:{disc_item.disc_data.get('name')}"

            if checkbox.get_active():
                self.selected_game_keys.add(disc_key)
            else:
                self.selected_game_keys.discard(disc_key)

            # Update UI
            self.update_action_buttons()
            self.update_selection_label()

        elif hasattr(checkbox, 'is_platform') and checkbox.is_platform:
            platform_name = checkbox.platform_item.platform_name
            platform_item = checkbox.platform_item

            # Check if this is collections view
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                # Handle collection selection - select/unselect all games in collection
                should_select = checkbox.get_active()
                
                if should_select:
                    for game in platform_item.games:
                        rom_id = game.get('rom_id')
                        collection_name = game.get('collection', platform_name)
                        if rom_id:
                            collection_key = f"collection:{rom_id}:{collection_name}"
                            self.selected_game_keys.add(collection_key)
                else:
                    for game in platform_item.games:
                        rom_id = game.get('rom_id')
                        collection_name = game.get('collection', platform_name)
                        if rom_id:
                            collection_key = f"collection:{rom_id}:{collection_name}"
                            self.selected_game_keys.discard(collection_key)
                
                # Update UI
                self.sync_selected_checkboxes()
                self.update_action_buttons()
                self.update_selection_label()
                GLib.idle_add(self.force_checkbox_sync)
                return
            
            # Platform view logic (restore original logic here)
            # Determine what the user wants based on current state
            if checkbox.get_inconsistent():
                should_select = True
                checkbox.set_inconsistent(False)
                checkbox.set_active(True)
            else:
                should_select = checkbox.get_active()
            
            # Add/remove games for platform view
            if should_select:
                for game in platform_item.games:
                    identifier_type, identifier_value = self.get_game_identifier(game)
                    if identifier_type == 'rom_id':
                        self.selected_rom_ids.add(identifier_value)
                    else:
                        self.selected_game_keys.add(identifier_value)
            else:
                for game in platform_item.games:
                    identifier_type, identifier_value = self.get_game_identifier(game)
                    if identifier_type == 'rom_id':
                        self.selected_rom_ids.discard(identifier_value)
                    else:
                        self.selected_game_keys.discard(identifier_value)
            
            # Update UI
            self.sync_selected_checkboxes()
            self.update_action_buttons()
            self.update_selection_label()
            GLib.idle_add(self.force_checkbox_sync)
            GLib.idle_add(self.refresh_all_platform_checkboxes)
                            
        elif hasattr(checkbox, 'game_item'):
            # Game checkbox toggled
            game_data = checkbox.game_item.game_data
            
            if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                # Collections view: use collection-aware identifier
                rom_id = game_data.get('rom_id')
                collection_name = game_data.get('collection', '')
                
                if checkbox.get_active():
                    if rom_id and collection_name:
                        collection_key = f"collection:{rom_id}:{collection_name}"
                        self.selected_game_keys.add(collection_key)
                    else:
                        name_key = f"collection:{game_data.get('name', '')}:{game_data.get('platform', '')}:{collection_name}"
                        self.selected_game_keys.add(name_key)
                    self.selected_checkboxes.add(checkbox.game_item)
                else:
                    if rom_id and collection_name:
                        collection_key = f"collection:{rom_id}:{collection_name}"
                        self.selected_game_keys.discard(collection_key)
                    else:
                        name_key = f"collection:{game_data.get('name', '')}:{game_data.get('platform', '')}:{collection_name}"
                        self.selected_game_keys.discard(name_key)
                    self.selected_checkboxes.discard(checkbox.game_item)
            else:
                # Platform view: use standard identifier
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

    def on_switch_toggled(self, switch, collection_name):
        """Handle switch toggle for collection auto-sync"""
        import time
        toggle_start = time.time()
        self.parent.log_message(f"[DEBUG] on_switch_toggled called for {collection_name}, active={switch.get_active()}")

        if switch.get_active():
            # ENABLE: Add collection to sync
            self.selected_collections_for_sync.add(collection_name)
            self.actively_syncing_collections.add(collection_name)
            # Add to currently_downloading to show orange immediately
            self.currently_downloading_collections.add(collection_name)
            # Remove from completed (if it was there) so it shows orange, not green
            if hasattr(self, 'completed_sync_collections'):
                self.completed_sync_collections.discard(collection_name)
            self.parent.log_message(f"[DEBUG] Added to collections ({time.time() - toggle_start:.3f}s)")

            # Download missing games for this collection immediately
            self.download_single_collection_games(collection_name)
            self.parent.log_message(f"📥 Enabling auto-sync for '{collection_name}'")
            self.parent.log_message(f"[DEBUG] Download queued ({time.time() - toggle_start:.3f}s)")

            # Initialize collection cache
            def init_collection_cache():
                try:
                    all_collections = self.parent.romm_client.get_collections()
                    for collection in all_collections:
                        if collection.get('name') == collection_name:
                            collection_id = collection.get('id')
                            collection_roms = self.parent.romm_client.get_collection_roms(collection_id)
                            cache_key = f'_collection_roms_{collection_name}'
                            setattr(self, cache_key, {rom.get('id') for rom in collection_roms if rom.get('id')})
                except Exception as e:
                    print(f"Error initializing collection cache: {e}")

            threading.Thread(target=init_collection_cache, daemon=True).start()

            # Start global sync if not already running
            if not self.collection_auto_sync_enabled or not self.collection_sync_thread:
                self.start_collection_auto_sync()
            else:
                self.collection_auto_sync_enabled = True
            self.parent.log_message(f"[DEBUG] Sync started ({time.time() - toggle_start:.3f}s)")

            # Update status indicator IMMEDIATELY (no delay)
            self.update_collection_sync_status(collection_name)
            self.parent.log_message(f"[DEBUG] Status updated ({time.time() - toggle_start:.3f}s)")
            self.parent.log_message(f"[DEBUG] TOTAL on_switch_toggled time: {time.time() - toggle_start:.3f}s")

        else:
            # DISABLE: Remove collection from sync
            self.selected_collections_for_sync.discard(collection_name)
            self.actively_syncing_collections.discard(collection_name)
            # Also remove from completed collections
            if hasattr(self, 'completed_sync_collections'):
                self.completed_sync_collections.discard(collection_name)

            # Stop global sync if no collections left
            if not self.actively_syncing_collections:
                self.stop_collection_auto_sync()

            # Update status indicator
            self.update_collection_sync_status(collection_name)

            self.parent.log_message(f"Stopped auto-sync for '{collection_name}'")

        # Save the selection
        self.save_selected_collections()

        # Refresh the checkbox display
        self.refresh_collection_checkboxes()

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
            pass

    def force_checkbox_sync(self):
        """Force all visible checkboxes to match current selection state"""
        def sync_checkboxes(widget):
            if isinstance(widget, Gtk.CheckButton):
                if hasattr(widget, 'is_platform'):
                    if widget.is_platform:  # Platform checkbox
                        # Check if all games in this platform are selected
                        if hasattr(widget, 'platform_item'):
                            platform_item = widget.platform_item
                            games = platform_item.games
                            total_games = len(games)
                            selected_games = 0

                            for game in games:
                                identifier_type, identifier_value = self.get_game_identifier(game)
                                if identifier_type == 'rom_id' and identifier_value in self.selected_rom_ids:
                                    selected_games += 1
                                elif identifier_type == 'game_key' and identifier_value in self.selected_game_keys:
                                    selected_games += 1

                            widget._updating = True
                            if selected_games == total_games and total_games > 0:
                                widget.set_active(True)
                                widget.set_inconsistent(False)
                            elif selected_games > 0:
                                widget.set_active(False)
                                widget.set_inconsistent(True)
                            else:
                                widget.set_active(False)
                                widget.set_inconsistent(False)
                            widget._updating = False
                    elif hasattr(widget, 'game_item'):  # Game checkbox
                        game_data = widget.game_item.game_data

                        should_be_active = False
                        if hasattr(self, 'current_view_mode') and self.current_view_mode == 'collection':
                            # Collections view: use collection-aware identifier
                            rom_id = game_data.get('rom_id')
                            collection_name = game_data.get('collection', '')
                            if rom_id and collection_name:
                                collection_key = f"collection:{rom_id}:{collection_name}"
                                should_be_active = collection_key in self.selected_game_keys
                        else:
                            # Platform view: use standard identifier
                            identifier_type, identifier_value = self.get_game_identifier(game_data)
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
        # Don't clear selections during bulk downloads - they'll be cleared when the bulk operation completes
        if hasattr(self.parent, '_bulk_download_in_progress') and self.parent._bulk_download_in_progress:
            return

        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.update_action_buttons()
        self.update_selection_label()

        # Force UI refresh to update checkboxes
        GLib.idle_add(lambda: self.update_games_library(self.parent.available_games))

    def clear_checkbox_selections_smooth(self):
        """Clear checkbox selections without full tree refresh"""
        # Don't clear selections during bulk downloads - they'll be cleared when the bulk operation completes
        if hasattr(self.parent, '_bulk_download_in_progress') and self.parent._bulk_download_in_progress:
            return

        self.selected_checkboxes.clear()
        self.selected_rom_ids.clear()
        self.selected_game_keys.clear()
        self.update_action_buttons()
        self.update_selection_label()
        GLib.idle_add(self.force_checkbox_sync)
        GLib.idle_add(self.refresh_all_platform_checkboxes) 

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
        self.device_id = None

        self.settings = SettingsManager()

        self.retroarch = RetroArchInterface(self.settings)

        self.game_cache = GameDataCache(self.settings)        
        
        # Progress tracking
        self.download_queue = []
        self.available_games = []  # Initialize games list

        # Timestamps for efficient polling with updated_after parameter
        self._last_full_fetch_time = None  # ISO 8601 datetime of last full data fetch
        self._polling_thread = None  # Background polling thread
        self._stop_polling = threading.Event()  # Event to stop polling thread

        self.download_progress = {}
        self._last_progress_update = {}  # rom_id -> timestamp
        self._progress_update_interval = 0.1  # Update UI every 100ms max

        # Download cancellation infrastructure
        self._cancelled_downloads = set()  # Track rom_ids of cancelled downloads
        self._download_threads = {}  # Track active download threads by rom_id
        self._cancellation_lock = threading.Lock()  # Thread-safe access to cancellation state
        self._bulk_download_cancelled = False  # Flag to cancel entire bulk operation
        self._bulk_download_in_progress = False  # Track if bulk download is active

        self.setup_ui()
        self.debug_retroarch_status() 
        self.connect('close-request', self.on_window_close_request)
        self.load_saved_settings()

        # Auto-update systemd service for new versions
        if self.settings.get('System', 'autostart') == 'true':
            self.update_systemd_service_if_needed()

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

        # Add quit action
        quit_action = Gio.SimpleAction.new("quit", None)
        quit_action.connect("activate", lambda action, param: self.get_application().quit())
        self.add_action(quit_action)

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

        # Schedule periodic memory cleanup for large libraries
        def setup_periodic_cleanup():
            if len(getattr(self, 'available_games', [])) > 1000:
                def periodic_cleanup():
                    import gc
                    gc.collect()
                    return True  # Continue running
                
                # Clean up every 5 minutes
                GLib.timeout_add(300000, periodic_cleanup)
                print("🧹 Periodic memory cleanup scheduled (every 5 minutes)")
            return False

        # Schedule cleanup check after initial load
        GLib.timeout_add(2000, setup_periodic_cleanup)
        # ADD AUTO-CONNECT LOGIC:
        GLib.timeout_add(50, self.try_auto_connect)

    def create_status_dot(self, color='grey', size=10):
        """Create a Cairo-drawn status dot widget

        Args:
            color: 'green', 'orange', 'red', 'grey', or 'yellow'
            size: Size of the dot in pixels (default: 10)

        Returns:
            Gtk.DrawingArea with colored dot
        """
        drawing_area = Gtk.DrawingArea()
        drawing_area.set_size_request(size, size)
        drawing_area.set_halign(Gtk.Align.CENTER)
        drawing_area.set_valign(Gtk.Align.CENTER)

        def draw_func(area, cr, width, height):
            # Determine RGB color
            if color == 'green':
                cr.set_source_rgb(0.29, 0.86, 0.50)  # #4ade80
            elif color == 'orange':
                cr.set_source_rgb(0.98, 0.57, 0.24)  # #fb923c
            elif color == 'red':
                cr.set_source_rgb(0.97, 0.44, 0.44)  # #f87171
            elif color == 'yellow':
                cr.set_source_rgb(0.98, 0.80, 0.27)  # #facc15
            else:  # grey
                cr.set_source_rgb(0.42, 0.45, 0.50)  # #6b7280

            # Draw filled circle
            radius = min(width, height) / 2.0
            cr.arc(width / 2.0, height / 2.0, radius - 1, 0, 2 * 3.14159)
            cr.fill()

        drawing_area.set_draw_func(draw_func)
        return drawing_area

    def update_status_dot(self, drawing_area, color='grey'):
        """Update an existing status dot with a new color

        Args:
            drawing_area: The Gtk.DrawingArea to update
            color: 'green', 'orange', 'red', 'grey', or 'yellow'
        """
        def draw_func(area, cr, width, height):
            # Determine RGB color
            if color == 'green':
                cr.set_source_rgb(0.29, 0.86, 0.50)  # #4ade80
            elif color == 'orange':
                cr.set_source_rgb(0.98, 0.57, 0.24)  # #fb923c
            elif color == 'red':
                cr.set_source_rgb(0.97, 0.44, 0.44)  # #f87171
            elif color == 'yellow':
                cr.set_source_rgb(0.98, 0.80, 0.27)  # #facc15
            else:  # grey
                cr.set_source_rgb(0.42, 0.45, 0.50)  # #6b7280

            # Draw filled circle
            radius = min(width, height) / 2.0
            cr.arc(width / 2.0, height / 2.0, radius - 1, 0, 2 * 3.14159)
            cr.fill()

        drawing_area.set_draw_func(draw_func)
        drawing_area.queue_draw()

    def draw_download_status_icon(self, drawing_area, status_type, progress=None):
        """Draw a download status icon or percentage using Cairo

        Args:
            drawing_area: The Gtk.DrawingArea to draw on
            status_type: 'downloaded' (green checkmark), 'not_downloaded' (blue down arrow),
                        'completed' (green checkmark), 'failed' (red X), 'downloading' (percentage)
            progress: Progress value (0.0 to 1.0) for downloading status
        """
        def draw_func(area, cr, width, height):
            center_x = width / 2.0
            center_y = height / 2.0

            if status_type in ['downloaded', 'completed']:
                # Green checkmark
                cr.set_source_rgb(0.29, 0.86, 0.50)  # Green #4ade80
                cr.set_line_width(2.0)
                cr.set_line_cap(1)  # Round caps
                cr.set_line_join(1)  # Round joins

                # Draw checkmark path
                cr.move_to(center_x - 4, center_y)
                cr.line_to(center_x - 1, center_y + 3)
                cr.line_to(center_x + 4, center_y - 3)
                cr.stroke()

            elif status_type == 'not_downloaded':
                # Blue down arrow
                cr.set_source_rgb(0.37, 0.51, 0.98)  # Blue #5e82fa
                cr.set_line_width(2.0)
                cr.set_line_cap(1)  # Round caps
                cr.set_line_join(1)  # Round joins

                # Draw arrow shaft
                cr.move_to(center_x, center_y - 4)
                cr.line_to(center_x, center_y + 3)
                cr.stroke()

                # Draw arrow head
                cr.move_to(center_x - 3, center_y)
                cr.line_to(center_x, center_y + 3)
                cr.line_to(center_x + 3, center_y)
                cr.stroke()

            elif status_type == 'failed':
                # Red X
                cr.set_source_rgb(0.97, 0.44, 0.44)  # Red #f87171
                cr.set_line_width(2.0)
                cr.set_line_cap(1)  # Round caps

                # Draw X
                cr.move_to(center_x - 4, center_y - 4)
                cr.line_to(center_x + 4, center_y + 4)
                cr.stroke()

                cr.move_to(center_x + 4, center_y - 4)
                cr.line_to(center_x - 4, center_y + 4)
                cr.stroke()

            elif status_type == 'downloading' and progress is not None:
                # Orange percentage text
                cr.set_source_rgb(0.98, 0.57, 0.24)  # Orange #fb923c

                # Draw percentage text
                percentage_text = f"{progress*100:.0f}%"
                cr.select_font_face("Sans", 0, 0)  # Normal, Non Bold
                cr.set_font_size(15)

                # Get text extents to center it
                extents = cr.text_extents(percentage_text)
                text_x = center_x - extents.width / 2 - extents.x_bearing
                text_y = center_y - extents.height / 2 - extents.y_bearing

                cr.move_to(text_x, text_y)
                cr.show_text(percentage_text)

        drawing_area.set_draw_func(draw_func)
        drawing_area.queue_draw()

    def format_sync_interval(self, seconds):
        """Format seconds into user-friendly string"""
        if seconds < 60:
            return f"{seconds}s"
        elif seconds < 3600:
            minutes = seconds // 60
            remaining_seconds = seconds % 60
            if remaining_seconds == 0:
                return f"{minutes}m"
            else:
                return f"{minutes}m {remaining_seconds}s"
        else:
            hours = seconds // 3600
            remaining_minutes = (seconds % 3600) // 60
            return f"{hours}h {remaining_minutes}m"

    def debug_retroarch_status(self):
            """Debug RetroArch status"""
            print("=== RetroArch Debug Info ===")
            print(f"Executable: {getattr(self.retroarch, 'retroarch_executable', 'NOT SET')}")
            print(f"RetroArch object: {self.retroarch}")
            print(f"Save dirs: {getattr(self.retroarch, 'save_dirs', 'NOT SET')}")
            print(f"Cores dir: {getattr(self.retroarch, 'cores_dir', 'NOT SET')}")
            print(f"UI elements exist:")
            print(f"  - retroarch_info_row: {hasattr(self, 'retroarch_info_row')}")
            print(f"  - cores_info_row: {hasattr(self, 'cores_info_row')}")
            print(f"  - core_count_row: {hasattr(self, 'core_count_row')}")
            print(f"  - retroarch_connection_row: {hasattr(self, 'retroarch_connection_row')}")
            print("========================")

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

    def handle_offline_mode(self):
        """Handle when not connected to RomM - show only downloaded games"""
        download_dir = Path(self.rom_dir_row.get_text())

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
            # No cache - scan local files (platform mapping will be fetched inside scan_local_games_only)
            local_games = self.scan_local_games_only(download_dir)

            def update_ui():
                self.available_games = local_games
                if hasattr(self, 'library_section'):
                    self.library_section.update_games_library(local_games)
                self.update_connection_ui("disconnected")
                self.log_message(f"📂 Offline mode: {len(local_games)} local games found")

            GLib.idle_add(update_ui)

    def on_autostart_changed(self, switch_row, pspec):
        """Handle autostart setting change"""
        enable = switch_row.get_active()
        
        def setup_autostart():
            try:
                if enable:
                    success = self.create_systemd_service()
                    if success:
                        GLib.idle_add(lambda: self.log_message("✅ Autostart enabled"))
                    else:
                        GLib.idle_add(lambda: self.log_message("❌ Failed to enable autostart"))
                        GLib.idle_add(lambda: switch_row.set_active(False))
                else:
                    success = self.remove_systemd_service()
                    if success:
                        GLib.idle_add(lambda: self.log_message("✅ Autostart disabled"))
                    else:
                        GLib.idle_add(lambda: self.log_message("❌ Failed to disable autostart"))
            except Exception as e:
                GLib.idle_add(lambda: self.log_message(f"❌ Autostart error: {e}"))
                GLib.idle_add(lambda: switch_row.set_active(False))
        
        threading.Thread(target=setup_autostart, daemon=True).start()

    def create_systemd_service(self):
        """Create systemd user service for autostart"""
        import subprocess
        import os
        import sys
        from pathlib import Path
        
        try:
            # Get current executable path
            if hasattr(sys, '_MEIPASS'):  # PyInstaller bundle
                exec_path = sys.executable
            elif os.environ.get('APPIMAGE'):  # AppImage
                exec_path = os.environ['APPIMAGE']
            else:  # Python script
                exec_path = f"python3 {os.path.abspath(__file__)}"
            
            # Create systemd user directory
            systemd_dir = Path.home() / '.config' / 'systemd' / 'user'
            systemd_dir.mkdir(parents=True, exist_ok=True)
            
            # Create service file
            service_content = f"""[Unit]
    Description=RomM RetroArch Sync
    After=multi-user.target

    [Service]
    Type=simple
    ExecStartPre=/bin/sleep 15
    ExecStart={exec_path} --daemon
    Restart=always
    RestartSec=10
    Environment=DISPLAY=:0
    KillMode=mixed
    KillSignal=SIGTERM

    [Install]
    WantedBy=default.target
    """
            
            service_file = systemd_dir / 'romm-retroarch-sync.service'
            with open(service_file, 'w') as f:
                f.write(service_content)
            
            # Enable and start service
            subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
            subprocess.run(['systemctl', '--user', 'enable', 'romm-retroarch-sync.service'], check=True)
            
            # Save setting
            self.settings.set('System', 'autostart', 'true')
            
            # Try to install Decky plugin (optional - don't fail if it doesn't work)
            decky_plugins_dir = Path.home() / 'homebrew' / 'plugins'
            if decky_plugins_dir.exists():
                try:
                    if os.access(decky_plugins_dir, os.W_OK):
                        self.install_decky_plugin()
                    else:
                        self.log_message("📱 Decky plugin skipped - no write permission")
                except Exception as e:
                    self.log_message(f"📱 Decky plugin install skipped: {e}")
            
            return True
            
        except Exception as e:
            print(f"Failed to create systemd service: {e}")
            return False

    def remove_systemd_service(self):
        """Remove systemd user service"""
        import subprocess
        from pathlib import Path
        
        try:
            # Disable and stop service
            subprocess.run(['systemctl', '--user', 'disable', 'romm-retroarch-sync.service'], 
                        capture_output=True)
            subprocess.run(['systemctl', '--user', 'stop', 'romm-retroarch-sync.service'], 
                        capture_output=True)
            
            # Remove service file
            service_file = Path.home() / '.config' / 'systemd' / 'user' / 'romm-retroarch-sync.service'
            if service_file.exists():
                service_file.unlink()
            
            subprocess.run(['systemctl', '--user', 'daemon-reload'], capture_output=True)
            
            # Try to remove Decky plugin (optional - don't fail if it doesn't work)
            try:
                self.remove_decky_plugin()
            except Exception as e:
                self.log_message(f"📱 Decky plugin removal skipped: {e}")
            
            # Save setting
            self.settings.set('System', 'autostart', 'false')
            return True
            
        except Exception as e:
            print(f"Failed to remove systemd service: {e}")
            return False

    def update_systemd_service_if_needed(self):
        """Update systemd service if current executable differs from service file"""
        try:
            import subprocess
            import os
            import sys
            from pathlib import Path
            
            service_file = Path.home() / '.config' / 'systemd' / 'user' / 'romm-retroarch-sync.service'
            
            if not service_file.exists():
                return False
                
            # Get current executable path
            if os.environ.get('APPIMAGE'):
                current_exec = os.environ['APPIMAGE']
            elif hasattr(sys, '_MEIPASS'):
                current_exec = sys.executable
            else:
                current_exec = f"python3 {os.path.abspath(__file__)}"
            
            # Read service file
            with open(service_file, 'r') as f:
                service_content = f.read()
            
            # Check if ExecStart path is different
            if f"ExecStart={current_exec}" not in service_content:
                self.log_message("🔄 Updating autostart service for new version...")
                
                # Recreate service with new path
                success = self.create_systemd_service()
                if success:
                    self.log_message("✅ Autostart service updated")
                    return True
                else:
                    self.log_message("❌ Failed to update autostart service")
            
            return False
            
        except Exception as e:
            self.log_message(f"❌ Service update check failed: {e}")
            return False

    def install_decky_plugin(self):
        """Install companion Decky plugin - DISABLED"""
        # Plugin installation disabled - install manually
        self.log_message("📱 Decky plugin installation disabled (install manually)")
        return False

    def remove_decky_plugin(self):
        """Remove companion Decky plugin - DISABLED"""
        # Plugin removal disabled  
        return False

    def check_autostart_status(self):
        """Check if autostart is currently enabled"""
        import subprocess
        try:
            result = subprocess.run(['systemctl', '--user', 'is-enabled', 'romm-retroarch-sync.service'], 
                                capture_output=True, text=True)
            return result.returncode == 0 and 'enabled' in result.stdout
        except:
            return False

    def filter_to_downloaded_games_only(self, cached_games, download_dir):
        """Filter cached games to only show those that are actually downloaded"""
        downloaded_games = []

        for game in cached_games:
            # Use platform_slug instead of platform for directory name
            platform_slug = game.get('platform_slug') or game.get('platform', 'Unknown')

            # Use cached local_path if available (already correct for multi-disc), otherwise construct from file_name
            cached_path = game.get('local_path')
            if cached_path:
                local_path = Path(cached_path)
            else:
                file_name = game.get('file_name', '')
                if not file_name:
                    continue
                platform_dir = download_dir / platform_slug
                local_path = platform_dir / file_name

            # Check if file/folder exists and is valid (handles both files and folders)
            if self.is_path_validly_downloaded(local_path):
                # Update game data with current local info
                game_copy = game.copy()
                # Update platform display name from mapping if available
                if platform_slug and self.game_cache.platform_mapping:
                    game_copy['platform'] = self.game_cache.get_platform_name(platform_slug)
                game_copy['is_downloaded'] = True
                game_copy['local_path'] = str(local_path)
                game_copy['local_size'] = self.get_actual_file_size(local_path)

                # Update disc status for multi-disc games
                if game_copy.get('is_multi_disc') and game_copy.get('discs'):
                    for disc in game_copy['discs']:
                        disc['is_downloaded'] = True

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

    def is_path_validly_downloaded(self, path):
        """Check if a path (file or folder) is validly downloaded

        Args:
            path: Path object or string to check

        Returns:
            bool: True if path is validly downloaded (folder with content or file with size > 1024)
        """
        path = Path(path)
        if not path.exists():
            return False

        if path.is_dir():
            # For folders, check if directory has content
            try:
                return any(path.iterdir())
            except (PermissionError, OSError):
                return False
        elif path.is_file():
            # For files, check if file has reasonable size
            return path.stat().st_size > 1024

        return False

    def get_actual_file_size(self, path):
        """Get actual size - sum all files for directories, file size for files"""
        path = Path(path)
        if path.is_dir():
            size = sum(f.stat().st_size for f in path.rglob('*') if f.is_file())
            return size
        elif path.is_file():
            return path.stat().st_size
        else:
            return 0

    def get_disc_total_size(self, disc_path, parent_folder):
        """
        Calculate total size of all files related to a disc.
        For multi-file discs, this handles two cases:
        1. Disc is a folder (e.g., "Disc 1/") - sum all files in that folder
        2. Disc is a file (e.g., "Game (Disc 1).cue") - sum all files with same base name

        Args:
            disc_path: Path to disc (either a folder or primary disc file)
            parent_folder: Path to the parent folder

        Returns:
            Total size in bytes of all related files
        """
        disc_path = Path(disc_path)
        parent_folder = Path(parent_folder)

        if not disc_path.exists():
            return 0

        # Case 1: Disc is a folder (multi-file disc in its own folder)
        if disc_path.is_dir():
            files_in_folder = list(disc_path.rglob('*'))
            total_size = sum(f.stat().st_size for f in files_in_folder if f.is_file())
            return total_size

        # Case 2: Disc is a file (e.g., .cue or .bin file)
        # Find all files with the same base name
        base_name = disc_path.stem

        total_size = 0
        try:
            all_files = list(parent_folder.iterdir())
            for file in all_files:
                if file.is_file():
                    matches = file.stem == base_name
                    if matches:
                        file_size = file.stat().st_size
                        total_size += file_size
        except (PermissionError, OSError) as e:
            return disc_path.stat().st_size if disc_path.is_file() else 0

        fallback_size = disc_path.stat().st_size
        final_size = total_size if total_size > 0 else fallback_size
        return final_size

    def get_disc_size_from_api(self, disc_file_name, all_api_files):
        """
        Calculate total size of all API files related to a disc.
        For multi-file discs (e.g., BIN/CUE), this sums all files with the same base name.

        Args:
            disc_file_name: Name of the primary disc file (e.g., "Game (Disc 1).bin")
            all_api_files: List of all API file objects for the game

        Returns:
            Total size in bytes of all related files
        """
        # Get the base name without extension (e.g., "Game (Disc 1)" from "Game (Disc 1).bin")
        base_name = Path(disc_file_name).stem

        # Find all API files with the same base name and sum their sizes
        total_size = 0
        for api_file in all_api_files:
            api_file_name = api_file.get('file_name', '')
            api_stem = Path(api_file_name).stem
            matches = api_stem == base_name
            file_size = api_file.get('file_size_bytes', 0)
            if matches:
                total_size += file_size

        return total_size

    def process_single_rom(self, rom, download_dir):
        """Process a single ROM with short directory names but full display names"""
        rom_id = rom.get('id')
        platform_display_name = rom.get('platform_name', 'Unknown')  # Full name for tree view
        platform_slug = rom.get('platform_slug', platform_display_name)  # Short name for directories

        # If platform_name is missing but we have platform_slug, look it up in the platform mapping
        if platform_display_name == 'Unknown' and platform_slug and platform_slug != 'Unknown':
            if hasattr(self, 'game_cache') and self.game_cache.platform_mapping:
                platform_display_name = self.game_cache.get_platform_name(platform_slug)
        # Clean up platform slug - prefer "megadrive" over "genesis"
        if 'genesis' in platform_slug.lower() and 'megadrive' in platform_slug.lower():
            platform_slug = 'megadrive'
        elif '-slash-' in platform_slug:
            platform_slug = platform_slug.replace('-slash-', '-')
        file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"

        # Use platform slug for local directory structure (RomM and RetroDECK now use the same slugs)
        platform_dir = download_dir / platform_slug
        local_path = platform_dir / file_name

        # Check download status (handles both files and folders)
        is_downloaded = self.is_path_validly_downloaded(local_path)

        display_name = Path(file_name).stem if file_name else rom.get('name', 'Unknown')

        # Check for multi-disc games from API data OR local filesystem
        discs = []

        # First, check API data for multi-file games (works for both downloaded and non-downloaded)
        api_files = rom.get('files', [])
        has_multiple_files = rom.get('has_multiple_files', False) or rom.get('multi', False) or len(api_files) > 1

        if has_multiple_files and len(api_files) > 1:
            # Multi-disc game detected from API
            disc_extensions = {'.chd', '.bin', '.cue', '.iso', '.img', '.pbp'}

            # Filter for actual disc files (skip metadata files, manuals, etc.)
            api_disc_files = [f for f in api_files if Path(f.get('file_name', '')).suffix.lower() in disc_extensions]

            if len(api_disc_files) > 1:
                # Collect disc files, filtering out .cue files paired with .bin files
                filtered_disc_files = []
                for api_file in sorted(api_disc_files, key=lambda x: x.get('file_name', '')):
                    file_name = api_file.get('file_name', 'unknown')
                    # Skip .cue files if there's a corresponding .bin file
                    if file_name.lower().endswith('.cue'):
                        bin_name = file_name[:-4] + '.bin'
                        if any(f.get('file_name') == bin_name for f in api_disc_files):
                            continue
                    filtered_disc_files.append(api_file)

                # Only treat as multi-disc if we have actual separate disc indicators
                # Check for disc naming patterns: (Disc N), (Disk N), (CD N), etc.
                # Exclude Track patterns as they're part of a single disc
                disc_pattern = re.compile(r'(\(|\[|_|-|\s)(disc|disk|cd|dvd)(\s|_|-)?(\d+)(\)|\]|_|-|\s)', re.IGNORECASE)
                track_pattern = re.compile(r'(track|tr)(\s|_|-)?(\d+)', re.IGNORECASE)

                # Extract disc numbers from filenames (only count files, not tracks)
                disc_numbers = set()
                for api_file in filtered_disc_files:
                    file_name = api_file.get('file_name', 'unknown')
                    # Skip if this is a track indicator (not a disc)
                    if track_pattern.search(file_name):
                        continue
                    # Check if this file has a disc indicator
                    match = disc_pattern.search(file_name)
                    if match:
                        disc_num = match.group(4)  # The disc number
                        disc_numbers.add(disc_num)

                # Only treat as multi-disc if we have multiple different disc numbers
                actual_discs = []
                if len(disc_numbers) > 1:
                    for api_file in filtered_disc_files:
                        file_name = api_file.get('file_name', 'unknown')
                        if track_pattern.search(file_name):
                            continue
                        if disc_pattern.search(file_name):
                            actual_discs.append(api_file)

                # Only add to discs list if we found multiple actual disc files
                if len(actual_discs) > 1:
                    for api_file in actual_discs:
                        file_name = api_file.get('file_name', 'unknown')
                        # For downloaded games, use local path; for non-downloaded, use None
                        disc_path = str(local_path / file_name) if is_downloaded and local_path.is_dir() else None

                        # Calculate total size including all related files (e.g., .bin + .cue)
                        total_size = self.get_disc_size_from_api(file_name, api_disc_files)

                        discs.append({
                            'name': file_name,
                            'path': disc_path,
                            'is_downloaded': is_downloaded and local_path.is_dir(),
                            'size': total_size,
                            'file_id': api_file.get('id'),  # Store file ID for direct download
                            'full_path': api_file.get('full_path')  # Store full path from API
                        })

        # Fallback: For downloaded games without API file data, scan local filesystem
        elif is_downloaded and local_path.is_dir() and not api_files:
            disc_extensions = {'.chd', '.bin', '.cue', '.iso', '.img', '.pbp'}
            disc_files = []

            try:
                for file in sorted(local_path.iterdir()):
                    if file.is_file() and file.suffix.lower() in disc_extensions:
                        # Skip .cue files if there are .bin files (they're paired)
                        if file.suffix.lower() == '.cue':
                            bin_file = file.with_suffix('.bin')
                            if bin_file.exists():
                                continue
                        disc_files.append(file)

                # Check for actual multi-disc patterns in filenames
                if len(disc_files) > 1:
                    disc_pattern = re.compile(r'(\(|\[|_|-|\s)(disc|disk|cd|dvd)(\s|_|-)?(\d+)(\)|\]|_|-|\s)', re.IGNORECASE)
                    track_pattern = re.compile(r'(track|tr)(\s|_|-)?(\d+)', re.IGNORECASE)

                    # Extract disc numbers (only from files, not tracks)
                    disc_numbers = set()
                    for f in disc_files:
                        if track_pattern.search(f.name):
                            continue
                        match = disc_pattern.search(f.name)
                        if match:
                            disc_num = match.group(4)
                            disc_numbers.add(disc_num)

                    # Only treat as multi-disc if we have multiple different disc numbers
                    actual_disc_files = []
                    if len(disc_numbers) > 1:
                        for f in disc_files:
                            if track_pattern.search(f.name):
                                continue
                            if disc_pattern.search(f.name):
                                actual_disc_files.append(f)

                    if len(actual_disc_files) > 1:
                        for disc_file in actual_disc_files:
                            # Calculate total size including all related files (e.g., .bin + .cue)
                            total_size = self.get_disc_total_size(disc_file, local_path)
                            discs.append({
                                'name': disc_file.name,
                                'path': str(disc_file),
                                'is_downloaded': True,
                                'size': total_size
                            })
            except (PermissionError, OSError) as e:
                print(f"Warning: Could not scan directory {local_path}: {e}")

        # For multi-disc games, update local_path to point to folder instead of disc file
        if discs and len(discs) > 1:
            # Multi-disc games are stored in folders named after the game
            local_path = platform_dir / display_name
            is_downloaded = self.is_path_validly_downloaded(local_path)

        # Extract only essential data from romm_data to save memory
        essential_romm_data = {
            'fs_name': rom.get('fs_name'),
            'fs_name_no_ext': rom.get('fs_name_no_ext'),
            'fs_size_bytes': rom.get('fs_size_bytes', 0),
            'platform_id': rom.get('platform_id'),
            'platform_slug': rom.get('platform_slug')
        }

        game_data = {
            'name': display_name,
            'rom_id': rom_id,
            'platform': platform_display_name,
            'platform_slug': platform_slug,
            'file_name': file_name,
            'is_downloaded': is_downloaded,
            'local_path': str(local_path) if is_downloaded else None,
            'local_size': self.get_actual_file_size(local_path) if is_downloaded else 0,
            'romm_data': essential_romm_data  # Much smaller object
        }

        # Add discs if this is a multi-disc game (only if there are multiple discs)
        if len(discs) > 1:
            game_data['discs'] = discs
            game_data['is_multi_disc'] = True
        else:
            game_data['is_multi_disc'] = False

        return game_data

    def on_auto_connect_changed(self, switch_row, pspec):
        """Handle auto-connect setting change"""
        self.settings.set('RomM', 'auto_connect', str(switch_row.get_active()).lower())

    def on_auto_refresh_changed(self, switch_row, pspec):
        """Handle auto-refresh setting change"""
        self.settings.set('RomM', 'auto_refresh', str(switch_row.get_active()).lower())    

    def on_about(self, action, param):
        """Show about dialog"""
        about = Adw.AboutWindow(
            transient_for=self,
            application_name="RomM - RetroArch Sync",
            application_icon="com.romm.retroarch.sync",
            version="1.3.2",
            developer_name='Hector Eduardo "Covin" Silveri',
            copyright="© 2025 Hector Eduardo Silveri",
            license_type=Gtk.License.GPL_3_0
        )
        about.set_website("https://github.com/Covin90/romm-retroarch-sync")
        about.set_issue_url("https://github.com/Covin90/romm-retroarch-sync/issues")
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

    def on_retroarch_override_changed(self, entry_row):
        """Handle RetroArch path override change"""
        custom_path = entry_row.get_text().strip()
        
        # Handle RetroDECK config directory input
        if 'retrodeck' in custom_path.lower() and 'config/retroarch' in custom_path:
            # User entered config directory, set to RetroDECK executable instead
            custom_path = 'flatpak run net.retrodeck.retrodeck retroarch'
            entry_row.set_text(custom_path)  # Update the field
        
        self.settings.set('RetroArch', 'custom_path', custom_path)
        
        # Re-initialize RetroArch with new path
        self.retroarch = RetroArchInterface()
        self.refresh_retroarch_info()
        
        if custom_path:
            self.log_message(f"RetroArch path overridden: {custom_path}")
        else:
            self.log_message("RetroArch path override cleared, using auto-detection")

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

        if hasattr(self, 'autostart_row'):
            # Defer autostart check until UI is fully ready
            def check_autostart_when_ready():
                is_enabled = self.check_autostart_status()
                self.autostart_row.set_active(is_enabled)
                return False  # Don't repeat
            
            GLib.timeout_add(100, check_autostart_when_ready)
        
        self.auto_connect_switch.set_active(self.settings.get('RomM', 'auto_connect') == 'true')
        self.auto_refresh_switch.set_active(self.settings.get('RomM', 'auto_refresh') == 'true') 

    def setup_ui(self):
            """Set up the user interface with actually working wider layout"""
            self.set_title("RomM - RetroArch Sync")
            self.set_default_size(800, 900)  # Increased width

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

            /* Much smaller toggle switches for collection view - using scale transform */
            switch.compact-switch {
                transform: scale(0.65);
                margin: -8px;
            }

            /* Also apply to collection-specific classes */
            switch.collection-synced,
            switch.collection-partial-sync,
            switch.collection-not-synced {
                transform: scale(0.65);
                margin: -8px;
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

            # Header bar with menu button
            header = Adw.HeaderBar()
            self.set_titlebar(header)

            # Add menu button to header bar
            menu_button = Gtk.MenuButton()
            menu_button.set_icon_name("open-menu-symbolic")
            menu_button.set_tooltip_text("Menu")

            # Create simple menu
            menu = Gio.Menu()
            menu.append("Logs / Advanced", "win.logs")
            menu.append("About", "win.about")
            menu.append("Quit", "win.quit")
            menu_button.set_menu_model(menu)
            header.pack_end(menu_button)

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
            self.create_bios_section() 

    def create_bios_section(self):
        """Create BIOS management section"""
        bios_group = Adw.PreferencesGroup()
        bios_group.set_title("BIOS &amp; Firmware")
        
        # BIOS status expander
        self.bios_expander = Adw.ExpanderRow()
        self.bios_expander.set_title("System BIOS Files")
        self.bios_expander.set_subtitle("Manage emulator BIOS/firmware files")
        
        # Download All button
        download_container = Gtk.Box()
        download_container.set_size_request(-1, 18)
        download_container.set_valign(Gtk.Align.CENTER)
        
        download_all_btn = Gtk.Button(label="Download All")
        download_all_btn.connect('clicked', self.on_download_all_bios)
        download_all_btn.set_size_request(100, -1)
        download_all_btn.set_valign(Gtk.Align.CENTER)
        download_container.append(download_all_btn)
        
        self.bios_expander.add_suffix(download_container)

        # BIOS path override
        self.bios_override_row = Adw.EntryRow()
        self.bios_override_row.set_title("Custom BIOS Directory (Override auto-detection)")
        self.bios_override_row.set_text(self.settings.get('BIOS', 'custom_path', ''))
        self.bios_override_row.connect('entry-activated', self.on_bios_override_changed)
        self.bios_expander.add_row(self.bios_override_row)

        # System directory info
        self.bios_dir_row = Adw.ActionRow()
        self.bios_dir_row.set_title("BIOS Directory")
        self.bios_dir_row.set_subtitle("Checking...")
        self.bios_expander.add_row(self.bios_dir_row)
        
        # Platform status rows (dynamically added)
        self.bios_platform_rows = {}
        
        bios_group.add(self.bios_expander)
        self.preferences_page.add(bios_group)
        
        # Update BIOS directory info
        self.update_bios_directory_info()
    
    def update_bios_directory_info(self):
        """Update BIOS directory display with debugging"""
        if self.retroarch.bios_manager:
            if self.retroarch.bios_manager.system_dir:
                self.bios_dir_row.set_subtitle(str(self.retroarch.bios_manager.system_dir))
            else:
                # Debug RetroDECK paths
                debug_paths = [
                    Path.home() / 'retrodeck',
                    Path.home() / 'retrodeck' / 'bios', 
                    Path.home() / 'retrodeck' / 'roms',
                    Path.home() / '.var/app/net.retrodeck.retrodeck',
                    Path.home() / '.var/app/net.retrodeck.retrodeck/config/retroarch/system'
                ]
                
                existing_paths = []
                for path in debug_paths:
                    if path.exists():
                        existing_paths.append(str(path))
                
                if existing_paths:
                    self.bios_dir_row.set_subtitle(f"Debug - Found: {', '.join(existing_paths[:2])}")
                else:
                    self.bios_dir_row.set_subtitle("Not found - no RetroDECK paths detected")
        else:
            self.bios_dir_row.set_subtitle("BIOS manager failed to initialize")
    
    def on_download_all_bios(self, button):
        """Download all missing BIOS files for current game platforms"""
        if not self.retroarch.bios_manager:
            self.log_message("⚠️ BIOS manager not available")
            return
        
        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("⚠️ Please connect to RomM first")
            return
        
        def download_all():
            try:
                self.retroarch.bios_manager.romm_client = self.romm_client
                
                # Get platforms from current games
                platforms_in_library = set()
                for game in self.available_games:
                    platform = game.get('platform')
                    if platform:
                        platforms_in_library.add(platform)
                
                GLib.idle_add(lambda: self.log_message(f"📥 Downloading BIOS for {len(platforms_in_library)} platforms..."))
                
                total_downloaded = 0
                for platform in platforms_in_library:
                    normalized = self.retroarch.bios_manager.normalize_platform_name(platform)
                    if self.retroarch.bios_manager.auto_download_missing_bios(normalized):
                        total_downloaded += 1
                
                GLib.idle_add(lambda: self.log_message(f"✅ BIOS download complete for {total_downloaded} platforms"))
                
            except Exception as e:
                GLib.idle_add(lambda: self.log_message(f"❌ BIOS download error: {e}"))
        
        threading.Thread(target=download_all, daemon=True).start()
    
    def download_missing_bios_files(self, platforms_needing_bios):
        """Download missing BIOS files for multiple platforms"""
        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("⚠️ Please connect to RomM first")
            return
        
        def download_all():
            try:
                self.retroarch.bios_manager.romm_client = self.romm_client
                total_downloaded = 0
                total_failed = 0
                
                for platform_name, missing_files in platforms_needing_bios:
                    GLib.idle_add(lambda p=platform_name: 
                                self.log_message(f"📥 Downloading BIOS for {p}..."))
                    
                    for bios_info in missing_files:
                        bios_file = bios_info['file']
                        
                        # Try to download from RomM
                        if self.retroarch.bios_manager.download_bios_from_romm(platform_name, bios_file):
                            total_downloaded += 1
                            GLib.idle_add(lambda f=bios_file: 
                                        self.log_message(f"   ✅ {f}"))
                        else:
                            total_failed += 1
                            GLib.idle_add(lambda f=bios_file: 
                                        self.log_message(f"   ❌ {f} - not found on server"))
                
                # Summary
                if total_failed == 0 and total_downloaded > 0:
                    GLib.idle_add(lambda n=total_downloaded: 
                                self.log_message(f"✅ Downloaded {n} BIOS files successfully!"))
                elif total_downloaded > 0:
                    GLib.idle_add(lambda d=total_downloaded, f=total_failed: 
                                self.log_message(f"⚠️ Downloaded {d} files, {f} not found on server"))
                else:
                    GLib.idle_add(lambda: 
                                self.log_message("❌ No BIOS files could be downloaded from server"))
                
            except Exception as e:
                GLib.idle_add(lambda: self.log_message(f"❌ BIOS download error: {e}"))
        
        threading.Thread(target=download_all, daemon=True).start()

    def on_bios_override_changed(self, entry_row, pspec=None):
        """Handle BIOS path override change"""
        custom_path = entry_row.get_text().strip()
        self.settings.set('BIOS', 'custom_path', custom_path)
        
        # Force complete reinitialization of RetroArch interface
        self.retroarch = RetroArchInterface(self.settings)
        
        # Update the directory display
        self.update_bios_directory_info()
        
        if custom_path:
            self.log_message(f"BIOS path overridden: {custom_path}")
            try:
                Path(custom_path).mkdir(parents=True, exist_ok=True)
                self.log_message(f"✅ BIOS directory ready: {custom_path}")
            except Exception as e:
                self.log_message(f"❌ Could not create BIOS directory: {e}")
        else:
            self.log_message("BIOS path override cleared, reverting to auto-detection")

    def create_connection_section(self):
        """Create combined server section with RomM connection and RetroArch status"""
        connection_group = Adw.PreferencesGroup()
        connection_group.set_title("Server &amp; Status")
        
        # RomM Connection expander (keep as is)
        self.connection_expander = Adw.ExpanderRow()
        self.connection_expander.set_title("RomM Connection")
        self.connection_expander.set_subtitle("Not connected - expand to configure")

        # Add status dot as prefix (15px size for better visibility)
        self.connection_status_dot = self.create_status_dot('grey', size=15)
        self.connection_status_dot.set_margin_end(8)
        self.connection_expander.add_prefix(self.connection_status_dot)

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

        # Auto-refresh switch
        self.auto_refresh_switch = Adw.SwitchRow()
        self.auto_refresh_switch.set_title("Auto-refresh library on startup")
        self.auto_refresh_switch.set_subtitle("Automatically fetch games if cache is outdated")
        self.auto_refresh_switch.connect('notify::active', self.on_auto_refresh_changed)
        self.connection_expander.add_row(self.auto_refresh_switch)

        # Autostart setting
        self.autostart_row = Adw.SwitchRow()
        self.autostart_row.set_title("Run at Startup")
        self.autostart_row.set_subtitle("Run as a service at startup")
        self.autostart_row.connect('notify::active', self.on_autostart_changed)
        self.connection_expander.add_row(self.autostart_row)

        # Device Information section
        device_expander = Adw.ExpanderRow()
        device_expander.set_title("Device Information")
        device_expander.set_subtitle("Registered device details")
        self.connection_expander.add_row(device_expander)

        # Device ID (read-only)
        self.device_id_row = Adw.ActionRow()
        self.device_id_row.set_title("Device ID")
        device_id_label = Gtk.Label()
        device_id_label.set_text("Not registered")
        device_id_label.add_css_class("monospace")
        self.device_id_row.add_suffix(device_id_label)
        self.device_id_label = device_id_label
        device_expander.add_row(self.device_id_row)

        # Device Name
        self.device_name_row = Adw.ActionRow()
        self.device_name_row.set_title("Device Name")
        device_name_label = Gtk.Label()
        device_name_label.set_text("-")
        self.device_name_row.add_suffix(device_name_label)
        self.device_name_label = device_name_label
        device_expander.add_row(self.device_name_row)

        # Device Platform
        self.device_platform_row = Adw.ActionRow()
        self.device_platform_row.set_title("Platform")
        device_platform_label = Gtk.Label()
        device_platform_label.set_text("-")
        self.device_platform_row.add_suffix(device_platform_label)
        self.device_platform_label = device_platform_label
        device_expander.add_row(self.device_platform_row)

        # Client Info
        self.device_client_row = Adw.ActionRow()
        self.device_client_row.set_title("Client")
        device_client_label = Gtk.Label()
        device_client_label.set_text("-")
        self.device_client_row.add_suffix(device_client_label)
        self.device_client_label = device_client_label
        device_expander.add_row(self.device_client_row)

        connection_group.add(self.connection_expander)
        
        # RetroArch section - simplified without status monitoring
        self.retroarch_expander = Adw.ExpanderRow()
        self.retroarch_expander.set_title("RetroArch")
        self.retroarch_expander.set_subtitle("Installation and core information")

        # Add status dot as prefix (15px size for better visibility)
        self.retroarch_status_dot = self.create_status_dot('grey', size=15)
        self.retroarch_status_dot.set_margin_end(8)
        self.retroarch_expander.add_prefix(self.retroarch_status_dot)

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

        # RetroArch installation override
        self.retroarch_override_row = Adw.EntryRow()
        self.retroarch_override_row.set_title("Custom Installation Path (Override auto-detection)")
        self.retroarch_override_row.set_text(self.settings.get('RetroArch', 'custom_path', ''))
        self.retroarch_override_row.connect('activate', self.on_retroarch_override_changed)
        self.retroarch_expander.add_row(self.retroarch_override_row)

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
        self.retroarch_connection_row.set_title("Network Commands")
        self.retroarch_connection_row.set_subtitle("Turn ON in Settings → Network Commands to get notifications")
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

    def initialize_device(self):
        """Initialize device registration with RomM on connection.

        Checks if device is already registered in config, if not registers a new one.
        Returns device_id on success, None on failure.
        """
        if not self.romm_client or not self.romm_client.authenticated:
            return None

        try:
            # Check if device is already registered
            existing_device_id = self.settings.get('Device', 'device_id', '')

            if existing_device_id:
                # Device already registered, just verify it still exists
                print(f"Device ID found in config: {existing_device_id}")
                device_info = self.romm_client.get_device(existing_device_id)

                if device_info:
                    print(f"Device verified on server")
                    self.device_id = existing_device_id  # Cache in app instance
                    return existing_device_id
                else:
                    print(f"Device not found on server, registering new device")
                    # Fall through to register new

            # Register new device
            device_name = self.settings.get('Device', 'device_name', socket.gethostname())
            platform = self.settings.get('Device', 'device_platform', 'Linux')
            client = self.settings.get('Device', 'client', 'RomM-RetroArch-Sync')
            client_version = self.settings.get('Device', 'client_version', '1.3.2')

            device_id = self.romm_client.register_device(
                device_name=device_name,
                platform=platform,
                client=client,
                client_version=client_version
            )

            if device_id:
                # Store device ID in config
                self.settings.set('Device', 'device_id', device_id)
                self.device_id = device_id  # Cache in app instance
                return device_id
            else:
                return None

        except Exception as e:
            print(f"Error initializing device: {e}")
            return None

    def update_device_info_display(self, device_id):
        """Update the device information display in the preferences"""
        try:
            if not self.romm_client or not device_id:
                return

            device_info = self.romm_client.get_device(device_id)
            if device_info:
                # Update ID
                self.device_id_label.set_text(device_id)

                # Update Name
                device_name = device_info.get('name', '-')
                self.device_name_label.set_text(device_name)

                # Update Platform
                device_platform = device_info.get('platform', '-')
                self.device_platform_label.set_text(device_platform)

                # Update Client
                client = device_info.get('client', '-')
                client_version = device_info.get('client_version', '')
                if client_version:
                    client_text = f"{client} ({client_version})"
                else:
                    client_text = client
                self.device_client_label.set_text(client_text)
        except Exception as e:
            print(f"Error updating device info display: {e}")

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
    
    def start_connection(self, url, username, password):
        """Simplified connection without additional testing"""
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
            import time
            
            # START TIMING
            start_time = time.time()
            self.log_message(f"🔗 Starting RomM connection...")
            
            GLib.idle_add(lambda: self.update_connection_ui("connecting"))
            
            # STEP 1: Initialize client
            init_start = time.time()
            self.romm_client = RomMClient(url, username, password)
            init_time = time.time() - init_start
            self.log_message(f"⚡ Client initialized in {init_time:.2f}s")
            
            def update_ui():
                # STEP 2: Check authentication result
                auth_time = time.time() - start_time
                
                if self.romm_client.authenticated:
                    self.log_message(f"✅ Authentication successful in {auth_time:.2f}s")

                    # Device Registration: Initialize or retrieve device ID
                    try:
                        device_reg_start = time.time()
                        device_id = self.initialize_device()
                        if device_id:
                            device_reg_time = time.time() - device_reg_start
                            self.log_message(f"📱 Device registered: {device_id} ({device_reg_time:.2f}s)")
                            # Update the device info display in preferences
                            GLib.idle_add(lambda: self.update_device_info_display(device_id))
                        else:
                            self.log_message(f"⚠️ Device registration skipped or failed")
                    except Exception as e:
                        self.log_message(f"⚠️ Device initialization error: {e}")

                    # CRITICAL: Fetch platform mapping immediately after authentication
                    # This ensures platform names are available for all operations
                    try:
                        platforms_start = time.time()
                        platforms = self.romm_client.get_platforms()
                        if platforms:
                            self.game_cache.build_platform_mapping_from_api(platforms)
                            platforms_time = time.time() - platforms_start
                            self.log_message(f"📋 Loaded {len(platforms)} platform names in {platforms_time:.2f}s")
                    except Exception as e:
                        self.log_message(f"⚠️ Could not fetch platform names: {e}")

                    # Move this right after authentication success, before other operations
                    def preload_collections_smart():
                        if hasattr(self, 'library_section'):
                            # Always cache collections data to ensure instant loading
                            self.library_section.cache_collections_data()

                    # Call immediately, not as thread
                    GLib.timeout_add(100, lambda: (threading.Thread(target=preload_collections_smart, daemon=True).start(), False)[1])

                    # STEP 3: Test basic API access
                    api_test_start = time.time()
                    try:
                        test_count = self.romm_client.get_games_count_only()
                        api_test_time = time.time() - api_test_start
                        
                        if test_count is not None:
                            self.log_message(f"📊 API test successful in {api_test_time:.2f}s ({test_count:,} games)")
                        else:
                            self.log_message(f"⚠️ API test completed in {api_test_time:.2f}s (count unavailable)")
                            
                    except Exception as e:
                        api_test_time = time.time() - api_test_start
                        self.log_message(f"❌ API test failed in {api_test_time:.2f}s: {str(e)[:100]}")
                    
                    if hasattr(self, 'auto_sync'):
                        self.auto_sync.romm_client = self.romm_client
                    
                    cached_count = len(self.game_cache.cached_games) if self.game_cache.is_cache_valid() else 0
                    if cached_count > 0:
                        # Show cached games immediately first
                        download_dir = Path(self.rom_dir_row.get_text())
                        all_cached_games = []

                        for game in list(self.game_cache.cached_games):
                            platform_slug = game.get('platform_slug') or game.get('platform', 'Unknown')
                            is_multi = game.get('is_multi_disc', False)

                            # Use cached local_path if available, otherwise construct from file_name
                            cached_path = game.get('local_path')
                            if cached_path:
                                local_path = Path(cached_path)
                            else:
                                file_name = game.get('file_name', '')
                                if not file_name:
                                    continue
                                platform_dir = download_dir / platform_slug
                                local_path = platform_dir / file_name

                            is_downloaded = self.is_path_validly_downloaded(local_path)

                            game_copy = game.copy()
                            # Platform name already resolved by cache.load_games_cache() at line 172
                            # Just update download status
                            game_copy['is_downloaded'] = is_downloaded
                            game_copy['local_path'] = str(local_path) if is_downloaded else None
                            game_copy['local_size'] = self.get_actual_file_size(local_path) if is_downloaded else 0

                            # Update disc status for multi-disc games
                            if game_copy.get('is_multi_disc') and game_copy.get('discs'):
                                for disc in game_copy['discs']:
                                    disc['is_downloaded'] = is_downloaded

                            all_cached_games.append(game_copy)

                            if is_multi:
                                print(f"  -> Added to all_cached_games with is_downloaded={game_copy['is_downloaded']}")
                        
                        # Update UI immediately with cached games
                        def update_games_ui():
                            self.available_games = all_cached_games
                            if hasattr(self, 'library_section'):
                                self.library_section.update_games_library(all_cached_games)
                        
                        GLib.idle_add(update_games_ui)
                        
                        # Define freshness check function first
                        def check_cache_freshness():
                            try:
                                server_count = self.romm_client.get_games_count_only()
                                
                                if server_count is not None:
                                    count_diff = abs(server_count - cached_count)
                                    if count_diff > 0:
                                        # Check auto-refresh setting before refreshing
                                        auto_refresh_enabled = self.settings.get('RomM', 'auto_refresh') == 'true'
                                        if auto_refresh_enabled:
                                            def auto_refresh():
                                                self.update_connection_ui_with_message(f"⟳ Cache outdated ({count_diff} games difference) - auto-refreshing...")
                                                self.log_message(f"📊 Auto-refreshing: {count_diff} games difference detected")
                                                self.refresh_games_list()
                                            GLib.idle_add(auto_refresh)
                                        else:
                                            def show_outdated():
                                                self.update_connection_ui_with_message(f"🟡 Connected - {cached_count:,} games cached • ⚠️ {count_diff} games difference detected - Consider refreshing the library")
                                                self.log_message(f"📊 Server has {count_diff} different games - consider refreshing")
                                            GLib.idle_add(show_outdated)
                                    else:
                                        def update_status():
                                            self.update_connection_ui_with_message(f"🟢 Connected - {cached_count:,} games cached")
                                            self.log_message(f"📊 Cache is up to date with server")
                                        GLib.idle_add(update_status)
                                else:
                                    # Server check failed, show cache info
                                    def update_status():
                                        self.update_connection_ui_with_message(f"🟢 Connected - {cached_count:,} games cached")
                                        self.log_message(f"⚠️ Could not check server, using cached data")
                                    GLib.idle_add(update_status)
                            except Exception as e:
                                def update_status():
                                    self.update_connection_ui_with_message(f"🟢 Connected - {cached_count:,} games cached")
                                    self.log_message(f"⚠️ Freshness check failed: {e}")
                                GLib.idle_add(update_status)
                        
                        # Check if auto-refresh is enabled
                        auto_refresh_enabled = self.settings.get('RomM', 'auto_refresh') == 'true'

                        if auto_refresh_enabled:
                            self.update_connection_ui_with_message(f"🟢 Connected - {cached_count:,} games cached • checking for updates...")
                            threading.Thread(target=check_cache_freshness, daemon=True).start()
                        else:
                            # Auto-refresh disabled, show cache info
                            self.update_connection_ui_with_message(f"🟢 Connected - {cached_count:,} games cached")
                            self.log_message(f"📂 Showing {cached_count:,} cached games (auto-refresh disabled)")
                        
                        # Check cache freshness in background
                        threading.Thread(target=check_cache_freshness, daemon=True).start()
                        
                    else:
                        # Always fetch on first startup (no cached games)
                        self.update_connection_ui("loading")
                        self.log_message("🔄 Connected! Loading games list for first time...")
                        self.refresh_games_list()
                    
                    # Auto-sync settings...
                    if self.settings.get('AutoSync', 'auto_enable_on_connect') == 'true':
                        self.autosync_enable_switch.set_active(True)
                        self.log_message("🔥 Auto-sync enabled automatically")

                    # Restore collection auto-sync if it was enabled
                    if hasattr(self, 'library_section'):
                        self.library_section.restore_collection_auto_sync_on_connect()

                    # Start background polling for new games
                    self._start_background_polling()

                    total_time = time.time() - start_time
                    self.log_message(f"🎉 Total connection time: {total_time:.2f}s")
                        
                else:
                    # Authentication failed logic...
                    auth_time = time.time() - start_time
                    self.log_message(f"❌ Authentication failed after {auth_time:.2f}s")
                    self.log_message(f"🔍 Debug: Check server accessibility and credentials")
                    self.update_connection_ui("failed")
                    self.connection_enable_switch.set_active(False)

            GLib.idle_add(update_ui)

        threading.Thread(target=connect, daemon=True).start() 

    def disconnect_from_romm(self):
        """Disconnect and switch to local-only view"""
        self.romm_client = None
        
        # Clear selections when disconnecting  
        if hasattr(self, 'library_section'):
            self.library_section.clear_checkbox_selections_smooth()
            self.library_section.stop_collection_auto_sync()
        
        if hasattr(self, 'auto_sync'):
            self.auto_sync.stop_auto_sync()
            # Turn off auto-sync switch when disconnected
            self.autosync_enable_switch.set_active(False)
            self.autosync_status_row.set_subtitle("Disabled - not connected to RomM")
            self.update_status_dot(self.autosync_status_dot, 'red')

        # Stop background polling
        self._stop_background_polling()

        self.update_connection_ui("disconnected")
        self.log_message("Disconnected from RomM")

        # Switch to local-only view immediately
        self.handle_offline_mode()

    def update_connection_ui(self, state):
        """Update connection UI based on state"""
        if state == "connecting":
            self.connection_expander.set_subtitle("Connecting...")
            self.update_status_dot(self.connection_status_dot, 'yellow')

        elif state == "loading":
            self.connection_expander.set_subtitle("Loading games...")
            self.update_status_dot(self.connection_status_dot, 'yellow')

        elif state == "connected":
            # Add game count when connected
            game_count = len(getattr(self, 'available_games', []))
            if game_count > 0:
                subtitle = f"Connected - {game_count:,} Games"
            else:
                subtitle = "Connected"
            self.connection_expander.set_subtitle(subtitle)
            self.update_status_dot(self.connection_status_dot, 'green')

        elif state == "failed":
            self.connection_expander.set_subtitle("Connection failed")
            self.update_status_dot(self.connection_status_dot, 'red')

        elif state == "disconnected":
            self.connection_expander.set_subtitle("Disconnected")
            self.update_status_dot(self.connection_status_dot, 'red')

    def update_connection_ui_with_message(self, message):
        """Update connection UI with custom message"""
        # Remove emoji and update dot color based on message content
        clean_message = message.replace("🟢 ", "").replace("🟡 ", "").replace("🔴 ", "")
        self.connection_expander.set_subtitle(clean_message)

        # Update dot color based on original message
        if "🟢" in message:
            self.update_status_dot(self.connection_status_dot, 'green')
        elif "🟡" in message:
            self.update_status_dot(self.connection_status_dot, 'yellow')
        elif "🔴" in message:
            self.update_status_dot(self.connection_status_dot, 'red')     


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
                self.autosync_status_row.set_subtitle("Active - monitoring for changes")
                self.update_status_dot(self.autosync_status_dot, 'green')
                
                self.log_message("🔄 Auto-sync enabled")
            else:
                self.log_message("⚠️ Please connect to RomM before enabling auto-sync")
                self.autosync_status_row.set_subtitle("Disabled - not connected to RomM")
                self.update_status_dot(self.autosync_status_dot, 'red')
                switch_row.set_active(False)
        else:
            self.auto_sync.stop_auto_sync()
            self.autosync_status_row.set_subtitle("Disabled")
            self.update_status_dot(self.autosync_status_dot, 'red')
            self.log_message("⏹️ Auto-sync disabled")

    def get_selected_game(self):
        """Get currently selected game from tree view"""
        if hasattr(self, 'library_section'):
            return self.library_section.selected_game
        return None

    def on_auto_enable_sync_changed(self, switch_row, pspec):
        """Handle auto-enable sync setting change"""
        self.settings.set('AutoSync', 'auto_enable_on_connect', str(switch_row.get_active()).lower())

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

        # Max concurrent downloads
        self.max_downloads_row = Adw.SpinRow()
        self.max_downloads_row.set_title("Max Concurrent Downloads")
        self.max_downloads_row.set_subtitle("Maximum simultaneous ROM downloads")
        downloads_adjustment = Gtk.Adjustment(value=3, lower=1, upper=10, step_increment=1)
        self.max_downloads_row.set_adjustment(downloads_adjustment)
        self.max_downloads_row.set_value(int(self.settings.get('Download', 'max_concurrent', '3')))
        self.max_downloads_row.connect('notify::value', self.on_max_downloads_changed)
        self.rom_dir_expander.add_row(self.max_downloads_row)

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

        # Collection sync settings
        collection_sync_row = Adw.SpinRow()
        collection_sync_row.set_title("Collection Sync Interval")
        collection_sync_row.set_subtitle("Seconds between collection updates (minimum 30s)")
        adjustment = Gtk.Adjustment(value=120, lower=30, upper=600, step_increment=30)  # 30s to 10min
        collection_sync_row.set_adjustment(adjustment)
        collection_sync_row.set_value(int(self.settings.get('Collections', 'sync_interval', '120')))
        collection_sync_row.connect('notify::value', self.on_collection_sync_interval_changed)
        self.autosync_expander.add_row(collection_sync_row)

        # Clear collection selection
        clear_collections_row = Adw.ActionRow()
        clear_collections_row.set_title("Clear Collection Selection")
        clear_collections_row.set_subtitle("Remove all collections from auto-sync")
        clear_btn = Gtk.Button(label="Clear All")
        clear_btn.connect('clicked', self.on_clear_collection_selection)
        clear_btn.set_valign(Gtk.Align.CENTER)
        clear_collections_row.add_suffix(clear_btn)
        self.autosync_expander.add_row(clear_collections_row)
        
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
        self.autosync_status_row.set_subtitle("Disabled")

        # Add status dot as prefix
        self.autosync_status_dot = self.create_status_dot('red')
        self.autosync_status_dot.set_margin_end(8)
        self.autosync_status_row.add_prefix(self.autosync_status_dot)

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
                self.autosync_status_row.set_subtitle("Active - monitoring for changes")
                self.update_status_dot(self.autosync_status_dot, 'green')
                
                self.log_message("🔄 Auto-sync enabled")
            else:
                self.log_message("⚠️ Please connect to RomM before enabling auto-sync")
                switch_row.set_active(False)
        else:
            self.auto_sync.stop_auto_sync()
            self.autosync_status_row.set_subtitle("Disabled")
            self.update_status_dot(self.autosync_status_dot, 'red')
            self.log_message("⏹️ Auto-sync disabled")

    def on_collection_sync_interval_changed(self, spin_row, pspec):
        """Save collection sync interval in seconds"""
        interval = int(spin_row.get_value())
        self.settings.set('Collections', 'sync_interval', str(interval))
        if hasattr(self, 'library_section'):
            self.library_section.collection_sync_interval = interval

    def on_clear_collection_selection(self, button):
        """Clear all selected collections"""
        if hasattr(self, 'library_section'):
            self.library_section.selected_collections_for_sync.clear()
            self.library_section.save_selected_collections()
            self.library_section.stop_collection_auto_sync()
            self.log_message("Cleared collection auto-sync selection")

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

        # Debug Sync Tracking
        sync_debug_row = Adw.ActionRow()
        sync_debug_row.set_title("Test Sync Tracking")
        sync_debug_row.set_subtitle("Test track/untrack save sync controls")
        sync_debug_btn = Gtk.Button(label="Test")
        sync_debug_btn.set_valign(Gtk.Align.CENTER)
        sync_debug_btn.set_size_request(80, -1)
        sync_debug_btn.connect('clicked', self.on_debug_sync_tracking)
        sync_debug_row.add_suffix(sync_debug_btn)
        advanced_group.add(sync_debug_row)

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
        """Add message to log view with buffer limit"""
        
        # ADD THIS: Also write to file for SteamOS debugging
        try:
            log_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'debug.log'
            log_file.parent.mkdir(parents=True, exist_ok=True)
            with open(log_file, 'a', encoding='utf-8') as f:
                import datetime
                timestamp = datetime.datetime.now().strftime("%H:%M:%S")
                f.write(f"[{timestamp}] {message}\n")
        except:
            pass
        
        def update_ui():
            try:
                buffer = self.log_view.get_buffer()
                
                # Limit buffer to last 1000 lines
                line_count = buffer.get_line_count()
                if line_count > 1000:
                    start = buffer.get_start_iter()
                    # Delete first 200 lines to avoid frequent trimming
                    line_iter = buffer.get_iter_at_line(200)
                    buffer.delete(start, line_iter)
                
                end_iter = buffer.get_end_iter()
                buffer.insert(end_iter, f"{message}\n")
                
                end_mark = buffer.get_insert()
                buffer.place_cursor(buffer.get_end_iter())
                self.log_view.scroll_to_mark(end_mark, 0.0, False, 0.0, 0.0)
            except:
                pass
        
        GLib.idle_add(update_ui)

    def send_desktop_notification(self, title, body):
        """Send a desktop notification (GNOME/KDE/etc)"""
        import subprocess
        import os

        try:
            # Method 1: Direct D-Bus call (most reliable for GNOME)
            try:
                # Use gdbus to send notification directly to the notification daemon
                # This bypasses GTK/Gio and talks directly to org.freedesktop.Notifications
                result = subprocess.run([
                    'gdbus', 'call', '--session',
                    '--dest=org.freedesktop.Notifications',
                    '--object-path=/org/freedesktop/Notifications',
                    '--method=org.freedesktop.Notifications.Notify',
                    'RomM Sync',  # app_name
                    '0',  # replaces_id
                    'folder-download',  # app_icon
                    title,  # summary
                    body,  # body
                    '[]',  # actions
                    '{}',  # hints
                    '5000'  # timeout (5 seconds)
                ], timeout=5, capture_output=True, text=True)

                if result.returncode == 0:
                    print(f"✅ Desktop notification sent (gdbus): {title}")
                    return True
                else:
                    print(f"⚠️ gdbus notification failed: {result.stderr}")

            except FileNotFoundError:
                print("⚠️ gdbus not found, trying notify-send...")
            except Exception as e:
                print(f"⚠️ gdbus error: {e}")

            # Method 2: notify-send (standard tool)
            try:
                result = subprocess.run(
                    ['notify-send',
                     '--app-name=RomM Sync',
                     '--icon=folder-download',
                     '--urgency=normal',
                     title,
                     body],
                    timeout=5,
                    capture_output=True,
                    text=True
                )

                if result.returncode == 0:
                    print(f"✅ Desktop notification sent (notify-send): {title}")
                    return True
                else:
                    print(f"⚠️ notify-send failed: {result.stderr}")

            except FileNotFoundError:
                print("❌ notify-send not found, trying Gio.Notification...")
            except Exception as e:
                print(f"⚠️ notify-send error: {e}")

            # Method 3: Fallback to Gio.Notification
            app = self.get_application()
            if app and hasattr(app, 'send_notification'):
                try:
                    notification = Gio.Notification.new(title)
                    notification.set_body(body)
                    icon = Gio.ThemedIcon.new("folder-download-symbolic")
                    notification.set_icon(icon)
                    notification_id = f"collection-sync-{hash(title) % 10000}"
                    app.send_notification(notification_id, notification)
                    print(f"✅ Sent Gio notification: {title}")
                    return True
                except Exception as e:
                    print(f"❌ Gio.Notification failed: {e}")

            print(f"❌ All notification methods failed for: {title}")
            return False

        except Exception as e:
            print(f"❌ Desktop notification error: {e}")
            import traceback
            traceback.print_exc()
            return False

    def on_choose_directory(self, button):
        """Choose download directory"""
        dialog = Gtk.FileDialog()
        dialog.set_title("Choose ROM Download Directory")
        
        def on_response(source, result):
            try:
                file = dialog.select_folder_finish(result)
                if file:
                    path = file.get_path()
                    self.rom_dir_row.set_text(path)
                    self.rom_dir_expander.set_subtitle(path)
                    self.settings.set('Download', 'rom_directory', path)
                    self.log_message(f"Download directory set to: {path}")
            except Exception as e:
                # User cancelled or error occurred
                pass
        
        dialog.select_folder(self, None, on_response)

    def on_max_downloads_changed(self, spin_row, pspec):
        """Save max concurrent downloads setting"""
        self.settings.set('Download', 'max_concurrent', str(int(spin_row.get_value())))

    def update_download_progress(self, progress_info, rom_id=None):
        """Update progress for specific game in tree view only"""
        if not rom_id:
            rom_id = getattr(self, '_current_download_rom_id', None)
        if not rom_id:
            return
        
        # Only update tree view progress data
        current_time = time.time()
        last_update = self._last_progress_update.get(rom_id, 0)
        
        if rom_id in self.download_progress:
            # ADD THIS: Validate progress only increases
            current_progress = progress_info.get('progress', 0)
            last_progress = self.download_progress[rom_id].get('progress', 0)
            
            # Skip if progress goes backwards (unless it's a restart from 0)
            if current_progress < last_progress and current_progress > 0.01:
                return
            
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
            
    def refresh_retroarch_info(self):
        """Update RetroArch information in UI with installation type"""
        def update_info():
            try:
                # Check RetroArch executable
                if hasattr(self, 'retroarch_info_row'):
                    if self.retroarch.retroarch_executable:
                        # Determine installation type
                        if 'retrodeck' in self.retroarch.retroarch_executable.lower():
                            install_type = "RetroDECK"
                        elif 'flatpak' in self.retroarch.retroarch_executable:
                            install_type = "Flatpak"
                        elif 'steam' in self.retroarch.retroarch_executable.lower():
                            install_type = "Steam"
                        elif 'snap' in self.retroarch.retroarch_executable:
                            install_type = "Snap"
                        elif '.AppImage' in self.retroarch.retroarch_executable:
                            install_type = "AppImage"
                        else:
                            install_type = "Native"
                        
                        self.retroarch_info_row.set_subtitle(f"Found: {install_type} - {self.retroarch.retroarch_executable}")
                        self.retroarch_expander.set_subtitle(f"{install_type} RetroArch detected")
                        self.update_status_dot(self.retroarch_status_dot, 'green')
                    else:
                        self.retroarch_info_row.set_subtitle("Not found")
                        self.retroarch_expander.set_subtitle("RetroArch not found")
                        self.update_status_dot(self.retroarch_status_dot, 'red')
            except Exception as e:
                print(f"Error updating RetroArch info: {e}")
                if hasattr(self, 'retroarch_info_row'):
                    self.retroarch_info_row.set_subtitle("Error checking installation")
            
            try:
                # Check cores directory
                if hasattr(self, 'cores_info_row'):
                    if self.retroarch.cores_dir:
                        self.cores_info_row.set_subtitle(f"Found: {self.retroarch.cores_dir}")
                        
                        # Count cores
                        cores = self.retroarch.get_available_cores()
                        core_count = len(cores)
                        
                        if hasattr(self, 'core_count_row'):
                            self.core_count_row.set_subtitle(f"{core_count} cores available")
                    else:
                        self.cores_info_row.set_subtitle("Cores directory not found")
                        if hasattr(self, 'core_count_row'):
                            self.core_count_row.set_subtitle("0 cores available")
                            
                # Check network commands configuration
                if hasattr(self, 'retroarch_connection_row'):
                    network_ok, network_status = self.retroarch.check_network_commands_config()
                    if network_ok:
                        self.retroarch_connection_row.set_subtitle(f"{network_status}")
                    else:
                        self.retroarch_connection_row.set_subtitle(f"{network_status} - Turn ON in Settings → Network Commands for improved sync and notifications")
                        
            except Exception as e:
                print(f"Error checking RetroArch info: {e}")
                if hasattr(self, 'cores_info_row'):
                    self.cores_info_row.set_subtitle("Error checking cores")
                if hasattr(self, 'retroarch_connection_row'):
                    self.retroarch_connection_row.set_subtitle("Turn ON in Settings → Network Commands to get notifications")
        
        # Ensure UI update happens in main thread
        from gi.repository import GLib
        GLib.idle_add(update_info)
            
    def on_refresh_retroarch_info(self, button):
        """Refresh RetroArch information"""
        self.log_message("Refreshing RetroArch information...")
        
        # Re-initialize RetroArch interface
        self.retroarch = RetroArchInterface()
        self.refresh_retroarch_info()
        
        self.log_message("RetroArch information updated")

    def refresh_games_list(self, force_full_refresh=False):
        """Smart sync with comprehensive change detection

        Args:
            force_full_refresh: If True, fetch all data regardless of timestamps (default: False)
        """
        if getattr(self, '_dialog_open', False):
            return

        def smart_sync():
            if not (self.romm_client and self.romm_client.authenticated):
                self.handle_offline_mode()
                return

            try:
                download_dir = Path(self.rom_dir_row.get_text())
                server_url = self.romm_client.base_url

                # Determine whether to do incremental or full refresh
                use_incremental = (
                    not force_full_refresh and
                    self._last_full_fetch_time is not None
                )

                if use_incremental:
                    self.log_message(f"🔄 Checking for updates from server: {server_url}")
                    self.perform_incremental_sync(download_dir, server_url)
                else:
                    self.log_message(f"🔄 Syncing with server: {server_url}")
                    self.perform_full_sync(download_dir, server_url)

            except Exception as e:
                self.log_message(f"❌ Sync error: {e}")
                self.use_cached_data_as_fallback()

        threading.Thread(target=smart_sync, daemon=True).start()

    def perform_full_sync(self, download_dir, server_url):
        """Perform full sync with live updates"""
        try:
            sync_start = time.time()

            # Preserve existing local games to keep them visible during fetch
            existing_local_games = []
            if hasattr(self, 'available_games') and self.available_games:
                # Keep all existing games for now (they'll be updated/merged with server data)
                existing_local_games = list(self.available_games)
                self.log_message(f"Preserving {len(existing_local_games)} existing games during fetch")

            # Debouncing: Track last UI update time to prevent excessive updates
            last_ui_update = [0]  # Use list to allow modification in nested function
            min_update_interval = 0.5  # Minimum 500ms between UI updates
            pending_update_source = [None]  # Track pending GLib timeout

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
                        process_start = time.time()
                        processed_games = []
                        for rom in chunk_games:
                            processed_game = self.process_single_rom(rom, download_dir)
                            processed_games.append(processed_game)

                        # Merge with existing local games to keep them visible
                        # Create a map to identify duplicates (use rom_id if available, otherwise use file path)
                        fetched_identifiers = set()
                        for g in processed_games:
                            rom_id = g.get('rom_id')
                            if rom_id:
                                fetched_identifiers.add(('rom_id', rom_id))
                            # Also track by file path for games without rom_id
                            local_path = g.get('local_path')
                            if local_path:
                                fetched_identifiers.add(('path', local_path))

                        # Add local games that aren't in the fetched data yet
                        added_count = 0
                        for local_game in existing_local_games:
                            local_rom_id = local_game.get('rom_id')
                            local_path = local_game.get('local_path')

                            # Check if this game is already in the fetched data
                            is_duplicate = False
                            if local_rom_id and ('rom_id', local_rom_id) in fetched_identifiers:
                                is_duplicate = True
                            elif local_path and ('path', local_path) in fetched_identifiers:
                                is_duplicate = True

                            if not is_duplicate:
                                processed_games.append(local_game)
                                added_count += 1

                        # Sort games
                        processed_games = self.library_section.sort_games_consistently(processed_games)

                        # Debounced UI update
                        current_time = time.time()
                        time_since_last_update = current_time - last_ui_update[0]

                        def do_ui_update():
                            ui_start = time.time()
                            # Update with merged games (fetched + remaining local)
                            self.available_games = processed_games
                            if hasattr(self, 'library_section'):
                                self.library_section.update_games_library(processed_games)
                            last_ui_update[0] = time.time()
                            pending_update_source[0] = None
                            return False  # Don't repeat

                        # If enough time has passed, update immediately
                        if time_since_last_update >= min_update_interval:
                            # Cancel any pending update
                            if pending_update_source[0]:
                                GLib.source_remove(pending_update_source[0])
                                pending_update_source[0] = None
                            GLib.idle_add(do_ui_update)
                        else:
                            # Schedule update for later (debounce)
                            if not pending_update_source[0]:  # Only schedule if not already pending
                                delay_ms = int((min_update_interval - time_since_last_update) * 1000)
                                pending_update_source[0] = GLib.timeout_add(delay_ms, do_ui_update)

            # Fetch with progress handler
            fetch_start = time.time()
            romm_result = self.romm_client.get_roms(progress_callback=progress_handler)

            if not romm_result or len(romm_result) != 2:
                self.log_message("Failed to fetch games from RomM")
                return

            final_games, total_count = romm_result

            # Final processing and UI update
            final_process_start = time.time()
            games = []
            for rom in final_games:
                processed_game = self.process_single_rom(rom, download_dir)
                games.append(processed_game)

            games = self.library_section.sort_games_consistently(games)

            def final_update():
                final_ui_start = time.time()
                self.available_games = games
                if hasattr(self, 'library_section'):
                    self.library_section.update_games_library(games)

                total_elapsed = time.time() - sync_start

                # Show completion message first
                completion_msg = f"✓ Full sync complete: {len(games):,} games in {total_elapsed:.2f}s"
                self.update_connection_ui_with_message(completion_msg)
                self.log_message(completion_msg)

                # After 3 seconds, show connected status
                def show_connected():
                    self.update_connection_ui("connected")
                    return False  # Don't repeat

                GLib.timeout_add(5000, show_connected)  # 3 second delay

            GLib.idle_add(final_update)

            # Set timestamp for future incremental updates
            self._last_full_fetch_time = datetime.datetime.now(timezone.utc).isoformat()

            # Save cache in background
            content_hash = hash(str(len(games)) + str(games[0].get('rom_id', '') if games else ''))
            threading.Thread(target=lambda: self.game_cache.save_games_data(games), daemon=True).start()

            # Clear collections cache after main library refresh
            if hasattr(self, 'library_section'):
                self.library_section.collections_cache_time = 0

        except Exception as e:
            self.log_message(f"Full sync error: {e}")

    def perform_incremental_sync(self, download_dir, server_url):
        """Perform incremental sync using updated_after parameter"""
        try:
            sync_start = time.time()

            # Fetch only ROMs updated since last check
            updated_after = self._last_full_fetch_time
            new_roms_data = self.romm_client.get_roms(
                limit=10000,  # High limit for incremental updates
                offset=0,
                updated_after=updated_after
            )

            if not new_roms_data or len(new_roms_data) != 2:
                self.log_message("Incremental sync: no data returned")
                return

            new_roms, _ = new_roms_data

            if not new_roms:
                self.log_message("✓ No new games found")
                return

            # Process new/updated ROMs
            new_count = 0
            updated_count = 0

            # Create a map for fast lookup by rom_id
            existing_games_map = {g['rom_id']: g for g in self.available_games if 'rom_id' in g}

            for rom in new_roms:
                rom_id = rom.get('id')
                was_existing = rom_id in existing_games_map

                processed_game = self.process_single_rom(rom, download_dir)
                existing_games_map[rom_id] = processed_game

                if was_existing:
                    updated_count += 1
                else:
                    new_count += 1

            # Update the games list
            updated_games = list(existing_games_map.values())
            updated_games = self.library_section.sort_games_consistently(updated_games)

            def update_ui():
                self.available_games = updated_games
                if hasattr(self, 'library_section'):
                    self.library_section.update_games_library(updated_games)

                total_elapsed = time.time() - sync_start
                if new_count > 0 or updated_count > 0:
                    msg = f"✓ Found {new_count} new, {updated_count} updated games ({total_elapsed:.2f}s)"
                    self.log_message(msg)
                    self.update_connection_ui_with_message(msg)

                    # After 3 seconds, show connected status
                    def show_connected():
                        self.update_connection_ui("connected")
                        return False
                    GLib.timeout_add(3000, show_connected)

            GLib.idle_add(update_ui)

            # Update timestamp
            self._last_full_fetch_time = datetime.datetime.now(timezone.utc).isoformat()

            # Save updated cache in background
            threading.Thread(target=lambda: self.game_cache.save_games_data(updated_games), daemon=True).start()

        except Exception as e:
            self.log_message(f"Incremental sync error: {e}")
            # Fall back to full sync on error
            self.log_message("Falling back to full sync...")
            self.perform_full_sync(download_dir, server_url)

    def _start_background_polling(self):
        """Start background polling thread for incremental updates"""
        if self._polling_thread and self._polling_thread.is_alive():
            return  # Already running

        self._stop_polling.clear()
        self._polling_thread = threading.Thread(
            target=self._polling_loop,
            daemon=True,
            name="romm-gtk-polling"
        )
        self._polling_thread.start()
        self.log_message("📡 Background polling started (60s interval)")

    def _stop_background_polling(self):
        """Stop background polling thread"""
        if self._polling_thread:
            self._stop_polling.set()
            self._polling_thread.join(timeout=2)
            self._polling_thread = None
            self.log_message("📡 Background polling stopped")

    def _polling_loop(self):
        """Background polling loop - checks for new games every 60 seconds"""
        # Wait a bit before starting to let the initial connection settle
        time.sleep(10)

        while not self._stop_polling.is_set():
            # Sleep for 60 seconds (or until stopped)
            if self._stop_polling.wait(timeout=60):
                break  # Event was set, exit loop

            try:
                # Skip if not connected or no timestamp yet
                if not (self.romm_client and self.romm_client.authenticated):
                    continue
                if self._last_full_fetch_time is None:
                    continue

                # Fetch only ROMs updated since last check
                download_dir = Path(self.rom_dir_row.get_text())
                new_roms_data = self.romm_client.get_roms(
                    limit=1000,
                    offset=0,
                    updated_after=self._last_full_fetch_time
                )

                if not new_roms_data or len(new_roms_data) != 2:
                    continue

                new_roms, _ = new_roms_data

                if new_roms:
                    # Process new/updated ROMs
                    existing_games_map = {g['rom_id']: g for g in self.available_games if 'rom_id' in g}
                    new_count = 0
                    updated_count = 0

                    for rom in new_roms:
                        rom_id = rom.get('id')
                        was_existing = rom_id in existing_games_map

                        processed_game = self.process_single_rom(rom, download_dir)
                        existing_games_map[rom_id] = processed_game

                        if was_existing:
                            updated_count += 1
                        else:
                            new_count += 1

                    # Update the games list
                    updated_games = list(existing_games_map.values())
                    updated_games = self.library_section.sort_games_consistently(updated_games)

                    def update_ui():
                        self.available_games = updated_games
                        if hasattr(self, 'library_section'):
                            self.library_section.update_games_library(updated_games)
                        return False

                    GLib.idle_add(update_ui)

                    # Update timestamp
                    self._last_full_fetch_time = datetime.datetime.now(timezone.utc).isoformat()

                    if new_count > 0 or updated_count > 0:
                        def log_update():
                            self.log_message(f"📡 Auto-detected {new_count} new, {updated_count} updated games")
                            return False
                        GLib.idle_add(log_update)

            except Exception as e:
                # Log errors but don't crash the polling thread
                def log_error():
                    self.log_message(f"📡 Polling error: {e}")
                    return False
                GLib.idle_add(log_error)

        self.log_message("📡 Polling loop exited")

    def scan_local_games_only(self, download_dir):
        """Enhanced local game scanning that handles both slug and full platform names"""
        games = []

        self.log_message(f"Scanning {download_dir}")
        self.log_message(f"Directory exists: {download_dir.exists()}")

        if not download_dir.exists():
            return games

        # Ensure platform mapping is populated before scanning
        if not self.game_cache.platform_mapping and self.romm_client and self.romm_client.authenticated:
            try:
                self.log_message("📋 Fetching platform names from RomM...")
                platforms = self.romm_client.get_platforms()
                if platforms:
                    self.game_cache.build_platform_mapping_from_api(platforms)
                    self.log_message(f"✅ Loaded {len(platforms)} platform names")
            except Exception as e:
                self.log_message(f"⚠️ Could not fetch platform names: {e}")

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
        
        dialog = Adw.AlertDialog.new("Delete Game?", f"Are you sure you want to delete '{selected_game['name']}'? This will permanently remove the ROM file from your computer.")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)
        dialog.set_default_response("cancel")
        dialog.set_close_response("cancel")
        dialog.connect('response', on_response)
        dialog.present()

    def download_multiple_games(self, games):
        """Download multiple games with concurrency limit"""
        count = len(games)
        games_to_download = list(games)

        # Filter to only games that aren't already downloaded
        not_downloaded = [g for g in games_to_download if not g.get('is_downloaded', False)]

        if not not_downloaded:
            self.log_message("All selected games are already downloaded")
            return

        # Update count to reflect actual games to download
        download_count = len(not_downloaded)

        # SET BULK DOWNLOAD STATE FIRST - before anything that might trigger UI updates
        self._bulk_download_in_progress = True
        self._bulk_download_cancelled = False

        # CAPTURE SELECTION STATE BEFORE BLOCKING
        if hasattr(self, 'library_section'):
            self._downloading_rom_ids = set()
            for game in not_downloaded:
                identifier_type, identifier_value = self.library_section.get_game_identifier(game)
                if identifier_type == 'rom_id':
                    self._downloading_rom_ids.add(identifier_value)

        # BLOCK TREE REFRESHES DURING BULK OPERATION
        self._dialog_open = True
        if hasattr(self, 'library_section'):
            self.library_section._block_selection_updates(True)

        # Update UI to show "Cancel All" button immediately
        if hasattr(self, 'library_section'):
            GLib.idle_add(lambda: self.library_section.update_action_buttons())

        # Track completion
        self._bulk_download_remaining = download_count
        
        # Get max concurrent setting and create semaphore
        max_concurrent = int(self.settings.get('Download', 'max_concurrent', '3'))
        self.log_message(f"🚀 Starting bulk download of {download_count} games (max {max_concurrent} concurrent)...")
        
        import threading
        semaphore = threading.Semaphore(max_concurrent)
        
        def controlled_download(game):
            """Download with proper semaphore control"""
            semaphore.acquire()  # Wait for slot
            try:
                # Call download_game but pass semaphore to control the actual download thread
                self.download_game_controlled(game, semaphore, is_bulk_operation=True)
            except Exception as e:
                self.log_message(f"Download error for {game.get('name')}: {e}")
                semaphore.release()  # Ensure release on error
        
        # Start all downloads (semaphore controls actual concurrency)
        for game in not_downloaded:
            threading.Thread(target=controlled_download, args=(game,), daemon=True).start()

        # Check for completion periodically
        def check_completion():
            if hasattr(self, '_bulk_download_remaining') and self._bulk_download_remaining <= 0:
                self._dialog_open = False
                self._bulk_download_in_progress = False
                if hasattr(self, 'library_section'):
                    self.library_section._block_selection_updates(False)
                    if hasattr(self, '_downloading_rom_ids'):
                        for rom_id in self._downloading_rom_ids:
                            self.library_section.selected_rom_ids.discard(rom_id)
                        self.library_section.sync_selected_checkboxes()
                        self.library_section.update_selection_label()
                        self.library_section.refresh_all_platform_checkboxes()
                        # Update visual checkbox states to match cleared selections
                        GLib.idle_add(self.library_section.force_checkbox_sync)
                        delattr(self, '_downloading_rom_ids')

                    # Update action buttons after bulk operation completes - always call this
                    # to ensure button state is refreshed (e.g., from "Cancel All" back to "Download")
                    self.library_section.update_action_buttons()

                # Check if this was a cancellation
                was_cancelled = self._bulk_download_cancelled
                self._bulk_download_cancelled = False

                if was_cancelled:
                    self.log_message(f"⊗ Bulk download cancelled")
                else:
                    self.log_message(f"✅ Bulk download complete ({download_count} games)")

                    # Send desktop notification when bulk download completes
                    self.send_desktop_notification(
                        "Downloads Complete",
                        f"Successfully downloaded {download_count} game{'s' if download_count != 1 else ''}"
                    )

                delattr(self, '_bulk_download_remaining')
                return False
            return True

        GLib.timeout_add(500, check_completion)

    def download_multiple_games_with_collection_tracking(self, games, collections_data):
        """Download multiple games with per-collection tracking and notifications"""
        count = len(games)
        games_to_download = list(games)

        # Filter to only games that aren't already downloaded
        not_downloaded = [g for g in games_to_download if not g.get('is_downloaded', False)]

        if not not_downloaded:
            self.log_message("All selected games are already downloaded")
            return

        # Update count to reflect actual games to download
        download_count = len(not_downloaded)

        # SET BULK DOWNLOAD STATE FIRST - before anything that might trigger UI updates
        self._bulk_download_in_progress = True
        self._bulk_download_cancelled = False

        # CAPTURE SELECTION STATE BEFORE BLOCKING
        if hasattr(self, 'library_section'):
            self._downloading_rom_ids = set()
            for game in not_downloaded:
                identifier_type, identifier_value = self.library_section.get_game_identifier(game)
                if identifier_type == 'rom_id':
                    self._downloading_rom_ids.add(identifier_value)

        # BLOCK TREE REFRESHES DURING BULK OPERATION
        self._dialog_open = True
        if hasattr(self, 'library_section'):
            self.library_section._block_selection_updates(True)

        # Update UI to show "Cancel All" button immediately
        if hasattr(self, 'library_section'):
            GLib.idle_add(lambda: self.library_section.update_action_buttons())

        # Track completion per collection
        self._bulk_download_remaining = download_count
        self._collection_downloads = {}

        # Initialize per-collection counters
        for collection_name, data in collections_data.items():
            if data['to_download'] > 0:
                self._collection_downloads[collection_name] = {
                    'total': data['total'],
                    'remaining': data['to_download'],
                    'downloaded': data['already_downloaded']
                }

        # Get max concurrent setting and create semaphore
        max_concurrent = int(self.settings.get('Download', 'max_concurrent', '3'))
        self.log_message(f"🚀 Starting bulk download of {download_count} games (max {max_concurrent} concurrent)...")

        import threading
        semaphore = threading.Semaphore(max_concurrent)
        download_lock = threading.Lock()

        def controlled_download(game):
            """Download with proper semaphore control and collection tracking"""
            semaphore.acquire()  # Wait for slot
            try:
                # Call download_game but pass semaphore to control the actual download thread
                self.download_game_controlled(game, semaphore, is_bulk_operation=True,
                                            on_complete=lambda g=game: on_game_complete(g))
            except Exception as e:
                self.log_message(f"Download error for {game.get('name')}: {e}")
                semaphore.release()  # Ensure release on error

        def on_game_complete(game):
            """Called when a game download completes - track per collection"""
            collection_name = game.get('_sync_collection')
            if collection_name and hasattr(self, '_collection_downloads') and collection_name in self._collection_downloads:
                with download_lock:
                    self._collection_downloads[collection_name]['remaining'] -= 1
                    self._collection_downloads[collection_name]['downloaded'] += 1

                    # If this collection is complete, send notification
                    if self._collection_downloads[collection_name]['remaining'] == 0:
                        total = self._collection_downloads[collection_name]['total']
                        downloaded = self._collection_downloads[collection_name]['downloaded']

                        self.send_desktop_notification(
                            f"✅ {collection_name} - Sync Complete",
                            f"{downloaded}/{total} ROMs synced"
                        )
                        self.log_message(f"✅ Collection '{collection_name}' sync complete: {downloaded}/{total} ROMs")

                        # Mark collection as completed (synced) instead of just removing from downloading
                        if hasattr(self, 'library_section'):
                            # Add to a new set tracking completed collections
                            if not hasattr(self.library_section, 'completed_sync_collections'):
                                self.library_section.completed_sync_collections = set()
                            self.library_section.completed_sync_collections.add(collection_name)
                            # Remove from downloading to transition orange -> green
                            self.library_section.currently_downloading_collections.discard(collection_name)
                            # Update status immediately
                            self.library_section.update_collection_sync_status(collection_name)

        # Start all downloads (semaphore controls actual concurrency)
        for game in not_downloaded:
            threading.Thread(target=controlled_download, args=(game,), daemon=True).start()

        # Check for completion periodically
        def check_completion():
            if hasattr(self, '_bulk_download_remaining') and self._bulk_download_remaining <= 0:
                self._dialog_open = False
                self._bulk_download_in_progress = False
                if hasattr(self, 'library_section'):
                    self.library_section._block_selection_updates(False)
                    if hasattr(self, '_downloading_rom_ids'):
                        for rom_id in self._downloading_rom_ids:
                            self.library_section.selected_rom_ids.discard(rom_id)
                        self.library_section.sync_selected_checkboxes()
                        self.library_section.update_selection_label()
                        self.library_section.refresh_all_platform_checkboxes()
                        # Update visual checkbox states to match cleared selections
                        GLib.idle_add(self.library_section.force_checkbox_sync)
                        delattr(self, '_downloading_rom_ids')

                    # Update action buttons after bulk operation completes - always call this
                    # to ensure button state is refreshed (e.g., from "Cancel All" back to "Download")
                    self.library_section.update_action_buttons()

                # Check if this was a cancellation
                was_cancelled = self._bulk_download_cancelled
                self._bulk_download_cancelled = False

                if was_cancelled:
                    self.log_message(f"⊗ Bulk download cancelled")
                else:
                    self.log_message(f"✅ All downloads complete ({download_count} games)")

                # Clean up collection tracking
                if hasattr(self, '_collection_downloads'):
                    delattr(self, '_collection_downloads')

                delattr(self, '_bulk_download_remaining')
                return False
            return True

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

        dialog = Adw.AlertDialog.new(f"Delete {count} Games?", f"Are you sure you want to delete {count} selected games?")
        dialog.add_response("cancel", "Cancel")
        dialog.add_response("delete", "Delete Selected")
        dialog.set_response_appearance("delete", Adw.ResponseAppearance.DESTRUCTIVE)

        def on_response(dialog, response):
            self._dialog_open = False
            if hasattr(self, 'library_section'):
                self.library_section._block_selection_updates(False)

                if response == "delete":
                    # Identify collections that contain the games being deleted
                    affected_collections = set()
                    for game in games_to_delete:
                        collection = game.get('collection')
                        if collection:
                            affected_collections.add(collection)

                    # Delete the games
                    for game in games_to_delete:
                        self.delete_game_file(game, is_bulk_operation=True)

                    # Disable autosync for affected collections that were actively syncing
                    if hasattr(self, 'library_section') and affected_collections:
                        collections_to_disable = affected_collections.intersection(
                            self.library_section.actively_syncing_collections
                        )

                        if collections_to_disable:
                            self.library_section.disable_autosync_for_collections(collections_to_disable)
                            collection_list = ", ".join(f"'{c}'" for c in collections_to_disable)
                            self.log_message(f"🔄 Auto-sync disabled for {len(collections_to_disable)} collection(s): {collection_list}")

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

                # For multi-disc or multi-file games, ensure we're deleting the folder
                # local_path can point to either a file (when scanned from filesystem) or folder (when freshly downloaded)
                if game.get('is_multi_disc'):
                    if game_path.is_file():
                        # If it points to a file, delete the parent folder
                        game_path = game_path.parent
                elif not game_path.exists() and game_path.suffix:
                    # If path doesn't exist and looks like a file (has extension), try the parent folder
                    # This handles multi-file games where local_path might point to a non-existent file
                    parent_folder = game_path.parent
                    if parent_folder.exists() and parent_folder.is_dir():
                        game_path = parent_folder

                GLib.idle_add(lambda n=game_name:
                            self.log_message(f"Deleting {n}..."))

                # After deletion, replace complex update with:
                if game_path.exists():
                    # Handle both files and directories (for multi-disc/multi-file games)
                    if game_path.is_dir():
                        import shutil
                        shutil.rmtree(game_path)
                    else:
                        game_path.unlink()

                    game['is_downloaded'] = False
                    game['local_path'] = None
                    game['local_size'] = 0

                    # Update all discs in multi-disc games to reflect deletion
                    if game.get('is_multi_disc') and game.get('discs'):
                        for disc in game['discs']:
                            disc['is_downloaded'] = False
                            disc['size'] = 0
                    
                    def refresh_ui():
                        for i, g in enumerate(self.available_games):
                            if g.get('rom_id') == game.get('rom_id'):
                                self.available_games[i] = game
                                break

                    GLib.idle_add(refresh_ui)
                    
                    # Update based on current view mode
                    def update_after_deletion():
                        if hasattr(self, 'library_section'):
                            current_mode = getattr(self.library_section, 'current_view_mode', 'platform')
                            
                            # For offline mode (not connected to RomM), we need a full refresh to remove items
                            if not (self.romm_client and self.romm_client.authenticated):
                                # If not connected to RomM, remove the game entirely from the list
                                if hasattr(self, 'available_games') and game in self.available_games:
                                    self.available_games.remove(game)

                                # Refresh the entire library to remove the item
                                if hasattr(self, 'library_section'):
                                    self.library_section.update_games_library(self.available_games)
                            else:
                                # Connected to RomM - just update the single item (works for both platform and collection view)
                                if hasattr(self, 'library_section'):
                                    self.library_section.update_single_game(game, skip_platform_update=is_bulk_operation)
                        
                        return False
                    
                    GLib.idle_add(update_after_deletion)

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

    def download_disc(self, game, disc):
        """Download a single disc from a multi-disc game

        DISABLED: Due to RomM API limitations, individual disc downloads don't work.
        The API always returns all discs regardless of which one is requested.
        Users should download the entire game instead to get all discs properly.
        """
        self.log_message(
            f"⚠️ Individual disc downloads are not supported. "
            f"Please download the entire game to get all discs."
        )
        return

        def download():
            try:
                rom_id = game['rom_id']
                rom_name = game['name']
                disc_name = disc['name']
                platform_slug = game.get('platform_slug', game.get('platform', 'Unknown'))

                # Initialize progress tracking for this disc download
                # Use a unique key combining rom_id and disc_name
                disc_progress_key = f"{rom_id}:{disc_name}"
                self.download_progress[disc_progress_key] = {
                    'progress': 0.0,
                    'downloading': True,
                    'filename': disc_name,
                    'speed': 0,
                    'downloaded': 0,
                    'total': 0
                }
                self._last_progress_update[disc_progress_key] = 0

                # Store reference to which disc is being downloaded for this game
                if not hasattr(self, '_disc_downloads'):
                    self._disc_downloads = {}
                self._disc_downloads[rom_id] = disc_name

                # Update UI to show download starting
                GLib.idle_add(lambda: self.library_section.update_disc_progress(rom_id, disc_name, self.download_progress[disc_progress_key])
                            if hasattr(self, 'library_section') else None)

                # Get download directory
                download_dir = Path(self.rom_dir_row.get_text())
                platform_dir = download_dir / platform_slug
                game_folder = platform_dir / rom_name
                game_folder.mkdir(parents=True, exist_ok=True)

                disc_path = game_folder / disc_name

                GLib.idle_add(lambda: self.log_message(f"Downloading {disc_name} from {rom_name}..."))

                # Download the disc file from RomM
                # For multi-disc games, try to use the file's full_path from API if available
                from urllib.parse import quote

                file_id = disc.get('file_id')
                full_path = disc.get('full_path')

                # Try different API endpoints in order of preference:
                # 1. If we have file_id, try /api/assets/{file_id}
                # 2. If we have full_path, try /assets/{full_path}
                # 3. Fall back to /api/roms/{rom_id}/content/{filename}

                if file_id:
                    url = f"{self.romm_client.base_url}/api/assets/{file_id}"
                elif full_path:
                    url = f"{self.romm_client.base_url}/assets/{full_path}"
                else:
                    encoded_disc_name = quote(disc_name)
                    url = f"{self.romm_client.base_url}/api/roms/{rom_id}/content/{encoded_disc_name}"

                response = self.romm_client.session.get(url, stream=True)

                # If first attempt failed, try fallback endpoints
                if response.status_code != 200 and file_id:
                    if full_path:
                        url = f"{self.romm_client.base_url}/assets/{full_path}"
                        response = self.romm_client.session.get(url, stream=True)

                if response.status_code != 200 and (file_id or full_path):
                    encoded_disc_name = quote(disc_name)
                    url = f"{self.romm_client.base_url}/api/roms/{rom_id}/content/{encoded_disc_name}"
                    response = self.romm_client.session.get(url, stream=True)

                if response.status_code == 200:
                    # Get expected size from disc metadata (more accurate for multi-disc games)
                    # The content-length header may report the entire game folder size
                    expected_size = disc.get('size', 0)
                    header_size = int(response.headers.get('content-length', 0))

                    # Use expected size from disc metadata if available, otherwise fall back to header
                    total_size = expected_size if expected_size > 0 else header_size

                    downloaded = 0
                    start_time = time.time()

                    with open(disc_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                                downloaded += len(chunk)

                                # Update progress
                                if total_size > 0:
                                    progress = downloaded / total_size
                                    elapsed = time.time() - start_time
                                    speed = downloaded / elapsed if elapsed > 0 else 0

                                    self.download_progress[disc_progress_key].update({
                                        'progress': progress,
                                        'downloaded': downloaded,
                                        'total': total_size,
                                        'speed': speed
                                    })

                                    # Throttle UI updates (every 100ms)
                                    current_time = time.time()
                                    if current_time - self._last_progress_update.get(disc_progress_key, 0) > 0.1:
                                        self._last_progress_update[disc_progress_key] = current_time
                                        GLib.idle_add(lambda: self.library_section.update_disc_progress(rom_id, disc_name, self.download_progress[disc_progress_key])
                                                    if hasattr(self, 'library_section') else None)

                    file_size = disc_path.stat().st_size

                    # Mark download complete in progress tracker
                    self.download_progress[disc_progress_key] = {
                        'progress': 1.0,
                        'downloading': False,
                        'completed': True,
                        'filename': disc_name,
                        'downloaded': file_size,
                        'total': file_size
                    }

                    # Final progress update
                    GLib.idle_add(lambda: self.library_section.update_disc_progress(rom_id, disc_name, self.download_progress[disc_progress_key])
                                if hasattr(self, 'library_section') else None)

                    # Find and update the actual game in available_games
                    updated_game = None
                    for i, existing_game in enumerate(self.available_games):
                        if existing_game.get('rom_id') == rom_id:
                            # Update the disc status in the actual game object
                            for disc_obj in existing_game.get('discs', []):
                                if disc_obj.get('name') == disc_name:
                                    disc_obj['is_downloaded'] = True
                                    # Calculate total size including all related files (e.g., .bin + .cue)
                                    disc_obj['size'] = self.get_disc_total_size(disc_path, game_folder)
                                    disc_obj['path'] = str(disc_path)
                                    break

                            # Update game status
                            existing_game['local_path'] = str(game_folder)
                            existing_game['is_downloaded'] = all(d.get('is_downloaded', False) for d in existing_game.get('discs', []))

                            updated_game = existing_game
                            break

                    # Save cache to persist download status across app restarts
                    if hasattr(self, 'game_cache'):
                        threading.Thread(target=lambda: self.game_cache.save_games_data(self.available_games), daemon=True).start()

                    GLib.idle_add(lambda: self.log_message(f"✓ Downloaded {disc_name}"))

                    # Update UI with the updated game data
                    def update_ui():
                        if updated_game and hasattr(self, 'library_section'):
                            # Create a copy to ensure GObject sees it as a new value
                            game_copy = updated_game.copy()
                            game_copy['discs'] = [d.copy() for d in updated_game.get('discs', [])]
                            self.library_section.update_single_game(game_copy)
                        return False

                    GLib.idle_add(update_ui)

                    # Clean up progress after a short delay
                    time.sleep(2)
                    if disc_progress_key in self.download_progress:
                        del self.download_progress[disc_progress_key]
                    if hasattr(self, '_disc_downloads') and rom_id in self._disc_downloads:
                        del self._disc_downloads[rom_id]
                else:
                    GLib.idle_add(lambda: self.log_message(f"✗ Failed to download {disc_name} (status {response.status_code})"))

            except Exception as e:
                import traceback
                traceback.print_exc()
                GLib.idle_add(lambda: self.log_message(f"Error downloading {disc_name}: {e}"))

        threading.Thread(target=download, daemon=True).start()

    def delete_disc(self, game, disc):
        """Delete a single disc from a multi-disc game"""
        def delete():
            try:
                disc_name = disc['name']
                platform_slug = game.get('platform_slug', game.get('platform', 'Unknown'))

                # Get game folder path
                download_dir = Path(self.rom_dir_row.get_text())
                platform_dir = download_dir / platform_slug
                game_folder = platform_dir / game['name']
                disc_path = game_folder / disc_name

                if disc_path.exists():
                    disc_path.unlink()
                    disc['is_downloaded'] = False
                    disc['size'] = 0

                    # Update game status - game is not fully downloaded if any disc is missing
                    game['is_downloaded'] = all(d.get('is_downloaded', False) for d in game.get('discs', []))

                    # Update available_games list to persist the changes
                    rom_id = game.get('rom_id')
                    for i, existing_game in enumerate(self.available_games):
                        if existing_game.get('rom_id') == rom_id:
                            self.available_games[i] = game
                            break

                    # Save cache to persist deletion status across app restarts
                    if hasattr(self, 'game_cache'):
                        threading.Thread(target=lambda: self.game_cache.save_games_data(self.available_games), daemon=True).start()

                    GLib.idle_add(lambda: self.log_message(f"✓ Deleted {disc_name}"))

                    # Update UI with a copy of the game data to trigger property notifications
                    def update_ui():
                        if hasattr(self, 'library_section'):
                            # Create a copy to ensure GObject sees it as a new value
                            game_copy = game.copy()
                            game_copy['discs'] = [d.copy() for d in game.get('discs', [])]
                            self.library_section.update_single_game(game_copy)
                        return False

                    GLib.idle_add(update_ui)
                else:
                    GLib.idle_add(lambda: self.log_message(f"Disc not found: {disc_name}"))

            except Exception as e:
                GLib.idle_add(lambda: self.log_message(f"Error deleting {disc_name}: {e}"))

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
        """Launch a game using RetroArch (with BIOS verification)"""
        if not game.get('is_downloaded'):
            self.log_message("Game is not downloaded")
            return

        # Auto-download missing BIOS if manager is available
        if self.retroarch.bios_manager:
            platform = game.get('platform')
            if platform:
                normalized = self.retroarch.bios_manager.normalize_platform_name(platform)
                present, missing = self.retroarch.bios_manager.check_platform_bios(normalized)
                required_missing = [b for b in missing if not b.get('optional', False)]

                if required_missing:
                    # Set RomM client and download silently
                    self.retroarch.bios_manager.romm_client = self.romm_client
                    self.retroarch.bios_manager.auto_download_missing_bios(normalized)

        # Actually launch the game
        local_path = game.get('local_path')
        if not local_path or not Path(local_path).exists():
            self.log_message("Game file not found")
            return

        platform_name = game.get('platform')
        rom_path = Path(local_path)

        print(f"🎮 Launching game: {game.get('name')}")
        print(f"📁 ROM path: {rom_path}")
        print(f"🎯 Platform: {platform_name}")

        # Download saves before launching
        rom_id = game.get('rom_id')
        if rom_id:
            try:
                self.download_saves_for_game(game)
            except Exception as e:
                print(f"⚠️ Save download error: {e}")

        # Let RetroArch interface handle the actual launching
        success, message = self.retroarch.launch_game(rom_path, platform_name)

        if success:
            self.log_message(f"🚀 {message}")
            # Send notification to RetroArch if possible
            self.retroarch.send_notification(f"Launching {game.get('name', 'Unknown')}")
        else:
            self.log_message(f"❌ Launch failed: {message}")
            # Show user-friendly dialog for missing core
            if "No suitable core found" in message:
                self._show_missing_core_dialog(game.get('name', 'Unknown'), platform_name)
            elif "Core not found" in message:
                self._show_missing_core_dialog(game.get('name', 'Unknown'), platform_name)

    def launch_disc(self, game, disc):
        """Launch a specific disc from a multi-disc game using RetroArch"""
        if not disc.get('is_downloaded', False):
            self.log_message("Disc is not downloaded")
            return

        # Auto-download missing BIOS if manager is available (same as launch_game)
        if self.retroarch.bios_manager:
            platform = game.get('platform')
            if platform:
                normalized = self.retroarch.bios_manager.normalize_platform_name(platform)
                present, missing = self.retroarch.bios_manager.check_platform_bios(normalized)
                required_missing = [b for b in missing if not b.get('optional', False)]

                if required_missing:
                    # Set RomM client and download silently
                    self.retroarch.bios_manager.romm_client = self.romm_client
                    self.retroarch.bios_manager.auto_download_missing_bios(normalized)

        # Build path to the specific disc file
        game_local_path = game.get('local_path')
        if not game_local_path:
            self.log_message("Game path not found")
            return

        # For multi-disc games, local_path points to the folder containing all discs
        game_folder = Path(game_local_path)
        disc_name = disc.get('name')
        disc_path = game_folder / disc_name

        if not disc_path.exists():
            self.log_message(f"Disc file not found: {disc_name}")
            return

        platform_name = game.get('platform')

        # Let RetroArch interface handle the actual launching
        success, message = self.retroarch.launch_game(disc_path, platform_name)

        if success:
            self.log_message(f"🚀 {message}")
            # Send notification to RetroArch if possible
            game_name = game.get('name', 'Unknown')
            self.retroarch.send_notification(f"Launching {game_name} - {disc_name}")
        else:
            self.log_message(f"❌ Launch failed: {message}")
            # Show user-friendly dialog for missing core
            if "No suitable core found" in message:
                self._show_missing_core_dialog(f"{game.get('name', 'Unknown')} - {disc_name}", platform_name)
            elif "Core not found" in message:
                self._show_missing_core_dialog(f"{game.get('name', 'Unknown')} - {disc_name}", platform_name)

    def _show_missing_core_dialog(self, game_name, platform_name):
        """Show a dialog informing the user that no RetroArch core is installed for the platform"""
        platform_display = platform_name if platform_name else "this platform"

        dialog = Adw.AlertDialog.new(
            "RetroArch Core Not Found",
            f"Cannot launch '{game_name}' because no RetroArch core is installed for {platform_display}.\n\n"
            f"Please install a compatible RetroArch core for {platform_display} and try again."
        )
        dialog.add_response("ok", "OK")
        dialog.set_default_response("ok")
        dialog.set_close_response("ok")
        dialog.present(self)

    def on_window_close_request(self, _window):
        """Overrides the default window close action.
        
        Instead of quitting, this will just hide the window to the tray.
        The actual quit logic is now handled by the StatusNotifierItem class.
        """
        self.set_visible(False)
        # Return True to prevent the window from being destroyed
        return True

    def cancel_download(self, rom_id):
        """Cancel an in-progress download. If part of a bulk operation, cancels all downloads.

        Args:
            rom_id: The ROM ID to cancel
        """
        with self._cancellation_lock:
            # Check if this is part of a bulk download
            if self._bulk_download_in_progress:
                # Cancel ALL downloads in the bulk operation
                self._bulk_download_cancelled = True

                # Mark all queued/downloading games for cancellation
                # Use _downloading_rom_ids which captures ALL games in the bulk operation
                if hasattr(self, '_downloading_rom_ids'):
                    for downloading_rom_id in self._downloading_rom_ids:
                        self._cancelled_downloads.add(downloading_rom_id)

                # Also mark currently active threads
                download_threads_keys = list(self._download_threads.keys())
                for downloading_rom_id in download_threads_keys:
                    self._cancelled_downloads.add(downloading_rom_id)

                self.log_message(f"Cancelling bulk download operation...")
                return True
            elif rom_id in self._download_threads:
                # Single download cancellation
                self._cancelled_downloads.add(rom_id)
                self.log_message(f"Cancelling download...")
                return True
            return False

    def download_game(self, game, is_bulk_operation=False):
        """Download a single game from RomM and its saves (with BIOS check)"""

        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("Please connect to RomM first")
            return

        # Check if bulk download has been cancelled
        if is_bulk_operation and self._bulk_download_cancelled:
            return  # Don't start new downloads if bulk operation is cancelled
        
        # Check BIOS requirements first if enabled
        auto_download_setting = self.settings.get('Download', 'auto_download_bios', fallback='true')
        has_bios_manager = bool(self.retroarch.bios_manager)

        self.log_message(f"🔍 BIOS auto-download setting: {auto_download_setting}")
        self.log_message(f"🔍 BIOS manager available: {has_bios_manager}")

        # Default to enabled if not set or empty
        auto_download_enabled = auto_download_setting in ['true', '', None]
        if (auto_download_enabled and has_bios_manager):
            platform = game.get('platform')
            if platform:
                self.log_message(f"🔍 Checking BIOS for platform: {platform}")
                normalized = self.retroarch.bios_manager.normalize_platform_name(platform)
                self.log_message(f"🔍 Normalized platform: {normalized}")
                
                present, missing = self.retroarch.bios_manager.check_platform_bios(normalized)
                required_missing = [b for b in missing if not b.get('optional', False)]
                
                self.log_message(f"🔍 Required missing BIOS: {len(required_missing)}")
                
                if required_missing:
                    self.log_message(f"📋 Auto-downloading BIOS for {platform}...")
                    
                    # Set RomM client
                    self.retroarch.bios_manager.romm_client = self.romm_client
                    
                    # Download all missing BIOS for this platform
                    if self.retroarch.bios_manager.auto_download_missing_bios(normalized):
                        self.log_message(f"✅ BIOS ready for {platform}")
                    else:
                        self.log_message(f"⚠️ Some BIOS files unavailable for {platform}")
                else:
                    self.log_message(f"✅ All required BIOS already present for {platform}")
        else:
            self.log_message(f"⚠️ BIOS auto-download disabled or manager unavailable")
        
        def download():
            try:
                rom_name = game['name']
                rom_id = game['rom_id']
                platform = game['platform']
                platform_slug = game.get('platform_slug', platform)
                file_name = game['file_name']

                # Track current download for progress updates
                self._current_download_rom_id = rom_id

                # Track this download thread
                current_thread = threading.current_thread()
                with self._cancellation_lock:
                    self._download_threads[rom_id] = current_thread
                    # Ensure this download is not marked as cancelled
                    self._cancelled_downloads.discard(rom_id)

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

                # Update action buttons to show "Cancel" button
                GLib.idle_add(lambda: self.library_section.update_action_buttons()
                            if hasattr(self, 'library_section') else None)
                
                # Get download directory and create platform directory
                download_dir = Path(self.rom_dir_row.get_text())
                # Use platform slug directly (RomM and RetroDECK now use the same slugs)
                platform_dir = download_dir / platform_slug
                platform_dir.mkdir(parents=True, exist_ok=True)
                download_path = platform_dir / file_name

                # Log file size for large downloads
                try:
                    # Try to get file size from ROM data
                    romm_data = game.get('romm_data', {})
                    expected_size = romm_data.get('fs_size_bytes', 0)
                except:
                    pass
                
                # Download with throttled progress tracking and cancellation support
                def is_cancelled():
                    with self._cancellation_lock:
                        return rom_id in self._cancelled_downloads

                success, message = self.romm_client.download_rom(
                    rom_id, rom_name, download_path,
                    progress_callback=lambda progress: self.update_download_progress(progress, rom_id),
                    cancellation_checker=is_cancelled
                )

                if success:
                    # Mark download complete
                    current_progress = self.download_progress.get(rom_id, {})
                    if current_progress.get('downloaded', 0) > 0:
                        # Keep the original download size from the download process
                        file_size = current_progress['downloaded']
                    else:
                        # Fallback for single files
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

                    # Update action buttons back to "Download" or "Launch"
                    # Don't update if bulk download is in progress - keep "Cancel All" button
                    if not is_bulk_operation:
                        GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                    if hasattr(self, 'library_section') else None)
                    
                    # Rest of success handling...
                    # Always update game status after successful download
                    if True:  # Changed from download_path.exists() to handle multi-disc folders
                        size_str = f"{file_size / (1024*1024*1024):.1f} GB" if file_size > 1024*1024*1024 else f"{file_size / (1024*1024):.1f} MB" if file_size > 1024*1024 else f"{file_size / 1024:.1f} KB"
                        
                        GLib.idle_add(lambda n=rom_name, s=size_str:
                                    self.log_message(f"✓ Downloaded {n} ({s})"))

                        # Update game data
                        game['is_downloaded'] = True

                        # Check if download created a folder (multi-file game)
                        actual_folder = platform_dir / rom_name

                        # Update disc status for multi-disc games
                        if game.get('is_multi_disc') and game.get('discs'):
                            # For multi-disc, RomM client creates folder with game name
                            game['local_path'] = str(actual_folder)
                            for disc in game['discs']:
                                disc['is_downloaded'] = True
                                disc_path = actual_folder / disc.get('name', '')
                                if disc_path.exists():
                                    # Calculate total size including all related files (e.g., .bin + .cue)
                                    disc['size'] = self.get_disc_total_size(disc_path, actual_folder)
                        elif actual_folder.exists() and actual_folder.is_dir():
                            # Multi-file game (folder exists with game name)
                            game['local_path'] = str(actual_folder)
                        elif download_path.exists():
                            # Single file download
                            game['local_path'] = str(download_path)
                        else:
                            # Fallback - use folder path if download_path doesn't exist
                            game['local_path'] = str(actual_folder)

                        game['local_size'] = file_size

                        # Update UI - update both the underlying games list AND current view
                        def update_ui():
                            # ALWAYS update the underlying available_games list first
                            for i, existing_game in enumerate(self.available_games):
                                if existing_game.get('rom_id') == game.get('rom_id'):
                                    self.available_games[i] = game
                                    break

                            # Update platform item directly
                            if hasattr(self, 'library_section'):
                                for j in range(self.library_section.library_model.root_store.get_n_items()):
                                    platform_item = self.library_section.library_model.root_store.get_item(j)
                                    if isinstance(platform_item, PlatformItem):
                                        for k, platform_game in enumerate(platform_item.games):
                                            if platform_game.get('rom_id') == game.get('rom_id'):
                                                platform_item.games[k] = game
                                                platform_item.notify('status-text')
                                                platform_item.notify('size-text')
                                                break
                            
                            # Update collections view data if in collections mode
                            if (hasattr(self.library_section, 'current_view_mode') and 
                                self.library_section.current_view_mode == 'collection'):
                                
                                # Update ALL instances of this game in collections_games list
                                if hasattr(self.library_section, 'collections_games'):
                                    updated_collections = set()  # Track which collections were updated
                                    
                                    for i, collection_game in enumerate(self.library_section.collections_games):
                                        if collection_game.get('rom_id') == game.get('rom_id'):
                                            updated_collection_game = game.copy()
                                            updated_collection_game['collection'] = collection_game.get('collection')
                                            self.library_section.collections_games[i] = updated_collection_game
                                            updated_collections.add(collection_game.get('collection'))
                                    
                                    # ADD THIS: Force property updates on affected collection platform items
                                    def force_collection_updates():
                                        model = self.library_section.library_model.tree_model
                                        for i in range(model.get_n_items() if model else 0):
                                            tree_item = model.get_item(i)
                                            if tree_item and tree_item.get_depth() == 0:  # Collection level
                                                platform_item = tree_item.get_item()
                                                if isinstance(platform_item, PlatformItem):
                                                    if platform_item.platform_name in updated_collections:
                                                        # Force property notifications to update Status/Size
                                                        platform_item.notify('status-text')
                                                        platform_item.notify('size-text')
                                        return False
                                    
                                    GLib.timeout_add(150, force_collection_updates)                          

                            # Call update_single_game as fallback
                            self.library_section.update_single_game(game, skip_platform_update=is_bulk_operation)

                        GLib.idle_add(update_ui)

                        # Update the GameItem
                        def update_game_item():
                            model = self.library_section.library_model.tree_model

                            for i in range(model.get_n_items() if model else 0):
                                tree_item = model.get_item(i)
                                if tree_item and tree_item.get_depth() == 1:  # Game level
                                    item = tree_item.get_item()
                                    if isinstance(item, GameItem):
                                        if item.game_data.get('rom_id') == rom_id:
                                            # Update data
                                            item.game_data.update(game)

                                            # Test progress tracking
                                            test_progress = {'progress': 0.5, 'downloading': True, 'filename': rom_name}
                                            self.library_section.update_game_progress(rom_id, test_progress)

                                            break

                            return False

                        GLib.idle_add(update_game_item)

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
                        
                        if file_size >= 1024:
                            GLib.idle_add(lambda n=rom_name: self.log_message(f"✓ {n} ready to play"))
                
                else:
                    # Check if this was a cancellation
                    was_cancelled = (message == "cancelled")

                    if was_cancelled:
                        # Mark download as cancelled
                        self.download_progress[rom_id] = {
                            'progress': 0.0,
                            'downloading': False,
                            'cancelled': True,
                            'filename': rom_name
                        }

                        # Clean up partial download file
                        if download_path.exists():
                            try:
                                if download_path.is_file():
                                    download_path.unlink()
                                elif download_path.is_dir():
                                    shutil.rmtree(download_path)
                            except Exception as e:
                                print(f"Failed to clean up partial download: {e}")

                        GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                                    if hasattr(self, 'library_section') else None)

                        # Update action buttons back to "Download"
                        # Don't update if bulk download is in progress - keep "Cancel All" button
                        if not is_bulk_operation:
                            GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                        if hasattr(self, 'library_section') else None)

                        # Decrement bulk download counter for cancelled downloads too
                        if is_bulk_operation and hasattr(self, '_bulk_download_remaining'):
                            self._bulk_download_remaining -= 1

                        GLib.idle_add(lambda n=rom_name:
                                    self.log_message(f"⊗ Cancelled download: {n}"))
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

                        # Update action buttons back to "Download"
                        # Don't update if bulk download is in progress - keep "Cancel All" button
                        if not is_bulk_operation:
                            GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                        if hasattr(self, 'library_section') else None)

                        GLib.idle_add(lambda n=rom_name, m=message:
                                    self.log_message(f"✗ Failed to download {n}: {m}"))
                
                # Clean up progress and throttling data
                def cleanup_progress():
                    time.sleep(3)  # Show completed/failed state for 3 seconds

                    # More thorough cleanup
                    if rom_id in self.download_progress:
                        del self.download_progress[rom_id]
                    if rom_id in self._last_progress_update:
                        del self._last_progress_update[rom_id]

                    # Clean up download thread tracking
                    with self._cancellation_lock:
                        if rom_id in self._download_threads:
                            del self._download_threads[rom_id]
                        self._cancelled_downloads.discard(rom_id)

                    # Clean up current download tracking
                    if hasattr(self, '_current_download_rom_id') and self._current_download_rom_id == rom_id:
                        delattr(self, '_current_download_rom_id')
                    
                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, None)
                                if hasattr(self, 'library_section') else None)
                    
                    # Force garbage collection for large downloads
                    import gc
                    gc.collect()

                threading.Thread(target=cleanup_progress, daemon=True).start()

            except Exception as e:
                import traceback
                traceback.print_exc()
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

    def download_game_controlled(self, game, semaphore, is_bulk_operation=False, on_complete=None):
        """Download with semaphore already acquired - releases when complete"""
        def download():
            success = False  # Track success for on_complete callback
            try:
                # Check if bulk download has been cancelled before starting
                if is_bulk_operation and self._bulk_download_cancelled:
                    semaphore.release()
                    if on_complete:
                        on_complete(False)
                    return  # Don't start if bulk operation is cancelled

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

                # Get download directory and create platform directory
                download_dir = Path(self.rom_dir_row.get_text())
                # Use platform slug directly (RomM and RetroDECK now use the same slugs)
                platform_dir = download_dir / platform_slug
                platform_dir.mkdir(parents=True, exist_ok=True)
                download_path = platform_dir / file_name

                # Log file size for large downloads
                try:
                    # Try to get file size from ROM data
                    romm_data = game.get('romm_data', {})
                    expected_size = romm_data.get('fs_size_bytes', 0)
                except:
                    pass

                # Download with throttled progress tracking and cancellation support
                def is_cancelled():
                    with self._cancellation_lock:
                        return rom_id in self._cancelled_downloads

                download_success, message = self.romm_client.download_rom(
                    rom_id, rom_name, download_path,
                    progress_callback=lambda progress: self.update_download_progress(progress, rom_id),
                    cancellation_checker=is_cancelled
                )

                if download_success:
                    success = True
                    # Mark download complete
                    current_progress = self.download_progress.get(rom_id, {})
                    if current_progress.get('downloaded', 0) > 0:
                        # Keep the original download size from the download process
                        file_size = current_progress['downloaded']
                    else:
                        # Fallback for single files
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

                    # Update action buttons back to "Download" or "Launch"
                    # Don't update if bulk download is in progress - keep "Cancel All" button
                    if not is_bulk_operation:
                        GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                    if hasattr(self, 'library_section') else None)
                    
                    # Rest of success handling...
                    # Always update game status after successful download
                    if True:  # Changed from download_path.exists() to handle multi-disc folders
                        size_str = f"{file_size / (1024*1024*1024):.1f} GB" if file_size > 1024*1024*1024 else f"{file_size / (1024*1024):.1f} MB" if file_size > 1024*1024 else f"{file_size / 1024:.1f} KB"
                        
                        GLib.idle_add(lambda n=rom_name, s=size_str:
                                    self.log_message(f"✓ Downloaded {n} ({s})"))

                        # Update game data
                        game['is_downloaded'] = True

                        # Update disc status for multi-disc games
                        if game.get('is_multi_disc') and game.get('discs'):
                            # For multi-disc, RomM client creates folder with game name
                            actual_folder = platform_dir / rom_name
                            game['local_path'] = str(actual_folder)
                            for disc in game['discs']:
                                disc['is_downloaded'] = True
                                disc_path = actual_folder / disc.get('name', '')
                                if disc_path.exists():
                                    # Calculate total size including all related files (e.g., .bin + .cue)
                                    disc['size'] = self.get_disc_total_size(disc_path, actual_folder)
                        else:
                            game['local_path'] = str(download_path)

                        game['local_size'] = file_size

                        # Update UI
                        def update_ui():
                            # Update master games list
                            for i, existing_game in enumerate(self.available_games):
                                if existing_game.get('rom_id') == game.get('rom_id'):
                                    self.available_games[i] = game
                                    break

                            # Update platform item directly
                            if hasattr(self, 'library_section'):
                                for j in range(self.library_section.library_model.root_store.get_n_items()):
                                    platform_item = self.library_section.library_model.root_store.get_item(j)
                                    if isinstance(platform_item, PlatformItem):
                                        for k, platform_game in enumerate(platform_item.games):
                                            if platform_game.get('rom_id') == game.get('rom_id'):
                                                platform_item.games[k] = game
                                                platform_item.notify('status-text')
                                                platform_item.notify('size-text')
                                                break
                            
                            # Update collections cache AND platform item data
                            if (hasattr(self.library_section, 'current_view_mode') and
                                self.library_section.current_view_mode == 'collection'):

                                # Update collections_games cache
                                updated_any = False
                                if hasattr(self.library_section, 'collections_games'):
                                    for i, collection_game in enumerate(self.library_section.collections_games):
                                        if collection_game.get('rom_id') == game.get('rom_id'):
                                            updated_game = game.copy()
                                            updated_game['collection'] = collection_game.get('collection')
                                            self.library_section.collections_games[i] = updated_game
                                            updated_any = True

                                # Update the actual PlatformItem.games data in the tree model
                                def update_platform_items():
                                    model = self.library_section.library_model.tree_model
                                    if model:
                                        for i in range(model.get_n_items()):
                                            tree_item = model.get_item(i)
                                            if tree_item and tree_item.get_depth() == 0:  # Collection level
                                                platform_item = tree_item.get_item()
                                                if isinstance(platform_item, PlatformItem):
                                                    # Update games in this platform item
                                                    for j, platform_game in enumerate(platform_item.games):
                                                        if platform_game.get('rom_id') == game.get('rom_id'):
                                                            platform_item.games[j] = game.copy()
                                                            platform_item.games[j]['collection'] = platform_item.platform_name

                                                    # Force property recalculation
                                                    platform_item.notify('status-text')
                                                    platform_item.notify('size-text')
                                    return False

                                GLib.timeout_add(200, update_platform_items)

                                # If we updated any collection games, reload the entire collections view
                                if updated_any:
                                    def reload_collections():
                                        self.library_section.library_model.update_library(
                                            self.library_section.collections_games, 
                                            group_by='collection'
                                        )
                                        return False
                                    
                                    GLib.timeout_add(100, reload_collections)
                            
                            # Update the GameItem directly
                            model = self.library_section.library_model.tree_model
                            for i in range(model.get_n_items() if model else 0):
                                tree_item = model.get_item(i)
                                if tree_item and tree_item.get_depth() == 1:
                                    item = tree_item.get_item()
                                    if isinstance(item, GameItem) and item.game_data.get('rom_id') == game.get('rom_id'):
                                        item.game_data.update(game)

                            # Clear selections after download completes
                            # Don't clear selections during bulk downloads - wait until all complete
                            if not is_bulk_operation and hasattr(self, 'library_section'):
                                def clear_selections():
                                    self.library_section.selected_checkboxes.clear()
                                    self.library_section.selected_rom_ids.clear()
                                    self.library_section.selected_game_keys.clear()
                                    self.library_section.selected_game = None
                                    self.library_section.update_action_buttons()
                                    self.library_section.update_selection_label()
                                    self.library_section.force_checkbox_sync()

                                # Clear selections after a short delay
                                GLib.timeout_add(1000, lambda: (clear_selections(), False)[1])

                        GLib.idle_add(update_ui)

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
                        
                        if file_size >= 1024:
                            GLib.idle_add(lambda n=rom_name: self.log_message(f"✓ {n} ready to play"))
                
                else:
                    # Check if this was a cancellation
                    was_cancelled = (message == "cancelled")

                    if was_cancelled:
                        # Mark download as cancelled
                        self.download_progress[rom_id] = {
                            'progress': 0.0,
                            'downloading': False,
                            'cancelled': True,
                            'filename': rom_name
                        }

                        # Clean up partial download file
                        if download_path.exists():
                            try:
                                if download_path.is_file():
                                    download_path.unlink()
                                elif download_path.is_dir():
                                    shutil.rmtree(download_path)
                            except Exception as e:
                                print(f"Failed to clean up partial download: {e}")

                        GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, self.download_progress[rom_id])
                                    if hasattr(self, 'library_section') else None)

                        # Update action buttons back to "Download"
                        # Don't update if bulk download is in progress - keep "Cancel All" button
                        if not is_bulk_operation:
                            GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                        if hasattr(self, 'library_section') else None)

                        # Decrement bulk download counter for cancelled downloads too
                        if is_bulk_operation and hasattr(self, '_bulk_download_remaining'):
                            self._bulk_download_remaining -= 1

                        GLib.idle_add(lambda n=rom_name:
                                    self.log_message(f"⊗ Cancelled download: {n}"))
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

                        # Update action buttons back to "Download"
                        # Don't update if bulk download is in progress - keep "Cancel All" button
                        if not is_bulk_operation:
                            GLib.idle_add(lambda: self.library_section.update_action_buttons()
                                        if hasattr(self, 'library_section') else None)

                        GLib.idle_add(lambda n=rom_name, m=message:
                                    self.log_message(f"✗ Failed to download {n}: {m}"))
                
                # Clean up progress and throttling data
                def cleanup_progress():
                    time.sleep(3)  # Show completed/failed state for 3 seconds

                    # More thorough cleanup
                    if rom_id in self.download_progress:
                        del self.download_progress[rom_id]
                    if rom_id in self._last_progress_update:
                        del self._last_progress_update[rom_id]

                    # Clean up download thread tracking
                    with self._cancellation_lock:
                        if rom_id in self._download_threads:
                            del self._download_threads[rom_id]
                        self._cancelled_downloads.discard(rom_id)

                    # Clean up current download tracking
                    if hasattr(self, '_current_download_rom_id') and self._current_download_rom_id == rom_id:
                        delattr(self, '_current_download_rom_id')
                    
                    GLib.idle_add(lambda: self.library_section.update_game_progress(rom_id, None)
                                if hasattr(self, 'library_section') else None)
                    
                    # Force garbage collection for large downloads
                    import gc
                    gc.collect()

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
            finally:
                # Call on_complete callback if provided (even on failure to track completion)
                if on_complete and success:
                    try:
                        on_complete(game)
                    except Exception as e:
                        print(f"Error in on_complete callback: {e}")
                semaphore.release()  # Always release when done
        
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

        # Check if we have device-aware sync data to skip unnecessary downloads
        device_saves_to_skip = set()
        device_states_to_skip = set()

        if self.device_id:
            # Query saves uploaded from this device to avoid re-downloading them (optimistic sync)
            try:
                device_saves = self.romm_client.get_saves_by_device(
                    self.device_id,
                    save_type='saves',
                    rom_id=rom_id,
                    limit=50
                )
                device_saves_to_skip = {s.get('id') for s in device_saves if s.get('id')}

                device_states = self.romm_client.get_saves_by_device(
                    self.device_id,
                    save_type='states',
                    rom_id=rom_id,
                    limit=50
                )
                device_states_to_skip = {s.get('id') for s in device_states if s.get('id')}

                if device_saves_to_skip or device_states_to_skip:
                    GLib.idle_add(lambda saves=len(device_saves_to_skip), states=len(device_states_to_skip):
                                self.log_message(f"🔄 Optimistic sync: {saves} saves, {states} states already on device"))
            except Exception as e:
                print(f"Could not query device saves: {e}")
                # Continue without optimistic sync

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

                # Only use the latest save file
                if user_saves:
                    latest_save = max(user_saves, key=lambda x: x.get('updated_at', ''), default=None)
                    user_saves = [latest_save] if latest_save else []

                if user_saves:
                    GLib.idle_add(lambda: self.log_message(f"Downloading latest save file"))

                    for save_file in user_saves:
                        if isinstance(save_file, dict):
                            save_file_id = save_file.get('id')
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

                                    # Optimistic sync with timestamp check
                                    should_skip = False
                                    if save_file_id in device_saves_to_skip:
                                        # Check if local file exists and compare timestamps
                                        if final_path.exists():
                                            import datetime
                                            local_mtime = final_path.stat().st_mtime
                                            server_updated = save_file.get('updated_at', '')

                                            if server_updated:
                                                try:
                                                    # Parse server timestamp
                                                    if server_updated.endswith('Z'):
                                                        server_updated = server_updated.replace('Z', '+00:00')
                                                    server_dt = datetime.datetime.fromisoformat(server_updated)
                                                    if server_dt.tzinfo is None:
                                                        server_dt = server_dt.replace(tzinfo=datetime.timezone.utc)
                                                    server_timestamp = server_dt.timestamp()

                                                    # Compare timestamps (allow 60 second tolerance for clock differences)
                                                    if local_mtime >= server_timestamp - 60:
                                                        should_skip = True
                                                except Exception as e:
                                                    pass  # If timestamp parsing fails, download to be safe

                                    if should_skip:
                                        GLib.idle_add(lambda fn=save_file.get('file_name', 'unknown'):
                                                    self.log_message(f"⏭️ Skipping save (local is up-to-date): {fn}"))
                                        continue
                                    
                                    GLib.idle_add(lambda f=original_filename, rf=retroarch_filename, e=romm_emulator: 
                                                self.log_message(f"Downloading save: {f} → {rf} ({e})"))
                                    
                                    # Download with original filename
                                    if self.romm_client.download_save(rom_id, 'saves', temp_path, self.device_id):
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

                # Only use the latest save state
                if user_states:
                    latest_state = max(user_states, key=lambda x: x.get('updated_at', ''), default=None)
                    user_states = [latest_state] if latest_state else []

                if user_states:
                    GLib.idle_add(lambda: self.log_message(f"Downloading latest save state"))

                    for state_file in user_states:
                        if isinstance(state_file, dict):
                            state_file_id = state_file.get('id')
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

                                    # Optimistic sync with timestamp check
                                    should_skip = False
                                    if state_file_id in device_states_to_skip:
                                        # Check if local file exists and compare timestamps
                                        if final_path.exists():
                                            import datetime
                                            local_mtime = final_path.stat().st_mtime
                                            server_updated = state_file.get('updated_at', '')

                                            if server_updated:
                                                try:
                                                    # Parse server timestamp
                                                    if server_updated.endswith('Z'):
                                                        server_updated = server_updated.replace('Z', '+00:00')
                                                    server_dt = datetime.datetime.fromisoformat(server_updated)
                                                    if server_dt.tzinfo is None:
                                                        server_dt = server_dt.replace(tzinfo=datetime.timezone.utc)
                                                    server_timestamp = server_dt.timestamp()

                                                    # Compare timestamps (allow 60 second tolerance for clock differences)
                                                    if local_mtime >= server_timestamp - 60:
                                                        should_skip = True
                                                except Exception as e:
                                                    pass  # If timestamp parsing fails, download to be safe

                                    if should_skip:
                                        GLib.idle_add(lambda fn=state_file.get('file_name', 'unknown'):
                                                    self.log_message(f"⏭️ Skipping state (local is up-to-date): {fn}"))
                                        continue
                                    
                                    GLib.idle_add(lambda f=original_filename, rf=retroarch_filename, e=romm_emulator: 
                                                self.log_message(f"Downloading state: {f} → {rf} ({e})"))
                                    
                                    # Download with original filename
                                    if self.romm_client.download_save(rom_id, 'states', temp_path, self.device_id):
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

                                            # Download thumbnail/screenshot for the save state
                                            try:
                                                # Extract user_id and platform slug from download_path
                                                download_path = state_file.get('download_path', '')
                                                print(f"Download path: {download_path}")
                                                user_id = None
                                                platform_slug = 'snes'  # fallback

                                                if '/users/' in download_path and '/states/' in download_path:
                                                    # Extract user_id: /users/{user_id}/
                                                    parts = download_path.split('/users/')
                                                    print(f"Split parts: {parts}")
                                                    if len(parts) > 1:
                                                        user_id = parts[1].split('/')[0]
                                                        print(f"Extracted user_id: {user_id}")
                                                        # Extract platform slug: /states/{platform}/
                                                        remaining = parts[1][len(user_id)+1:]
                                                        print(f"Remaining after user_id: {remaining}")
                                                        if 'states/' in remaining:
                                                            slug_part = remaining.split('states/')[1].split('/')[0]
                                                            print(f"Extracted slug_part: {slug_part}")
                                                            if slug_part:
                                                                platform_slug = slug_part
                                                                print(f"Set platform_slug to: {platform_slug}")
                                                        else:
                                                            print(f"states/ not found in remaining: {remaining}")
                                                else:
                                                    print(f"Missing /users/ or /states/ in download_path")

                                                if user_id:
                                                    # Construct screenshot URL using the pattern from RomM
                                                    # /api/raw/assets/users/{user_id}/screenshots/{platform}/{rom_id}/{filename}.png
                                                    screenshot_filename = original_filename.replace('.state', '.png').replace('.State', '.png')
                                                    screenshot_url = f"{self.romm_client.base_url}/api/raw/assets/users/{user_id}/screenshots/{platform_slug}/{rom_id}/{screenshot_filename}"

                                                    try:
                                                        print(f"Attempting to download screenshot from: {screenshot_url}")
                                                        thumb_response = self.romm_client.session.get(screenshot_url, timeout=10)
                                                        print(f"Screenshot response status: {thumb_response.status_code}, size: {len(thumb_response.content)} bytes")
                                                        print(f"Response headers: {thumb_response.headers.get('content-type')}")

                                                        if thumb_response.status_code == 200:
                                                            thumbnail_path = final_path.with_suffix(final_path.suffix + '.png')
                                                            print(f"Saving thumbnail to: {thumbnail_path}")
                                                            with open(thumbnail_path, 'wb') as f:
                                                                f.write(thumb_response.content)
                                                            GLib.idle_add(lambda: self.log_message(f"✓ Thumbnail downloaded"))
                                                        else:
                                                            print(f"Screenshot download failed with status {thumb_response.status_code}")
                                                    except Exception as e:
                                                        print(f"Screenshot download error: {e}")
                                            except Exception as e:
                                                pass
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
                            success = self.romm_client.upload_save_with_thumbnail(rom_id, save_type, save_path, thumbnail_path, emulator, self.device_id)
                            
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

    def on_debug_sync_tracking(self, button):
        """Test track/untrack save sync functionality"""
        if not self.romm_client or not self.romm_client.authenticated:
            self.log_message("Please connect to RomM first")
            return

        if not self.device_id:
            self.log_message("No device ID found - device registration may have failed")
            return

        def test_tracking():
            try:
                self.log_message("=== Testing Sync Tracking API ===")
                self.log_message(f"Device ID: {self.device_id}")

                # Query all saves/states directly from the API
                test_save_id = None
                test_state_id = None

                # Try to get saves directly from /api/saves endpoint
                try:
                    saves_response = self.romm_client.session.get(
                        urljoin(self.romm_client.base_url, '/api/saves'),
                        params={'limit': 10},
                        timeout=10
                    )

                    if saves_response.status_code == 200:
                        saves_data = saves_response.json()

                        # Handle both list and dict responses
                        if isinstance(saves_data, list):
                            items = saves_data
                        elif isinstance(saves_data, dict):
                            items = saves_data.get('items', [])
                        else:
                            items = []

                        if items:
                            test_save_id = items[0].get('id')
                            rom_id = items[0].get('rom_id')
                            GLib.idle_add(lambda sid=test_save_id, rid=rom_id:
                                        self.log_message(f"Found save ID: {sid} (ROM: {rid})"))
                except Exception as e:
                    GLib.idle_add(lambda err=str(e):
                                self.log_message(f"Could not fetch saves: {err}"))

                # If no saves, try states
                if not test_save_id:
                    try:
                        states_response = self.romm_client.session.get(
                            urljoin(self.romm_client.base_url, '/api/states'),
                            params={'limit': 10},
                            timeout=10
                        )

                        if states_response.status_code == 200:
                            states_data = states_response.json()

                            # Handle both list and dict responses
                            if isinstance(states_data, list):
                                items = states_data
                            elif isinstance(states_data, dict):
                                items = states_data.get('items', [])
                            else:
                                items = []

                            if items:
                                test_state_id = items[0].get('id')
                                rom_id = items[0].get('rom_id')
                                GLib.idle_add(lambda sid=test_state_id, rid=rom_id:
                                            self.log_message(f"Found state ID: {sid} (ROM: {rid})"))
                    except Exception as e:
                        GLib.idle_add(lambda err=str(e):
                                    self.log_message(f"Could not fetch states: {err}"))

                if not test_save_id and not test_state_id:
                    GLib.idle_add(lambda: self.log_message("❌ No saves or states found to test with"))
                    GLib.idle_add(lambda: self.log_message("💡 Upload a save file first, then try again"))
                    return

                # Test with save or state
                save_id = test_save_id or test_state_id
                save_type = 'saves' if test_save_id else 'states'

                GLib.idle_add(lambda: self.log_message(f"\n--- Testing with {save_type[:-1]} ID: {save_id} ---"))

                # Test 1: Untrack the save
                GLib.idle_add(lambda: self.log_message("\n1️⃣ Testing UNTRACK..."))
                untrack_result = self.romm_client.untrack_save(save_id, save_type, self.device_id)
                if untrack_result:
                    GLib.idle_add(lambda: self.log_message("✅ Untrack successful"))
                else:
                    GLib.idle_add(lambda: self.log_message("⚠️ Untrack failed (may already be untracked)"))

                # Wait a bit
                import time
                time.sleep(1)

                # Test 2: Track the save again
                GLib.idle_add(lambda: self.log_message("\n2️⃣ Testing TRACK..."))
                track_result = self.romm_client.track_save(save_id, save_type, self.device_id)
                if track_result:
                    GLib.idle_add(lambda: self.log_message("✅ Track successful"))
                else:
                    GLib.idle_add(lambda: self.log_message("⚠️ Track failed (may already be tracked)"))

                # Test 3: Check device_syncs in metadata
                GLib.idle_add(lambda: self.log_message("\n3️⃣ Checking metadata..."))
                check_response = self.romm_client.session.get(
                    urljoin(self.romm_client.base_url, f'/api/{save_type}/{save_id}'),
                    timeout=10
                )

                if check_response.status_code == 200:
                    save_data = check_response.json()
                    device_syncs = save_data.get('device_syncs', [])
                    GLib.idle_add(lambda syncs=device_syncs:
                                self.log_message(f"📱 Device syncs: {syncs}"))

                    # Look for our device in the sync list
                    our_sync = None
                    for sync in device_syncs:
                        if sync.get('device_id') == self.device_id:
                            our_sync = sync
                            break

                    if our_sync:
                        GLib.idle_add(lambda: self.log_message(f"✅ Found our device in sync list:"))
                        GLib.idle_add(lambda s=our_sync:
                                    self.log_message(f"   Tracking: {s.get('is_tracking', 'unknown')}"))
                        GLib.idle_add(lambda s=our_sync:
                                    self.log_message(f"   Downloaded: {s.get('downloaded', 'unknown')}"))
                    else:
                        GLib.idle_add(lambda: self.log_message("⚠️ Our device not found in sync list"))
                else:
                    GLib.idle_add(lambda code=check_response.status_code:
                                self.log_message(f"⚠️ Could not fetch metadata: HTTP {code}"))

                # Test 4: Query saves by device
                GLib.idle_add(lambda: self.log_message("\n4️⃣ Testing device-filtered query..."))
                device_saves = self.romm_client.get_saves_by_device(
                    self.device_id,
                    save_type=save_type,
                    limit=5
                )

                if device_saves:
                    GLib.idle_add(lambda count=len(device_saves):
                                self.log_message(f"✅ Found {count} {save_type} for this device"))
                    for save in device_saves[:3]:  # Show first 3
                        save_id_item = save.get('id')
                        rom_id_item = save.get('rom_id')
                        filename = save.get('file_name', 'unknown')
                        GLib.idle_add(lambda sid=save_id_item, rid=rom_id_item, fn=filename:
                                    self.log_message(f"   - {fn} (save:{sid}, rom:{rid})"))
                else:
                    GLib.idle_add(lambda: self.log_message("⚠️ No saves found for device (query may not be supported)"))

                # Test 5: Get saves summary for the ROM
                if save_id and save_type == 'saves':  # Only test with saves, not states
                    GLib.idle_add(lambda: self.log_message("\n5️⃣ Testing saves summary..."))

                    # Get the ROM ID from the save
                    summary_rom_id = None
                    if device_saves:
                        for save in device_saves:
                            if save.get('id') == save_id:
                                summary_rom_id = save.get('rom_id')
                                break

                    if summary_rom_id:
                        summary = self.romm_client.get_saves_summary(summary_rom_id, save_type='saves')

                        if summary:
                            GLib.idle_add(lambda: self.log_message("✅ Retrieved saves summary"))
                            if isinstance(summary, dict):
                                for key, value in list(summary.items())[:3]:  # Show first 3 items
                                    GLib.idle_add(lambda k=key, v=str(value)[:80]:
                                                self.log_message(f"   {k}: {v}"))
                            elif isinstance(summary, list):
                                GLib.idle_add(lambda count=len(summary):
                                            self.log_message(f"   Summary contains {count} slots"))
                        else:
                            GLib.idle_add(lambda: self.log_message("⚠️ Summary not available (may not be supported)"))
                    else:
                        GLib.idle_add(lambda: self.log_message("⚠️ Could not determine ROM ID for summary"))

                GLib.idle_add(lambda: self.log_message("\n=== Test Complete ==="))
                GLib.idle_add(lambda: self.log_message("💡 Check the RomM web interface to verify sync status"))

            except Exception as e:
                GLib.idle_add(lambda err=str(e): self.log_message(f"Test error: {err}"))

        threading.Thread(target=test_tracking, daemon=True).start()

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
        super().__init__(application_id='com.romm.retroarch.sync')
        self.connect('activate', self.on_activate)
        self.connect('shutdown', self.on_shutdown)
    
    def on_activate(self, app):
        """Application activation handler"""
        # Only create window if it doesn't exist
        windows = self.get_windows()
        if windows:
            windows[0].present()
        else:
            win = SyncWindow(application=app)
            
            # Handle minimized startup
            if hasattr(app, 'start_minimized') and app.start_minimized:
                print("🔽 Starting minimized to tray")
                win.set_visible(False)  # Start hidden
            else:
                win.present()
    
    def on_shutdown(self, app):  # Add this method
        """Clean up before shutdown"""
        print("🚪 Application shutting down...")
        for window in self.get_windows():
            if hasattr(window, 'tray'):
                window.tray.cleanup()

def main():
    """Main entry point"""
    import argparse
    
    # Parse command line arguments
    parser = argparse.ArgumentParser(description='RomM-RetroArch Sync')
    parser.add_argument('--minimized', action='store_true', 
                       help='Start minimized to tray')
    parser.add_argument('--daemon', action='store_true',
                        help='Run in daemon mode (no GUI)')
    args = parser.parse_args()
    
    print("🚀 Starting RomM-RetroArch Sync...")
    
    # Handle daemon mode first
    if args.daemon:
        print("🔧 Starting in daemon mode...")
        return run_daemon_mode()
    
    # GUI mode continues here...
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
    app.start_minimized = args.minimized  # Pass the flag to the app
    return app.run()


if __name__ == '__main__':
    main()
