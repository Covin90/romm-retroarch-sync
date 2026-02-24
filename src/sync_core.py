#!/usr/bin/env python3
"""Core sync logic - GTK-free. Shared by both the desktop app and Decky plugin."""

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
import psutil
import stat
import re

from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
import queue
from collections import defaultdict

# Fix SSL certificate path for AppImage environment
import ssl
os.environ['REQUESTS_CA_BUNDLE'] = '/etc/ssl/certs/ca-certificates.crt'
os.environ['SSL_CERT_FILE'] = '/etc/ssl/certs/ca-certificates.crt'

# GLib is optional - used for GUI thread scheduling when GTK is available.
# In headless/Decky mode, callbacks are called directly instead.
try:
    from gi.repository import GLib
    def _idle_add(f, *a): GLib.idle_add(f, *a)
except ImportError:
    def _idle_add(f, *a): f(*a)  # call directly in headless mode

class DownloadCancelledException(Exception):
    """Raised when a download is cancelled by the user"""
    pass

class PerformanceTimer:
    """Utility for tracking performance timing"""
    def __init__(self, label, enabled=True):
        self.label = label
        self.enabled = enabled
        self.start_time = None
        self.checkpoints = []

    def __enter__(self):
        if self.enabled:
            self.start_time = time.time()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        if self.enabled and self.start_time:
            elapsed = time.time() - self.start_time
        return False

    def checkpoint(self, label):
        """Mark a checkpoint with elapsed time"""
        if self.enabled and self.start_time:
            elapsed = time.time() - self.start_time
            self.checkpoints.append((label, elapsed))

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
        # CRITICAL: Load platform mapping FIRST so it's available when processing cached games
        self.platform_mapping = self.load_platform_mapping()
        self.filename_mapping = self.load_filename_mapping()
        self.cached_games = self.load_games_cache()
    
    def save_games_data(self, games_data):
        """Non-blocking cache save with memory optimization"""
        import threading
        import time
        import gc  # Add this import
        
        def save_in_background():
            try:
                start_time = time.time()
                
                # MEMORY OPTIMIZATION: Clean up before caching
                processed_games = []
                for game in games_data:
                    # Create a clean copy with only essential data
                    clean_game = {
                        'name': game.get('name'),
                        'rom_id': game.get('rom_id'),
                        'platform': game.get('platform'),
                        'platform_slug': game.get('platform_slug'),
                        'file_name': game.get('file_name'),
                        'is_downloaded': game.get('is_downloaded', False),
                        'local_path': game.get('local_path'),
                        'local_size': game.get('local_size', 0),
                        'romm_data': game.get('romm_data', {}),  # Already cleaned by step 1
                        'is_multi_disc': game.get('is_multi_disc', False),  # Preserve multi-disc flag
                        'discs': game.get('discs', [])  # Preserve disc data for multi-disc games
                    }
                    processed_games.append(clean_game)
                
                cache_data = {
                    'timestamp': time.time(),
                    'games': processed_games,  # Use cleaned data
                    'count': len(processed_games)
                }
                
                # Force garbage collection
                gc.collect()
                
                temp_file = self.games_cache_file.with_suffix('.tmp')
                
                with open(temp_file, 'w', encoding='utf-8') as f:
                    json.dump(cache_data, f, separators=(',', ':'))
                
                temp_file.rename(self.games_cache_file)
                
                self.update_mappings(processed_games)
                self.cached_games = processed_games  # Store cleaned data
                
                elapsed = time.time() - start_time
                print(f"✅ Background: Cached {len(processed_games):,} games in {elapsed:.2f}s")
                
            except Exception as e:
                print(f"❌ Background cache save failed: {e}")
        
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

            # CRITICAL: Always resolve platform names from platform_slug using the mapping
            # This ensures cached games display proper names even if they were cached with slugs
            for game in games:
                if isinstance(game, dict):
                    platform_slug = game.get('platform_slug')
                    if platform_slug:
                        # Use mapping to get proper platform name (fallback mapping always available)
                        game['platform'] = self.get_platform_name(platform_slug)

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
        """Load platform mapping from file, with fallback to common platforms"""
        # Start with a hardcoded fallback mapping for common platforms
        # This ensures proper display even without connecting to RomM
        fallback_mapping = {
            # Nintendo platforms
            'nes': 'Nintendo Entertainment System',
            'famicom': 'Famicom',
            'fds': 'Famicom Disk System',
            'snes': 'Super Nintendo Entertainment System',
            'sfam': 'Super Famicom',
            'satellaview': 'Satellaview',
            'n64': 'Nintendo 64',
            '64dd': 'Nintendo 64DD',
            'gc': 'Nintendo GameCube',
            'ngc': 'Nintendo GameCube',
            'wii': 'Nintendo Wii',
            'wiiu': 'Nintendo Wii U',
            'switch': 'Nintendo Switch',
            'gb': 'Game Boy',
            'gbc': 'Game Boy Color',
            'gba': 'Game Boy Advance',
            'nds': 'Nintendo DS',
            'nintendo-dsi': 'Nintendo DSi',
            '3ds': 'Nintendo 3DS',
            'n3ds': 'Nintendo 3DS',
            'virtualboy': 'Virtual Boy',
            'g-and-w': 'Game & Watch',
            'poke-mini': 'Pokemon Mini',
            'pokemon-mini': 'Pokemon Mini',
            'nintendo-playstation': 'Nintendo PlayStation',

            # Sega platforms
            'genesis': 'Sega Genesis',
            'megadrive': 'Sega Mega Drive',
            'sega-genesis': 'Sega Genesis',
            'sega-mega-drive': 'Sega Mega Drive',
            'genesis-slash-megadrive': 'Sega Genesis / Mega Drive',
            'mastersystem': 'Sega Master System',
            'sms': 'Sega Master System',
            'gamegear': 'Sega Game Gear',
            'segacd': 'Sega CD',
            'sega-cd': 'Sega CD',
            'sega32': 'Sega 32X',
            'saturn': 'Sega Saturn',
            'dreamcast': 'Sega Dreamcast',
            'dc': 'Sega Dreamcast',
            'sg1000': 'SG-1000',
            'sega-pico': 'Sega Pico',

            # Sony platforms
            'psx': 'PlayStation',
            'ps': 'PlayStation',
            'ps1': 'PlayStation',
            'ps2': 'PlayStation 2',
            'ps3': 'PlayStation 3',
            'ps4--1': 'PlayStation 4',
            'ps4': 'PlayStation 4',
            'ps5': 'PlayStation 5',
            'psp': 'PlayStation Portable',
            'psvita': 'PlayStation Vita',
            'pocketstation': 'PocketStation',

            # Arcade platforms
            'arcade': 'Arcade',
            'mame': 'MAME',
            'fbneo': 'FBNeo',
            'neogeo': 'Neo Geo',
            'neogeoaes': 'Neo Geo AES',
            'neogeomvs': 'Neo Geo MVS',
            'neo-geo-cd': 'Neo Geo CD',
            'neo-geo-pocket': 'Neo Geo Pocket',
            'ngp': 'Neo Geo Pocket',
            'neo-geo-pocket-color': 'Neo Geo Pocket Color',
            'ngpc': 'Neo Geo Pocket Color',
            'neo-geo-x': 'Neo Geo X',

            # Atari platforms
            'atari2600': 'Atari 2600',
            'atari5200': 'Atari 5200',
            'atari7800': 'Atari 7800',
            'atari8bit': 'Atari 8-bit',
            'lynx': 'Atari Lynx',
            'atarilynx': 'Atari Lynx',
            'jaguar': 'Atari Jaguar',
            'atarijaguar': 'Atari Jaguar',
            'atari-jaguar-cd': 'Atari Jaguar CD',
            'atari-st': 'Atari ST',
            'atari-vcs': 'Atari VCS',

            # NEC platforms
            'pcengine': 'PC Engine',
            'turbografx16--1': 'TurboGrafx-16',
            'turbografx16': 'TurboGrafx-16',
            'turbografx-16': 'TurboGrafx-16',
            'turbografx-16-slash-pc-engine-cd': 'TurboGrafx-16 / PC Engine CD',
            'pcenginecd': 'PC Engine CD',
            'supergrafx': 'SuperGrafx',
            'pc-fx': 'PC-FX',

            # SNK platforms
            'wonderswan': 'WonderSwan',
            'wonderswancolor': 'WonderSwan Color',
            'wonderswan-color': 'WonderSwan Color',
            'swancrystal': 'SwanCrystal',

            # Panasonic / Other consoles
            '3do': '3DO',
            'philips-cd-i': 'Philips CD-i',
            'jaguar': 'Atari Jaguar',
            'amiga': 'Amiga',
            'amiga-cd32': 'Amiga CD32',
            'intellivision': 'Intellivision',
            'colecovision': 'ColecoVision',
            'vectrex': 'Vectrex',
            'odyssey-2-slash-videopac-g7000': 'Odyssey 2 / Videopac G7000',
            'fairchild-channel-f': 'Fairchild Channel F',
            'astrocade': 'Astrocade',
            'supervision': 'Supervision',
            'casio-loopy': 'Casio Loopy',
            'casio-pv-1000': 'Casio PV-1000',
            'creativision': 'CreatiVision',
            'gamate': 'Gamate',
            'game-dot-com': 'Game.com',
            'mega-duck-slash-cougar-boy': 'Mega Duck / Cougar Boy',
            'microvision--1': 'Microvision',
            'arcadia-2001': 'Arcadia 2001',
            'vc-4000': 'VC 4000',
            'adventure-vision': 'Adventure Vision',
            'epoch-cassette-vision': 'Epoch Cassette Vision',
            'epoch-super-cassette-vision': 'Epoch Super Cassette Vision',

            # Computer platforms
            'dos': 'DOS',
            'win': 'Windows',
            'win3x': 'Windows 3.x',
            'windows-apps': 'Windows Apps',
            'mac': 'Macintosh',
            'linux': 'Linux',
            'c64': 'Commodore 64',
            'c128': 'Commodore 128',
            'vic-20': 'Commodore VIC-20',
            'c-plus-4': 'Commodore Plus/4',
            'c16': 'Commodore 16',
            'cpet': 'Commodore PET',
            'commodore-cdtv': 'Commodore CDTV',
            'zxspectrum': 'ZX Spectrum',
            'zxs': 'ZX Spectrum',
            'sinclair-zx81': 'Sinclair ZX81',
            'zx80': 'ZX80',
            'zx-spectrum-next': 'ZX Spectrum Next',
            'msx': 'MSX',
            'msx2': 'MSX2',
            'bbcmicro': 'BBC Micro',
            'acorn-electron': 'Acorn Electron',
            'acorn-archimedes': 'Acorn Archimedes',
            'acpc': 'Amstrad CPC',
            'amstrad-pcw': 'Amstrad PCW',
            'appleii': 'Apple II',
            'apple2gs': 'Apple IIGS',
            'apple-iigs': 'Apple IIGS',
            'apple-i': 'Apple I',
            'sharp-x68000': 'Sharp X68000',
            'sharp-x1': 'Sharp X1',
            'x1': 'Sharp X1',
            'fm-towns': 'FM Towns',
            'fm-7': 'FM-7',
            'pc-8800-series': 'PC-8800 Series',
            'pc-9800-series': 'PC-9800 Series',
            'pc-6001': 'PC-6001',
            'pc-8000': 'PC-8000',
            'oric': 'Oric',
            'dragon-32-slash-64': 'Dragon 32/64',
            'trs-80': 'TRS-80',
            'trs-80-color-computer': 'TRS-80 Color Computer',
            'ti-99': 'TI-99',
            'ti-994a': 'TI-99/4A',
            'thomson-mo5': 'Thomson MO5',
            'thomson-to': 'Thomson TO',
            'atom': 'Atom',
            'sam-coupe': 'SAM Coupé',
            'sinclair-ql': 'Sinclair QL',
            'enterprise': 'Enterprise',
            'spectravideo': 'SpectraVideo',
            'sord-m5': 'Sord M5',
            'smc-777': 'SMC-777',

            # Mobile platforms
            'android': 'Android',
            'ios': 'iOS',
            'mobile': 'Mobile',
            'ngage': 'N-Gage',
            'ngage2': 'N-Gage 2.0',
            'gizmondo': 'Gizmondo',
            'zeebo': 'Zeebo',
            'ouya': 'OUYA',
            'leapster': 'Leapster',
            'leapster-explorer-slash-leadpad-explorer': 'Leapster Explorer / LeadPad Explorer',
            'didj': 'Didj',

            # Modern platforms
            'stadia': 'Google Stadia',
            'xboxcloudgaming': 'Xbox Cloud Gaming',
            'playstation-now': 'PlayStation Now',
            'geforce-now': 'GeForce Now',

            # Xbox platforms
            'xbox': 'Xbox',
            'xbox360': 'Xbox 360',
            'xboxone': 'Xbox One',
            'series-x': 'Xbox Series X/S',

            # Handheld platforms
            'gp32': 'GP32',
            'gp2x': 'GP2X',
            'gp2x-wiz': 'GP2X Wiz',
            'pandora': 'Pandora',
            'playdate': 'Playdate',
            'evercade': 'Evercade',
            'arduboy': 'Arduboy',
            'pokitto': 'Pokitto',

            # VR platforms
            'psvr': 'PlayStation VR',
            'psvr2': 'PlayStation VR2',
            'oculus-quest': 'Oculus Quest',
            'oculus-rift': 'Oculus Rift',
            'meta-quest-2': 'Meta Quest 2',
            'meta-quest-3': 'Meta Quest 3',

            # Web/Browser
            'browser': 'Web Browser',

            # Other
            'pico': 'PICO-8',
            'tic-80': 'TIC-80',
        }

        try:
            if self.platform_mapping_file.exists():
                with open(self.platform_mapping_file, 'r', encoding='utf-8') as f:
                    cached_mapping = json.load(f)
                    # Merge: Start with cached, then add fallback for any missing entries
                    # This ensures fallback values are used for common platforms
                    # but cached values add any additional platforms from RomM
                    merged_mapping = fallback_mapping.copy()

                    # Only add cached entries that provide actual platform names (not just slugs)
                    for slug, name in cached_mapping.items():
                        # If cached value looks like a proper platform name (not just the slug)
                        # or if it's not in fallback, add it
                        if slug not in fallback_mapping or (name != slug and len(name) > len(slug)):
                            merged_mapping[slug] = name

                    return merged_mapping
        except Exception as e:
            print(f"Failed to load platform mapping: {e}")

        return fallback_mapping
    
    def load_filename_mapping(self):
        """Load filename mapping from file"""
        try:
            if self.filename_mapping_file.exists():
                with open(self.filename_mapping_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
        except Exception as e:
            print(f"Failed to load filename mapping: {e}")
        return {}
    
    def build_platform_mapping_from_api(self, platforms_data):
        """Build platform mapping from RomM API platforms response"""
        platform_mapping = {}

        for platform in platforms_data:
            if not isinstance(platform, dict):
                continue

            platform_name = platform.get('name', '')
            platform_slug = platform.get('slug', '')

            if not platform_name or not platform_slug:
                continue

            # Map slug to name
            platform_mapping[platform_slug] = platform_name

            # Also map common variations
            variations = [
                platform_name,
                platform_name.replace(' ', '_'),
                platform_name.replace(' ', ''),
            ]

            for variation in variations:
                if variation:
                    platform_mapping[variation] = platform_name

        # Save and update in-memory mapping
        self.save_platform_mapping(platform_mapping)
        self.platform_mapping = platform_mapping
        print(f"📋 Built platform mapping with {len(platform_mapping)} entries from API")

        return platform_mapping

    def get_platform_name(self, directory_name):
        """Get proper platform name from directory name with case-insensitive fallback"""
        # Try exact match first
        if directory_name in self.platform_mapping:
            return self.platform_mapping[directory_name]

        # Try lowercase match
        lower_name = directory_name.lower()
        if lower_name in self.platform_mapping:
            return self.platform_mapping[lower_name]

        # Try case-insensitive search through all keys
        for key, value in self.platform_mapping.items():
            if key.lower() == lower_name:
                return value

        # No match found, return original
        return directory_name
    
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

def detect_retrodeck():
    """Detect if RetroDECK is installed.

    Uses lightweight directory checks first (no subprocess).  Falls back to
    ``flatpak list`` only when the directories are absent.

    Returns a dict with ``rom_directory`` and ``save_directory`` set to the
    RetroDECK defaults, or ``None`` when RetroDECK is not detected.
    """
    retrodeck_home = Path.home() / 'retrodeck'
    retrodeck_flatpak_config = Path.home() / '.var' / 'app' / 'net.retrodeck.retrodeck'

    found = retrodeck_home.exists() or retrodeck_flatpak_config.exists()

    if not found:
        try:
            import subprocess
            result = subprocess.run(
                ['flatpak', 'list'], capture_output=True, text=True, timeout=5
            )
            found = 'net.retrodeck.retrodeck' in result.stdout
        except Exception:
            pass

    if found:
        return {
            'rom_directory': str(Path.home() / 'retrodeck' / 'roms'),
            'save_directory': str(Path.home() / 'retrodeck' / 'saves'),
        }
    return None


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
            # Migrate settings from older versions
            self._migrate_settings()
        else:
            # Create default settings
            self.config['RomM'] = {
                'url': '',
                'username': '',
                'password': '',
                'remember_credentials': 'false',
                'auto_connect': 'false',
                'auto_refresh': 'false'
            }
            self.config['Download'] = {
                'rom_directory': str(Path.home() / 'RomMSync' / 'roms'),
                'save_directory': str(Path.home() / 'RomMSync' / 'saves'),
            }
            self.config['BIOS'] = {
                'verify_on_launch': 'false',
                'backup_existing': 'true',
            }
            self.config['AutoSync'] = {
                'auto_enable_on_connect': 'true',
                'overwrite_behavior': '0'
            }
            self.config['System'] = {
                'autostart': 'false'
            }
            self.config['Collections'] = {
                'sync_interval': '120',
                'selected_for_sync': '',
                'auto_download': 'true',
                'auto_delete': 'false',
                'auto_sync_enabled': 'false'
            }
            self.config['Device'] = {
                'device_id': '',
                'device_name': socket.gethostname(),
                'device_platform': 'Linux',
                'client': 'RomM-RetroArch-Sync',
                'client_version': '1.3.2',
                'sync_enabled': 'true'
            }

            self.save_settings()

    def _migrate_settings(self):
        """Migrate settings from older app versions - add missing sections/keys"""
        modified = False

        # Ensure Device section exists (added in v1.3.3+)
        if 'Device' not in self.config:
            self.config['Device'] = {}
            modified = True

        device_defaults = {
            'device_id': '',
            'device_name': socket.gethostname(),
            'device_platform': 'Linux',
            'client': 'RomM-RetroArch-Sync',
            'client_version': '1.3.2',
            'sync_enabled': 'true'
        }

        # Add any missing Device fields
        for key, default_value in device_defaults.items():
            if key not in self.config['Device']:
                self.config['Device'][key] = default_value
                modified = True

        # Save if any migrations were applied
        if modified:
            self.save_settings()
            print(f"✅ Settings migrated to latest version")
    
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
        self.authenticated = False

        # OAuth2 token storage
        self.access_token = None
        self.refresh_token = None
        self.token_type = 'bearer'
        self.token_expiry = None

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
            'Accept-Encoding': 'gzip, deflate',
            'Accept': 'application/json',
            'User-Agent': 'RomM-RetroArch-Sync/1.3.2',
            'Connection': 'keep-alive',
            'Keep-Alive': 'timeout=30, max=100'
        })
        
        if username and password:
            self.authenticate(username, password)
    
    def authenticate(self, username, password):
        """Authenticate with RomM using Basic Auth, Token, or Session fallback"""
        try:
            # Method 1: Test if we already have a valid session (for OIDC/Authentik users)
            print("Testing existing session...")
            test_response = self.session.get(
                urljoin(self.base_url, '/api/roms'),
                params={'limit': 1},
                timeout=10
            )
            
            if test_response.status_code == 200:
                print("✅ Session authentication successful (OIDC/Authentik)")
                self.authenticated = True
                return True
            
            # Method 2: Basic Authentication (for traditional setups)
            print("Trying Basic Authentication...")
            import base64
            
            credentials = f"{username}:{password}"
            encoded_credentials = base64.b64encode(credentials.encode('utf-8')).decode('utf-8')
            
            self.session.headers.update({
                'Authorization': f'Basic {encoded_credentials}'
            })
            
            test_response = self.session.get(
                urljoin(self.base_url, '/api/roms'),
                timeout=10
            )
            
            if test_response.status_code == 200:
                print("✅ Basic Authentication successful!")
                self.authenticated = True
                return True
            elif test_response.status_code in [401, 403]:
                print("Basic auth failed (401/403), trying token endpoint...")
                
                # Method 3: Token-based authentication (OAuth2)
                if 'Authorization' in self.session.headers:
                    del self.session.headers['Authorization']

                # Use OAuth2 standard format (application/x-www-form-urlencoded)
                token_data = {
                    'username': username,
                    'password': password,
                    'grant_type': 'password',
                    'scope': 'read:roms write:roms read:platforms write:platforms read:saves write:saves read:states write:states'
                }

                print("Requesting access token...")
                token_response = self.session.post(
                    urljoin(self.base_url, '/api/token'),
                    data=token_data,  # Use data= for form-encoded (not json=)
                    timeout=10
                )

                if token_response.status_code == 200:
                    import time
                    token_info = token_response.json()
                    self.access_token = token_info.get('access_token')
                    self.refresh_token = token_info.get('refresh_token')
                    self.token_type = token_info.get('token_type', 'bearer')

                    # Calculate expiration time (default 1 hour if not specified)
                    expires_in = token_info.get('expires_in', 3600)
                    self.token_expiry = time.time() + expires_in

                    if self.access_token:
                        self.session.headers.update({
                            'Authorization': f'Bearer {self.access_token}'
                        })

                        test_response = self.session.get(
                            urljoin(self.base_url, '/api/roms'),
                            timeout=10
                        )

                        if test_response.status_code == 200:
                            print("✅ Token authentication successful!")
                            if self.refresh_token:
                                print(f"   Refresh token captured (expires in {expires_in}s)")
                            self.authenticated = True
                            return True
                else:
                    print(f"❌ Token endpoint failed: HTTP {token_response.status_code}")
                    try:
                        error_detail = token_response.json()
                        print(f"   Error: {error_detail}")
                    except:
                        print(f"   Response: {token_response.text[:200]}")

            print("All authentication methods failed")
            self.authenticated = False
            return False
            
        except Exception as e:
            print(f"Authentication error: {e}")
            self.authenticated = False
            return False

    def refresh_access_token(self):
        """Refresh the access token using refresh_token"""
        if not self.refresh_token:
            print("⚠️ No refresh token available")
            return False

        try:
            import time
            refresh_data = {
                'grant_type': 'refresh_token',
                'refresh_token': self.refresh_token
            }

            print("🔄 Refreshing access token...")
            response = self.session.post(
                urljoin(self.base_url, '/api/token'),
                data=refresh_data,
                timeout=10
            )

            if response.status_code == 200:
                token_info = response.json()
                self.access_token = token_info.get('access_token')
                # Server may return a new refresh token, or we keep the old one
                self.refresh_token = token_info.get('refresh_token', self.refresh_token)

                expires_in = token_info.get('expires_in', 3600)
                self.token_expiry = time.time() + expires_in

                self.session.headers.update({
                    'Authorization': f'Bearer {self.access_token}'
                })

                print(f"✅ Token refreshed successfully (expires in {expires_in}s)")
                return True
            else:
                print(f"❌ Token refresh failed: HTTP {response.status_code}")
                self.authenticated = False
                return False

        except Exception as e:
            print(f"❌ Error refreshing token: {e}")
            self.authenticated = False
            return False

    def ensure_authenticated(self):
        """Ensure token is valid, refresh if needed"""
        import time

        if not self.authenticated:
            return False

        # Check if token will expire in next 5 minutes (300 seconds)
        if hasattr(self, 'token_expiry') and self.token_expiry:
            time_until_expiry = self.token_expiry - time.time()
            if time_until_expiry < 300:
                print(f"⏰ Token expires in {int(time_until_expiry)}s, refreshing...")
                return self.refresh_access_token()

        return True

    def register_device(self, device_name=None, platform=None, client=None, client_version=None):
        """Register or get device ID with RomM.

        Uses allow_existing=True to return existing device if already registered.
        Returns device_id on success, None on failure.
        """
        if not self.authenticated:
            return None

        try:
            import socket
            import platform as sys_platform

            # Prepare device payload
            payload = {
                'name': device_name or socket.gethostname(),
                'platform': platform or sys_platform.system(),
                'client': client or 'RomM-RetroArch-Sync',
                'client_version': client_version or '1.3.2',
                'hostname': socket.gethostname(),
                'allow_existing': True,
                'allow_duplicate': False
            }

            print(f"📱 Registering device: {payload['name']}")

            response = self.session.post(
                urljoin(self.base_url, '/api/devices'),
                json=payload,
                timeout=10
            )

            if response.status_code in [200, 201]:
                data = response.json()
                device_id = data.get('device_id') or data.get('id')
                if device_id:
                    print(f"✅ Device registered: {device_id}")
                    return device_id
                else:
                    print(f"⚠️ Device registered but no ID in response: {data}")
                    return None
            else:
                print(f"❌ Device registration failed: HTTP {response.status_code}")
                try:
                    error_data = response.json()
                    print(f"   Error: {error_data}")
                except:
                    print(f"   Response: {response.text[:200]}")
                return None

        except Exception as e:
            print(f"❌ Error registering device: {e}")
            return None

    def get_device(self, device_id):
        """Get device information by device ID"""
        if not self.authenticated or not device_id:
            return None

        try:
            response = self.session.get(
                urljoin(self.base_url, f'/api/devices/{device_id}'),
                timeout=10
            )

            if response.status_code == 200:
                return response.json()
            else:
                print(f"Failed to get device {device_id}: HTTP {response.status_code}")
                return None

        except Exception as e:
            print(f"Error getting device: {e}")
            return None

    def update_device(self, device_id, updates):
        """Update device information"""
        if not self.authenticated or not device_id:
            return False

        try:
            response = self.session.put(
                urljoin(self.base_url, f'/api/devices/{device_id}'),
                json=updates,
                timeout=10
            )

            if response.status_code == 200:
                print(f"✅ Device updated: {device_id}")
                return True
            else:
                print(f"Failed to update device: HTTP {response.status_code}")
                return False

        except Exception as e:
            print(f"Error updating device: {e}")
            return False

    def delete_device(self, device_id):
        """Unregister a device and remove its sync records from the server.

        Args:
            device_id: The device ID to delete

        Returns:
            True if deletion was successful, False otherwise
        """
        if not self.authenticated or not device_id:
            return False

        try:
            response = self.session.delete(
                urljoin(self.base_url, f'/api/devices/{device_id}'),
                timeout=10
            )

            if response.status_code in [200, 204]:
                logging.info(f"Device deleted: {device_id}")
                return True
            elif response.status_code == 404:
                logging.debug(f"Device not found (already gone): {device_id}")
                return True  # Already gone
            else:
                logging.warning(f"Failed to delete device {device_id}: HTTP {response.status_code}")
                return False

        except Exception as e:
            logging.warning(f"Error deleting device: {e}")
            return False

    def get_games_count_only(self):
        """Get total games count without fetching data - lightweight check"""
        if not self.ensure_authenticated():
            return None

        try:
            response = self.session.get(
                urljoin(self.base_url, '/api/roms'),
                params={'limit': 1, 'offset': 0},  # Just get 1 item to see total
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                return data.get('total', 0)
        except:
            pass
        return None

    def get_roms(self, progress_callback=None, limit=500, offset=0, updated_after=None):
        """Get ROMs with pagination support - FIXED to fetch ALL games

        Args:
            progress_callback: Optional callback for progress updates
            limit: Number of items per page
            offset: Offset for pagination
            updated_after: Optional ISO 8601 datetime string to only fetch ROMs updated after this time
        """
        if not self.ensure_authenticated():
            return [], 0

        try:
            # For backward compatibility, if no specific limit is requested, fetch ALL games
            if limit == 500 and offset == 0 and updated_after is None:
                return self._fetch_all_games_chunked(progress_callback)
            else:
                # Specific pagination request or filtered by updated_after
                params = {
                    'limit': limit,
                    'offset': offset,
                    'fields': 'id,name,fs_name,platform_name,platform_slug,files,multi'
                }

                # Add updated_after filter if provided
                if updated_after:
                    params['updated_after'] = updated_after

                response = self.session.get(
                    urljoin(self.base_url, '/api/roms'),
                    params=params,
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

    def get_collections(self, updated_after=None):
        """Get custom collections from RomM

        Args:
            updated_after: Optional ISO 8601 datetime string to only fetch collections updated after this time
        """
        if not self.ensure_authenticated():
            return []

        try:
            params = {}
            if updated_after:
                params['updated_after'] = updated_after

            response = self.session.get(
                urljoin(self.base_url, '/api/collections'),
                params=params if params else None,
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
        except Exception as e:
            print(f"Error fetching collections: {e}")
        return []

    def get_platforms(self):
        """Get list of all platforms from RomM"""
        if not self.ensure_authenticated():
            return []

        try:
            response = self.session.get(
                urljoin(self.base_url, '/api/platforms'),
                timeout=10
            )
            if response.status_code == 200:
                return response.json()
            else:
                print(f"Failed to get platforms: {response.status_code}")
                return []
        except Exception as e:
            print(f"Error fetching platforms: {e}")
        return []

    def get_collection_roms(self, collection_id):
        """Get ROMs in a specific collection"""
        if not self.ensure_authenticated():
            return []

        try:
            response = self.session.get(
                urljoin(self.base_url, f'/api/roms?collection_id={collection_id}'),
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                items = data.get('items', [])

                # Debug: Check for Final Fantasy in collections
                for rom in items:
                    rom_name = rom.get('name', '') or rom.get('fs_name', '')

                return items
            else:
                print(f"Failed to get collection ROMs: {response.status_code}")
                return []
                
        except Exception as e:
            print(f"Error fetching collection ROMs: {e}")
            return []

    def _fetch_all_games_chunked(self, progress_callback):
        """Fetch all games using parallel requests"""
        try:
            with PerformanceTimer("API fetch - full sync") as timer:
                # First, get total count (optimized with shorter timeout and caching)
                count_start = time.time()

                # Try to use cached count if available and recent (within 30 seconds)
                cached_count = getattr(self, '_cached_game_count', None)
                cache_time = getattr(self, '_cached_game_count_time', 0)
                current_time = time.time()

                if cached_count and (current_time - cache_time) < 30:
                    total_games = cached_count
                    timer.checkpoint(f"Initial count request: {time.time() - count_start:.2f}s (from cache)")
                else:
                    # Fetch count with optimized timeout
                    response = self.session.get(
                        urljoin(self.base_url, '/api/roms'),
                        params={'limit': 1, 'offset': 0, 'fields': 'id'},
                        timeout=10  # Reduced from 30s to 10s
                    )
                    timer.checkpoint(f"Initial count request: {time.time() - count_start:.2f}s")

                    if response.status_code != 200:
                        return [], 0

                    data = response.json()
                    total_games = data.get('total', 0)

                    # Cache the count for future requests
                    self._cached_game_count = total_games
                    self._cached_game_count_time = current_time

                if total_games == 0:
                    return [], 0

                chunk_size = 500
                total_chunks = (total_games + chunk_size - 1) // chunk_size

                print(f"📚 Fetching {total_games:,} games in {total_chunks} chunks of {chunk_size:,} (parallel)...")

                # Use existing parallel fetching
                fetch_start = time.time()
                all_games = self._fetch_pages_parallel(total_games, chunk_size, total_chunks, progress_callback)
                timer.checkpoint(f"Parallel fetch complete: {time.time() - fetch_start:.2f}s")
                timer.checkpoint(f"Total fetch time: {time.time() - count_start:.2f}s")

                return all_games, len(all_games)

        except Exception as e:
            print(f"❌ Parallel fetch error: {e}")
            return [], 0
        
    def _fetch_pages_parallel(self, total_items, page_size, pages_needed, progress_callback):
        """Memory-optimized: Stream and process games in smaller chunks"""
        import concurrent.futures
        import threading
        
        max_workers = 4
        completed_pages = 0
        lock = threading.Lock()
        
        # Instead of accumulating ALL games, process in streaming chunks
        final_games = []
        chunk_size = 200  # Process in smaller chunks
        current_chunk = []
        
        def fetch_single_page(page_num):
            offset = (page_num - 1) * page_size
            if offset >= total_items:
                return page_num, []
            
            try:
                response = self.session.get(
                    urljoin(self.base_url, '/api/roms'),
                    params={
                        'limit': page_size,
                        'offset': offset,
                        'fields': 'id,name,fs_name,platform_name,platform_slug,files,multi'
                    },
                    timeout=60
                )
                
                if response.status_code == 200:
                    data = response.json()
                    items = data.get('items', [])
                    return page_num, items
            except Exception as e:
                print(f"❌ Page {page_num} error: {e}")
            
            return page_num, []
        
        # Process in smaller batches to reduce memory spikes
        batch_size = 2  # Smaller batches = less memory pressure
        
        for batch_start in range(1, pages_needed + 1, batch_size):
            batch_end = min(batch_start + batch_size, pages_needed + 1)
            batch_pages = list(range(batch_start, batch_end))
            
            # Add clear batch progress message
            if progress_callback:
                progress_callback('page', f'⟳ Batch {batch_start}-{batch_end-1} of {pages_needed} pages')

            with concurrent.futures.ThreadPoolExecutor(max_workers=min(batch_size, max_workers)) as executor:
                future_to_page = {executor.submit(fetch_single_page, page): page for page in batch_pages}
                
                batch_games_count = 0  # Track games in this batch
                
                for future in concurrent.futures.as_completed(future_to_page):
                    page_num, page_roms = future.result()
                    batch_games_count += len(page_roms)  # Count games in batch
                    
                    # Process games immediately instead of accumulating
                    for rom in page_roms:
                        # Debug: Check for Final Fantasy
                        rom_name = rom.get('name', '') or rom.get('fs_name', '')

                        current_chunk.append(rom)

                        if len(current_chunk) >= chunk_size:
                            final_games.extend(current_chunk)
                            current_chunk = []
                    
                    with lock:
                        completed_pages += 1
                        # Restore clear progress messages
                        if progress_callback:
                            progress_callback('page', f'⟳ Completed {completed_pages}/{pages_needed} pages ({len(final_games)} games loaded)')
            
            # Add batch completion message
            if progress_callback:
                progress_callback('batch', {
                    'items': [],  # Don't send items for UI updates during fetch
                    'total': total_items,
                    'accumulated_games': final_games[:],  # Send all accumulated games for progressive UI
                    'batch_completed': batch_start,
                    'total_batches': (pages_needed + batch_size - 1) // batch_size
                })
            
            print(f"✓ Batch {batch_start}-{batch_end-1} complete: {batch_games_count} games in this batch")
            
            import gc
            gc.collect()
        
        # Handle remaining chunk
        if current_chunk:
            final_games.extend(current_chunk)
        
        print(f"✓ Fetch complete: {len(final_games):,} games loaded with optimized memory usage")
        
        return final_games
        
    def download_rom(self, rom_id, rom_name, download_path, progress_callback=None, cancellation_checker=None):
        """Download a ROM file with progress tracking

        Args:
            rom_id: ROM identifier
            rom_name: ROM display name
            download_path: Path to save the download
            progress_callback: Optional callback for progress updates
            cancellation_checker: Optional callable that returns True if download should be cancelled
        """
        if not self.ensure_authenticated():
            return False, "Not authenticated"

        try:
            # First, get detailed ROM info to find the filename
            rom_details_response = self.session.get(
                urljoin(self.base_url, f'/api/roms/{rom_id}'),
                timeout=10
            )
            
            if rom_details_response.status_code != 200:
                return False, f"Could not get ROM details: HTTP {rom_details_response.status_code}"
            
            rom_details = rom_details_response.json()
            
            # Try to find the filename in the ROM details
            filename = None
            possible_filename_fields = ['file_name', 'filename', 'fs_name', 'name', 'file', 'path']
            
            for field in possible_filename_fields:
                if field in rom_details and rom_details[field]:
                    filename = rom_details[field]
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
            
            # Check if this is a folder
            is_folder = rom_details.get('multi', False) or \
                    rom_details.get('fs_extension', '') == '' or \
                    len(rom_details.get('files', [])) > 1

            if is_folder:
                folder_name = rom_details.get('fs_name', filename)
                download_path = download_path.parent / folder_name
                download_path.mkdir(parents=True, exist_ok=True)

            # Use same API endpoint for both files and folders
            api_endpoint = f'/api/roms/{rom_id}/content/{filename}'

            response = self.session.get(
                urljoin(self.base_url, api_endpoint),
                stream=True,
                timeout=30
            )

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

            # For folders, use ROM metadata size for progress tracking
            if is_folder:
                metadata_size = rom_details.get('fs_size_bytes', 0)
                if metadata_size > 0:
                    total_size = metadata_size
                    print(f"Using ROM metadata size for folder: {total_size} bytes")
            
            # Create progress tracker
            if progress_callback:
                progress = DownloadProgress(total_size, rom_name)
            
            # Ensure download directory exists
            download_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Download with progress tracking
            actual_downloaded = 0
            start_time = time.time()
            
            if is_folder:
                # For folders, download as zip then extract
                import io
                import zipfile

                # Check for cancellation before starting folder download
                if cancellation_checker and cancellation_checker():
                    raise DownloadCancelledException(f"Download cancelled: {rom_name}")

                # Download with progress tracking
                content_chunks = []
                for chunk in response.iter_content(chunk_size=8192):
                    # Check for cancellation
                    if cancellation_checker and cancellation_checker():
                        raise DownloadCancelledException(f"Download cancelled: {rom_name}")

                    if chunk:
                        content_chunks.append(chunk)
                        actual_downloaded += len(chunk)

                        # Update progress
                        if progress_callback and total_size > 0:
                            progress_info = progress.update(len(chunk))
                            progress_callback(progress_info)

                # Combine chunks
                content = b''.join(content_chunks)

                # Check for cancellation before extraction
                if cancellation_checker and cancellation_checker():
                    raise DownloadCancelledException(f"Download cancelled: {rom_name}")

                with zipfile.ZipFile(io.BytesIO(content)) as zip_ref:
                    # Filter out .m3u files (playlist files that don't exist in RomM)
                    # and other non-ROM files that might cause download errors
                    excluded_extensions = {'.m3u', '.m3u8'}

                    for member in zip_ref.namelist():
                        # Skip files with excluded extensions
                        if not any(member.lower().endswith(ext) for ext in excluded_extensions):
                            zip_ref.extract(member, download_path)
            else:
                with open(download_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        # Check for cancellation
                        if cancellation_checker:
                            if cancellation_checker():
                                raise DownloadCancelledException(f"Download cancelled: {rom_name}")

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
                                        'progress': min(0.8, actual_downloaded / (10 * 1024 * 1024)),
                                        'downloaded': actual_downloaded,
                                        'total': max(actual_downloaded, 1024 * 1024),
                                        'speed': speed,
                                        'eta': 0,
                                        'filename': rom_name
                                    }
                                progress_callback(progress_info)
                
                # After successful download, check if extraction is needed (only for single files)
                if download_path.suffix.lower() == '.zip' and actual_downloaded > 0:
                    should_extract = False
                    
                    # For single files that happen to be zipped, apply heuristics
                    import zipfile
                    try:
                        with zipfile.ZipFile(download_path, 'r') as zip_ref:
                            file_list = zip_ref.namelist()
                            
                            # Only extract if it looks like a directory-based game
                            has_subdirs = any('/' in f and not f.endswith('/') for f in file_list)
                            has_pc_game_files = any(f.lower().endswith(('.exe', '.bat', '.cfg', '.ini', '.dll')) for f in file_list)
                            
                            should_extract = has_subdirs and has_pc_game_files
                            
                    except zipfile.BadZipFile:
                        should_extract = False
                    
                    if should_extract:
                        import zipfile
                        extract_dir = download_path.parent / download_path.stem
                        extract_dir.mkdir(exist_ok=True)
                        with zipfile.ZipFile(download_path, 'r') as zip_ref:
                            zip_ref.extractall(extract_dir)
                        download_path.unlink()

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
                    'filename': rom_name,
                    'completed': True
                }
                progress_callback(final_progress)
            
            return True, f"Download successful ({actual_downloaded} bytes)"

        except DownloadCancelledException as e:
            print(f"Download cancelled: {e}")
            return False, "cancelled"
        except Exception as e:
            print(f"Download exception: {e}")
            return False, f"Download error: {e}"
    
    def download_save(self, rom_id, save_type, download_path, device_id=None):
        """Download the latest save or state file for the given ROM

        Args:
            rom_id: ROM identifier
            save_type: Type of save ('saves' or 'states')
            download_path: Path where to save the downloaded file
            device_id: Optional device ID for optimistic downloads
        """
        if not self.ensure_authenticated():
            return False

        try:
            suffix = download_path.suffix.lower()
            filename = None
            download_url = None
            expected_size = 0
            save_id = None

            # Step 1: Get ROM details to check metadata first
            rom_details_url = urljoin(self.base_url, f"/api/roms/{rom_id}")
            rom_response = self.session.get(rom_details_url, timeout=10)

            if rom_response.status_code == 200:
                rom_data = rom_response.json()
                metadata_key = 'user_saves' if save_type == 'saves' else 'user_states'
                possible_files = rom_data.get(metadata_key, [])

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
                        # Sort by updated_at timestamp (most recent first) and pick the latest
                        def get_timestamp(file_obj):
                            timestamp_str = file_obj.get('updated_at', file_obj.get('created_at', ''))
                            if timestamp_str:
                                return timestamp_str
                            # Fallback to filename sorting if no timestamp
                            return file_obj.get('file_name', '')

                        latest_file = sorted(matching_files, key=get_timestamp, reverse=True)[0]
                        filename = latest_file['file_name']
                        expected_size = latest_file.get('file_size_bytes', 0)
                        save_id = latest_file.get('id')  # Extract save/state ID

                        logging.debug(f"Selected latest file from {len(matching_files)} candidates: ID={save_id}, file={filename}, updated={latest_file.get('updated_at')}, size={expected_size}B")

                        # Use proper /api/saves/{id}/content or /api/states/{id}/content endpoint
                        if save_id:
                            download_url = urljoin(self.base_url, f"/api/{save_type}/{save_id}/content")
                            # Add device_id and optimistic params if provided
                            if device_id:
                                download_url += f"?device_id={device_id}&optimistic=true"
                            logging.debug(f"Using API endpoint: /api/{save_type}/{save_id}/content")
                        elif 'download_path' in latest_file:
                            # Fallback to download_path from metadata if no ID
                            download_url = urljoin(self.base_url, latest_file['download_path'])
                            logging.debug(f"Using metadata download_path fallback")
                        else:
                            logging.warning(f"No save_id or download_path available for {save_type}")
                            return False
                    else:
                        logging.debug(f"No {save_type} files with {suffix} extension found in metadata")
                        return False
                else:
                    logging.debug(f"No {save_type} files found in ROM metadata")
                    return False
            else:
                logging.warning(f"Failed to retrieve ROM metadata: {rom_response.status_code}")
                return False

            # Step 2: Try to download the file with enhanced debugging
            if download_url and filename:
                logging.debug(f"Downloading {filename} from {download_url}")

                # Make request with detailed logging
                download_response = self.session.get(download_url, stream=True, timeout=30)
                used_fallback = False  # Track if we used fallback path

                if download_response.status_code != 200:
                    logging.warning(f"Failed to download {filename}: {download_response.status_code}")
                    logging.debug(f"Response text: {download_response.text[:500]}")

                    # If 404 with device_id, retry without device_id (state may predate device sync)
                    if download_response.status_code == 404 and save_id and device_id:
                        plain_url = urljoin(self.base_url, f"/api/{save_type}/{save_id}/content")
                        logging.debug(f"Retrying without device_id for {filename}")
                        download_response = self.session.get(plain_url, stream=True, timeout=30)
                        if download_response.status_code == 200:
                            download_url = plain_url
                            used_fallback = True  # No device confirmation possible

                    # If still failing, try raw download_path as last resort
                    if download_response.status_code != 200:
                        if save_id and 'download_path' in latest_file:
                            fallback_url = urljoin(self.base_url, latest_file['download_path'])
                            logging.info(f"Trying fallback download_path for {filename}")
                            download_response = self.session.get(fallback_url, stream=True, timeout=30)
                            if download_response.status_code != 200:
                                logging.warning(f"Fallback also failed for {filename}: {download_response.status_code}")
                                return False
                            download_url = fallback_url
                            used_fallback = True
                        else:
                            return False

                # Check content type and headers
                content_type = download_response.headers.get('content-type', 'unknown')
                content_length = download_response.headers.get('content-length')
                
                if content_length:
                    reported_size = int(content_length)
                    logging.debug(f"Server reports content-length: {reported_size} bytes")
                    if expected_size > 0 and abs(reported_size - expected_size) > 1000:
                        logging.warning(f"Size mismatch for {filename}: expected {expected_size}, server reports {reported_size}")

                # Check if we're getting an error response instead of the file
                if 'text/html' in content_type.lower():
                    logging.warning(f"Got HTML response instead of binary file for {filename}")
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
                                    logging.debug(f"Downloaded {actual_bytes} bytes so far...")

                    logging.debug(f"Download completed: {actual_bytes} bytes written to disk")

                    # Verify the download
                    if download_path.exists():
                        file_size = download_path.stat().st_size

                        if file_size != actual_bytes:
                            logging.warning(f"Bytes written ({actual_bytes}) != file size ({file_size}) for {filename}")

                        if expected_size > 0 and abs(file_size - expected_size) > 1000:
                            logging.warning(f"Downloaded size ({file_size}) significantly different from expected ({expected_size}) for {filename}")

                            # Check if it might be a text error response
                            try:
                                with open(download_path, 'rb') as f:
                                    first_bytes = f.read(100)
                                    text_content = first_bytes.decode('utf-8', errors='ignore')
                                    if any(indicator in text_content.lower() for indicator in ['error', 'not found', '404', 'unauthorized', 'html']):
                                        logging.warning(f"File appears to be an error response: {text_content}")
                                        return False
                            except Exception:
                                pass

                        if file_size > 0:
                            # Confirm successful download to server (only if we used the proper API endpoint)
                            if save_id and device_id and not used_fallback:
                                self.confirm_save_downloaded(save_id, save_type, device_id)
                            elif used_fallback:
                                logging.debug(f"Skipping download confirmation (used fallback path)")
                            return True
                        else:
                            logging.warning(f"Downloaded file is empty: {filename}")
                            return False
                    else:
                        logging.warning(f"Downloaded file not found after write: {filename}")
                        return False

                except Exception as write_error:
                    logging.warning(f"Error writing file {filename}: {write_error}")
                    return False
            else:
                logging.warning(f"Could not determine download URL for {save_type}")
                return False

        except Exception as e:
            logging.warning(f"Error downloading {save_type} for ROM {rom_id}: {e}")
            return False

    def confirm_save_downloaded(self, save_id, save_type, device_id):
        """Confirm to the server that a save/state was successfully downloaded by this device

        Args:
            save_id: The ID of the save/state that was downloaded
            save_type: Type of save ('saves' or 'states')
            device_id: The device ID that downloaded the save

        Returns:
            True if confirmation was successful, False otherwise
        """
        if not self.authenticated or not save_id or not device_id:
            return False

        try:
            confirm_url = urljoin(self.base_url, f'/api/{save_type}/{save_id}/downloaded')

            # Send device_id as query parameter or in body
            payload = {'device_id': device_id}

            logging.debug(f"Confirming download of {save_type[:-1]} {save_id} for device {device_id}")

            response = self.session.post(
                confirm_url,
                json=payload,
                timeout=10
            )

            if response.status_code in [200, 201, 204]:
                logging.debug(f"Download confirmation successful for {save_type[:-1]} {save_id}")
                return True
            else:
                logging.warning(f"Download confirmation failed for {save_type[:-1]} {save_id}: HTTP {response.status_code}")
                return False

        except Exception as e:
            logging.warning(f"Error confirming download: {e}")
            return False

    def track_save(self, save_id, save_type, device_id):
        """Re-enable sync tracking for a save/state on this device

        Args:
            save_id: The ID of the save/state to track
            save_type: Type of save ('saves' or 'states')
            device_id: The device ID that should track this save

        Returns:
            True if tracking was enabled successfully, False otherwise
        """
        if not self.authenticated or not save_id or not device_id:
            return False

        try:
            track_url = urljoin(self.base_url, f'/api/{save_type}/{save_id}/track')
            payload = {'device_id': device_id}

            logging.debug(f"Enabling sync tracking for {save_type[:-1]} {save_id} on device {device_id}")

            response = self.session.post(
                track_url,
                json=payload,
                timeout=10
            )

            if response.status_code in [200, 201, 204]:
                logging.debug(f"Sync tracking enabled for {save_type[:-1]} {save_id}")
                return True
            else:
                logging.warning(f"Failed to enable tracking for {save_type[:-1]} {save_id}: HTTP {response.status_code}")
                return False

        except Exception as e:
            logging.warning(f"Error enabling tracking: {e}")
            return False

    def untrack_save(self, save_id, save_type, device_id):
        """Disable sync tracking for a save/state on this device

        Args:
            save_id: The ID of the save/state to stop tracking
            save_type: Type of save ('saves' or 'states')
            device_id: The device ID that should stop tracking this save

        Returns:
            True if tracking was disabled successfully, False otherwise
        """
        if not self.authenticated or not save_id or not device_id:
            return False

        try:
            untrack_url = urljoin(self.base_url, f'/api/{save_type}/{save_id}/untrack')
            payload = {'device_id': device_id}

            logging.debug(f"Disabling sync tracking for {save_type[:-1]} {save_id} on device {device_id}")

            response = self.session.post(
                untrack_url,
                json=payload,
                timeout=10
            )

            if response.status_code in [200, 201, 204]:
                logging.debug(f"Sync tracking disabled for {save_type[:-1]} {save_id}")
                return True
            else:
                logging.warning(f"Failed to disable tracking for {save_type[:-1]} {save_id}: HTTP {response.status_code}")
                return False

        except Exception as e:
            logging.warning(f"Error disabling tracking: {e}")
            return False

    def get_saves_by_device(self, device_id, save_type='saves', rom_id=None, limit=100, slot=None):
        """Get saves/states filtered by device ID

        Args:
            device_id: The device ID to filter by
            save_type: Type of save ('saves' or 'states')
            rom_id: Optional ROM ID to further filter results
            limit: Maximum number of results to return
            slot: Optional slot name to filter by

        Returns:
            List of saves/states for this device, or empty list on error
        """
        if not self.authenticated or not device_id:
            return []

        try:
            params = {
                'device_id': device_id,
                'limit': limit
            }

            if rom_id:
                params['rom_id'] = rom_id
            if slot:
                params['slot'] = slot

            query_url = urljoin(self.base_url, f'/api/{save_type}')

            logging.debug(f"Querying {save_type} for device {device_id}" + (f" (ROM {rom_id})" if rom_id else ""))

            response = self.session.get(
                query_url,
                params=params,
                timeout=10
            )

            if response.status_code == 200:
                data = response.json()

                # Handle both list and dict responses
                if isinstance(data, list):
                    items = data
                elif isinstance(data, dict):
                    items = data.get('items', [])
                else:
                    items = []

                logging.debug(f"Found {len(items)} {save_type} for device")
                return items
            else:
                logging.warning(f"Query {save_type} failed: HTTP {response.status_code}")
                return []

        except Exception as e:
            logging.warning(f"Error querying {save_type}: {e}")
            return []

    def get_saves_summary(self, rom_id, save_type='saves'):
        """Get saves/states summary grouped by slot for a ROM

        Args:
            rom_id: The ROM ID to get saves for
            save_type: Type of save ('saves' or 'states')

        Returns:
            Summary data grouped by slot, or None on error
        """
        if not self.authenticated or not rom_id:
            return None

        try:
            summary_url = urljoin(self.base_url, f'/api/{save_type}/summary')
            params = {'rom_id': rom_id}

            logging.debug(f"Getting {save_type} summary for ROM {rom_id}")

            response = self.session.get(
                summary_url,
                params=params,
                timeout=10
            )

            if response.status_code == 200:
                return response.json()
            else:
                logging.warning(f"Summary query failed: HTTP {response.status_code}")
                return None

        except Exception as e:
            logging.warning(f"Error getting summary: {e}")
            return None

    @staticmethod
    def get_slot_info(file_path):
        """Derive RomM slot name and autocleanup settings from a RetroArch file path.

        Returns:
            (slot, autocleanup, autocleanup_limit) tuple
        """
        import re
        name = Path(file_path).name.lower()

        # Auto save state: .state.auto (Path.suffix returns .auto, so check name)
        if name.endswith('.state.auto'):
            return "auto", True, 5

        suffix = Path(file_path).suffix.lower()

        # Battery saves — no slot, no cleanup
        if suffix in ('.srm', '.sav'):
            return None, False, None

        # Numbered state slots: .state1 through .state9
        match = re.match(r'\.state(\d+)$', suffix)
        if match:
            return f"slot{match.group(1)}", True, 5

        # Quick save state: .state
        if suffix == '.state':
            return "quicksave", True, 10

        return None, False, None

    def upload_save(self, rom_id, save_type, file_path, emulator=None, device_id=None, overwrite=False, slot=None, autocleanup=False, autocleanup_limit=None):
        """Upload save file using RomM naming convention with timestamps"""
        if not self.ensure_authenticated():
            return False

        try:
            file_path = Path(file_path)
            if not file_path.exists():
                logging.warning(f"Upload error: file not found at {file_path}")
                return False

            file_size = file_path.stat().st_size
            logging.debug(f"Uploading {file_path.name} ({file_size} bytes) to ROM {rom_id} as {save_type}")

            # Correct endpoint with rom_id as query parameter
            params = [f'rom_id={rom_id}']
            if emulator:
                params.append(f'emulator={emulator}')
            if device_id:
                params.append(f'device_id={device_id}')
            if overwrite:
                params.append('overwrite=true')
            if slot:
                params.append(f'slot={slot}')
            if autocleanup:
                params.append('autocleanup=true')
                if autocleanup_limit:
                    params.append(f'autocleanup_limit={autocleanup_limit}')
            endpoint = f'/api/{save_type}?' + '&'.join(params)
            upload_url = urljoin(self.base_url, endpoint)
            logging.debug(f"Upload endpoint: {upload_url}")

            # Use correct field names discovered from web interface
            if save_type == 'states':
                file_field_name = 'stateFile'
            elif save_type == 'saves':
                file_field_name = 'saveFile'
            else:
                logging.warning(f"Unknown save type: {save_type}")
                return False

            # Generate RomM-style filename with timestamp
            original_basename = file_path.stem
            file_extension = file_path.suffix

            import datetime
            now = datetime.datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H-%M-%S-%f")[:-3]

            romm_filename = f"{original_basename} [{timestamp}]{file_extension}"
            logging.debug(f"Upload filename: {romm_filename}")

            try:
                with open(file_path, 'rb') as f:
                    # Upload with RomM-style filename
                    files = {file_field_name: (romm_filename, f, 'application/octet-stream')}
                    
                    response = self.session.post(
                        upload_url,
                        files=files,
                        timeout=60
                    )
                
                logging.debug(f"Upload response: {response.status_code}")

                if response.status_code in [200, 201]:
                    try:
                        response_data = response.json()
                        if isinstance(response_data, dict):
                            file_id = response_data.get('id', 'unknown')
                            server_filename = response_data.get('file_name', 'unknown')
                            logging.info(f"Upload accepted ({response.status_code}): id={file_id}, file={server_filename}")
                            return True
                    except Exception as parse_error:
                        logging.debug(f"Upload accepted but response not parseable: {response.text[:200]}")
                        return True

                elif response.status_code == 409:
                    try:
                        error_data = response.json()
                        error_type = error_data.get('error', 'conflict')
                        message = error_data.get('message', 'Save conflict detected')
                        logging.info(f"Upload conflict (409): {message} (type: {error_type})")
                    except:
                        logging.info(f"Upload conflict (409): {response.text[:200]}")
                    return 'conflict'

                elif response.status_code == 422:
                    try:
                        error_data = response.json()
                        logging.warning(f"Upload validation error (422): {error_data}")
                    except:
                        logging.warning(f"Upload validation error (422): {response.text[:300]}")

                elif response.status_code == 400:
                    logging.warning(f"Upload bad request (400): {response.text[:300]}")

                else:
                    logging.warning(f"Upload unexpected status {response.status_code}: {response.text[:200]}")

            except Exception as e:
                logging.error(f"Upload exception: {e}")

            logging.warning(f"Upload failed for {file_path.name}")
            return False

        except Exception as e:
            logging.error(f"Error in upload_save: {e}")
            return False
            
    def upload_save_with_thumbnail(self, rom_id, save_type, file_path, thumbnail_path=None, emulator=None, device_id=None, overwrite=False, slot=None, autocleanup=False, autocleanup_limit=None):
        """Upload save file with optional thumbnail using separate linked uploads"""

        try:
            file_path = Path(file_path)
            if not file_path.exists():
                print(f"Upload error: file not found at {file_path}")
                return False

            # Upload the save state file first and get its ID and server filename
            save_state_id, server_filename = self.upload_save_and_get_id(rom_id, save_type, file_path, emulator, device_id, overwrite, slot, autocleanup, autocleanup_limit)

            # Propagate conflict status
            if save_state_id == 'conflict':
                return 'conflict'

            if not save_state_id:
                return self.upload_save(rom_id, save_type, file_path, emulator, device_id, overwrite, slot, autocleanup, autocleanup_limit)

            # Upload thumbnail and link it to the save state using MATCHING timestamp
            if thumbnail_path and thumbnail_path.exists():
                screenshot_success = self.upload_screenshot_with_matching_timestamp(
                    rom_id, save_state_id, save_type, server_filename, thumbnail_path
                )

                if screenshot_success:
                    return True
                else:
                    return True  # Still consider it successful since save file worked
            else:
                return True

        except Exception as e:
            return self.upload_save(rom_id, save_type, file_path, emulator, device_id, overwrite, slot, autocleanup, autocleanup_limit)

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
            else:
                import datetime
                now = datetime.datetime.now()
                timestamp = now.strftime("%Y-%m-%d %H-%M-%S-%f")[:-3]

            # Extract base name (remove timestamp and extension)
            base_name_match = re.match(r'^(.+?)\s*\[', save_state_filename)
            if base_name_match:
                base_name = base_name_match.group(1).strip()
            else:
                base_name = Path(save_state_filename).stem
                base_name = re.sub(r'\s*\[.*?\]', '', base_name)

            screenshot_filename = f"{base_name} [{timestamp}].png"
            logging.debug(f"Screenshot upload: {screenshot_filename} → state {save_state_id}")

            upload_url = urljoin(self.base_url, f'/api/screenshots?rom_id={rom_id}&state_id={save_state_id}')

            try:
                with open(thumbnail_path, 'rb') as thumb_f:
                    files = {'screenshotFile': (screenshot_filename, thumb_f.read(), 'image/png')}
                    data = {
                        'rom_id': str(rom_id),
                        'state_id': str(save_state_id),
                        'filename': screenshot_filename,
                        'file_name': screenshot_filename,
                    }

                    response = self.session.post(upload_url, files=files, data=data, timeout=30)

                    if response.status_code in [200, 201]:
                        try:
                            response_data = response.json()
                            screenshot_id = response_data.get('id')
                            verification_success = self.verify_screenshot_link(save_state_id, screenshot_id, save_type)
                            if verification_success:
                                logging.info(f"Screenshot linked to state {save_state_id}")
                                return True
                            else:
                                logging.warning(f"Screenshot uploaded but link verification failed")
                                return False
                        except Exception as parse_error:
                            logging.debug(f"Screenshot uploaded but response not parseable: {response.text[:200]}")
                            return True
                    else:
                        logging.warning(f"Screenshot upload failed ({response.status_code}): {response.text[:200]}")
                        return False

            except Exception as upload_error:
                logging.error(f"Screenshot upload error: {upload_error}")
                return False

        except Exception as e:
            logging.error(f"Error in screenshot upload: {e}")
            return False

    def upload_save_and_get_id(self, rom_id, save_type, file_path, emulator=None, device_id=None, overwrite=False, slot=None, autocleanup=False, autocleanup_limit=None):
        try:
            file_path = Path(file_path)

            # Build endpoint with optional parameters
            params = [f'rom_id={rom_id}']
            if emulator:
                params.append(f'emulator={emulator}')
            if device_id:
                params.append(f'device_id={device_id}')
            if overwrite:
                params.append('overwrite=true')
            if slot:
                params.append(f'slot={slot}')
            if autocleanup:
                params.append('autocleanup=true')
                if autocleanup_limit:
                    params.append(f'autocleanup_limit={autocleanup_limit}')

            endpoint = f'/api/{save_type}?' + '&'.join(params)
            upload_url = urljoin(self.base_url, endpoint)
            logging.debug(f"Upload endpoint: {upload_url}")

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
                # Reuse existing server filename for saves
                existing_filename = self.get_existing_save_filename(rom_id, save_type)
                if existing_filename:
                    romm_filename = existing_filename
                    logging.debug(f"Reusing server filename: {romm_filename}")
                else:
                    import datetime
                    now = datetime.datetime.now()
                    timestamp = now.strftime("%Y-%m-%d %H-%M-%S-%f")[:-3]
                    romm_filename = f"{original_basename} [{timestamp}]{file_extension}"
            else:
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
                        _ = response.content
                        response_data = response.json()
                        save_state_id = response_data.get('id')
                        server_filename = response_data.get('file_name', romm_filename)
                        if save_state_id:
                            logging.info(f"Upload accepted ({response.status_code}): id={save_state_id}, file={server_filename}")
                            return save_state_id, server_filename
                        else:
                            logging.warning(f"Upload accepted but no ID in response: {response_data}")
                            return None, None
                    except Exception as e:
                        logging.warning(f"Upload accepted but response parse error: {e}")
                        return None, None

                elif response.status_code == 409:
                    try:
                        error_data = response.json()
                        error_type = error_data.get('error', 'conflict')
                        message = error_data.get('message', 'Save conflict detected')
                        logging.info(f"Upload conflict (409): {message} (type: {error_type})")
                    except:
                        logging.info(f"Upload conflict (409): {response.text[:200]}")
                    return 'conflict', None
                else:
                    logging.warning(f"Upload failed ({response.status_code}): {response.text[:200]}")
                    return None, None

        except Exception as e:
            logging.error(f"Error uploading save: {e}")
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
                        logging.debug(f"Screenshot {screenshot_id} linked to state {save_state_id}")
                        return True
                    else:
                        logging.warning(f"Wrong screenshot linked: expected {screenshot_id}, got {linked_screenshot_id}")
                        return False
                else:
                    logging.debug(f"No screenshot linked to state {save_state_id}")
                    return False
            else:
                logging.debug(f"Could not verify screenshot link: HTTP {response.status_code}")
                return False

        except Exception as e:
            logging.error(f"Error verifying screenshot link: {e}")
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

    def get_platform_bios_list(self, platform_slug):
        """Get available BIOS files for a platform from RomM

        Args:
            platform_slug: Platform slug (e.g., 'sony-playstation')

        Returns:
            List of firmware/BIOS objects with 'id' and 'file_name' fields
        """
        if not self.ensure_authenticated():
            return []

        try:
            # Step 1: Get all platforms to find the platform_id from slug
            platforms_response = self.session.get(
                urljoin(self.base_url, '/api/platforms'),
                timeout=10
            )

            if platforms_response.status_code != 200:
                print(f"Failed to get platforms list: {platforms_response.status_code}")
                return []

            platforms = platforms_response.json()

            # Step 2: Find matching platform by slug and extract ID
            platform_id = None
            for platform in platforms:
                if platform.get('slug') == platform_slug:
                    platform_id = platform.get('id')
                    print(f"✓ Found platform '{platform_slug}' with ID: {platform_id}")
                    break

            if not platform_id:
                print(f"❌ Platform not found: {platform_slug}")
                return []

            # Step 3: Get firmware list using platform_id (integer)
            response = self.session.get(
                urljoin(self.base_url, '/api/firmware'),
                params={'platform_id': platform_id},  # Use platform_id instead of platform slug
                timeout=10
            )
            
            if response.status_code == 200:
                return response.json()
                
        except Exception as e:
            print(f"Error fetching BIOS list: {e}")
        
        return []
    
    def download_bios_file(self, bios_id, file_name, download_path, progress_callback=None):
        """Download a BIOS file from RomM

        Args:
            bios_id: Firmware ID from RomM
            file_name: Filename of the BIOS file (e.g., 'scph5500.bin')
            download_path: Path where to save the downloaded file
            progress_callback: Optional callback for progress updates

        Returns:
            True on success, False on failure
        """
        if not self.ensure_authenticated():
            return False

        try:
            # Use correct endpoint: /api/firmware/{firmware_id}/content/{file_name}
            response = self.session.get(
                urljoin(self.base_url, f'/api/firmware/{bios_id}/content/{file_name}'),
                stream=True,
                timeout=30
            )
            
            if response.status_code == 200:
                total_size = int(response.headers.get('content-length', 0))
                downloaded = 0
                
                with open(download_path, 'wb') as f:
                    for chunk in response.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
                            downloaded += len(chunk)
                            
                            if progress_callback and total_size > 0:
                                progress = downloaded / total_size
                                progress_callback({
                                    'progress': progress,
                                    'downloaded': downloaded,
                                    'total': total_size
                                })
                
                return True
                
        except Exception as e:
            print(f"BIOS download error: {e}")
        
        return False
    
    def search_bios_files(self, filename):
        """Search for a specific BIOS file on RomM server"""
        try:
            # Search firmware/BIOS files
            response = self.session.get(
                urljoin(self.base_url, '/api/search'),
                params={'q': filename, 'type': 'firmware'},
                timeout=10
            )
            
            if response.status_code == 200:
                results = response.json()
                for result in results:
                    if result.get('filename', '').lower() == filename.lower():
                        return result
                        
        except Exception as e:
            print(f"BIOS search error: {e}")
        
        return None

class RetroArchInterface:
    """Interface for RetroArch network commands and file monitoring"""
    
    def __init__(self, settings=None):
        self.settings = settings
        self.settings = SettingsManager()
        self.save_dirs = self.find_retroarch_dirs()

        self.bios_manager = None
        self._init_bios_manager()

        # Cache for RetroDECK detection
        self._is_retrodeck_cache = None

        # Check for custom path override first
        custom_path = self.settings.get('RetroArch', 'custom_path', '').strip()

        if custom_path and Path(custom_path).exists():
            self.retroarch_executable = custom_path
            print(f"🎮 Using custom RetroArch path: {custom_path}")
            
            # ALSO CHECK FOR CORES RELATIVE TO CUSTOM PATH
            custom_config_dir = Path(custom_path).parent
            if (custom_config_dir / 'config/retroarch').exists():
                custom_config_dir = custom_config_dir / 'config/retroarch'
            custom_cores_dir = custom_config_dir / 'cores'
            if custom_cores_dir.exists():
                self.cores_dir = custom_cores_dir
                print(f"🔧 Using custom cores directory: {custom_cores_dir}")
            else:
                self.cores_dir = self.find_cores_directory()
        else:
            self.retroarch_executable = self.find_retroarch_executable()
            self.cores_dir = self.find_cores_directory()

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
            'Nintendo DS': ['desmume', 'melonds'],
            'Nintendo - Nintendo DS': ['desmume', 'melonds'],
            'nds': ['desmume', 'melonds'], 
            'Sega Saturn': ['beetle_saturn', 'kronos'],
            'Arcade': ['mame', 'fbneo', 'fbalpha'],
            'PlayStation 2': ['pcsx2', 'play'],
            'Nintendo GameCube': ['dolphin'],
            'Sega Dreamcast': ['flycast', 'redream'],
            'Atari 2600': ['stella'],
            'Sony - PlayStation': ['beetle_psx', 'beetle_psx_hw', 'pcsx_rearmed', 'swanstation'],
            'Sony - PlayStation 2': ['pcsx2', 'play'],
            'Sony - PlayStation Portable': ['ppsspp'],
            'Nintendo - Nintendo 3DS': ['citra'],
            'Nintendo - Game Boy': ['gambatte', 'sameboy', 'tgbdual'],
            'Nintendo - Game Boy Color': ['gambatte', 'sameboy', 'tgbdual'],
            'Nintendo - Game Boy Advance': ['mgba', 'vba_next', 'vbam'],
            'Nintendo - Nintendo Entertainment System': ['nestopia', 'fceumm', 'mesen'],
            'Nintendo - Super Nintendo Entertainment System': ['snes9x', 'bsnes', 'mesen-s'],
            'Nintendo - Nintendo 64': ['mupen64plus_next', 'parallel_n64'],
            'Nintendo - GameCube': ['dolphin'],
            'Sega - Genesis': ['genesis_plus_gx', 'blastem', 'picodrive'],
            'Sega - Mega Drive': ['genesis_plus_gx', 'blastem', 'picodrive'],
            'Sega - Saturn': ['beetle_saturn', 'kronos'],
            'Sega - Dreamcast': ['flycast', 'redream'],
            'Sega - Mega-CD': ['genesis_plus_gx', 'picodrive'],
            'Sega - CD': ['genesis_plus_gx', 'picodrive'],
            'SNK - Neo Geo': ['fbneo', 'mame'],
            'NEC - PC Engine': ['beetle_pce', 'beetle_pce_fast'],
            'NEC - TurboGrafx-16': ['beetle_pce', 'beetle_pce_fast'],
            'Atari - 2600': ['stella'],
            'Atari - 7800': ['prosystem'],
            'Atari - Lynx': ['handy', 'beetle_lynx'],
            '3DO': ['opera', '4do'],
            'Microsoft - MSX': ['bluemsx', 'fmsx'],
            'Commodore - Amiga': ['puae', 'fsuae'],
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

    def _init_bios_manager(self):
        """Initialize BIOS manager"""
        try:
            from bios_manager import BiosManager
            self.bios_manager = BiosManager(
                retroarch_interface=self,
                romm_client=None,  # Will be set when connected
                log_callback=lambda msg: print(f"[BIOS] {msg}"),
                settings=self.settings  # Pass the main settings instance
            )
        except ImportError as e:
            print(f"⚠️ BIOS manager not available: {e}")
            self.bios_manager = None

    def check_game_bios_requirements(self, game):
        """Check if a game has all required BIOS files"""
        if not self.bios_manager:
            return True  # Assume OK if no BIOS manager
        
        platform = game.get('platform', '')
        present, missing = self.bios_manager.check_platform_bios(platform)
        
        # Filter to only required files
        required_missing = [b for b in missing if not b.get('optional', False)]
        
        return len(required_missing) == 0

    def launch_game_retrodeck(self, rom_path):
        """Launch game through RetroDECK (which handles core selection automatically)"""
        try:
            import subprocess
            
            # RetroDECK methods to try (in order of preference)
            commands_to_try = [
                ['flatpak', 'run', 'net.retrodeck.retrodeck', str(rom_path)],
                ['flatpak', 'run', 'net.retrodeck.retrodeck', '--pass-args', str(rom_path)],
                ['flatpak', 'run', 'net.retrodeck.retrodeck', '--run', str(rom_path)]
            ]
            
            for cmd in commands_to_try:
                print(f"🎮 Trying RetroDECK command: {' '.join(cmd)}")

                result = subprocess.Popen(cmd,
                                        stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE,
                                        text=True)

                time.sleep(3)  # Wait to see if it fails immediately

                poll = result.poll()
                if poll is None:
                    # Still running — success
                    return True, f"Launched via RetroDECK: {rom_path.name}"
                elif poll == 0:
                    # Exited with success — flatpak launcher exits after spawning the emulator
                    return True, f"Launched via RetroDECK: {rom_path.name}"
                else:
                    stdout, stderr = result.communicate()
                    print(f"❌ Command failed (exit code {poll}): {stderr[:200]}")
                    continue
            
            return False, "All RetroDECK launch methods failed"
            
        except Exception as e:
            return False, f"RetroDECK launch error: {e}"

    def find_retroarch_executable(self):
        """Find RetroArch executable with comprehensive installation support"""
        import shutil
        import subprocess
        from pathlib import Path
        
        retroarch_candidates = []

        # Method 1: Flatpak - REPLACE THE RETRODECK PART
        try:
            result = subprocess.run(['flatpak', 'list'], capture_output=True, text=True)
            # Check for RetroDECK first
            if 'net.retrodeck.retrodeck' in result.stdout:
                retroarch_candidates.append({
                    'type': 'retrodeck',
                    'command': 'flatpak run net.retrodeck.retrodeck',  # Remove 'retroarch'
                    'priority': 2
                })
            elif 'org.libretro.RetroArch' in result.stdout:
                retroarch_candidates.append({
                    'type': 'flatpak',
                    'command': 'flatpak run org.libretro.RetroArch', 
                    'priority': 3
                })
        except:
            pass
        
        # Method 2: Steam installation
        steam_paths = [
            Path.home() / '.steam/steam/steamapps/common/RetroArch/retroarch',
            Path.home() / '.local/share/Steam/steamapps/common/RetroArch/retroarch',
            Path('/usr/games/retroarch'),
            Path.home() / '.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/RetroArch/retroarch',
            Path.home() / '.var/app/com.valvesoftware.Steam/home/.local/share/Steam/steamapps/common/RetroArch/retroarch',
        ]
        
        for steam_path in steam_paths:
            if steam_path.exists() and steam_path.is_file():
                retroarch_candidates.append({
                    'type': 'steam',
                    'command': str(steam_path),
                    'priority': 2
                })
                break
        
        # Method 3: Native package installations
        native_paths = [
            '/usr/bin/retroarch',
            '/usr/local/bin/retroarch',
            '/opt/retroarch/bin/retroarch',
        ]
        
        for path in native_paths:
            if shutil.which(path):
                retroarch_candidates.append({
                    'type': 'native',
                    'command': path,
                    'priority': 1  # Highest priority
                })
                break
        
        # Method 4: Snap package
        try:
            result = subprocess.run(['snap', 'list', 'retroarch'], capture_output=True, text=True)
            if result.returncode == 0:
                retroarch_candidates.append({
                    'type': 'snap',
                    'command': 'snap run retroarch',
                    'priority': 4
                })
        except:
            pass
        
        # Method 5: AppImage (check common locations)
        appimage_locations = [
            Path.home() / 'Applications',
            Path.home() / 'Downloads',
            Path.home() / '.local/bin',
            Path('/opt'),
        ]
        
        for location in appimage_locations:
            if location.exists():
                for appimage in location.glob('*RetroArch*.AppImage'):
                    if appimage.is_file() and os.access(appimage, os.X_OK):
                        # Skip our own app
                        if 'RomM-RetroArch-Sync' in appimage.name:
                            continue
                        retroarch_candidates.append({
                            'type': 'appimage', 
                            'command': str(appimage),
                            'priority': 5
                        })
        
        # Method 6: Generic PATH search
        path_command = shutil.which('retroarch')
        if path_command and not any(c['command'] == path_command for c in retroarch_candidates):
            retroarch_candidates.append({
                'type': 'path',
                'command': path_command,
                'priority': 6
            })
        
        # Select best candidate (lowest priority number = highest priority)
        if retroarch_candidates:
            best_candidate = min(retroarch_candidates, key=lambda x: x['priority'])
            print(f"🎮 Selected RetroArch: {best_candidate['type']} - {best_candidate['command']}")
            return best_candidate['command']
        
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

    def detect_core_from_state_file(self, state_path):
        """Detect core from save state file content"""
        try:
            with open(state_path, 'rb') as f:
                header = f.read(64)  # Read first 64 bytes
            
            # Known signatures
            if b'SNES9X' in header:
                return 'snes9x'
            elif b'FCEU' in header:
                return 'fceumm'
            elif b'mGBA' in header:
                return 'mgba'
            elif b'BEETLE' in header:
                return 'beetle_psx'
            # Add more signatures as needed
            
        except:
            pass
        
        return None

    def suggest_core_for_platform(self, platform_name):
        """Suggest best core for a platform"""
        available_cores = self.get_available_cores()

        print(f"🎮 Looking for core for platform: '{platform_name}'")

        # Try exact match first
        suggested_cores = self.platform_core_map.get(platform_name, [])

        # Find first available suggested core
        for core in suggested_cores:
            if core in available_cores:
                print(f"✅ Found exact match core: {core}")
                return core, available_cores[core]

        # Try fuzzy matching if exact match fails
        platform_lower = platform_name.lower()

        # Enhanced keyword mapping for better platform detection
        platform_keywords = {
            'n64': ['mupen64plus_next', 'parallel_n64'],
            'nintendo 64': ['mupen64plus_next', 'parallel_n64'],
            'snes': ['snes9x', 'bsnes', 'mesen-s'],
            'super nintendo': ['snes9x', 'bsnes', 'mesen-s'],
            'nes': ['nestopia', 'fceumm', 'mesen'],
            'nintendo entertainment': ['nestopia', 'fceumm', 'mesen'],
            'game boy advance': ['mgba', 'vba_next', 'vbam'],
            'gba': ['mgba', 'vba_next', 'vbam'],
            'game boy color': ['gambatte', 'sameboy', 'tgbdual'],
            'gbc': ['gambatte', 'sameboy', 'tgbdual'],
            'game boy': ['gambatte', 'sameboy', 'tgbdual'],
            'gb': ['gambatte', 'sameboy', 'tgbdual'],
            'playstation 2': ['pcsx2', 'play'],
            'ps2': ['pcsx2', 'play'],
            'playstation': ['beetle_psx', 'beetle_psx_hw', 'mednafen_psx_hw', 'mednafen_psx', 'pcsx_rearmed', 'swanstation'],
            'psx': ['beetle_psx', 'beetle_psx_hw', 'mednafen_psx_hw', 'mednafen_psx', 'pcsx_rearmed', 'swanstation'],
            'ps1': ['beetle_psx', 'beetle_psx_hw', 'mednafen_psx_hw', 'mednafen_psx', 'pcsx_rearmed', 'swanstation'],
            'genesis': ['genesis_plus_gx', 'blastem', 'picodrive'],
            'mega drive': ['genesis_plus_gx', 'blastem', 'picodrive'],
            'nintendo ds': ['desmume', 'melonds'],
            'nds': ['desmume', 'melonds'],
        }

        # Try keyword matching
        for keyword, cores in platform_keywords.items():
            if keyword in platform_lower:
                for core in cores:
                    if core in available_cores:
                        print(f"✅ Found fuzzy match core: {core} (matched keyword: {keyword})")
                        return core, available_cores[core]

        print(f"❌ No suitable core found for platform: {platform_name}")
        print(f"Available cores: {list(available_cores.keys())}")
        return None, None

    def launch_game(self, rom_path, platform_name=None, core_name=None):
        """Launch a game in RetroArch with multi-installation support"""
        if not self.retroarch_executable:
            return False, "RetroArch executable not found"
        
        # Special handling for RetroDECK
        if 'retrodeck' in self.retroarch_executable.lower():
            return self.launch_game_retrodeck(rom_path)
    
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
            
            # Build command based on RetroArch type - REPLACE THIS SECTION
            if 'retrodeck' in self.retroarch_executable.lower():
                # RetroDECK launches games differently - try multiple approaches
                cmd = ['flatpak', 'run', 'net.retrodeck.retrodeck', '--pass-args', str(rom_path)]
            elif 'flatpak' in self.retroarch_executable:
                cmd = ['flatpak', 'run', 'org.libretro.RetroArch', '-L', core_path, str(rom_path)]
            elif 'snap' in self.retroarch_executable:
                cmd = ['snap', 'run', 'retroarch', '-L', core_path, str(rom_path)]
            else:
                cmd = [self.retroarch_executable, '-L', core_path, str(rom_path)]
            
            logging.debug(f"Launching: {' '.join(cmd)}")
            logging.debug(f"ROM path exists: {os.path.exists(rom_path)}, Core path exists: {os.path.exists(core_path)}")

            # Launch RetroArch with debugging (don't capture output to see what happens)
            result = subprocess.Popen(cmd,
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE,
                                    text=True)

            # Wait a moment to see if it fails immediately
            import time
            time.sleep(2)

            if result.poll() is not None:
                # Process has already exited
                stdout, stderr = result.communicate()
                error_msg = f"Launch failed immediately.\n"
                error_msg += f"Exit code: {result.returncode}\n"
                error_msg += f"Command: {' '.join(cmd)}\n"
                if stdout:
                    error_msg += f"STDOUT: {stdout[:500]}\n"
                if stderr:
                    error_msg += f"STDERR: {stderr[:500]}\n"
                if not stdout and not stderr:
                    error_msg += "No output from process. This could mean:\n"
                    error_msg += "1. RetroArch/core path is incorrect\n"
                    error_msg += "2. Missing library dependencies\n"
                    error_msg += "3. Permission issues\n"
                    error_msg += f"Try running manually: {' '.join(cmd)}"
                print(error_msg)
                return False, error_msg

            return True, f"Launched {rom_path.name} with {core_name} core"
            
        except Exception as e:
            return False, f"Launch error: {e}"

    def send_notification(self, message):
        """Send notification to RetroArch using SHOW_MSG command"""
        try:
            # Use SHOW_MSG instead of NOTIFICATION
            command = f'SHOW_MSG {message}'
            logging.debug(f"Sending RetroArch notification: {message}")
            
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(1.0)
            
            message_bytes = command.encode('utf-8')
            sock.sendto(message_bytes, (self.host, self.port))
            sock.close()
            
            logging.debug(f"RetroArch notification sent")
            return True
            
        except Exception as e:
            print(f"❌ Failed to send RetroArch notification: {e}")
            return False
    
    def find_retroarch_dirs(self):
        """Find RetroArch save directories with comprehensive installation support"""
        save_dirs = {}
        
        # All possible RetroArch config locations (ordered by likelihood)
        possible_dirs = [
        
            # RetroDECK
            Path.home() / 'retrodeck',

            # Flatpak
            Path.home() / '.var/app/org.libretro.RetroArch/config/retroarch',

            # Native/Steam installations
            Path.home() / '.config/retroarch',
            Path.home() / '/.retroarch',
            
            # Steam specific locations
            Path.home() / '.steam/steam/steamapps/common/RetroArch',
            Path.home() / '.local/share/Steam/steamapps/common/RetroArch',

            Path.home() / '.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/RetroArch',
            Path.home() / '.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/RetroArch/config',
            
            # Snap
            Path.home() / 'snap/retroarch/current/.config/retroarch',
            
            # AppImage (usually creates config in user dir)
            Path.home() / '.retroarch-appimage',
            
            # System-wide installations
            Path('/etc/retroarch'),
            Path('/usr/local/etc/retroarch'),
        ]
        
        for base_dir in possible_dirs:
            if base_dir.exists():
                # RetroDECK uses different structure
                if 'retrodeck' in str(base_dir) and base_dir.name == 'retrodeck':
                    saves_dir = base_dir / 'saves'
                    states_dir = base_dir / 'states'
                else:
                    # Standard RetroArch structure
                    saves_dir = base_dir / 'saves'
                    states_dir = base_dir / 'states'
                
                if saves_dir.exists():
                    save_dirs['saves'] = saves_dir
                if states_dir.exists():
                    save_dirs['states'] = states_dir
                    
                # If we found both or either, we're done
                if save_dirs:
                    print(f"📁 Found RetroArch save dirs: {base_dir}")
                    print(f"   Saves: {save_dirs.get('saves', 'Not found')}")
                    print(f"   States: {save_dirs.get('states', 'Not found')}")
                    break
        
        return save_dirs

    def find_retroarch_config_dir(self):
        """Find RetroArch config directory for the detected installation"""
        # Check for custom path override first
        custom_path = self.settings.get('RetroArch', 'custom_path', '').strip()
        if custom_path and Path(custom_path).exists():
            custom_config_dir = Path(custom_path).parent
            if (custom_config_dir / 'config/retroarch').exists():
                custom_config_dir = custom_config_dir / 'config/retroarch'
            if custom_config_dir.exists():
                print(f"🔧 Using custom config directory: {custom_config_dir}")
                return custom_config_dir
        
        # Standard detection logic
        possible_dirs = [
            # RetroDECK (correct path)
            Path.home() / '.var/app/net.retrodeck.retrodeck/config/retroarch',
            Path.home() / 'retrodeck',
            # Flatpak
            Path.home() / '.var/app/org.libretro.RetroArch/config/retroarch',
            # Native/Steam installations
            Path.home() / '.config/retroarch',
            Path.home() / '/.retroarch',
            # Steam specific locations
            Path.home() / '.steam/steam/steamapps/common/RetroArch',
            Path.home() / '.local/share/Steam/steamapps/common/RetroArch',
            # Snap
            Path.home() / 'snap/retroarch/current/.config/retroarch',
            # AppImage
            Path.home() / '.retroarch-appimage',
        ]
        
        for config_dir in possible_dirs:
            if config_dir.exists():
                return config_dir
        return None

    def is_retrodeck_installation(self):
        """Enhanced RetroDECK detection using multiple methods (cached)"""
        # Return cached result if available
        if self._is_retrodeck_cache is not None:
            return self._is_retrodeck_cache

        # Method 1: Check executable command
        if self.retroarch_executable and 'retrodeck' in str(self.retroarch_executable).lower():
            self._is_retrodeck_cache = True
            return True

        # Method 2: Check if RetroDECK directories exist
        retrodeck_paths = [
            Path.home() / 'retrodeck',
            Path.home() / '.var/app/net.retrodeck.retrodeck'
        ]

        for path in retrodeck_paths:
            if path.exists():
                self._is_retrodeck_cache = True
                return True

        # Method 3: Check save directories for RetroDECK structure
        for save_type, directory in self.save_dirs.items():
            if 'retrodeck' in str(directory).lower():
                self._is_retrodeck_cache = True
                return True

        # Method 4: Check Flatpak list
        try:
            import subprocess
            result = subprocess.run(['flatpak', 'list'], capture_output=True, text=True, timeout=5)
            if 'net.retrodeck.retrodeck' in result.stdout:
                print("🔍 RetroDECK detected via Flatpak list")
                self._is_retrodeck_cache = True
                return True
        except:
            pass

        # Cache negative result
        self._is_retrodeck_cache = False
        return False

    def get_core_from_platform_slug(self, platform_slug):
        """Map RomM platform slugs to RetroArch cores"""
        platform_to_core_map = {
            'snes': 'snes9x',
            'nes': 'nestopia',
            'gba': 'mgba',
            'gbc': 'sameboy',
            'gb': 'sameboy',
            'psx': 'beetle_psx_hw',
            'genesis': 'genesis_plus_gx',
            'n64': 'mupen64plus_next',
            'saturn': 'beetle_saturn',
            'arcade': 'mame',
            'mame': 'mame',
            'fbneo': 'fbneo',
            'atari2600': 'stella',
        }
        return platform_to_core_map.get(platform_slug.lower(), platform_slug)

    def detect_save_folder_structure(self):
        """Detect if saves use core names or platform slugs by examining actual folders"""
        folder_types = {'core_names': 0, 'platform_slugs': 0}
        
        for save_type, directory in self.save_dirs.items():
            if not directory.exists():
                continue
                
            for subdir in directory.iterdir():
                if subdir.is_dir():
                    folder_name = subdir.name.lower()
                    
                    # Expanded core name patterns to match RetroArch core folder names
                    core_patterns = [
                        'snes9x', 'beetle', 'mgba', 'nestopia', 'gambatte', 'fceumm',
                        'genesis plus gx', 'plus gx', 'genesis_plus_gx',  # Genesis Plus GX variants
                        'mupen64plus', 'parallel n64', 'blastem', 'picodrive',
                        'pcsx rearmed', 'swanstation', 'flycast', 'redream',
                        'stella', 'handy', 'prosystem', 'vecx', 'o2em'
                    ]
                    
                    # Check for known core name patterns
                    if any(core in folder_name for core in core_patterns):
                        folder_types['core_names'] += 1
                    # Check for platform slug patterns (short names)
                    elif any(platform in folder_name for platform in ['snes', 'nes', 'gba', 'psx', 'genesis', 'megadrive', 'n64']):
                        folder_types['platform_slugs'] += 1
        
        # Return the dominant pattern
        if folder_types['core_names'] > folder_types['platform_slugs']:
            return 'core_names'
        elif folder_types['platform_slugs'] > 0:
            return 'platform_slugs'
        else:
            return 'unknown'

    def get_emulator_info_from_path(self, file_path):
        """Enhanced emulator detection that handles both folder structures"""
        file_path = Path(file_path)
        
        # DEBUG: Show detection info (use print instead of self.log)
        is_retrodeck = self.is_retrodeck_installation()

        if file_path.parent.name in ['saves', 'states']:
            return {
                'directory_name': None,
                'retroarch_emulator': None,
                'romm_emulator': None,
                'folder_structure': 'root'
            }
        
        directory_name = file_path.parent.name
        folder_structure = self.detect_save_folder_structure()
        is_retrodeck = self.is_retrodeck_installation()
        
        if folder_structure == 'platform_slugs':
            # Using RomM platform slugs
            retroarch_emulator = directory_name
            romm_emulator = self.get_core_from_platform_slug(directory_name)
        else:
            # Using RetroArch core names
            retroarch_emulator = directory_name
            romm_emulator = self.get_romm_emulator_name(directory_name)
        
        return {
            'directory_name': directory_name,
            'retroarch_emulator': retroarch_emulator,
            'romm_emulator': romm_emulator,
            'folder_structure': folder_structure,
            'is_retrodeck': is_retrodeck
        }

    def find_cores_directory(self):
        """Find RetroArch cores directory with comprehensive installation support"""
        possible_dirs = [
            # Flatpak
            Path.home() / '.var/app/org.libretro.RetroArch/config/retroarch/cores',
            
            # RetroDECK
            Path.home() / '.var/app/net.retrodeck.retrodeck/config/retroarch/cores',

            # Native installations
            Path.home() / '.config/retroarch/cores',
            Path('/usr/lib/libretro'),
            Path('/usr/local/lib/libretro'),
            Path('/usr/lib/x86_64-linux-gnu/libretro'),
            
            # Steam installations
            Path.home() / '.steam/steam/steamapps/common/RetroArch/cores',
            Path.home() / '.local/share/Steam/steamapps/common/RetroArch/cores',
            
            # Snap
            Path('/snap/retroarch/current/usr/lib/libretro'),
            
            # AppImage bundled cores
            Path.home() / '.retroarch-appimage/cores',

            Path.home() / '.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/RetroArch/cores',
            Path.home() / '.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/RetroArch/info',
        ]
        
        for cores_dir in possible_dirs:
            if cores_dir.exists() and any(cores_dir.glob('*.so')):
                print(f"🔧 Found cores directory: {cores_dir}")
                return cores_dir
        
        return None

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

    def get_romm_emulator_name(self, retroarch_directory_name):
        """Convert RetroArch directory name to RomM emulator name using standard convention"""
        # RomM naming convention: lowercase + replace hyphens/spaces with underscores
        romm_name = retroarch_directory_name.lower().replace(' ', '_').replace('-', '_')
        return romm_name

    def convert_to_retroarch_filename(self, original_filename, save_type, target_directory, slot=None):
        """
        Convert RomM filename with timestamp to RetroArch expected format
        """
        import re
        from pathlib import Path

        # Extract the base filename by removing timestamp brackets
        # Pattern matches: [YYYY-MM-DD HH-MM-SS-mmm] or similar timestamp formats
        timestamp_pattern = r'\s*\[[\d\-\s:]+\]'

        # Handle .state.auto compound extension (Path.stem strips only .auto)
        if original_filename.lower().endswith('.state.auto'):
            # Strip .state.auto, then remove timestamp from the game name
            game_name = original_filename[:-len('.state.auto')]
            game_name = re.sub(timestamp_pattern, '', game_name)
            return f"{game_name}.state.auto"

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
            # If slot is "auto", produce .state.auto
            if slot == 'auto':
                target_filename = f"{base_name}.state.auto"
            else:
                # For save states, determine the slot from existing files
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
        
        # Remove state slot numbers (.state1, .state2, etc.)
        import re
        game_name = re.sub(r'\.state\d*$', '', base_name)
        
        # Possible thumbnail locations (UPDATED - prioritize same directory)
        possible_thumbnails = [
            # SAME DIRECTORY - Multiple naming patterns for RetroDECK compatibility
            state_path.with_name(state_path.name + '.png'),           # "game.state" -> "game.state.png" 
            state_path.with_suffix('.png'),                           # "game.state" -> "game.png"
            state_path.parent / f"{game_name}.png",                   # Same dir, base game name
            state_path.parent / f"{base_name}.png",                   # Same dir, full stem
            state_path.with_name(state_path.stem + '_screenshot.png'), # "game.state" -> "game_screenshot.png"
            state_path.with_name(game_name + '_thumb.png'),           # RetroDECK style naming
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
                    logging.debug(f"Found thumbnail: {thumbnail_path} ({file_size} bytes)")
                    return thumbnail_path
                else:
                    logging.debug(f"Found empty thumbnail file: {thumbnail_path}")
            else:
                # Debug: Show first few failed attempts
                pass
        
        return None

    def check_network_commands_config(self):
        """Check if RetroArch network commands are properly configured"""
        try:
            config_dir = self.find_retroarch_config_dir()
            if not config_dir:
                return False, "Config directory not found"

            config_file = config_dir / 'retroarch.cfg'
            if not config_file.exists():
                return False, "retroarch.cfg not found"

            network_enabled = False
            network_port = None

            with open(config_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('network_cmd_enable = '):
                        network_enabled = 'true' in line.lower()
                    elif line.startswith('network_cmd_port = '):
                        try:
                            network_port = int(line.split('=')[1].strip().strip('"'))
                        except:
                            pass

            if not network_enabled:
                return False, "Network commands disabled in RA"
            elif network_port != 55355:
                return False, f"Wrong port: {network_port} (should be 55355)"
            else:
                return True, "Network commands enabled (port 55355)"

        except Exception as e:
            return False, f"Config check failed: {e}"

    def check_savestate_thumbnail_config(self):
        """Check if RetroArch save state thumbnails are enabled"""
        try:
            config_dir = self.find_retroarch_config_dir()
            if not config_dir:
                return False, "Config directory not found"

            config_file = config_dir / 'retroarch.cfg'
            if not config_file.exists():
                return False, "retroarch.cfg not found"

            thumbnail_enabled = False

            with open(config_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith('savestate_thumbnail_enable = '):
                        thumbnail_enabled = 'true' in line.lower()
                        break

            if not thumbnail_enabled:
                return False, "Save state thumbnails disabled in RA"
            else:
                return True, "Save state thumbnails enabled"

        except Exception as e:
            return False, f"Config check failed: {e}"

    def enable_retroarch_setting(self, setting_type):
        """Enable a specific RetroArch setting by modifying retroarch.cfg

        Args:
            setting_type: Either 'network_commands' or 'savestate_thumbnails'

        Returns:
            (success: bool, message: str)
        """
        try:
            config_dir = self.find_retroarch_config_dir()
            if not config_dir:
                return False, "Config directory not found"

            config_file = config_dir / 'retroarch.cfg'
            if not config_file.exists():
                return False, "retroarch.cfg not found"

            # Read the entire config file
            with open(config_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            # Track which settings we found and modified
            modified = False
            setting_found = False

            if setting_type == 'network_commands':
                # Modify network_cmd_enable and network_cmd_port
                network_enable_found = False
                network_port_found = False

                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped.startswith('network_cmd_enable = '):
                        lines[i] = 'network_cmd_enable = "true"\n'
                        network_enable_found = True
                        modified = True
                    elif stripped.startswith('network_cmd_port = '):
                        lines[i] = 'network_cmd_port = "55355"\n'
                        network_port_found = True
                        modified = True

                # If settings don't exist, append them
                if not network_enable_found:
                    lines.append('network_cmd_enable = "true"\n')
                    modified = True
                if not network_port_found:
                    lines.append('network_cmd_port = "55355"\n')
                    modified = True

                setting_found = True

            elif setting_type == 'savestate_thumbnails':
                # Modify savestate_thumbnail_enable
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped.startswith('savestate_thumbnail_enable = '):
                        lines[i] = 'savestate_thumbnail_enable = "true"\n'
                        setting_found = True
                        modified = True
                        break

                # If setting doesn't exist, append it
                if not setting_found:
                    lines.append('savestate_thumbnail_enable = "true"\n')
                    setting_found = True
                    modified = True

            else:
                return False, f"Unknown setting type: {setting_type}"

            if not modified:
                return True, "Setting already enabled"

            # Write the modified config back
            with open(config_file, 'w', encoding='utf-8') as f:
                f.writelines(lines)

            if setting_type == 'network_commands':
                return True, "Network commands enabled (restart RetroArch to apply)"
            elif setting_type == 'savestate_thumbnails':
                return True, "Save state thumbnails enabled (restart RetroArch to apply)"

        except Exception as e:
            return False, f"Failed to enable setting: {e}"

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
        self.last_uploaded = {}  # file_path -> (size, mtime) of last successful upload
        
        # Game session tracking
        self.current_game = None
        self.last_sync_time = {}  # game_id -> timestamp
        self.should_stop = threading.Event()
        
        # Upload worker thread
        self.upload_worker = None

        # Add these new attributes at the end
        self.retroarch_monitor = None
        self.current_retroarch_game = None
        self.retroarch_running = False

        # Add lock mechanism
        self.lock = AutoSyncLock()
        self.instance_id = f"{'gui' if parent_window else 'daemon'}_{os.getpid()}"

    def start_auto_sync(self):
        """Start all auto-sync components"""
        if self.enabled:
            self.log("Auto-sync already running")
            return
        
        # Try to acquire lock
        if not self.lock.acquire(self.instance_id):
            self.log("⚠️ Auto-sync blocked - another instance is already running")
            return
            
        self.enabled = True
        self.should_stop.clear()
        
        try:
            # Start file system monitoring
            self.start_file_monitoring()
            
            # Start upload worker
            self.start_upload_worker()
            
            self.start_retroarch_monitoring()
            self.start_playlist_monitoring()
            
            self.log("🔄 Auto-sync started (file monitoring + RetroArch + playlist monitoring)")
            
        except Exception as e:
            self.log(f"❌ Failed to start auto-sync: {e}")
            self.stop_auto_sync()

    def stop_auto_sync(self):
        """Stop all auto-sync components"""
        if not self.enabled:
            return
            
        self.enabled = False
        self.should_stop.set()
        
        # Release lock
        self.lock.release()
        
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

        # Skip file events for the first 5 seconds to avoid uploading existing saves on startup
        self.startup_time = time.time()
        self.startup_grace_period = 5

        self.observer = Observer()
        
        for save_type, directory in self.retroarch.save_dirs.items():
            # Create directory if it doesn't exist
            try:
                directory.mkdir(parents=True, exist_ok=True)
                handler = SaveFileHandler(self.on_save_file_changed, save_type)
                self.observer.schedule(handler, str(directory), recursive=True)
                self.log(f"📁 Monitoring {save_type}: {directory}")
            except Exception as e:
                self.log(f"❌ Failed to create/monitor {save_type} directory {directory}: {e}")
        
        self.observer.start()

    def start_playlist_monitoring(self):
        """Monitor RetroArch playlist files for library launches"""
        def monitor_playlists():
            playlist_mtimes = {}
            logged_path = False
            
            while not self.should_stop.is_set():
                try:
                    config_dir = self.retroarch.find_retroarch_config_dir()
                    if (config_dir and 'retrodeck' in str(config_dir) and 
                        not (config_dir / 'content_history.lpl').exists()):
                        config_dir = Path.home() / '.var/app/net.retrodeck.retrodeck/config/retroarch'

                    if not config_dir:
                        continue

                    # Find all playlist files
                    playlist_files = list(config_dir.glob('*.lpl'))
                    
                    if not logged_path:
                        self.log(f"🎮 Monitoring {len(playlist_files)} playlist files")
                        logged_path = True
                    
                    for playlist_path in playlist_files:
                        if playlist_path.name == 'content_history.lpl':
                            continue  # Skip history, already monitored
                            
                        current_mtime = playlist_path.stat().st_mtime
                        last_mtime = playlist_mtimes.get(str(playlist_path), 0)
                        
                        if current_mtime != last_mtime:
                            playlist_mtimes[str(playlist_path)] = current_mtime
                            
                            # Get the most recently played item from this playlist
                            recent_content = self.get_recent_from_playlist(playlist_path)
                            if recent_content:
                                self.log(f"🎯 Library launch: {Path(recent_content).name}")
                                self.sync_saves_for_rom_file(recent_content)
                    
                    time.sleep(3)
                    
                except Exception as e:
                    self.log(f"Playlist monitoring error: {e}")
                    time.sleep(10)
        
        threading.Thread(target=monitor_playlists, daemon=True).start()

    def get_recent_from_playlist(self, playlist_path):
        """Get most recently added/played item from a playlist file"""
        try:
            import json
            with open(playlist_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            
            items = data.get('items', [])
            if items:
                # Return the first item (most recent)
                first_item = items[0]
                rom_path = first_item.get('path', '')
                if rom_path and rom_path != 'N/A' and Path(rom_path).exists():
                    return rom_path
        except:
            pass
        return None

    def set_games_list(self, games):
        """Set the games list for daemon mode"""
        self.available_games = games
        self.get_games = lambda: games

    def start_retroarch_monitoring(self):
        """Enhanced monitoring: prioritize network detection over history"""
        def monitor_retroarch():
            last_content = None
            last_mtime = 0
            retroarch_was_running = False
            last_network_state = False  # Local variable, not self._last_network_state
            startup_grace_period = True
            network_retry_count = 0

            while not self.should_stop.is_set():
                try:
                    current_time = time.time()

                    # 1. Check if RetroArch process is running
                    retroarch_running = self.is_retroarch_running()

                    # 2. Check if RetroArch network is responding
                    network_responding = self.is_retroarch_network_active()

                    # Log state changes
                    if retroarch_running != retroarch_was_running:
                        if retroarch_running:
                            self.log("🎮 RetroArch launched")
                            # Pre-launch sync: download saves before content loads
                            # RetroArch needs .srm and .state.auto before it finishes loading
                            try:
                                current_content = self.get_retroarch_current_game()
                                if current_content:
                                    self.log(f"📥 Pre-launch sync for: {Path(current_content).name}")
                                    self.sync_saves_for_rom_file(current_content)
                                    self.last_sync_time[current_content] = current_time
                            except Exception as e:
                                logging.debug(f"Pre-launch sync error: {e}")
                        else:
                            self.log("🎮 RetroArch closed")
                            network_retry_count = 0  # Reset on close
                            # Clear pending uploads — saves were already uploaded during gameplay
                            # RetroArch flushes saves on exit, which would trigger duplicate uploads
                            self.upload_debounce.clear()
                        retroarch_was_running = retroarch_running

                    # 3. PRIORITY: Network state detection (content loaded/unloaded)
                    if network_responding != last_network_state:
                        if network_responding:
                            current_content = self.get_retroarch_current_game()
                            if current_content:
                                # Skip if pre-launch sync already handled this content
                                last_synced = self.last_sync_time.get(current_content, 0)
                                if current_time - last_synced < 30:
                                    self.log(f"🎯 RetroArch content loaded: {Path(current_content).name} (already synced)")
                                else:
                                    self.log(f"🎯 RetroArch content loaded: {Path(current_content).name}")
                                    self.sync_saves_for_rom_file(current_content)
                                    self.last_sync_time[current_content] = current_time
                                last_network_state = network_responding
                                network_retry_count = 0
                            elif network_retry_count < 3:
                                network_retry_count += 1
                                self.log(f"🎯 RetroArch network active but no content detected, retrying ({network_retry_count}/3)...")
                                # Don't update last_network_state — retry on next iteration
                            else:
                                # Give up retrying, accept network state to stop spam
                                self.log("🎯 RetroArch network active, no content detected — will sync when content loads")
                                last_network_state = network_responding
                        else:
                            self.log("🎮 RetroArch content unloaded (network inactive)")
                            last_network_state = network_responding
                            network_retry_count = 0
                    
                    # 4. FALLBACK: History file detection (for initial state and missed events)
                    elif retroarch_running and not network_responding:
                        current_content = self.get_retroarch_current_game()
                        
                        config_dir = self.retroarch.find_retroarch_config_dir()
                        history_path = None
                        if config_dir:
                            for candidate in [config_dir / 'content_history.lpl',
                                              config_dir / 'playlists' / 'builtin' / 'content_history.lpl']:
                                if candidate.exists():
                                    history_path = candidate
                                    break
                        if history_path:
                            current_mtime = history_path.stat().st_mtime
                            
                            if startup_grace_period:
                                last_content = current_content
                                last_mtime = current_mtime
                                startup_grace_period = False
                                if current_content:
                                    self.log(f"🔍 RetroArch history shows: {Path(current_content).name}")
                            elif current_mtime != last_mtime and current_content:
                                self.log(f"🎯 History fallback - game change: {Path(current_content).name}")
                                self.sync_saves_for_rom_file(current_content)
                                self.last_sync_time[current_content] = current_time
                                last_content = current_content
                                last_mtime = current_mtime
                    
                    time.sleep(1)  # Faster polling for network detection
                    
                except Exception as e:
                    self.log(f"RetroArch monitoring error: {e}")
                    time.sleep(5)
            
        threading.Thread(target=monitor_retroarch, daemon=True).start()
        self.log("🔄 RetroArch monitoring started (network priority + history fallback)")

    def is_retroarch_running(self):
        """Check if RetroArch process is actually running (not just flatpak containers)"""
        try:
            import psutil
            current_pid = os.getpid()  # Exclude our own process
            
            for proc in psutil.process_iter(['pid', 'name', 'cmdline', 'status']):
                try:
                    if proc.info['pid'] == current_pid:  # Skip our own process
                        continue
                        
                    name = proc.info['name'].lower()
                    cmdline = proc.info['cmdline'] if proc.info['cmdline'] else []
                    status = proc.info['status']
                    
                    # Skip zombie/dead processes
                    if status in ['zombie', 'dead']:
                        continue
                    
                    # More specific detection - exclude our own AppImage
                    if name == 'retroarch':  # Exact binary name match
                        return True
                    elif len(cmdline) > 0:
                        cmd_str = ' '.join(cmdline).lower()
                        # Exclude our own app but include real RetroArch
                        if ('retroarch' in cmd_str and 
                            'romm-retroarch-sync' not in cmd_str and  # Exclude our app
                            ('--menu' in cmd_str or '--verbose' in cmd_str or 
                            '.so' in cmd_str or 'content' in cmd_str or 
                            'bwrap' in cmd_str)):  # Include Bazzite's bwrap
                            return True
                    
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    continue
            return False
        except ImportError:
            # Fallback logic unchanged
            import subprocess
            try:
                result = subprocess.run(['flatpak', 'ps'], capture_output=True, text=True, timeout=2)
                return 'org.libretro.RetroArch' in result.stdout
            except:
                return False

    def is_retroarch_network_active(self):
        """Check if RetroArch has content loaded via network commands"""
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)
            
            # Send GET_STATUS command
            sock.sendto(b'GET_STATUS', ('127.0.0.1', 55355))
            
            # Try to receive response
            try:
                response, _ = sock.recvfrom(1024)
                response_text = response.decode('utf-8', errors='ignore').strip()
                
                # Check if response indicates content is loaded
                # RetroArch returns different status when content is loaded vs just menu
                content_loaded = (
                    'CONTENTLESS' not in response_text.upper() and
                    'MENU' not in response_text.upper() and 
                    len(response_text) > 0 and
                    response_text != 'N/A'
                )
                
                sock.close()
                return content_loaded
                
            except socket.timeout:
                sock.close()
                return False
                
        except Exception:
            return False

    def get_retroarch_current_game(self):
        """Get currently loaded game from RetroArch history playlist (JSON format)"""
        try:
            import json
            config_dir = self.retroarch.find_retroarch_config_dir()
            
            # Apply same RetroDECK fix here
            if (config_dir and 'retrodeck' in str(config_dir) and 
                not (config_dir / 'content_history.lpl').exists()):
                config_dir = Path.home() / '.var/app/net.retrodeck.retrodeck/config/retroarch'
                
            if not config_dir:
                return None
            # Check standard location and RetroDECK's playlists/builtin subdirectory
            history_path = config_dir / 'content_history.lpl'
            if not history_path.exists():
                history_path = config_dir / 'playlists' / 'builtin' / 'content_history.lpl'
            
            if history_path.exists():
                with open(history_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                
                items = data.get('items', [])
                
                if items and len(items) > 0:
                    first_item = items[0]
                    rom_path = first_item.get('path', '')
                    
                    if rom_path and rom_path != 'N/A':
                        # Handle archive paths (file.zip#internal.file)
                        if '#' in rom_path:
                            archive_path = rom_path.split('#')[0]
                            if Path(archive_path).exists():
                                return rom_path
                        else:
                            if Path(rom_path).exists():
                                return rom_path
                            
        except Exception as e:
            logging.debug(f"History parsing error: {e}")
        return None

    def sync_saves_for_rom_file(self, rom_path):
        """Sync saves for a specific ROM file that's being launched"""
        try:
            # Handle archive paths (ZIP files with # separator)
            if '#' in rom_path:
                archive_path, internal_file = rom_path.split('#', 1)
                rom_filename = Path(archive_path).name  # Use archive filename
                rom_stem = Path(archive_path).stem
                self.log(f"🎯 Detected ROM from archive: {rom_filename} - syncing saves...")
            else:
                rom_filename = Path(rom_path).name
                rom_stem = Path(rom_path).stem
                self.log(f"🎯 Detected ROM: {rom_filename} - syncing saves...")
            
            # Find matching game in library
            games = self.get_games()
            matching_game = None
            
            for game in games:
                game_filename = game.get('file_name', '')
                if game_filename == rom_filename or Path(game_filename).stem == rom_stem:
                    matching_game = game
                    break
            
            if matching_game:
                self.log(f"📥 Syncing saves for: {matching_game.get('name')}")
                self.download_saves_for_specific_game(matching_game)
            else:
                self.log(f"⚠️ ROM not in library - downloading all recent saves as fallback")
                self.sync_recent_saves()
                
        except Exception as e:
            self.log(f"❌ ROM-specific sync error: {e}")

    def sync_recent_saves(self):
        """Download saves for games that have local save files (recently played)"""
        try:
            if not self.retroarch.save_dirs:
                return
                
            # Get all local save files
            local_saves = self.retroarch.get_save_files()
            recently_played_games = set()
            
            # Find ROM IDs for games with local saves
            for save_type, files in local_saves.items():
                for save_file in files:
                    save_basename = Path(save_file['name']).stem
                    rom_id = self.find_rom_id_for_save_file(Path(save_file['path']))
                    if rom_id:
                        recently_played_games.add(rom_id)
            
            # Sync saves for these games
            games = self.get_games()
            synced_count = 0
            for game in games:
                if game.get('rom_id') in recently_played_games:
                    self.download_saves_for_specific_game(game)
                    synced_count += 1
            
            self.log(f"📥 Synced saves for {synced_count} recently played games")
            
        except Exception as e:
            self.log(f"❌ Recent saves sync error: {e}")

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

        # Skip uploads during startup grace period (first 5 seconds)
        if hasattr(self, 'startup_time') and hasattr(self, 'startup_grace_period'):
            elapsed = time.time() - self.startup_time
            if elapsed < self.startup_grace_period:
                return

        # Queue for upload - the background upload worker will handle actual upload
        current_time = time.time()
        if file_path in self.upload_debounce:
            time_since_last = current_time - self.upload_debounce[file_path]
            if time_since_last < 10.0:  # Ignore rapid re-triggers within 10 seconds
                return

        # Update debounce time - the upload worker will process this when it's stable
        self.upload_debounce[file_path] = current_time
    
    def process_save_upload(self, file_path):
        """Process a queued save file upload — server handles conflict detection via 409"""
        try:
            file_path = Path(file_path)
            if not file_path.exists():
                return

            # Skip if file hasn't changed since last successful upload
            stat = file_path.stat()
            current_fingerprint = (stat.st_size, stat.st_mtime)
            last = self.last_uploaded.get(str(file_path))
            if last == current_fingerprint:
                logging.debug(f"Skipping duplicate upload for {file_path.name} (unchanged since last upload)")
                return

            # Find matching ROM for this save file
            rom_id = self.find_rom_id_for_save_file(file_path)
            if not rom_id:
                self.log(f"⚠️ No matching ROM found for {file_path.name}")
                return

            # Determine save type and slot info
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

            slot, autocleanup, autocleanup_limit = RomMClient.get_slot_info(file_path)

            # Find emulator from file path
            emulator_info = self.retroarch.get_emulator_info_from_path(file_path)
            emulator = emulator_info['romm_emulator']  # Use RomM-compatible name

            # Upload the file with thumbnail and emulator info
            slot_info = f" [slot={slot}, autocleanup={autocleanup_limit}]" if slot else ""
            file_size = file_path.stat().st_size
            size_str = f"{file_size / 1024:.1f}KB" if file_size < 1048576 else f"{file_size / 1048576:.1f}MB"
            if thumbnail_path:
                self.log(f"⬆️ Uploading {file_path.name} ({size_str}) with screenshot...{slot_info}")
            else:
                self.log(f"⬆️ Uploading {file_path.name} ({size_str})...{slot_info}")

            # Get device_id from settings (more reliable than parent_window)
            device_id = self.settings.get('Device', 'device_id', '')
            if not device_id:
                device_id = None
            result = self.romm_client.upload_save_with_thumbnail(
                rom_id, save_type, file_path, thumbnail_path, emulator, device_id,
                slot=slot, autocleanup=autocleanup, autocleanup_limit=autocleanup_limit
            )

            if result == 'conflict':
                self.log(f"⚠️ Server returned 409 Conflict for {file_path.name} — server has newer version, download it first")
                self.retroarch.send_notification(f"Sync conflict: {file_path.name}")
            elif result:
                # Record successful upload fingerprint to avoid duplicate uploads
                self.last_uploaded[str(file_path)] = current_fingerprint
                if thumbnail_path:
                    self.log(f"✅ Server accepted {file_path.name} (200 OK) with screenshot")
                else:
                    self.log(f"✅ Server accepted {file_path.name} (200 OK)")
                self.retroarch.send_notification(f"{save_type.rstrip('s').capitalize()} uploaded")
            else:
                self.log(f"❌ Server rejected {file_path.name} — upload failed")
                self.retroarch.send_notification(f"Upload failed: {file_path.name}")
                
        except Exception as e:
            self.log(f"❌ Upload error for {file_path}: {e}")

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
    
    def get_platform_slug_from_emulator(self, romm_emulator):
        """Reverse map RetroArch core names to RomM platform slugs"""
        core_to_platform = {
            'snes9x': 'snes',
            'nestopia': 'nes',
            'mgba': 'gba',
            'sameboy': 'gb',
            'beetle_psx_hw': 'psx',
            'genesis_plus_gx': 'genesis',
            'mupen64plus_next': 'n64',
            'beetle_saturn': 'saturn',
            'mame': 'arcade',
            'stella': 'atari2600',
        }
        return core_to_platform.get(romm_emulator, romm_emulator)

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
        try:
            from gi.repository import Adw as _Adw
        except ImportError:
            _Adw = None

        try:
            from urllib.parse import urljoin
            import datetime
            rom_id = game['rom_id']
            game_name = game.get('name', 'Unknown')

            # Check if we have device-aware sync data to skip unnecessary downloads
            device_saves_to_skip = set()
            device_states_to_skip = set()

            if self.parent_window and self.parent_window.device_id:
                # Query saves uploaded from this device to avoid re-downloading them (optimistic sync)
                try:
                    device_saves = self.romm_client.get_saves_by_device(
                        self.parent_window.device_id,
                        save_type='saves',
                        rom_id=rom_id,
                        limit=50
                    )
                    device_saves_to_skip = {s.get('id') for s in device_saves if s.get('id')}

                    device_states = self.romm_client.get_saves_by_device(
                        self.parent_window.device_id,
                        save_type='states',
                        rom_id=rom_id,
                        limit=50
                    )
                    device_states_to_skip = {s.get('id') for s in device_states if s.get('id')}

                    if device_saves_to_skip or device_states_to_skip:
                        self.log(f"🔄 Optimistic sync: {len(device_saves_to_skip)} saves, {len(device_states_to_skip)} states already on device")
                except Exception as e:
                    logging.debug(f"Could not query device saves: {e}")
                    # Continue without optimistic sync

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
                    self.log(f"Failed to parse timestamp '{timestamp_str}': {e}")
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
                    self.log(f"Failed to parse filename timestamp '{timestamp_str}': {e}")
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
                    if time_diff > 60:  # Local is more than 1 minute newer
                        self.log(f"     → Local is newer, keeping local")
                        return False, f"Local {file_type} is newer ({time_diff:.1f}s difference)"
                    elif abs(time_diff) <= 10:  # Within 10s = same file (upload latency)
                        self.log(f"     → Timestamps within 10s, skipping (likely same file)")
                        return False, f"{file_type} timestamps are equivalent ({abs(time_diff):.1f}s difference)"
                    else:
                        self.log(f"     → Server is newer, downloading")
                        return True, f"Server {file_type} is newer ({-time_diff:.1f}s newer)"
                            
                elif overwrite_behavior == "Ask each time":
                    # Ask user in main thread
                    import threading
                    user_choice = threading.Event()
                    download_choice = [False]  # Use list to modify from nested function
                    
                    def ask_user():
                        if _Adw is None:
                            download_choice[0] = False
                            user_choice.set()
                            return
                        dialog = _Adw.AlertDialog.new(f"{file_type.title()} Conflict Detected", f"Local {file_type}: {local_str}\nServer {file_type}: {server_str}\n\nWhich version do you want to keep?")
                        dialog.add_response("local", "Keep Local")
                        dialog.add_response("server", "Download Server")
                        dialog.set_default_response("local")
                        
                        def on_response(dialog, response):
                            download_choice[0] = (response == "server")
                            user_choice.set()
                        
                        dialog.connect('response', on_response)
                        dialog.present()
                    
                    _idle_add(ask_user)
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
                    logging.debug(f"Found {total_count} {file_type_name} revisions, selecting latest")
                else:
                    logging.debug(f"Found 1 {file_type_name} file")
                    
                return latest_file

            # Process saves
            if 'saves' in self.retroarch.save_dirs:
                save_base_dir = self.retroarch.save_dirs['saves']
                user_saves = rom_details.get('user_saves', [])

                latest_save = get_latest_file(user_saves, "save")

                if latest_save:
                    save_id = latest_save.get('id')
                    original_filename = latest_save.get('file_name', '')
                    romm_emulator = latest_save.get('emulator', 'unknown')

                    # Compute local path to check if file exists before skipping
                    final_path = None
                    emulator_save_dir = None
                    if original_filename:
                        folder_structure = self.retroarch.detect_save_folder_structure()
                        if folder_structure == 'platform_slugs':
                            platform_slug = self.get_platform_slug_from_emulator(romm_emulator)
                            emulator_save_dir = save_base_dir / platform_slug
                        else:
                            retroarch_emulator_dir = self.retroarch.get_retroarch_directory_name(romm_emulator)
                            emulator_save_dir = save_base_dir / retroarch_emulator_dir
                        if emulator_save_dir:
                            retroarch_filename = self.retroarch.convert_to_retroarch_filename(
                                original_filename, 'saves', emulator_save_dir
                            )
                            final_path = emulator_save_dir / retroarch_filename

                    # Only skip download if the local file actually exists
                    skip = False
                    if final_path and final_path.exists():
                        device_id = self.settings.get('Device', 'device_id', '') or None
                        device_has_current = False
                        if device_id and latest_save.get('device_syncs'):
                            for sync in latest_save['device_syncs']:
                                if sync.get('device_id') == device_id and sync.get('is_current'):
                                    device_has_current = True
                                    break

                        if device_has_current:
                            self.log(f"  ⏭️ Skipping save (device has current version): {latest_save.get('file_name', 'unknown')}")
                            skip = True
                        elif save_id in device_saves_to_skip:
                            self.log(f"  ⏭️ Skipping save (already on device): {latest_save.get('file_name', 'unknown')}")
                            skip = True

                    if not skip and original_filename and emulator_save_dir and final_path:
                        downloads_attempted += 1
                        emulator_save_dir.mkdir(parents=True, exist_ok=True)

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

                            # Get device_id from settings
                            device_id = self.settings.get('Device', 'device_id', '') or None

                            if self.romm_client.download_save(rom_id, 'saves', temp_path, device_id):
                                try:
                                    if temp_path != final_path:
                                        temp_path.rename(final_path)
                                    downloads_successful += 1
                                    self.log(f"  ✅ Save ready: {retroarch_filename}")
                                    # Skip auto-upload for recently downloaded files
                                    if hasattr(self, 'upload_debounce'):
                                        self.upload_debounce[str(final_path)] = time.time() + 30
                                    elif hasattr(self, 'parent_window') and hasattr(self.parent_window, 'auto_sync') and self.parent_window.auto_sync:
                                        self.parent_window.auto_sync.upload_debounce[str(final_path)] = time.time() + 30
                                    self.retroarch.send_notification(f"Save downloaded: {game_name}")
                                except Exception as e:
                                    self.log(f"  ❌ Failed to rename save: {e}")

            # Process states
            if 'states' in self.retroarch.save_dirs:
                state_base_dir = self.retroarch.save_dirs['states']
                user_states = rom_details.get('user_states', [])

                latest_state = get_latest_file(user_states, "state")

                if latest_state:
                    state_id = latest_state.get('id')
                    original_filename = latest_state.get('file_name', '')
                    romm_emulator = latest_state.get('emulator', 'unknown')

                    # Compute local path to check if file exists before skipping
                    final_path = None
                    emulator_state_dir = None
                    if original_filename:
                        folder_structure = self.retroarch.detect_save_folder_structure()
                        if folder_structure == 'platform_slugs':
                            platform_slug = self.get_platform_slug_from_emulator(romm_emulator)
                            emulator_state_dir = state_base_dir / platform_slug
                        else:
                            retroarch_emulator_dir = self.retroarch.get_retroarch_directory_name(romm_emulator)
                            emulator_state_dir = state_base_dir / retroarch_emulator_dir
                        if emulator_state_dir:
                            retroarch_filename = self.retroarch.convert_to_retroarch_filename(
                                original_filename, 'states', emulator_state_dir
                            )
                            final_path = emulator_state_dir / retroarch_filename

                    # Only skip download if the local file actually exists
                    skip = False
                    if final_path and final_path.exists():
                        device_id = self.settings.get('Device', 'device_id', '') or None
                        device_has_current = False
                        if device_id and latest_state.get('device_syncs'):
                            for sync in latest_state['device_syncs']:
                                if sync.get('device_id') == device_id and sync.get('is_current'):
                                    device_has_current = True
                                    break

                        if device_has_current:
                            self.log(f"  ⏭️ Skipping state (device has current version): {latest_state.get('file_name', 'unknown')}")
                            skip = True
                        elif state_id in device_states_to_skip:
                            self.log(f"  ⏭️ Skipping state (already on device): {latest_state.get('file_name', 'unknown')}")
                            skip = True

                    if not skip and original_filename and emulator_state_dir and final_path:
                        downloads_attempted += 1
                        emulator_state_dir.mkdir(parents=True, exist_ok=True)

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

                            # Get device_id from settings
                            device_id = self.settings.get('Device', 'device_id', '') or None

                            if self.romm_client.download_save(rom_id, 'states', temp_path, device_id):
                                try:
                                    if temp_path != final_path:
                                        temp_path.rename(final_path)
                                    downloads_successful += 1
                                    self.log(f"  ✅ State ready: {retroarch_filename}")
                                    # Skip auto-upload for recently downloaded files
                                    if hasattr(self, 'upload_debounce'):
                                        self.upload_debounce[str(final_path)] = time.time() + 30
                                    elif hasattr(self, 'parent_window') and hasattr(self.parent_window, 'auto_sync') and self.parent_window.auto_sync:
                                        self.parent_window.auto_sync.upload_debounce[str(final_path)] = time.time() + 30
                                    self.retroarch.send_notification(f"Save state downloaded: {game_name}")

                                    # Download screenshot if available
                                    screenshot_filename = f"{final_path.name}.png"
                                    screenshot_path = final_path.parent / screenshot_filename

                                    screenshot_data = latest_state.get('screenshot')
                                    if screenshot_data and isinstance(screenshot_data, dict):
                                        screenshot_url = screenshot_data.get('download_path')
                                        if screenshot_url:
                                            try:
                                                full_screenshot_url = urljoin(self.romm_client.base_url, screenshot_url)
                                                screenshot_response = self.romm_client.session.get(full_screenshot_url, timeout=30)
                                                if screenshot_response.status_code == 200:
                                                    with open(screenshot_path, 'wb') as f:
                                                        f.write(screenshot_response.content)
                                                    self.log(f"  📸 Downloaded screenshot: {screenshot_filename}")
                                            except Exception as e:
                                                logging.debug(f"Failed to download screenshot: {e}")
                                    else:
                                        # Fallback: fetch state details for screenshot
                                        try:
                                            state_detail_id = latest_state.get('id')
                                            if state_detail_id:
                                                state_details_url = urljoin(self.romm_client.base_url, f'/api/states/{state_detail_id}')
                                                state_response = self.romm_client.session.get(state_details_url, timeout=10)
                                                if state_response.status_code == 200:
                                                    state_details = state_response.json()
                                                    screenshot_data = state_details.get('screenshot')
                                                    if screenshot_data and isinstance(screenshot_data, dict):
                                                        screenshot_url = screenshot_data.get('download_path')
                                                        if screenshot_url:
                                                            full_screenshot_url = urljoin(self.romm_client.base_url, screenshot_url)
                                                            screenshot_response = self.romm_client.session.get(full_screenshot_url, timeout=30)
                                                            if screenshot_response.status_code == 200:
                                                                with open(screenshot_path, 'wb') as f:
                                                                    f.write(screenshot_response.content)
                                                                self.log(f"  📸 Downloaded screenshot: {screenshot_filename}")
                                        except Exception as e:
                                            logging.debug(f"Screenshot fetch error: {e}")

                                except Exception as e:
                                    self.log(f"  ❌ Failed to process state: {e}")
            
            # Process auto save state (.state.auto) separately
            if 'states' in self.retroarch.save_dirs:
                state_base_dir = self.retroarch.save_dirs['states']
                user_states = rom_details.get('user_states', [])

                # Find latest state with slot="auto"
                auto_states = [s for s in user_states if isinstance(s, dict) and s.get('slot') == 'auto']
                if auto_states:
                    latest_auto = max(auto_states, key=lambda x: x.get('updated_at', ''))
                    auto_id = latest_auto.get('id')
                    auto_filename = latest_auto.get('file_name', '')
                    auto_emulator = latest_auto.get('emulator', 'unknown')

                    if auto_filename:
                        # Compute local path
                        folder_structure = self.retroarch.detect_save_folder_structure()
                        if folder_structure == 'platform_slugs':
                            platform_slug = self.get_platform_slug_from_emulator(auto_emulator)
                            auto_state_dir = state_base_dir / platform_slug
                        else:
                            retroarch_emulator_dir = self.retroarch.get_retroarch_directory_name(auto_emulator)
                            auto_state_dir = state_base_dir / retroarch_emulator_dir

                        if auto_state_dir:
                            retroarch_filename = self.retroarch.convert_to_retroarch_filename(
                                auto_filename, 'states', auto_state_dir, slot='auto'
                            )
                            auto_final_path = auto_state_dir / retroarch_filename

                            # Check if we should skip
                            auto_skip = False
                            if auto_final_path.exists():
                                device_id = self.settings.get('Device', 'device_id', '') or None
                                if device_id and latest_auto.get('device_syncs'):
                                    for sync in latest_auto['device_syncs']:
                                        if sync.get('device_id') == device_id and sync.get('is_current'):
                                            auto_skip = True
                                            break
                                if not auto_skip and auto_id in device_states_to_skip:
                                    auto_skip = True

                            if auto_skip:
                                self.log(f"  ⏭️ Skipping auto state (already on device): {auto_filename}")
                            else:
                                auto_state_dir.mkdir(parents=True, exist_ok=True)
                                temp_path = auto_state_dir / auto_filename
                                device_id = self.settings.get('Device', 'device_id', '') or None

                                self.log(f"  📥 Downloading auto state: {auto_filename} → {retroarch_filename}")
                                if self.romm_client.download_save(rom_id, 'states', temp_path, device_id):
                                    try:
                                        if temp_path != auto_final_path:
                                            if auto_final_path.exists():
                                                auto_final_path.unlink()
                                            temp_path.rename(auto_final_path)
                                        downloads_successful += 1
                                        self.log(f"  ✅ Auto state ready: {retroarch_filename}")
                                        # Skip auto-upload for recently downloaded files
                                        if hasattr(self, 'upload_debounce'):
                                            self.upload_debounce[str(auto_final_path)] = time.time() + 30
                                        elif hasattr(self, 'parent_window') and hasattr(self.parent_window, 'auto_sync') and self.parent_window.auto_sync:
                                            self.parent_window.auto_sync.upload_debounce[str(auto_final_path)] = time.time() + 30
                                    except Exception as e:
                                        self.log(f"  ❌ Failed to process auto state: {e}")

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

class AutoSyncLock:
    """Linux-only file locking to prevent multiple auto-sync instances"""
    
    def __init__(self):
        self.lock_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'autosync.lock'
        self.lock_file.parent.mkdir(parents=True, exist_ok=True)
        self.lock_fd = None
    
    def acquire(self, instance_id):
        """Acquire exclusive lock"""
        import fcntl
        
        try:
            # Open lock file
            self.lock_fd = open(self.lock_file, 'w')
            
            # Try to acquire exclusive lock (non-blocking)
            fcntl.flock(self.lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            
            # Write instance info
            self.lock_fd.write(f"{os.getpid()}:{instance_id}:{time.time()}\n")
            self.lock_fd.flush()
            
            return True
            
        except (IOError, OSError):
            # Lock already held by another process
            if self.lock_fd:
                self.lock_fd.close()
                self.lock_fd = None
            return False
    
    def release(self):
        """Release the lock"""
        if self.lock_fd:
            self.lock_fd.close()  # Automatically releases flock
            self.lock_fd = None
            
        try:
            self.lock_file.unlink()  # Clean up lock file
        except FileNotFoundError:
            pass
    
    def __del__(self):
        self.release()

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

    def is_save_file(self, file_path):
        """Check if the file is a save file we should monitor"""
        try:
            path = Path(file_path)
            # Handle .state.auto (compound extension — Path.suffix returns .auto)
            if path.name.lower().endswith('.state.auto'):
                return self.save_type == 'states'
            return path.suffix.lower() in self.extensions
        except:
            return False

def is_path_validly_downloaded(path):
    """Check if a path (file or folder) is validly downloaded"""
    path = Path(path)
    if not path.exists():
        return False
    if path.is_dir():
        try:
            return any(path.iterdir())
        except (PermissionError, OSError):
            return False
    elif path.is_file():
        return path.stat().st_size > 1024
    return False


def build_sync_status(romm_client, collection_sync, auto_sync, available_games,
                      known_collections=None, disabled_collection_counts=None, retroarch=None,
                      bios_tracking=None):
    """Build the current status dict from live sync object state.

    Args:
        romm_client:        authenticated RomMClient (or None if disconnected)
        collection_sync:    CollectionSyncManager instance (or None)
        auto_sync:          AutoSyncManager instance (or None)
        available_games:    list of game dicts loaded on connect
        known_collections:  pre-fetched list of collection dicts from RomM.
                            When provided the function makes zero API calls and
                            returns in milliseconds (used for toggle-triggered
                            rebuilds where low latency matters).  When None the
                            list is fetched from the API (used for periodic
                            deep refreshes).
        retroarch:          RetroArchInterface instance for config checks (optional)
        bios_tracking:      BiosTrackingManager instance (or None)

    Returns a new status dict ready to write into the shared status dict.
    """
    actively_syncing = set()
    if collection_sync and collection_sync.running:
        actively_syncing = collection_sync.selected_collections

    collections_list = []

    if romm_client and romm_client.authenticated:
        try:
            if known_collections is not None:
                all_collections = known_collections
            else:
                all_collections = romm_client.get_collections()

            for collection in all_collections:
                collection_name = collection.get('name', 'Unknown')
                collection_id   = collection.get('id')
                is_auto_sync    = collection_name in actively_syncing

                sync_state      = 'not_synced'
                downloaded_roms = None
                total_roms      = None
                _dl_speed       = 0
                _dl_pct         = None

                if is_auto_sync:
                    # Read counts from CollectionSyncManager's cache (no extra API calls)
                    if collection_sync and hasattr(collection_sync, 'collection_caches'):
                        cached_rom_ids = collection_sync.collection_caches.get(collection_name)
                        if cached_rom_ids:
                            total_roms      = len(cached_rom_ids)
                            downloaded_roms = total_roms
                            sync_state      = 'synced'

                    # Active per-chunk download progress overrides the synced state
                    if collection_sync and hasattr(collection_sync, 'download_progress'):
                        progress = collection_sync.download_progress.get(collection_name)
                        if progress:
                            downloaded_roms = progress['downloaded']
                            total_roms      = progress['total']
                            sync_state      = 'syncing'
                            _dl_speed       = progress.get('speed', 0)
                            _dl_pct         = progress.get('downloaded_pct', None)
                            logging.info(f"[STATUS] Active download for "
                                         f"{collection_name}: {downloaded_roms}/{total_roms}")
                # Non-auto-sync collections: use the cached rom_ids set and compute
                # downloaded live from available_games so cross-collection downloads
                # are reflected immediately without any extra API calls.
                if not is_auto_sync and disabled_collection_counts:
                    counts = disabled_collection_counts.get(collection_name)
                    if counts and counts.get('total'):
                        col_rom_ids = counts.get('rom_ids', set())
                        downloaded_game_ids = {g['rom_id'] for g in available_games
                                               if g.get('is_downloaded')}
                        downloaded_roms = sum(1 for rid in col_rom_ids
                                             if rid in downloaded_game_ids)
                        total_roms      = counts['total']

                collection_data = {
                    'name':       collection_name,
                    'id':         collection_id,
                    'auto_sync':  is_auto_sync,
                    'sync_state': sync_state,
                }
                if total_roms is not None:
                    collection_data['downloaded'] = downloaded_roms
                    collection_data['total']      = total_roms
                if sync_state == 'syncing' and _dl_speed > 0:
                    collection_data['speed'] = _dl_speed
                if sync_state == 'syncing' and _dl_pct is not None:
                    collection_data['downloaded_pct'] = _dl_pct
                collections_list.append(collection_data)

        except Exception as e:
            logging.error(f"Failed to fetch collections for status: {e}")

    # Attach any pending removal events so the frontend can show a notification.
    # Events live in collection_sync.last_removals until explicitly cleared.
    if collection_sync and hasattr(collection_sync, 'last_removals'):
        for collection_data in collections_list:
            name = collection_data['name']
            if name in collection_sync.last_removals:
                collection_data['last_removal'] = collection_sync.last_removals[name]

    # Check RetroArch configuration warnings
    warnings = []
    if retroarch:
        try:
            network_ok, network_msg = retroarch.check_network_commands_config()
            if not network_ok:
                warnings.append({
                    'type': 'network_commands',
                    'message': network_msg
                })

            thumbnail_ok, thumbnail_msg = retroarch.check_savestate_thumbnail_config()
            if not thumbnail_ok:
                warnings.append({
                    'type': 'savestate_thumbnails',
                    'message': thumbnail_msg
                })
        except Exception as e:
            logging.debug(f"RetroArch config check failed: {e}")

    # Build status dict
    status = {
        'running':                True,
        'connected':              bool(romm_client and romm_client.authenticated),
        'auto_sync':              bool(auto_sync and auto_sync.enabled),
        'game_count':             len(available_games),
        'collections':            collections_list,
        'collection_count':       len(collections_list),
        'actively_syncing_count': len(actively_syncing),
        'last_update':            time.time(),
        'warnings':               warnings,
    }

    # Include BIOS status if tracking manager available
    if bios_tracking:
        status['bios_status'] = bios_tracking.get_status()

    return status

class CollectionSyncManager:
    """Manages collection synchronization"""
    
    def __init__(self, romm_client, settings, selected_collections, sync_interval, available_games, log_callback):
        self.romm_client = romm_client
        self.settings = settings
        self.selected_collections = selected_collections
        self.sync_interval = sync_interval
        self.available_games = available_games
        self.log = log_callback
        self.running = False
        self.thread = None
        self._stop_event = threading.Event()
        self.collection_caches = {}
        # Per-collection download progress — read directly by get_service_status()
        self.download_progress = {}  # {collection_name: {'downloaded': int, 'downloaded_pct': float, 'total': int, 'speed': float}}
        # Last removal event per collection — for frontend notification
        self.last_removals = {}  # {collection_name: {'removed_count': int, 'deleted_count': int, 'timestamp': float}}

    def start(self):
        """Start collection monitoring"""
        if self.running:
            return

        self.running = True
        self._stop_event.clear()
        self.initialize_caches()

        def sync_worker():
            while self.running:
                try:
                    self.check_for_changes()
                    self._stop_event.wait(self.sync_interval)
                except Exception as e:
                    self.log(f"Collection sync error: {e}")
                    self._stop_event.wait(60)

        self.thread = threading.Thread(target=sync_worker, daemon=True)
        self.thread.start()
        self.log(f"Collection auto-sync started for {len(self.selected_collections)} collections")

    def stop(self):
        """Stop collection monitoring"""
        self.running = False
        self._stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)

    def set_removal_event(self, collection_name, removed_count, deleted_count):
        """Record a removal event so build_sync_status can include it in the status."""
        self.last_removals[collection_name] = {
            'removed_count': removed_count,
            'deleted_count': deleted_count,
            'timestamp':     time.time(),
        }
        logging.info(f"[REMOVAL] Recorded for {collection_name}: {removed_count} removed, {deleted_count} deleted")

    def update_collections(self, new_collections):
        """Update the collection list without restarting - just add/remove caches"""
        new_set = set(new_collections)
        old_set = self.selected_collections

        # Remove collections that are no longer selected
        for removed in (old_set - new_set):
            if removed in self.collection_caches:
                del self.collection_caches[removed]
                self.log(f"Removed collection from sync: {removed}")

        # Update selected_collections immediately so build_sync_status reflects the
        # change on the very next poll — before the background fetch finishes.
        self.selected_collections = new_set

        # Add new collections in a background thread so we don't block the IPC
        # caller (toggle_collection_sync). The download progress will be visible
        # to get_service_status() as soon as _init_added_collection sets it.
        for added in (new_set - old_set):
            threading.Thread(
                target=self._init_added_collection,
                args=(added,),
                daemon=True,
                name=f"romm-add-{added}",
            ).start()
    
    def _init_added_collection(self, collection_name):
        """Fetch ROM list, populate cache, and download missing ROMs for a newly
        added collection.  Runs in a background thread so update_collections()
        returns immediately and the IPC caller is never blocked."""
        try:
            all_collections = self.romm_client.get_collections()
            for collection in all_collections:
                if collection.get('name') == collection_name:
                    collection_id   = collection.get('id')
                    collection_roms = self.romm_client.get_collection_roms(collection_id)
                    rom_ids = {rom.get('id') for rom in collection_roms if rom.get('id')}
                    self.collection_caches[collection_name] = rom_ids
                    self.log(f"Added collection to sync: {collection_name} ({len(rom_ids)} games)")
                    self.handle_added_games(collection_roms, rom_ids, collection_name)
                    break
        except Exception as e:
            self.log(f"Error adding collection {collection_name}: {e}")

    def initialize_caches(self):
        """Initialize ROM ID caches and download all existing ROMs"""
        try:
            all_collections = self.romm_client.get_collections()
            for collection in all_collections:
                collection_name = collection.get('name', '')
                if collection_name in self.selected_collections:
                    collection_id = collection.get('id')
                    collection_roms = self.romm_client.get_collection_roms(collection_id)
                    rom_ids = {rom.get('id') for rom in collection_roms if rom.get('id')}
                    self.collection_caches[collection_name] = rom_ids
                    self.log(f"Initialized cache for '{collection_name}': {len(rom_ids)} games")

                    # Download all existing ROMs in the collection
                    all_rom_ids = {rom.get('id') for rom in collection_roms if rom.get('id')}
                    if all_rom_ids:
                        self.log(f"Starting initial download for '{collection_name}'...")
                        self.handle_added_games(collection_roms, all_rom_ids, collection_name)
        except Exception as e:
            self.log(f"Cache initialization error: {e}")
    
    def check_for_changes(self):
        """Check for collection changes"""
        self.log("Checking collections for changes...")
        try:
            all_collections = self.romm_client.get_collections()
            
            for collection in all_collections:
                collection_name = collection.get('name', '')
                if collection_name not in self.selected_collections:
                    continue
                
                collection_id = collection.get('id')
                collection_roms = self.romm_client.get_collection_roms(collection_id)
                current_rom_ids = {rom.get('id') for rom in collection_roms if rom.get('id')}
                previous_rom_ids = self.collection_caches.get(collection_name, set())
                
                if current_rom_ids != previous_rom_ids:
                    added = current_rom_ids - previous_rom_ids
                    removed = previous_rom_ids - current_rom_ids
                    
                    if added:
                        self.log(f"Collection '{collection_name}': {len(added)} games added")
                        self.handle_added_games(collection_roms, added, collection_name)
                    
                    if removed:
                        self.log(f"Collection '{collection_name}': {len(removed)} games removed") 
                        self.handle_removed_games(removed, collection_name)
                    
                    self.collection_caches[collection_name] = current_rom_ids
                    
        except Exception as e:
            self.log(f"Change check error: {e}")
    
    def handle_added_games(self, collection_roms, added_rom_ids, collection_name):
        """Handle newly added games - simplified for daemon"""
        # Check if auto-download is enabled
        auto_download = self.settings.get('Collections', 'auto_download', 'true') == 'true'
        if not auto_download:
            self.log(f"New games in '{collection_name}' but auto-download disabled")
            return

        download_dir = Path(self.settings.get('Download', 'rom_directory'))
        downloaded_count = 0

        # First pass: count ROMs that actually need downloading AND count existing ROMs
        roms_to_download = []
        existing_roms_count = 0
        # Total collection size is ALL ROMs in the collection, not just newly added ones
        total_collection_size = len(collection_roms)

        for rom in collection_roms:
            if rom.get('id') not in added_rom_ids:
                # This ROM is not newly added, but check if it exists locally to count it
                platform_slug = rom.get('platform_slug', 'Unknown')
                file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
                local_path = download_dir / platform_slug / file_name
                if is_path_validly_downloaded(local_path):
                    existing_roms_count += 1
                continue

            # This ROM is newly added - check if we need to download it
            platform_slug = rom.get('platform_slug', 'Unknown')
            file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
            local_path = download_dir / platform_slug / file_name
            if is_path_validly_downloaded(local_path):
                existing_roms_count += 1
            else:
                roms_to_download.append(rom)

        total_to_download = len(roms_to_download)
        if total_to_download == 0:
            self.log(f"All ROMs in '{collection_name}' already downloaded")
            return

        # Initialize progress tracking with TOTAL collection size, not just download count
        # Start with existing_roms_count since those are already local
        self.download_progress[collection_name] = {
            'downloaded': existing_roms_count,
            'total': total_collection_size
        }
        logging.info(f"[PROGRESS] Initialized download_progress for {collection_name}: {existing_roms_count}/{total_collection_size} ({total_to_download} to download)")

        for rom in roms_to_download:
            # Simple game processing for daemon
            platform_slug = rom.get('platform_slug', 'Unknown')
            file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
            platform_dir = download_dir / platform_slug
            local_path = platform_dir / file_name

            # Create directories
            platform_dir.mkdir(parents=True, exist_ok=True)

            # Progress callback: update download_progress on every chunk so
            # get_service_status() always reads fresh data (no throttle needed
            # since the frontend only polls every 2 s anyway).
            # _base + 1 shows "currently on ROM N" rather than freezing at N-1.
            _completed_before_this = existing_roms_count + downloaded_count

            def _chunk_progress(info, _coll=collection_name, _base=_completed_before_this,
                                 _total=total_collection_size):
                frac  = info.get('progress', 0.0)   # 0.0–1.0 within this ROM
                speed = info.get('speed',    0.0)   # bytes/sec
                overall_pct = round((_base + frac) / _total * 100.0, 1) if _total > 0 else 0.0
                if _coll in self.download_progress:
                    self.download_progress[_coll]['downloaded']     = _base + 1
                    self.download_progress[_coll]['downloaded_pct'] = overall_pct
                    self.download_progress[_coll]['speed']          = speed

            # Download the ROM
            self.log(f"  ⬇️ Downloading {rom.get('name')}...")

            # Update progress immediately to show we're starting this ROM
            # (don't wait for first chunk callback)
            # Use a tiny fraction (0.01) to show we've started without jumping to the next whole number
            if collection_name in self.download_progress:
                self.download_progress[collection_name]['downloaded'] = _completed_before_this + 1
                self.download_progress[collection_name]['downloaded_pct'] = round((_completed_before_this + 0.01) / total_collection_size * 100.0, 1) if total_collection_size > 0 else 0.0
                self.download_progress[collection_name]['speed'] = 0.0

            success, message = self.romm_client.download_rom(
                rom.get('id'),
                rom.get('name', 'Unknown'),
                local_path,
                progress_callback=_chunk_progress
            )

            if success:
                self.log(f"  ✅ Downloaded {rom.get('name')}")
                downloaded_count += 1

                # Update progress - add to existing_roms_count to show total local count
                current_local_count = existing_roms_count + downloaded_count
                self.download_progress[collection_name]['downloaded'] = current_local_count
                logging.info(f"[PROGRESS] Updated download_progress for {collection_name}: {current_local_count}/{total_collection_size}")

                # Update available_games with collection info
                for game in self.available_games:
                    if game.get('rom_id') == rom.get('id'):
                        game['collection'] = collection_name
                        game['is_downloaded'] = True
                        game['local_path'] = str(local_path)
                        game['local_size'] = local_path.stat().st_size
                        break
            else:
                self.log(f"  ❌ Failed to download {rom.get('name')}: {message}")
        
        # Clear progress — get_service_status() will show 'synced' on next poll.
        if collection_name in self.download_progress:
            logging.info(f"[PROGRESS] Clearing download_progress for {collection_name}")
            del self.download_progress[collection_name]

        if downloaded_count > 0:
            self.log(f"Auto-downloaded {downloaded_count} new games from '{collection_name}'")
    
    def handle_removed_games(self, removed_rom_ids, collection_name):
        """Handle removed games - simplified for daemon"""
        # Track removal event for UI notification (even if auto-delete is disabled)
        if not hasattr(self, 'removal_events'):
            self.removal_events = {}

        self.removal_events[collection_name] = {
            'removed_count': len(removed_rom_ids),
            'timestamp': time.time()
        }
        logging.info(f"[REMOVAL] Tracked removal event for {collection_name}: {len(removed_rom_ids)} games removed")

        # Check if auto-delete is enabled
        auto_delete = self.settings.get('Collections', 'auto_delete', 'false') == 'true'
        if not auto_delete:
            self.log(f"Games removed from '{collection_name}' but auto-delete disabled")
            self.set_removal_event(collection_name, len(removed_rom_ids), 0)
            return

        download_dir = Path(self.settings.get('Download', 'rom_directory'))
        deleted_count = 0

        # Find and delete removed games
        for game in self.available_games:
            if game.get('rom_id') in removed_rom_ids and game.get('is_downloaded'):
                # Check if game exists in other synced collections
                found_in_other = False
                for other_collection in self.selected_collections:
                    if other_collection != collection_name:
                        # Simple check - in real implementation you'd check the actual collection contents
                        pass

                if not found_in_other:
                    local_path = Path(game.get('local_path', ''))
                    if local_path.exists():
                        try:
                            local_path.unlink()
                            self.log(f"  🗑️ Deleted {game.get('name')}")
                            deleted_count += 1
                        except Exception as e:
                            self.log(f"  ❌ Failed to delete {game.get('name')}: {e}")

        if deleted_count > 0:
            self.log(f"Auto-deleted {deleted_count} games removed from '{collection_name}'")

        self.set_removal_event(collection_name, len(removed_rom_ids), deleted_count)


# ==============================================================================
# GameListPollingManager
# ==============================================================================

class GameListPollingManager:
    """Manages lightweight polling for game list updates using updated_after timestamps.

    Polls RomM every 30 seconds for games added/updated since last check, enabling
    near-real-time game library updates without full data fetches.
    """

    def __init__(self, romm_client, settings, available_games_list,
                 platform_slug_to_name, log_callback, update_callback=None):
        """Initialize polling manager.

        Args:
            romm_client: Authenticated RomMClient instance
            settings: SettingsManager instance for download directory
            available_games_list: Reference to the shared available_games list (modified in-place)
            platform_slug_to_name: dict mapping platform slugs to names
            log_callback: Function for logging messages
            update_callback: Optional callable(new_count, updated_count, total) called after successful poll
        """
        self.romm_client = romm_client
        self.settings = settings
        self.available_games = available_games_list
        self.platform_slug_to_name = platform_slug_to_name
        self.log = log_callback
        self.on_update = update_callback

        # Threading state
        self.running = False
        self.thread = None
        self._stop_event = threading.Event()

        # Polling state
        self.last_poll_time = None  # ISO 8601 timestamp string
        self.poll_interval = 30  # seconds
        self.initial_delay = 10  # Wait 10s after start before first poll

    def start(self):
        """Start polling thread."""
        if self.running:
            return

        self.running = True
        self._stop_event.clear()

        def polling_worker():
            # Initial delay to let connection settle
            self._stop_event.wait(self.initial_delay)

            while self.running:
                self._stop_event.wait(self.poll_interval)
                if self._stop_event.is_set():
                    break

                try:
                    self._poll_for_updates()
                except Exception as e:
                    self.log(f"Polling error: {e}")
                    import traceback
                    self.log(traceback.format_exc())

        self.thread = threading.Thread(target=polling_worker, daemon=True, name="game-polling")
        self.thread.start()
        self.log(f"Game polling started (interval: {self.poll_interval}s)")

    def stop(self):
        """Stop polling thread."""
        self.running = False
        self._stop_event.set()
        if self.thread:
            self.thread.join(timeout=5)
        self.log("Game polling stopped")

    def set_last_poll_time(self, iso_timestamp):
        """Set the timestamp for next incremental poll.

        Args:
            iso_timestamp: ISO 8601 datetime string (e.g., from datetime.now(timezone.utc).isoformat())
        """
        self.last_poll_time = iso_timestamp

    def _poll_for_updates(self):
        """Poll RomM for new/updated games."""
        if not (self.romm_client and self.romm_client.authenticated):
            return  # Skip if not connected

        if self.last_poll_time is None:
            return  # Skip if no baseline timestamp set

        # Fetch only ROMs updated since last poll
        roms_result = self.romm_client.get_roms(
            limit=1000,
            offset=0,
            updated_after=self.last_poll_time
        )

        if not roms_result or len(roms_result) != 2:
            return

        new_roms, _ = roms_result

        if not new_roms:
            return  # No updates

        # Process updates
        download_dir = Path(self.settings.get('Download', 'rom_directory',
                                              '~/RomMSync/roms')).expanduser()

        # Create fast lookup map
        existing_games_map = {g['rom_id']: g for g in self.available_games if 'rom_id' in g}

        new_count = 0
        updated_count = 0

        for rom in new_roms:
            rom_id = rom.get('id')
            was_existing = rom_id in existing_games_map

            platform_slug = rom.get('platform_slug', 'Unknown')
            file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
            local_path = download_dir / platform_slug / file_name
            is_downloaded = is_path_validly_downloaded(local_path)
            local_size = 0

            if is_downloaded and local_path.exists():
                if local_path.is_dir():
                    local_size = sum(f.stat().st_size for f in local_path.rglob('*') if f.is_file())
                else:
                    local_size = local_path.stat().st_size

            game_data = {
                'name': Path(file_name).stem if file_name else rom.get('name', 'Unknown'),
                'rom_id': rom_id,
                'platform': rom.get('platform_name', 'Unknown'),
                'platform_slug': platform_slug,
                'file_name': file_name,
                'is_downloaded': is_downloaded,
                'local_path': str(local_path) if is_downloaded else None,
                'local_size': local_size,
                'romm_data': {
                    'fs_name': rom.get('fs_name'),
                    'fs_name_no_ext': rom.get('fs_name_no_ext'),
                    'fs_size_bytes': rom.get('fs_size_bytes', 0),
                    'platform_id': rom.get('platform_id'),
                    'platform_slug': rom.get('platform_slug'),
                },
            }

            existing_games_map[rom_id] = game_data

            if was_existing:
                updated_count += 1
            else:
                new_count += 1

        # Update the shared list reference (in-place modification)
        self.available_games.clear()
        self.available_games.extend(existing_games_map.values())

        # Update timestamp
        from datetime import datetime, timezone
        self.last_poll_time = datetime.now(timezone.utc).isoformat()

        if new_count > 0 or updated_count > 0:
            self.log(f"Poll: {new_count} new, {updated_count} updated games "
                    f"(total: {len(self.available_games)})")

            # Notify callback
            if self.on_update:
                self.on_update(new_count, updated_count, len(self.available_games))




# ==============================================================================
# BiosTrackingManager
# ==============================================================================

class BiosTrackingManager:
    """Manages BIOS download tracking and orchestration for synced collections.

    Scans for missing BIOS files and triggers parallel downloads from RomM.
    Tracks download status per platform and exposes status for build_sync_status().
    """

    def __init__(self, retroarch, romm_client, collection_sync, available_games_list,
                 platform_slug_to_name, log_callback):
        """Initialize BIOS tracking manager.

        Args:
            retroarch: RetroArchInterface instance with bios_manager
            romm_client: Authenticated RomMClient instance
            collection_sync: CollectionSyncManager instance (or None)
            available_games_list: Reference to shared available_games list
            platform_slug_to_name: dict mapping platform slugs to names
            log_callback: Function for logging messages
        """
        self.retroarch = retroarch
        self.romm_client = romm_client
        self.collection_sync = collection_sync
        self.available_games = available_games_list
        self.platform_slug_to_name = platform_slug_to_name
        self.log = log_callback

        # BIOS tracking state (protected by lock)
        self._lock = threading.Lock()
        self.downloads_in_progress = set()  # Platform slugs currently downloading
        self.platforms_ready = set()  # Platform slugs with all required BIOS
        self.download_failures = {}  # {platform_slug: error_message}
        self.platform_status = {}  # {platform_slug: status_dict}

        # Threading state for background scan
        self.running = False
        self.scan_thread = None

    def scan_library_bios(self):
        """Scan BIOS status for all platforms in library (background thread).

        Updates platform_status cache. Should be called once after initial game fetch.
        """
        if not self.retroarch or not self.retroarch.bios_manager:
            self.log("BIOS manager not available, skipping scan")
            return

        if not self.available_games:
            self.log("No games in library, skipping BIOS scan")
            return

        def scan_worker():
            try:
                # Collect unique platforms from all games
                platforms_in_library = {}
                for game in self.available_games:
                    platform_slug = game.get('platform_slug')
                    platform_name = game.get('platform')
                    if not platform_name or platform_name == 'Unknown':
                        platform_name = self.platform_slug_to_name.get(platform_slug)
                    if platform_slug and platform_name:
                        platforms_in_library[platform_slug] = platform_name

                if not platforms_in_library:
                    self.log("No platforms found in library")
                    return

                self.log(f"Scanning BIOS status for {len(platforms_in_library)} platforms...")
                bios_manager = self.retroarch.bios_manager

                platform_status = {}
                for platform_slug, platform_name in platforms_in_library.items():
                    try:
                        normalized_platform = bios_manager.normalize_platform_name(platform_name)
                        present, missing = bios_manager.check_platform_bios(normalized_platform)
                        required_missing = [b for b in missing if b.get('required', False)]
                        total_required = len(present) + len(required_missing)

                        # Skip platforms with no BIOS requirements
                        if total_required == 0:
                            continue

                        is_ready = len(required_missing) == 0

                        platform_status[platform_slug] = {
                            'name': platform_name,
                            'ready': is_ready,
                            'present': len(present),
                            'missing': len(required_missing),
                            'total_required': total_required,
                        }

                        if is_ready:
                            with self._lock:
                                self.platforms_ready.add(platform_slug)

                    except Exception as e:
                        self.log(f"Error scanning BIOS for {platform_name}: {e}")
                        platform_status[platform_slug] = {
                            'name': platform_name,
                            'ready': False,
                            'present': 0,
                            'missing': 0,
                            'total_required': 0,
                            'error': str(e),
                        }

                # Update cache atomically
                with self._lock:
                    self.platform_status = platform_status

                ready_count = sum(1 for p in platform_status.values() if p.get('ready', False))
                self.log(f"BIOS scan complete: {ready_count}/{len(platform_status)} platforms ready")

            except Exception as e:
                self.log(f"Error scanning BIOS for library: {e}")
                import traceback
                self.log(traceback.format_exc())

        # Run scan in background
        self.scan_thread = threading.Thread(target=scan_worker, daemon=True, name="bios-scan")
        self.scan_thread.start()

    def download_bios_for_platform(self, platform_slug, platform_name):
        """Download BIOS for a single platform (runs in background thread).

        Args:
            platform_slug: Platform slug (e.g., 'sony-playstation')
            platform_name: Human-readable platform name (e.g., 'Sony - PlayStation')
        """
        try:
            if not self.retroarch or not self.retroarch.bios_manager:
                self.log(f"BIOS manager not available")
                return

            bios_manager = self.retroarch.bios_manager
            bios_manager.romm_client = self.romm_client  # Set client for downloads

            normalized_platform = bios_manager.normalize_platform_name(platform_name)

            # Check if already present
            present, missing = bios_manager.check_platform_bios(normalized_platform)
            required_missing = [b for b in missing if b.get('required', False)]

            if not required_missing:
                self.log(f"✅ All required BIOS already present for {platform_name}")
                with self._lock:
                    self.platforms_ready.add(platform_slug)
                return

            self.log(f"📥 Downloading BIOS for {platform_name} ({len(required_missing)} files)...")

            # Download
            success = bios_manager.auto_download_missing_bios(normalized_platform)

            if success:
                self.log(f"✅ BIOS download complete for {platform_name}")
                # Re-check to get accurate present count
                present_after, missing_after = bios_manager.check_platform_bios(normalized_platform)
                required_missing_after = [b for b in missing_after if b.get('required', False)]
                with self._lock:
                    self.platforms_ready.add(platform_slug)
                    self.download_failures.pop(platform_slug, None)
                    if platform_slug in self.platform_status:
                        self.platform_status[platform_slug]['ready'] = True
                        self.platform_status[platform_slug]['present'] = len(present_after)
                        self.platform_status[platform_slug]['missing'] = len(required_missing_after)
                        self.platform_status[platform_slug]['total_required'] = len(present_after) + len(required_missing_after)
            else:
                error_msg = "unavailable_on_server"
                self.log(f"⚠️ BIOS unavailable on server for {platform_name}")
                with self._lock:
                    self.download_failures[platform_slug] = error_msg

        except Exception as e:
            error_msg = str(e)
            self.log(f"❌ BIOS download error for {platform_name}: {e}")
            import traceback
            self.log(traceback.format_exc())
            with self._lock:
                self.download_failures[platform_slug] = error_msg

        finally:
            with self._lock:
                self.downloads_in_progress.discard(platform_slug)

    def trigger_downloads_for_games(self, games):
        """Trigger parallel BIOS downloads for platforms in game list.

        Args:
            games: List of game dicts with 'platform' and 'platform_slug' keys
        """
        if not games:
            return

        # Collect unique platforms
        platforms_needed = {}
        for game in games:
            platform_slug = game.get('platform_slug') or game.get('platform', {}).get('slug')
            platform_name = game.get('platform_name') or game.get('platform', {}).get('name')

            if platform_slug and platform_name:
                platforms_needed[platform_slug] = platform_name

        # Start downloads for platforms not already handled
        for platform_slug, platform_name in platforms_needed.items():
            with self._lock:
                if (platform_slug in self.downloads_in_progress or
                    platform_slug in self.platforms_ready):
                    continue

                self.downloads_in_progress.add(platform_slug)

            # Start download thread
            threading.Thread(
                target=self.download_bios_for_platform,
                args=(platform_slug, platform_name),
                daemon=True,
                name=f"bios-{platform_slug}"
            ).start()
            self.log(f"🎮 Started BIOS download for {platform_name}")

    def download_for_collection(self, collection_name):
        """Fetch collection ROMs and trigger BIOS downloads (background thread).

        Args:
            collection_name: Name of collection being enabled
        """
        def download_worker():
            try:
                if not (self.romm_client and self.romm_client.authenticated):
                    self.log("Cannot start BIOS downloads: not connected to RomM")
                    return

                # Get collection ID from RomM
                collections = self.romm_client.get_collections()
                collection_id = None
                for col in collections:
                    if col.get('name') == collection_name:
                        collection_id = col.get('id')
                        break

                if collection_id is None:
                    self.log(f"Collection '{collection_name}' not found")
                    return

                # Fetch ROMs
                collection_roms = self.romm_client.get_collection_roms(collection_id)
                self.log(f"Checking BIOS requirements for {len(collection_roms)} games "
                        f"in '{collection_name}'")

                # Enrich with platform names from mapping
                for rom in collection_roms:
                    if 'platform_name' not in rom or not rom['platform_name']:
                        slug = rom.get('platform_slug')
                        if slug and slug in self.platform_slug_to_name:
                            rom['platform_name'] = self.platform_slug_to_name[slug]

                # Trigger downloads
                self.trigger_downloads_for_games(collection_roms)

            except Exception as e:
                self.log(f"Error starting BIOS downloads for collection {collection_name}: {e}")
                import traceback
                self.log(traceback.format_exc())

        threading.Thread(target=download_worker, daemon=True,
                        name=f"bios-collection-{collection_name}").start()

    def get_platforms_in_synced_collections(self):
        """Get set of platform slugs in actively syncing collections.

        Returns:
            set: Platform slugs that have games in synced collections
        """
        if not self.collection_sync:
            return set()

        synced_platforms = set()
        collection_caches = getattr(self.collection_sync, 'collection_caches', {})

        for collection_name, rom_ids in collection_caches.items():
            for game in (self.available_games or []):
                if game.get('rom_id') in rom_ids:
                    platform_slug = game.get('platform_slug')
                    if platform_slug:
                        synced_platforms.add(platform_slug)

        return synced_platforms

    def get_status(self):
        """Get current BIOS status (filtered to synced collections).

        Returns:
            dict with BIOS status summary
        """
        with self._lock:
            synced_platforms = self.get_platforms_in_synced_collections()

            # Filter to synced collections
            platforms_in_sync = {
                slug: p for slug, p in self.platform_status.items()
                if slug in synced_platforms
            }

            # Lenient: ready if at least 1 BIOS file present
            platforms_ready = sum(
                1 for p in platforms_in_sync.values()
                if p.get('present', 0) > 0
            )
            total_platforms = len(platforms_in_sync)

            # Filter downloading/failures to synced collections only
            synced_downloading = [s for s in self.downloads_in_progress if s in synced_platforms]
            synced_failures = {s: msg for s, msg in self.download_failures.items() if s in synced_platforms}

            return {
                'downloading_count': len(synced_downloading),
                'ready_count': len(self.platforms_ready),
                'failed_count': len(synced_failures),
                'downloading': synced_downloading,
                'ready': list(self.platforms_ready),
                'failures': synced_failures,
                'platforms': dict(platforms_in_sync),
                'total_platforms': total_platforms,
                'platforms_ready': platforms_ready,
            }
