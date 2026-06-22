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

# PIL is optional - used for Steam grid image generation
try:
    from PIL import Image
    PIL_AVAILABLE = True
except ImportError as e:
    import sys
    print(f"[ROMM-SYNC] PIL import failed: {e}", file=sys.stderr)
    print(f"[ROMM-SYNC] sys.path: {sys.path}", file=sys.stderr)
    PIL_AVAILABLE = False
    Image = None

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
        self.original_total = 0  # Initialize BEFORE load_games_cache (will be set by load)
        self.cached_games = self.load_games_cache()  # This sets original_total from cache
    
    def save_games_data(self, games_data, original_total=None):
        """Non-blocking cache save with memory optimization

        Args:
            games_data: List of game dictionaries (after grouping)
            original_total: Optional original ungrouped ROM count from server
        """
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
                        'discs': game.get('discs', []),  # Preserve disc data for multi-disc games
                        '_sibling_files': game.get('_sibling_files', [])  # Preserve regional variants
                    }
                    processed_games.append(clean_game)
                
                cache_data = {
                    'timestamp': time.time(),
                    'games': processed_games,  # Use cleaned data
                    'count': len(processed_games),
                    'original_total': original_total if original_total is not None else len(processed_games)
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

            # Load original ungrouped count from cache (for accurate server comparison)
            self.original_total = cache_data.get('original_total', len(games))
            print(f"🔍 CACHE LOAD: Read original_total={self.original_total} from cache, len(games)={len(games)}")

            # Detect old/invalid cache: if original_total equals grouped count AND
            # the cache doesn't explicitly have an original_total field (old cache format)
            # Only force refresh if original_total field is MISSING from cache
            has_original_total_field = 'original_total' in cache_data
            if not has_original_total_field and len(games) > 0:
                # Old cache format (before fix) - no original_total field
                print(f"⚠️ Old cache format detected (no original_total field) - marking for refresh")
                self.original_total = 0  # Force refresh by making count check fail
                print(f"🔍 CACHE LOAD: Set original_total to 0 (forced refresh)")
            elif has_original_total_field and self.original_total == len(games):
                # Only warn if original_total equals grouped count but is suspicious
                # (This could be valid if there are no regional variants)
                print(f"ℹ️ Cache has original_total == grouped count ({self.original_total})")
                # Don't force refresh - this could be valid (no regional variants)

            # CRITICAL: Always resolve platform names from platform_slug using the mapping
            # This ensures cached games display proper names even if they were cached with slugs
            for game in games:
                if isinstance(game, dict):
                    platform_slug = game.get('platform_slug')
                    if platform_slug:
                        # Use mapping to get proper platform name (fallback mapping always available)
                        game['platform'] = self.get_platform_name(platform_slug)

            print(f"📂 Loaded {len(games)} games from cache (original total: {self.original_total})")
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
                'overwrite_behavior': '0',
                'startup_sync_enabled': 'true',
                'startup_scan_days': '7',
                'last_shutdown_time': ''
            }
            self.config['System'] = {
                'autostart': 'false',
                'debug_mode': 'false'
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
                'client_version': '1.5',
                'sync_enabled': 'true'
            }
            self.config['Steam'] = {
                'enabled': 'false',
                'userdata_path': '',
                'collections': '',
                'artwork_enabled': 'true',
                'artwork_quality': 'high',
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
            'client_version': '1.5',
            'sync_enabled': 'true'
        }

        # Add any missing Device fields
        for key, default_value in device_defaults.items():
            if key not in self.config['Device']:
                self.config['Device'][key] = default_value
                modified = True

        # Ensure Steam section exists (added in v1.5+)
        if 'Steam' not in self.config:
            self.config['Steam'] = {}
            modified = True

        steam_defaults = {
            'enabled': 'false',
            'userdata_path': '',
            'collections': '',
            'artwork_enabled': 'true',
            'artwork_quality': 'high',
        }
        for key, default_value in steam_defaults.items():
            if key not in self.config['Steam']:
                self.config['Steam'][key] = default_value
                modified = True

        # Ensure System section has debug_mode (added in v1.5+)
        if 'System' not in self.config:
            self.config['System'] = {}
            modified = True

        if 'debug_mode' not in self.config['System']:
            self.config['System']['debug_mode'] = 'false'
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
        if section == 'RomM' and key in ['username', 'password', 'client_token'] and value:
            value = self._decrypt(value)
        
        return value

    def set(self, section, key, value):
        """Set a setting value with encryption for sensitive data"""
        if section not in self.config:
            self.config[section] = {}
        
        # Encrypt sensitive fields
        if section == 'RomM' and key in ['username', 'password', 'client_token'] and value:
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

class CoverArtManager:
    """Manages cover art downloads and local caching for Steam grid images"""

    def __init__(self, settings_manager, romm_client):
        self.settings = settings_manager
        self.romm_client = romm_client
        self.cache_dir = Path.home() / 'RomMSync' / 'covers'
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    def get_cover_cache_path(self, rom_id, platform_slug):
        """Return local cache path for a ROM's cover art

        Args:
            rom_id: ROM identifier from RomM API
            platform_slug: Platform slug for directory organization

        Returns:
            Path object for cached cover image
        """
        platform_dir = self.cache_dir / platform_slug
        platform_dir.mkdir(parents=True, exist_ok=True)
        return platform_dir / f"{rom_id}.jpg"

    def download_cover(self, rom_id, platform_slug, cover_url, progress_callback=None):
        """Download cover art from RomM API with caching

        Args:
            rom_id: ROM identifier
            platform_slug: Platform slug for organization
            cover_url: Cover URL from RomM API (path_cover_large or path_cover_small)
            progress_callback: Optional progress callback

        Returns:
            Tuple of (success: bool, local_path: Path or None, message: str)
        """
        if not cover_url:
            return False, None, "No cover URL provided"

        cache_path = self.get_cover_cache_path(rom_id, platform_slug)

        # Return cached cover if exists and valid (at least 1KB)
        if cache_path.exists() and cache_path.stat().st_size > 1024:
            logging.debug(f"Using cached cover for ROM {rom_id}")
            return True, cache_path, "Using cached cover"

        # Download cover from RomM
        try:
            # Build full URL (cover_url is a path like /assets/romm/resources/...)
            from urllib.parse import urljoin
            full_url = urljoin(self.romm_client.base_url, cover_url)

            logging.debug(f"Downloading cover from: {full_url}")
            response = self.romm_client.session.get(full_url, stream=True, timeout=30)

            if response.status_code != 200:
                logging.warning(f"Cover download failed: HTTP {response.status_code}")
                return False, None, f"HTTP {response.status_code}"

            # Stream download to cache file
            temp_path = cache_path.with_suffix('.tmp')
            with open(temp_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)

            # Verify file was written and has content
            if temp_path.stat().st_size < 100:
                temp_path.unlink()
                return False, None, "Downloaded file too small (corrupt)"

            # Move temp file to final location
            temp_path.rename(cache_path)
            logging.debug(f"Cover cached at: {cache_path}")
            return True, cache_path, "Cover downloaded"

        except Exception as e:
            logging.warning(f"Cover download failed for ROM {rom_id}: {e}")
            # Clean up temp file if it exists
            if temp_path.exists():
                temp_path.unlink()
            return False, None, f"Download failed: {e}"

class SteamGridImageGenerator:
    """Generate Steam grid images from cover art in multiple formats"""

    # Steam grid image dimensions
    GRID_PORTRAIT = (600, 900)    # Vertical cover for library
    GRID_LANDSCAPE = (920, 430)   # Horizontal grid view
    GRID_HERO = (1920, 620)       # Hero/banner for big picture mode
    GRID_ICON = (256, 256)        # Square icon for overlay/search

    @staticmethod
    def generate_grid_images(source_image_path, output_dir, appid):
        """Generate all Steam grid image variants from a cover image

        Args:
            source_image_path: Path to source cover image (jpg/png)
            output_dir: Steam grid directory (userdata/config/grid/)
            appid: Steam shortcut appid (signed int32)

        Returns:
            Tuple of (success: bool, generated_count: int, message: str)
        """
        # Try importing PIL directly if not available at module level
        global PIL_AVAILABLE, Image
        if not PIL_AVAILABLE:
            try:
                from PIL import Image as PILImage
                Image = PILImage
                PIL_AVAILABLE = True
                logging.info("[PIL] Successfully imported PIL inside generate_grid_images")
            except ImportError as e:
                logging.error(f"[PIL] PIL import failed in generate_grid_images: {e}")
                return False, 0, f"Pillow not installed (pip install Pillow) - {e}"

        if not Path(source_image_path).exists():
            return False, 0, "Source image not found"

        try:
            # Load source image
            source = Image.open(source_image_path)

            # Convert RGBA to RGB if needed (Steam expects RGB)
            if source.mode == 'RGBA':
                rgb_source = Image.new('RGB', source.size, (0, 0, 0))
                rgb_source.paste(source, mask=source.split()[3])  # Use alpha channel as mask
                source = rgb_source
            elif source.mode != 'RGB':
                source = source.convert('RGB')

            # Convert appid to unsigned for filenames
            unsigned_appid = appid if appid >= 0 else appid + 0x100000000

            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

            generated = 0

            # Generate portrait (600x900) - typical for cover art
            portrait_path = output_dir / f"{unsigned_appid}p.png"
            portrait = SteamGridImageGenerator._resize_and_pad(
                source.copy(),
                SteamGridImageGenerator.GRID_PORTRAIT
            )
            portrait.save(portrait_path, 'PNG', optimize=True)
            generated += 1

            # Generate landscape (920x430) - for grid view
            landscape_path = output_dir / f"{unsigned_appid}.png"
            landscape = SteamGridImageGenerator._resize_and_crop(
                source.copy(),
                SteamGridImageGenerator.GRID_LANDSCAPE
            )
            landscape.save(landscape_path, 'PNG', optimize=True)
            generated += 1

            # Generate hero (1920x620) - for big picture mode
            hero_path = output_dir / f"{unsigned_appid}_hero.png"
            hero = SteamGridImageGenerator._resize_and_crop(
                source.copy(),
                SteamGridImageGenerator.GRID_HERO
            )
            hero.save(hero_path, 'PNG', optimize=True)
            generated += 1

            # Generate square icon (256x256) - for Steam overlay and search
            icon_path = output_dir / f"{unsigned_appid}_icon.png"
            icon = SteamGridImageGenerator._resize_and_pad(
                source.copy(),
                SteamGridImageGenerator.GRID_ICON
            )
            icon.save(icon_path, 'PNG', optimize=True)
            generated += 1

            return True, generated, f"Generated {generated} grid images"

        except Exception as e:
            logging.warning(f"Image processing failed: {e}")
            return False, 0, f"Image processing failed: {e}"

    @staticmethod
    def _resize_and_pad(image, target_size):
        """Resize image preserving aspect ratio with padding (letterbox/pillarbox)

        Args:
            image: PIL Image object
            target_size: Target (width, height) tuple

        Returns:
            PIL Image object resized and padded to target_size
        """
        from PIL import Image

        # Calculate aspect-ratio-preserving size (scale to fit, up or down)
        img_ratio = image.width / image.height
        target_ratio = target_size[0] / target_size[1]
        if img_ratio > target_ratio:
            new_width = target_size[0]
            new_height = int(new_width / img_ratio)
        else:
            new_height = target_size[1]
            new_width = int(new_height * img_ratio)
        image = image.resize((new_width, new_height), Image.Resampling.LANCZOS)

        # Create new image with black background
        result = Image.new('RGB', target_size, (0, 0, 0))

        # Center paste the resized image
        paste_x = (target_size[0] - image.width) // 2
        paste_y = (target_size[1] - image.height) // 2
        result.paste(image, (paste_x, paste_y))

        return result

    @staticmethod
    def _resize_and_crop(image, target_size):
        """Resize and crop image to fill target size (cover fit)

        Args:
            image: PIL Image object
            target_size: Target (width, height) tuple

        Returns:
            PIL Image object resized and cropped to target_size
        """
        from PIL import Image

        # Calculate crop to fit target aspect ratio
        target_aspect = target_size[0] / target_size[1]
        image_aspect = image.width / image.height

        if image_aspect > target_aspect:
            # Image is wider - crop width
            new_width = int(image.height * target_aspect)
            left = (image.width - new_width) // 2
            image = image.crop((left, 0, left + new_width, image.height))
        else:
            # Image is taller - crop height
            new_height = int(image.width / target_aspect)
            top = (image.height - new_height) // 2
            image = image.crop((0, top, image.width, top + new_height))

        # Resize to exact target size
        image = image.resize(target_size, Image.Resampling.LANCZOS)
        return image

    @staticmethod
    def generate_square_icon(source_image_path, output_path, size=256):
        """Generate a square icon from cover art by extracting center square

        Args:
            source_image_path: Path to source cover image
            output_path: Path where to save the icon
            size: Icon size in pixels (default 256x256)

        Returns:
            Tuple of (success: bool, message: str)
        """
        # Try importing PIL directly if not available at module level
        global PIL_AVAILABLE, Image
        if not PIL_AVAILABLE:
            try:
                from PIL import Image as PILImage
                Image = PILImage
                PIL_AVAILABLE = True
                logging.info("[PIL] Successfully imported PIL inside generate_square_icon")
            except ImportError as e:
                logging.error(f"[PIL] PIL import failed in generate_square_icon: {e}")
                return False, f"Pillow not installed - {e}"

        if not Path(source_image_path).exists():
            return False, "Source image not found"

        try:
            # Load source image
            source = Image.open(source_image_path)

            # Convert RGBA to RGB if needed
            if source.mode == 'RGBA':
                rgb_source = Image.new('RGB', source.size, (255, 255, 255))
                rgb_source.paste(source, mask=source.split()[3])
                source = rgb_source
            elif source.mode != 'RGB':
                source = source.convert('RGB')

            # Extract center square
            width, height = source.size

            if width > height:
                # Wider image - crop width to match height
                left = (width - height) // 2
                icon = source.crop((left, 0, left + height, height))
            elif height > width:
                # Taller image - crop height to match width
                top = (height - width) // 2
                icon = source.crop((0, top, width, top + width))
            else:
                # Already square
                icon = source

            # Resize to target size
            icon = icon.resize((size, size), Image.Resampling.LANCZOS)

            # Save as PNG for transparency support
            icon.save(output_path, 'PNG', optimize=True)

            return True, f"Icon saved to {output_path}"

        except Exception as e:
            logging.warning(f"Icon generation failed: {e}")
            return False, f"Icon generation failed: {e}"


def _find_7z():
    """Locate a 7-Zip CLI binary, returning its path or None.

    Search order: $ROMM_7ZIP env override, a bundled static binary shipped with
    the app (bin/7zz next to sync_core or one level up — e.g. the Decky plugin's
    bin/, since SteamOS has no system 7z), then anything on PATH. The bundled
    static 7zz has no dependencies, avoiding py7zr's C-extension bundling issues.
    """
    import shutil as _sh
    env = os.environ.get('ROMM_7ZIP')
    if env and os.path.isfile(env) and os.access(env, os.X_OK):
        return env
    here = Path(__file__).resolve().parent
    for cand in (here / 'bin' / '7zz', here.parent / 'bin' / '7zz'):
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    for exe in ('7zz', '7z', '7za', '7zr'):
        found = _sh.which(exe)
        if found:
            return found
    return None


def _archive_member_names(archive_path):
    """List member names in a .zip or .7z archive. Returns [] on failure/unsupported.

    .7z uses py7zr if installed, else the system 7z/7za CLI. Returns [] (rather
    than raising) when no .7z backend is available so callers degrade gracefully.
    """
    archive_path = Path(archive_path)
    ext = archive_path.suffix.lower()
    try:
        if ext == '.zip':
            import zipfile
            with zipfile.ZipFile(archive_path, 'r') as zf:
                return zf.namelist()
        if ext == '.7z':
            try:
                import py7zr
                with py7zr.SevenZipFile(archive_path, 'r') as z:
                    return z.getnames()
            except ImportError:
                import subprocess
                exe = _find_7z()
                if exe:
                    out = subprocess.run([exe, 'l', '-slt', str(archive_path)],
                                         capture_output=True, text=True).stdout
                    return [ln[len('Path = '):] for ln in out.splitlines()
                            if ln.startswith('Path = ')][1:]  # [0] is the archive itself
                logging.info("No .7z lister available (install py7zr or p7zip)")
                return []
    except Exception as e:
        logging.warning(f"Could not list archive {archive_path}: {e}")
    return []


def _extract_archive(archive_path, dest_dir):
    """Extract a .zip or .7z archive to dest_dir. Returns True on success.

    .7z uses py7zr if installed, else the system 7z/7za/7zr CLI. Returns False
    (without raising) when .7z support is unavailable, so the caller can leave the
    archive in place (RetroArch reads .zip/.7z natively for most cores anyway).
    """
    archive_path = Path(archive_path)
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    ext = archive_path.suffix.lower()
    try:
        if ext == '.zip':
            import zipfile
            with zipfile.ZipFile(archive_path, 'r') as zf:
                zf.extractall(dest_dir)
            return True
        if ext == '.7z':
            try:
                import py7zr
                with py7zr.SevenZipFile(archive_path, 'r') as z:
                    z.extractall(path=dest_dir)
                return True
            except ImportError:
                import subprocess
                exe = _find_7z()
                if exe:
                    subprocess.run([exe, 'x', '-y', f'-o{dest_dir}', str(archive_path)],
                                   check=True, capture_output=True)
                    return True
                logging.warning("No .7z extractor available (install py7zr or p7zip); "
                                "leaving archive as-is for RetroArch to load")
                return False
    except Exception as e:
        logging.warning(f"Failed to extract {archive_path}: {e}")
    return False


class RomMClient:
    """Client for interacting with RomM API"""
    
    def __init__(self, base_url, username=None, password=None, client_token=None):
        self.base_url = base_url.rstrip('/')
        self.session = requests.Session()
        self.authenticated = False
        self.client_token = None  # RomM Client API Token (rmm_...) if used

        # OAuth2 token storage
        self.access_token = None
        self.refresh_token = None
        self.token_type = 'bearer'
        self.token_expiry = None

        # Cover art manager (set externally after initialization)
        self.cover_manager = None

        # Force HTTP/2 and connection reuse
        from requests.adapters import HTTPAdapter
        from urllib3.util.retry import Retry

        adapter = HTTPAdapter(
            pool_connections=10,
            pool_maxsize=10,
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
        
        # Prefer a Client API Token (RomM's recommended companion-app auth) over
        # storing the user's password. Fall back to Basic auth if no token.
        if client_token:
            self.authenticate_with_token(client_token)
        elif username and password:
            self.authenticate(username, password)

    def authenticate_with_token(self, client_token):
        """Authenticate using a RomM Client API Token (Authorization: Bearer rmm_...).

        This is RomM's recommended auth for companion apps — scope-narrowed and
        revocable, unlike Basic auth which needs the account password. Returns
        True on success.
        """
        if not client_token:
            return False
        try:
            self.session.headers.update({'Authorization': f'Bearer {client_token}'})
            test = self.session.get(
                urljoin(self.base_url, '/api/roms'), params={'limit': 1}, timeout=10
            )
            if test.status_code == 200:
                self.client_token = client_token
                self.authenticated = True
                print("✅ Client API Token authentication successful")
                return True
            # Bad/expired token — drop the header so we don't poison later attempts.
            self.session.headers.pop('Authorization', None)
            print(f"❌ Client API Token rejected: HTTP {test.status_code}")
            return False
        except Exception as e:
            self.session.headers.pop('Authorization', None)
            print(f"❌ Client API Token auth error: {e}")
            return False

    def exchange_pair_code(self, code):
        """Exchange an 8-digit pairing code for a Client API Token.

        The user creates a token in the RomM web UI and starts pairing, which
        shows a short code. The device posts that code to the unauthenticated
        /api/client-tokens/exchange endpoint and receives the full rmm_ token.
        Returns the raw token string on success, else None.
        """
        if not code:
            return None
        try:
            resp = self.session.post(
                urljoin(self.base_url, '/api/client-tokens/exchange'),
                json={'code': str(code).strip()},
                timeout=15,
            )
            if resp.status_code in (200, 201):
                raw = resp.json().get('raw_token')
                if raw:
                    print("✅ Pairing code exchanged for Client API Token")
                    return raw
                print("⚠️ Exchange succeeded but no raw_token in response")
                return None
            print(f"❌ Pairing exchange failed: HTTP {resp.status_code}: {resp.text[:200]}")
            return None
        except Exception as e:
            print(f"❌ Pairing exchange error: {e}")
            return None

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
                'client_version': client_version or '1.5',
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
                    # RomM 4.9.0 made file expansion opt-in (with_files defaults to
                    # False); without this the `files` field comes back empty.
                    'with_files': 'true',
                    'fields': 'id,name,fs_name,platform_name,platform_slug,files,multi,path_cover_large,path_cover_small'
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
                urljoin(self.base_url, '/api/roms'),
                params={
                    'collection_id': collection_id,
                    # RomM 4.9.0: file expansion is opt-in (with_files default False).
                    'with_files': 'true',
                    'fields': 'id,name,fs_name,fs_extension,platform_name,platform_slug,files,multi,path_cover_large,path_cover_small,sibling_roms,rom_user'
                },
                timeout=30
            )

            if response.status_code == 200:
                data = response.json()
                items = data.get('items', [])

                # Return items as-is: in the collection view each ROM the user
                # added is shown as its own entry (children are NOT grouped under
                # their parent folder ROM — that is a platform-view concern).
                return items
            else:
                print(f"Failed to get collection ROMs: {response.status_code}")
                return []
                
        except Exception as e:
            print(f"Error fetching collection ROMs: {e}")
            return []

    def _group_sibling_roms(self, items):
        """Group sibling ROMs (regional variants) under a main ROM

        Args:
            items: List of ROM dicts from API

        Returns:
            List of ROMs with siblings grouped under _sibling_files
        """
        # Group sibling ROMs (regional variants, etc.) under a main ROM
        # ROMs with 'sibling_roms' arrays are related regional/language variants
        # (RomM 4.9.0 renamed this field from 'siblings' to 'sibling_roms')
        sibling_groups = {}  # Map of group_key -> list of ROMs in that group
        standalone_roms = []  # ROMs without siblings

        # First pass: build sibling groups
        for rom in items:
            siblings_list = rom.get('sibling_roms', [])
            rom_id = rom.get('id')

            if siblings_list:
                # This ROM has siblings - create a group key from all related ROM IDs
                all_related_ids = sorted([rom_id] + [s['id'] for s in siblings_list])
                group_key = tuple(all_related_ids)

                if group_key not in sibling_groups:
                    sibling_groups[group_key] = []
                sibling_groups[group_key].append(rom)
            else:
                # Standalone ROM (no siblings)
                standalone_roms.append(rom)

        # Second pass: pick a "main" ROM for each sibling group and attach others
        result_roms = []
        for group_key, group_roms in sibling_groups.items():
            if len(group_roms) <= 1:
                # Single ROM in group (sibling not in this collection)
                result_roms.extend(group_roms)
                continue

            # Pick the "main" ROM - prefer the folder ROM (parent that contains all
            # variant files).  The folder ROM has the most entries in its `files`
            # array (one per variant); individual file ROMs typically have 1 or 0.
            # Fall back to the original extension / is_main_sibling logic when no
            # `files` data is available (e.g. older API responses).
            main_rom = None
            sibling_files = []

            has_files_data = any(rom.get('files') for rom in group_roms)
            candidates = (
                sorted(group_roms, key=lambda r: len(r.get('files', [])), reverse=True)
                if has_files_data
                else group_roms
            )

            for rom in candidates:
                fs_extension = rom.get('fs_extension', '')
                is_main = rom.get('rom_user', {}).get('is_main_sibling', False)

                # Folder ROMs have no extension or empty extension
                if not fs_extension or is_main:
                    if main_rom is None:
                        main_rom = rom
                    else:
                        sibling_files.append(rom)
                else:
                    sibling_files.append(rom)

            # If no folder ROM found, use the first candidate as main
            if main_rom is None:
                main_rom = candidates[0]
                sibling_files = [r for r in group_roms if r is not main_rom]

            # Attach siblings to the main ROM
            if sibling_files:
                main_rom['_sibling_files'] = sibling_files
                rom_name = main_rom.get('name', '') or main_rom.get('fs_name', '')
                print(f"Grouped ROM '{rom_name}' with {len(sibling_files)} regional variant(s)")

            result_roms.append(main_rom)

        # Add standalone ROMs
        result_roms.extend(standalone_roms)

        if len(items) != len(result_roms):
            print(f"Grouped {len(items)} API ROMs → {len(result_roms)} display ROMs ({len(items) - len(result_roms)} variants grouped)")

        return result_roms

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

                return all_games, total_games  # Return ungrouped count for cache comparison

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
                        # RomM 4.9.0: file expansion is opt-in (with_files default False).
                        'with_files': 'true',
                        'fields': 'id,name,fs_name,fs_extension,platform_name,platform_slug,files,multi,path_cover_large,path_cover_small,sibling_roms,rom_user'
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
                    'accumulated_games': [],  # Don't send ungrouped games - grouping happens at the end
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

        # Group sibling ROMs (regional variants) before returning
        final_games = self._group_sibling_roms(final_games)

        # Return just the grouped games (total_games is tracked by caller)
        return final_games
        
    def download_rom(self, rom_id, rom_name, download_path, progress_callback=None, cancellation_checker=None, file_ids=None):
        """Download a ROM file with progress tracking

        Args:
            rom_id: ROM identifier
            rom_name: ROM display name
            download_path: Path to save the download
            progress_callback: Optional callback for progress updates
            cancellation_checker: Optional callable that returns True if download should be cancelled
            file_ids: Optional comma-separated list of file IDs to download (for multi-file ROMs)
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
            files = rom_details.get('files', [])
            files_amount = len(files)
            file_extension = rom_details.get('fs_extension', '')

            is_folder = rom_details.get('multi', False) or \
                    files_amount > 1
            
            if (files_amount == 1 and file_extension == ''):
                download_path = download_path / files[0].get('file_name', 'file')
                print("Single foldered file detected, using download path: " + download_path.__str__())

            if is_folder:
                folder_name = rom_details.get('fs_name', filename)
                folder_path = download_path.parent / folder_name
                folder_path.mkdir(parents=True, exist_ok=True)

                # When downloading specific files (file_ids), save inside the folder
                # When downloading entire folder, use the folder as download_path
                if file_ids:
                    # Individual file download - rom_name contains the filename
                    download_path = folder_path / rom_name
                    print(f"Downloading individual file to: {download_path}")
                else:
                    # Entire folder download (as zip)
                    download_path = folder_path

            if file_ids:
                encoded_filename = quote(filename)
                api_endpoint = f'/api/roms/{rom_id}/content/{encoded_filename}'
                params = {'file_ids': file_ids}
                print(f"Downloading specific file from folder '{filename}', file_ids: {file_ids}")
                print(f"Individual file will be saved as: {rom_name}")
            else:
                encoded_filename = quote(filename)
                api_endpoint = f'/api/roms/{rom_id}/content/{encoded_filename}'
                params = {}
                print(f"Downloading entire ROM: {filename}")

            full_url = urljoin(self.base_url, api_endpoint)
            print(f"Requesting ROM download: {full_url}")
            if params:
                print(f"With params: {params}")

            response = self.session.get(
                full_url,
                params=params if params else None,
                stream=True,
                timeout=30
            )

            print(f"Response status: {response.status_code}")
            print(f"Response headers: {dict(response.headers)}")
            
            if response.status_code != 200:
                print(f"ROM download failed with status {response.status_code}")
                return False, f"API download failed: HTTP {response.status_code}"
            
            # Check if we're getting HTML (error page) instead of a ROM file
            content_type = response.headers.get('content-type', '').lower()
            print(f"Content-Type: {content_type}")
            print(f"Response headers: {dict(response.headers)}")
            if 'text/html' in content_type:
                sample = response.content[:200]
                print(f"Got HTML response: {sample}")
                return False, "API returned HTML page instead of ROM file"
            
            # Get total file size from headers
            total_size = int(response.headers.get('content-length', 0))

            # For folders, use ROM metadata size for progress tracking
            # BUT only if downloading the entire folder, not individual files
            if is_folder and not file_ids:
                metadata_size = rom_details.get('fs_size_bytes', 0)
                if metadata_size > 0:
                    total_size = metadata_size
                    print(f"Using ROM metadata size for folder: {total_size} bytes")
            elif file_ids:
                print(f"Using actual response size for individual file: {total_size} bytes")
            
            # Create progress tracker
            if progress_callback:
                progress = DownloadProgress(total_size, rom_name)
            
            # Ensure download directory exists
            download_path.parent.mkdir(parents=True, exist_ok=True)
            
            # Download with progress tracking
            actual_downloaded = 0
            start_time = time.time()

            # When using file_ids, we're downloading specific files (not a zip)
            # even if the parent ROM is a folder
            if is_folder and not file_ids:
                # For folders, download as zip then extract
                import io
                import zipfile
                import tempfile

                # Check for cancellation before starting folder download
                if cancellation_checker and cancellation_checker():
                    raise DownloadCancelledException(f"Download cancelled: {rom_name}")

                # Download to temporary file instead of memory to avoid OOM crashes
                temp_path = None
                try:
                    with tempfile.NamedTemporaryFile(delete=False, suffix='.zip') as temp_file:
                        temp_path = temp_file.name
                        
                        # Stream download directly to temporary file
                        for chunk in response.iter_content(chunk_size=8192):
                            # Check for cancellation
                            if cancellation_checker and cancellation_checker():
                                raise DownloadCancelledException(f"Download cancelled: {rom_name}")

                            if chunk:
                                temp_file.write(chunk)
                                actual_downloaded += len(chunk)

                                # Update progress
                                if progress_callback and total_size > 0:
                                    progress_info = progress.update(len(chunk))
                                    progress_callback(progress_info)
                
                except DownloadCancelledException:
                    # Clean up temp file on cancellation
                    if temp_path and os.path.exists(temp_path):
                        os.unlink(temp_path)
                    raise
                
                # Check for cancellation before extraction
                if cancellation_checker and cancellation_checker():
                    if temp_path and os.path.exists(temp_path):
                        os.unlink(temp_path)
                    raise DownloadCancelledException(f"Download cancelled: {rom_name}")
                
                # Extract from temporary file
                try:
                    with zipfile.ZipFile(temp_path, 'r') as zip_ref:
                        # Extract everything, INCLUDING the .m3u playlist. RomM's
                        # download endpoint generates a "<fs_name>.m3u" inside the zip
                        # for multi-disc/multi-file ROMs (listing the disc .chd/.cue
                        # files). RetroArch needs that playlist to boot multi-disc
                        # games and to swap discs — without it, the game lands on the
                        # console BIOS. (Previously these were stripped on the false
                        # assumption that RomM doesn't provide them.)
                        for member in zip_ref.namelist():
                            zip_ref.extract(member, download_path)
                finally:
                    # Clean up temporary file
                    if temp_path and os.path.exists(temp_path):
                        os.unlink(temp_path)
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
                
                # After successful download, check if extraction is needed (only for
                # single-file archives: .zip or .7z). Console ROM archives are left
                # as-is (RetroArch loads them natively); only directory-based PC games
                # are extracted.
                if download_path.suffix.lower() in ('.zip', '.7z') and actual_downloaded > 0:
                    file_list = _archive_member_names(download_path)

                    # Only extract if it looks like a directory-based game
                    has_subdirs = any('/' in f and not f.endswith('/') for f in file_list)
                    has_pc_game_files = any(f.lower().endswith(('.exe', '.bat', '.cfg', '.ini', '.dll')) for f in file_list)
                    should_extract = has_subdirs and has_pc_game_files

                    if should_extract:
                        extract_dir = download_path.parent / download_path.stem
                        if _extract_archive(download_path, extract_dir):
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
            logging.exception("Download exception")
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

    def download_save_by_id(self, save_id, save_type, download_path, device_id=None, fallback_url=None, session_id=None):
        """Download a specific save/state by its ID.

        Args:
            save_id: The save/state ID to download
            save_type: 'saves' or 'states'
            download_path: Path where to save the downloaded file
            device_id: Optional device ID for optimistic download confirmation
            fallback_url: Optional fallback download_path URL from metadata
        Returns:
            True if download succeeded, False otherwise
        """
        if not self.ensure_authenticated() or not save_id:
            return False

        try:
            download_url = urljoin(self.base_url, f"/api/{save_type}/{save_id}/content")
            params = []
            if device_id:
                params.append(f"device_id={device_id}")
                params.append("optimistic=true")
            if session_id:
                params.append(f"session_id={session_id}")
            if params:
                download_url += "?" + "&".join(params)

            response = self.session.get(download_url, stream=True, timeout=30)
            used_device_id = True

            # Retry without device_id on 404
            if response.status_code == 404 and device_id:
                download_url = urljoin(self.base_url, f"/api/{save_type}/{save_id}/content")
                response = self.session.get(download_url, stream=True, timeout=30)
                used_device_id = False

            # Try fallback URL (download_path from metadata)
            if response.status_code != 200 and fallback_url:
                full_fallback = urljoin(self.base_url, fallback_url)
                logging.debug(f"Trying fallback URL for {save_type} {save_id}: {fallback_url}")
                response = self.session.get(full_fallback, stream=True, timeout=30)
                used_device_id = False

            if response.status_code != 200:
                logging.warning(f"Failed to download {save_type} {save_id}: HTTP {response.status_code}")
                return False

            content_type = response.headers.get('content-type', '')
            if 'text/html' in content_type.lower():
                logging.warning(f"Got HTML response instead of binary for {save_type} {save_id}")
                return False

            download_path.parent.mkdir(parents=True, exist_ok=True)
            actual_bytes = 0
            with open(download_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    if chunk:
                        f.write(chunk)
                        actual_bytes += len(chunk)

            if download_path.exists() and download_path.stat().st_size > 0:
                if device_id and used_device_id:
                    self.confirm_save_downloaded(save_id, save_type, device_id)
                return True

            logging.warning(f"Download failed or empty for {save_type} {save_id}")
            return False

        except Exception as e:
            logging.warning(f"Error downloading {save_type} {save_id}: {e}")
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
        suffix = Path(file_path).suffix.lower()

        # Numbered state slots: .state1 through .state9
        match = re.match(r'\.state(\d+)$', suffix)
        if match:
            return f"slot{match.group(1)}", True, 5

        # Quick/auto save state: .state (also covers .auto.state since Path.suffix returns .state)
        if 'state' in suffix:
            return "quicksave", True, 10

        # Battery / memory-card saves (.srm, .sav, .mcr, .eep, ...): the primary
        # per-ROM save. RomM's save-sync engine ignores slot=None rows
        # (slot_not_null filter), so a stable non-null slot is required. Use the
        # canonical "autosave" slot — the same value RomM's reference clients
        # (grout, muos-app, etc.) report the primary save under — so a game's
        # battery save pairs across ALL clients on (rom_id, slot) instead of
        # fragmenting per-client. Autocleanup matches grout (keep last 10).
        if suffix:
            return "autosave", True, 10

        return None, False, None

    @staticmethod
    def compute_content_hash(file_path):
        """Compute a save file's content hash matching RomM 4.9.0's save-sync engine.

        Must byte-for-byte match the server (backend assets_handler.compute_content_hash)
        or the /negotiate engine flags every save as a conflict. The server uses MD5:
          - Plain files: md5 of the raw bytes, read in 8192-byte chunks.
          - Zip files: per-entry "name:md5(content)" lines (entries sorted by name,
            directories skipped) joined by "\\n", then md5 of that combined string.
        RetroArch saves (.srm/.state) are plain files; the zip branch mirrors the
        server for completeness.

        Returns the hex digest string, or None on error.
        """
        import zipfile
        import hashlib
        try:
            file_path = Path(file_path)
            if zipfile.is_zipfile(file_path):
                with zipfile.ZipFile(file_path, 'r') as zf:
                    file_hashes = []
                    for name in sorted(zf.namelist()):
                        if not name.endswith('/'):
                            content = zf.read(name)
                            file_hash = hashlib.md5(content, usedforsecurity=False).hexdigest()
                            file_hashes.append(f"{name}:{file_hash}")
                    combined = "\n".join(file_hashes)
                    return hashlib.md5(combined.encode(), usedforsecurity=False).hexdigest()

            hash_obj = hashlib.md5(usedforsecurity=False)
            with open(file_path, 'rb') as f:
                while chunk := f.read(8192):
                    hash_obj.update(chunk)
            return hash_obj.hexdigest()
        except Exception as e:
            logging.debug(f"Failed to compute content hash for {file_path}: {e}")
            return None

    def negotiate_sync(self, device_id, saves):
        """Negotiate a save-sync session with RomM 4.9.0's engine.

        Args:
            device_id: This device's registered ID (must be sync_enabled server-side).
            saves: list of ClientSaveState dicts (see AutoSyncManager.build_sync_inventory).
                   Keys beginning with '_' are stripped before sending (local-only data).

        Returns:
            (session_id, operations) on success, or (None, []) on failure.
            Each operation is a dict with action in {upload, download, conflict, no_op}.
        """
        if not self.ensure_authenticated() or not device_id:
            return None, []

        # Strip local-only keys (e.g. '_path') the server doesn't expect.
        clean_saves = [
            {k: v for k, v in s.items() if not k.startswith('_')}
            for s in saves
        ]

        try:
            response = self.session.post(
                urljoin(self.base_url, '/api/sync/negotiate'),
                json={'device_id': device_id, 'saves': clean_saves},
                timeout=30
            )
            if response.status_code == 200:
                data = response.json()
                return data.get('session_id'), data.get('operations', [])

            logging.warning(f"Sync negotiate failed: HTTP {response.status_code}: {response.text[:300]}")
            return None, []
        except Exception as e:
            logging.warning(f"Error during sync negotiate: {e}")
            return None, []

    def complete_sync_session(self, session_id, play_sessions=None,
                              operations_completed=None, operations_failed=None):
        """Mark a sync session complete (optionally ingesting play sessions).

        operations_completed/operations_failed report how the planned operations
        actually went, matching RomM's reference client (grout) SyncCompletePayload.

        Returns True on success, False otherwise.
        """
        if not self.ensure_authenticated() or not session_id:
            return False

        try:
            payload = {}
            if operations_completed is not None:
                payload['operations_completed'] = int(operations_completed)
            if operations_failed is not None:
                payload['operations_failed'] = int(operations_failed)
            if play_sessions:
                payload['play_sessions'] = play_sessions
            response = self.session.post(
                urljoin(self.base_url, f'/api/sync/sessions/{session_id}/complete'),
                json=payload,
                timeout=15
            )
            if response.status_code == 200:
                return True
            logging.warning(f"Complete sync session {session_id} failed: HTTP {response.status_code}")
            return False
        except Exception as e:
            logging.warning(f"Error completing sync session {session_id}: {e}")
            return False

    def upload_save(self, rom_id, save_type, file_path, emulator=None, device_id=None, overwrite=False, slot=None, autocleanup=False, autocleanup_limit=None, session_id=None):
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
            if session_id:
                params.append(f'session_id={session_id}')
            if overwrite:
                params.append('overwrite=true')
            if slot:
                params.append(f'slot={quote(str(slot))}')
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
            import datetime
            now = datetime.datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H-%M-%S-%f")[:-3]

            # RetroArch auto-savestate "<content>.state.auto" is a two-part suffix.
            # The server convention puts the full suffix AFTER the timestamp
            # ("X [ts].state.auto"); the generic stem/suffix split would instead
            # emit "X.state [ts].auto", which fails to round-trip on download.
            if file_path.name.lower().endswith('.state.auto'):
                base = file_path.name[:-len('.state.auto')]
                romm_filename = f"{base} [{timestamp}].state.auto"
            else:
                original_basename = file_path.stem
                file_extension = file_path.suffix
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

    def upload_save_and_get_id(self, rom_id, save_type, file_path, emulator=None, device_id=None, overwrite=False, slot=None, autocleanup=False, autocleanup_limit=None, session_id=None):
        try:
            file_path = Path(file_path)

            # Build endpoint with optional parameters
            params = [f'rom_id={rom_id}']
            if emulator:
                params.append(f'emulator={emulator}')
            if device_id:
                params.append(f'device_id={device_id}')
            if session_id:
                params.append(f'session_id={session_id}')
            if overwrite:
                params.append('overwrite=true')
            if slot:
                params.append(f'slot={quote(str(slot))}')
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

            # RetroArch auto-savestate "<content>.state.auto" is a two-part suffix; the
            # server convention puts the full suffix AFTER the timestamp
            # ("X [ts].state.auto"). The generic stem/suffix split emits the broken
            # "X.state [ts].auto", which fails to round-trip on download (-> .state.state).
            is_auto_state = file_path.name.lower().endswith('.state.auto')
            if is_auto_state:
                original_basename = file_path.name[:-len('.state.auto')]
                file_extension = '.state.auto'
            else:
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
            'PlayStation': ['pcsx_rearmed', 'swanstation', 'beetle_psx', 'beetle_psx_hw'],
            'Nintendo Entertainment System': ['nestopia', 'fceumm', 'mesen'],
            'Game Boy': ['gambatte', 'sameboy', 'tgbdual'],
            'Game Boy Color': ['gambatte', 'sameboy', 'tgbdual'],
            'Game Boy Advance': ['mgba', 'vba_next', 'vbam'],
            'Sega Genesis': ['genesis_plus_gx', 'blastem', 'picodrive'],
            'Nintendo 64': ['mupen64plus_next', 'parallel_n64'],
            'Nintendo DS': ['desmume', 'melonds', 'melondsds'],
            'Nintendo - Nintendo DS': ['desmume', 'melonds', 'melondsds'],
            'nds': ['desmume', 'melonds', 'melondsds'], 
            'Sega Saturn': ['beetle_saturn', 'kronos'],
            'Arcade': ['mame', 'fbneo', 'fbalpha'],
            'PlayStation 2': ['pcsx2', 'play'],
            'Nintendo GameCube': ['dolphin'],
            'Sega Dreamcast': ['flycast', 'redream'],
            'Atari 2600': ['stella'],
            'Sony - PlayStation': ['pcsx_rearmed', 'swanstation', 'beetle_psx', 'beetle_psx_hw'],
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
            # Prefer SOFTWARE-rendered PSX cores first. The hardware-renderer
            # variants (*_hw) require a conformant Vulkan/GL driver and boot to a
            # black screen / BIOS on systems without one (e.g. mesa radv flagged as
            # "not a conformant Vulkan implementation"). Software cores run anywhere,
            # so they're the safe automatic default; _hw cores are last-resort.
            'playstation': ['pcsx_rearmed', 'swanstation', 'beetle_psx', 'mednafen_psx', 'beetle_psx_hw', 'mednafen_psx_hw'],
            'psx': ['pcsx_rearmed', 'swanstation', 'beetle_psx', 'mednafen_psx', 'beetle_psx_hw', 'mednafen_psx_hw'],
            'ps1': ['pcsx_rearmed', 'swanstation', 'beetle_psx', 'mednafen_psx', 'beetle_psx_hw', 'mednafen_psx_hw'],
            'genesis': ['genesis_plus_gx', 'blastem', 'picodrive'],
            'mega drive': ['genesis_plus_gx', 'blastem', 'picodrive'],
            'nintendo ds': ['desmume', 'melonds', 'melondsds'],
            'nds': ['desmume', 'melonds', 'melondsds'],
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
    
    def get_retroarch_config_setting(self, key, default=None):
        """Read a single setting value from retroarch.cfg, returning default if not found."""
        config_dir = self.find_retroarch_config_dir()
        if not config_dir:
            return default
        config_file = config_dir / 'retroarch.cfg'
        if not config_file.exists():
            return default
        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith(f'{key} = '):
                        return line.split('=', 1)[1].strip().strip('"')
        except Exception:
            pass
        return default

    def get_save_subdir_mode(self, save_type='saves'):
        """Return the folder-sorting mode RetroArch uses for saves or states.

        Reads sort_save(files|states)_enable and sort_save(files|states)_by_content_enable
        from retroarch.cfg.

        Returns:
            'core'    — subdirectory per core name (sort_*_enable = true)
            'content' — subdirectory mirrors ROM directory name (sort_*_by_content_enable = true)
            'flat'    — no subdirectory
        """
        if save_type == 'saves':
            enable_key = 'sort_savefiles_enable'
            content_key = 'sort_savefiles_by_content_enable'
        else:
            enable_key = 'sort_savestates_enable'
            content_key = 'sort_savestates_by_content_enable'

        if self.get_retroarch_config_setting(enable_key, 'false').lower() == 'true':
            return 'core'
        if self.get_retroarch_config_setting(content_key, 'false').lower() == 'true':
            return 'content'
        return 'flat'

    def parse_retroarch_save_dirs_from_config(self, config_dir):
        """Parse savefile_directory and savestate_directory from retroarch.cfg

        Args:
            config_dir: Path to the RetroArch config directory

        Returns:
            dict: Dictionary with 'saves' and/or 'states' keys pointing to configured paths
        """
        save_dirs = {}
        config_file = config_dir / 'retroarch.cfg'

        if not config_file.exists():
            return save_dirs

        try:
            with open(config_file, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()

                    # Parse savefile_directory setting
                    if line.startswith('savefile_directory = '):
                        # Extract the path, removing quotes
                        path_str = line.split('=', 1)[1].strip().strip('"')
                        if path_str:
                            # Expand ~ to home directory
                            save_path = Path(path_str).expanduser()
                            if save_path.exists():
                                save_dirs['saves'] = save_path
                                print(f"📁 Using configured savefile_directory: {save_path}")
                            else:
                                print(f"⚠️ Configured savefile_directory doesn't exist: {save_path}")

                    # Parse savestate_directory setting
                    elif line.startswith('savestate_directory = '):
                        # Extract the path, removing quotes
                        path_str = line.split('=', 1)[1].strip().strip('"')
                        if path_str:
                            # Expand ~ to home directory
                            state_path = Path(path_str).expanduser()
                            if state_path.exists():
                                save_dirs['states'] = state_path
                                print(f"📁 Using configured savestate_directory: {state_path}")
                            else:
                                print(f"⚠️ Configured savestate_directory doesn't exist: {state_path}")

        except Exception as e:
            print(f"⚠️ Error reading retroarch.cfg: {e}")

        return save_dirs

    def find_retroarch_dirs(self):
        """Find RetroArch save directories with comprehensive installation support"""
        save_dirs = {}

        # First, try to get the config directory and read configured paths
        config_dir = self.find_retroarch_config_dir()
        if config_dir:
            save_dirs = self.parse_retroarch_save_dirs_from_config(config_dir)
            if save_dirs:
                # User has configured paths and they exist - use them
                return save_dirs

        # If no configured paths found, fall back to auto-detection
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
                    print(f"📁 Found RetroArch save dirs (auto-detected): {base_dir}")
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
            # Flatpak RetroArch (prioritize over generic retrodeck folder)
            Path.home() / '.var/app/org.libretro.RetroArch/config/retroarch',
            # Native/Steam installations
            Path.home() / '.config/retroarch',
            Path.home() / '.retroarch',
            # RetroDECK generic folder (may not have retroarch.cfg directly)
            Path.home() / 'retrodeck',
            # Steam specific locations
            Path.home() / '.steam/steam/steamapps/common/RetroArch',
            Path.home() / '.local/share/Steam/steamapps/common/RetroArch',
            # Snap
            Path.home() / 'snap/retroarch/current/.config/retroarch',
            # AppImage
            Path.home() / '.retroarch-appimage',
        ]

        # Check each directory and verify retroarch.cfg exists
        for config_dir in possible_dirs:
            if config_dir.exists():
                # Check if retroarch.cfg actually exists in this directory
                config_file = config_dir / 'retroarch.cfg'
                if config_file.exists():
                    return config_dir
                else:
                    # Directory exists but no retroarch.cfg - try subdirectories
                    for subdir in ['config/retroarch', '.config/retroarch']:
                        subconfig_dir = config_dir / subdir
                        subconfig_file = subconfig_dir / 'retroarch.cfg'
                        if subconfig_file.exists():
                            return subconfig_dir

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

        # Method 2: Check the RetroDECK flatpak data directory — this only exists when
        # RetroDECK is actually installed as a flatpak, unlike ~/retrodeck which can be
        # any user-created directory (e.g., a ROM storage folder).
        if (Path.home() / '.var/app/net.retrodeck.retrodeck').exists():
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

    @staticmethod
    def ensure_m3u_for_disc_folder(folder_path, base_name=None):
        """Generate an <base_name>.m3u playlist for a multi-disc folder if missing.

        Mirrors RomM's own download-zip behaviour (which our whole-ROM download
        path preserves): list the disc images so RetroArch can boot the game and
        swap discs. Needed for the variant/individual-file download path, where
        RomM serves each disc as a bare file with no playlist.

        Rules (matching RomM): if .cue files are present, list those (avoids
        listing raw .bin tracks); otherwise list disc images. Only writes when
        there are 2+ disc entries and no .m3u already exists. Returns the m3u
        Path if written/empty-handed, else None.
        """
        folder = Path(folder_path)
        if not folder.is_dir():
            return None
        files = [f for f in folder.iterdir() if f.is_file()]
        if any(f.suffix.lower() == '.m3u' for f in files):
            return None  # RomM already provided one (or we already wrote it)

        cue = sorted(f for f in files if f.suffix.lower() == '.cue')
        disc_exts = {'.chd', '.iso', '.img', '.pbp', '.cso'}
        discs = cue if cue else sorted(f for f in files if f.suffix.lower() in disc_exts)
        if len(discs) < 2:
            return None  # single-disc (or non-disc) — no playlist needed

        base_name = base_name or folder.name
        m3u_path = folder / f"{base_name}.m3u"
        m3u_path.write_text("\n".join(d.name for d in discs) + "\n", encoding='utf-8')
        logging.info(f"Generated multi-disc playlist: {m3u_path.name} ({len(discs)} discs)")
        return m3u_path

    def convert_to_retroarch_filename(self, original_filename, save_type, target_directory, slot=None):
        """
        Convert RomM filename with timestamp to RetroArch expected format.

        For states, the slot parameter (e.g. "quicksave", "slot5") determines the
        correct extension (.state, .state5, etc.).
        """
        import re
        from pathlib import Path

        # Extract the base filename by removing timestamp brackets.
        # Pattern matches both timestamp styles seen across clients:
        #   space style (this client):  [YYYY-MM-DD HH-MM-SS-mmm]
        #   underscore style (RomM/grout/muos): [YYYY-MM-DD_HH-MM-SS]
        # The '_' is required — without it underscore-style timestamps survive into
        # the local filename (e.g. "Game [2026-06-17_08-18-45].srm"), so RetroArch
        # never loads the synced save. Kept char-scoped (no letters) so legitimate
        # ROM bracket tags like [T-Eng] or [!] are preserved.
        timestamp_pattern = r'\s*\[[\d\-\s:_]+\]'
        base_name = re.sub(timestamp_pattern, '', Path(original_filename).stem)

        # Get the original extension
        original_ext = Path(original_filename).suffix.lower()

        if save_type == 'saves':
            # For save files, preserve the original extension for all known save formats
            known_save_exts = {'.srm', '.sav', '.dsv', '.mcr', '.eep', '.fla', '.mpk', '.sra'}
            if original_ext in known_save_exts:
                target_filename = f"{base_name}{original_ext}"
            else:
                # Default to .srm if unknown save extension
                target_filename = f"{base_name}.srm"

        elif save_type == 'states':
            # RetroArch auto-savestate is literally "<content>.state.auto". Path.stem
            # only strips ".auto", leaving ".state" inside base_name, which would
            # otherwise produce a doubled "<content>.state.state". Stripping the
            # timestamp from the FULL name collapses BOTH the correct server form
            # ("X [ts].state.auto") AND the legacy broken form some of our earlier
            # uploads created ("X.state [ts].auto") down to "X.state.auto", so both
            # round-trip to the verbatim suffix here.
            stripped_full = re.sub(timestamp_pattern, '', original_filename).strip()
            if stripped_full.lower().endswith('.state.auto'):
                target_filename = stripped_full
            # Use slot info to determine correct state extension
            elif slot:
                target_filename = self._state_filename_from_slot(base_name, slot)
            else:
                target_filename = self.determine_state_filename(base_name, target_directory)

        else:
            # Unknown save type, keep original
            target_filename = original_filename

        return target_filename

    def _state_filename_from_slot(self, base_name, slot):
        """Map a RomM slot name back to the RetroArch state filename.

        Slot mapping (mirrors get_slot_info upload logic):
          "quicksave" → .state
          "slot1"     → .state1
          "slot5"     → .state5
          etc.
        """
        import re
        match = re.match(r'slot(\d+)$', slot)
        if match:
            return f"{base_name}.state{match.group(1)}"
        # quicksave or any other slot name → default .state
        return f"{base_name}.state"

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
        save_extensions = {'.srm', '.sav', '.dsv', '.mcr', '.eep', '.fla', '.mpk', '.sra'}
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
                            elif save_type == 'states' and (file_path.suffix.lower() in state_extensions or file_path.name.lower().endswith('.state.auto')):
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
                return False, "Config directory not found (see logs for checked paths)"

            config_file = config_dir / 'retroarch.cfg'
            if not config_file.exists():
                print(f"⚠️ Expected retroarch.cfg at: {config_file}")
                return False, f"retroarch.cfg not found at {config_dir}"

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
                return False, "Network commands disabled"
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
                return False, "Save state thumbnails disabled"
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
                print("⚠️ Cannot enable setting: config directory not found")
                return False, "Config directory not found (see logs for checked paths)"

            config_file = config_dir / 'retroarch.cfg'
            if not config_file.exists():
                print(f"⚠️ Expected retroarch.cfg at: {config_file}")
                return False, f"retroarch.cfg not found at {config_dir}"

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

    def toggle_retroarch_setting(self, setting_type):
        """Toggle a specific RetroArch setting (enable if disabled, disable if enabled)

        Args:
            setting_type: Either 'network_commands' or 'savestate_thumbnails'

        Returns:
            (success: bool, message: str)
        """
        try:
            # Check current state
            if setting_type == 'network_commands':
                is_enabled, _ = self.check_network_commands_config()
            elif setting_type == 'savestate_thumbnails':
                is_enabled, _ = self.check_savestate_thumbnail_config()
            else:
                return False, f"Unknown setting type: {setting_type}"

            # Get config file
            config_dir = self.find_retroarch_config_dir()
            if not config_dir:
                return False, "Config directory not found (see logs for checked paths)"

            config_file = config_dir / 'retroarch.cfg'
            if not config_file.exists():
                return False, f"retroarch.cfg not found at {config_dir}"

            # Read the config file
            with open(config_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()

            modified = False
            new_value = "false" if is_enabled else "true"  # Toggle the value
            action = "disabled" if is_enabled else "enabled"

            if setting_type == 'network_commands':
                # Toggle network_cmd_enable
                network_enable_found = False
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped.startswith('network_cmd_enable = '):
                        lines[i] = f'network_cmd_enable = "{new_value}"\n'
                        network_enable_found = True
                        modified = True
                        break

                # If not found, add it
                if not network_enable_found:
                    lines.append(f'network_cmd_enable = "{new_value}"\n')
                    if new_value == "true":
                        lines.append('network_cmd_port = "55355"\n')
                    modified = True

            elif setting_type == 'savestate_thumbnails':
                # Toggle savestate_thumbnail_enable
                thumbnail_found = False
                for i, line in enumerate(lines):
                    stripped = line.strip()
                    if stripped.startswith('savestate_thumbnail_enable = '):
                        lines[i] = f'savestate_thumbnail_enable = "{new_value}"\n'
                        thumbnail_found = True
                        modified = True
                        break

                # If not found, add it
                if not thumbnail_found:
                    lines.append(f'savestate_thumbnail_enable = "{new_value}"\n')
                    modified = True

            if not modified:
                return True, f"Setting already {action}"

            # Write the modified config back
            with open(config_file, 'w', encoding='utf-8') as f:
                f.writelines(lines)

            if setting_type == 'network_commands':
                return True, f"Network commands {action} (restart RetroArch to apply)"
            elif setting_type == 'savestate_thumbnails':
                return True, f"Save state thumbnails {action} (restart RetroArch to apply)"

        except Exception as e:
            return False, f"Failed to toggle setting: {e}"

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
        # Coalesces concurrent session syncs (connect vs RetroArch-close triggers).
        self._session_sync_lock = threading.Lock()
        
        # Game session tracking
        self.current_game = None
        self.last_sync_time = {}  # game_id -> timestamp
        self.should_stop = threading.Event()
        
        # Upload worker thread
        self.upload_worker = None
        self.startup_sync_thread = None

        # Upload fingerprints persistence
        self.upload_fingerprints_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'cache' / 'upload_fingerprints.json'
        self.upload_fingerprints_file.parent.mkdir(parents=True, exist_ok=True)
        self._load_upload_fingerprints()

        # Add these new attributes at the end
        self.retroarch_monitor = None
        self.current_retroarch_game = None
        self.retroarch_running = False

        # Add lock mechanism
        self.lock = AutoSyncLock()
        self.instance_id = f"{'gui' if parent_window else 'daemon'}_{os.getpid()}"

    def _load_upload_fingerprints(self):
        """Load upload fingerprints from disk"""
        try:
            if self.upload_fingerprints_file.exists():
                with open(self.upload_fingerprints_file, 'r') as f:
                    data = json.load(f)
                    # Convert JSON arrays back to tuples
                    self.last_uploaded = {path: tuple(fingerprint) for path, fingerprint in data.items()}
                logging.debug(f"Loaded {len(self.last_uploaded)} upload fingerprints from cache")
        except Exception as e:
            logging.debug(f"Could not load upload fingerprints: {e}")
            self.last_uploaded = {}

    def _save_upload_fingerprints(self):
        """Save upload fingerprints to disk"""
        try:
            # Convert tuples to lists for JSON serialization
            data = {path: list(fingerprint) for path, fingerprint in self.last_uploaded.items()}
            with open(self.upload_fingerprints_file, 'w') as f:
                json.dump(data, f, indent=2)
            logging.debug(f"Saved {len(self.last_uploaded)} upload fingerprints to cache")
        except Exception as e:
            logging.debug(f"Could not save upload fingerprints: {e}")

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
            # Start upload worker
            self.start_upload_worker()

            # Start startup save sync
            if self.settings.get('AutoSync', 'startup_sync_enabled', 'true') == 'true':
                self.start_startup_save_sync()

            # Start file system monitoring
            self.start_file_monitoring()

            self.start_retroarch_monitoring()
            self.start_playlist_monitoring()

            # RomM session-based sync on connect: pull saves changed on other
            # devices (and push any local changes) as a single batch negotiate.
            self.trigger_session_save_sync("connect")

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

        # Save shutdown time for startup sync
        try:
            self.settings.config['AutoSync']['last_shutdown_time'] = str(time.time())
            self.settings.save_settings()
        except Exception as e:
            logging.debug(f"Could not save shutdown time: {e}")

        # Save upload fingerprints to persist between restarts
        self._save_upload_fingerprints()

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

                    # 2. Check if RetroArch network is responding (also returns content path)
                    network_responding, network_content_path = self.is_retroarch_network_active()

                    # Log state changes
                    if retroarch_running != retroarch_was_running:
                        if retroarch_running:
                            self.log("🎮 RetroArch launched")
                        else:
                            self.log("🎮 RetroArch closed")
                            network_retry_count = 0  # Reset on close
                            # Clear per-file debounce; the session boundary (close)
                            # is where we push this play session's saves as a batch.
                            self.upload_debounce.clear()
                            # RomM session-based sync: reconcile all saves now that
                            # RetroArch has flushed them on exit.
                            self.trigger_session_save_sync("RetroArch closed")
                        retroarch_was_running = retroarch_running

                    # 3. PRIORITY: Network state detection (content loaded/unloaded)
                    if network_responding != last_network_state:
                        if network_responding:
                            # Use content path from GET_STATUS (instant, no race condition)
                            # Fall back to history file if GET_STATUS didn't include a path
                            current_content = network_content_path or self.get_retroarch_current_game()

                            if current_content:
                                # For display: use filename if path, strip CRC if content label
                                display_name = Path(current_content).name if '/' in current_content else current_content.split(',crc32=')[0]
                                self.log(f"🎯 RetroArch content loaded: {display_name}")
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
        """Check if RetroArch has content loaded via network commands.

        Returns:
            tuple: (network_responding: bool, content_path: str or None)
            - (False, None) — network not responding
            - (True, None) — network active but no content loaded (menu/contentless)
            - (True, path) — network active with content loaded
        """
        try:
            import socket
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.settimeout(0.5)

            # Send GET_STATUS command
            sock.sendto(b'GET_STATUS', ('127.0.0.1', 55355))

            # Try to receive response
            try:
                response, _ = sock.recvfrom(4096)
                response_text = response.decode('utf-8', errors='ignore').strip()

                sock.close()

                if not response_text or response_text == 'N/A':
                    return (False, None)

                # Check if content is loaded vs just menu/contentless
                upper = response_text.upper()
                if 'CONTENTLESS' in upper or 'MENU' in upper:
                    return (True, None)

                # Parse content path from GET_STATUS response
                # Format: "GET_STATUS PLAYING corename,/path/to/content"
                # or:     "GET_STATUS PAUSED corename,/path/to/content"
                content_path = self._parse_content_path_from_status(response_text)
                return (True, content_path)

            except socket.timeout:
                sock.close()
                return (False, None)

        except Exception:
            return (False, None)

    def _parse_content_path_from_status(self, status_response):
        """Parse the content path from a GET_STATUS response.

        Expected format: "GET_STATUS PLAYING corename,/path/to/content"
        """
        try:
            # Split into parts: ["GET_STATUS", "PLAYING", "corename,/path/to/content"]
            parts = status_response.split(' ', 2)

            if len(parts) < 3:
                return None

            core_and_path = parts[2]

            # Split on first comma: core name vs content path
            comma_idx = core_and_path.find(',')
            if comma_idx < 0:
                return None

            content_path = core_and_path[comma_idx + 1:].strip()

            if not content_path or content_path == 'N/A':
                return None

            return content_path
        except Exception as e:
            logging.debug(f"Exception in _parse_content_path_from_status: {e}")
            return None

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
            print(f"❌ History parsing error: {e}")
        return None

    def sync_saves_for_rom_file(self, rom_path):
        """Sync saves for a specific ROM file or content identifier.

        rom_path can be:
        - A file path: /path/to/Star Wars - Shadows of the Empire (Europe).zip
        - An archive path: /path/to/file.zip#internal.rom
        - A GET_STATUS content label: Star Wars - Shadows of the Empire (Europe),crc32=f0a191bf
        """
        try:
            # Detect GET_STATUS content label (not a file path)
            # Format: "Game Name,crc32=XXXXXXXX" or "Game Name"
            is_content_label = not ('/' in rom_path or '\\' in rom_path)

            if is_content_label:
                # Strip CRC suffix if present: "Game Name,crc32=abc123" → "Game Name"
                content_name = rom_path.split(',crc32=')[0].strip()
                self.log(f"🎯 Detected content: {content_name} - syncing saves...")
                rom_filename = None
                rom_stem = content_name
            elif '#' in rom_path:
                # Handle archive paths (ZIP files with # separator)
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

            # DEBUG: Log what we're trying to match
            

            for game in games:
                game_filename = game.get('file_name', '')
                game_stem = Path(game_filename).stem if game_filename else ''
                game_name = game.get('name', '')

                if rom_filename and (game_filename == rom_filename or game_stem == rom_stem):
                    matching_game = game
                    break
                elif is_content_label and (game_stem == rom_stem or game.get('name', '') == rom_stem):
                    matching_game = game
                    break

                # Multi-disc / multi-file ROMs: the launched content (e.g.
                # "Final Fantasy VII (Europe) (Disc 1)") is one entry in the parent
                # ROM's `files` array, not its fs_name. Match against those files so
                # multi-disc games don't fall through to the destructive fallback.
                # (Requires with_files=true on the ROM list — see get_roms.)
                rom_files = (game.get('romm_data') or {}).get('files') or []
                for rf in rom_files:
                    rf_name = rf.get('file_name', '')
                    if not rf_name:
                        continue
                    if (rom_filename and rf_name == rom_filename) or \
                       (Path(rf_name).stem == rom_stem):
                        matching_game = game
                        break
                if matching_game:
                    break

                # Check regional variants (_sibling_files)
                if game.get('_sibling_files'):
                    for sibling in game['_sibling_files']:
                        sibling_fs_name = sibling.get('fs_name', '')
                        sibling_fs_extension = sibling.get('fs_extension', '')
                        
                        # Build full filename
                        if sibling_fs_name:
                            if sibling_fs_extension and not sibling_fs_name.lower().endswith(f'.{sibling_fs_extension.lower()}'):
                                sibling_filename = f"{sibling_fs_name}.{sibling_fs_extension}"
                            else:
                                sibling_filename = sibling_fs_name
                        else:
                            sibling_filename = sibling.get('name', 'Unknown')
                        
                        sibling_stem = Path(sibling_filename).stem if sibling_filename else ''
                        
                        # Match against the variant filename
                        if rom_filename and sibling_filename == rom_filename:
                            matching_game = game
                            # Store the specific variant ROM ID for save sync
                            game['_matched_variant_rom_id'] = sibling.get('id')
                            break
                        elif is_content_label and (sibling_stem == rom_stem or sibling.get('name', '') == rom_stem):
                            matching_game = game
                            game['_matched_variant_rom_id'] = sibling.get('id')
                            break
                    
                    if matching_game:
                        break

            if matching_game:
                self.log(f"📥 Syncing saves for: {matching_game.get('name')}")
                self.download_saves_for_specific_game(matching_game)
            else:
                # Do NOT fall back to sync_recent_saves() here: that downloads and
                # overwrites local saves for EVERY locally-present game, which is
                # both wrong (we only care about the launched ROM) and dangerous —
                # especially after a RomM upgrade bumps every save's updated_at,
                # making the legacy "server is newer" check clobber local saves.
                # Better to do nothing than to touch unrelated games.
                self.log(f"⚠️ ROM not in library ({rom_stem!r}) - skipping pre-launch sync "
                         f"(no destructive recent-saves fallback)")
                
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

    def start_startup_save_sync(self):
        """Launch background thread to upload saves modified while app was closed"""
        def startup_sync_worker():
            try:
                # Wait for upload worker to be ready
                time.sleep(2)

                # Get timestamp of last shutdown
                last_shutdown = self.settings.get('AutoSync', 'last_shutdown_time', '')
                current_time = time.time()

                if not last_shutdown:
                    # First run - scan last 24 hours
                    cutoff_time = current_time - 86400
                    self.log("📅 First startup sync - scanning saves from last 24 hours")
                else:
                    cutoff_time = float(last_shutdown)
                    max_window = int(self.settings.get('AutoSync', 'startup_scan_days', '7')) * 86400

                    # Don't scan too far back
                    if current_time - cutoff_time > max_window:
                        cutoff_time = current_time - max_window
                        self.log(f"📅 Startup sync - scanning saves from last {max_window/86400:.0f} days")
                    else:
                        hours = (current_time - cutoff_time) / 3600
                        self.log(f"📅 Startup sync - scanning saves modified in last {hours:.1f} hours")

                # Scan for modified files
                modified_files = []
                save_files = self.retroarch.get_save_files()

                for save_type, files in save_files.items():
                    for file_info in files:
                        file_path = Path(file_info['path'])
                        if not file_path.exists():
                            continue

                        mtime = file_info['modified']

                        # Check if modified since last shutdown
                        if mtime > cutoff_time:
                            # Check if already uploaded (fingerprint match)
                            stat = file_path.stat()
                            current_fingerprint = (stat.st_size, stat.st_mtime)
                            last = self.last_uploaded.get(str(file_path))

                            if last != current_fingerprint:
                                modified_files.append(str(file_path))

                if not modified_files:
                    self.log("✅ No modified saves found - all up to date")
                    return

                self.log(f"📤 Found {len(modified_files)} modified saves, queuing for upload...")

                # Queue files for upload in chunks to avoid overwhelming server
                chunk_size = 10
                for i in range(0, len(modified_files), chunk_size):
                    if self.should_stop.is_set():
                        break

                    chunk = modified_files[i:i+chunk_size]
                    current_time = time.time()

                    for file_path in chunk:
                        # Queue with short delay (0.5s) for relatively quick processing
                        self.upload_debounce[file_path] = current_time - self.upload_delay + 0.5

                    self.log(f"  Queued {len(chunk)} files for upload (batch {i//chunk_size + 1})")

                    # Brief pause between chunks
                    if i + chunk_size < len(modified_files):
                        time.sleep(2)

                self.log(f"✅ Startup sync complete - {len(modified_files)} files queued")

            except Exception as e:
                self.log(f"❌ Startup sync error: {e}")

        self.startup_sync_thread = threading.Thread(target=startup_sync_worker, daemon=True)
        self.startup_sync_thread.start()

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
                # SAVES: RomM's protocol is session-based ("sync once per session,
                # not per save"). Rather than negotiate on this single file, mark it
                # synced (so RetroArch's exit-flush doesn't re-trigger) and kick a
                # coalesced full-inventory session sync. The primary push happens on
                # RetroArch close; this watcher trigger is a debounced safety net.
                # States are NOT engine-managed and fall through to the legacy path.
                self.last_uploaded[str(file_path)] = current_fingerprint
                self._save_upload_fingerprints()
                self.trigger_session_save_sync("save changed")
                return
            elif 'state' in file_path.suffix.lower() or file_path.name.lower().endswith('.state.auto'):
                # ".state.auto" has suffix ".auto" (no "state"), so match by name too.
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
            size_str = f"{file_size / 1000:.1f}KB" if file_size < 1000000 else f"{file_size / 1000000:.1f}MB"
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
                self.log(f"⚠️ Server returned 409 Conflict for {file_path.name} — server has newer version, triggering download")
                self.retroarch.send_notification(f"Sync conflict: {file_path.name}")
                # Trigger download so the newer server version lands locally.
                # Next play session will start with the correct save.
                conflict_rom_id = rom_id
                conflict_games = self.get_games()
                conflict_game = next(
                    (g for g in conflict_games if g.get('rom_id') == conflict_rom_id),
                    None
                )
                if conflict_game:
                    threading.Thread(
                        target=self.download_saves_for_specific_game,
                        args=(conflict_game,),
                        daemon=True
                    ).start()
            elif result:
                # Record successful upload fingerprint to avoid duplicate uploads
                self.last_uploaded[str(file_path)] = current_fingerprint
                self._save_upload_fingerprints()
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

    def _resolve_save_conflict(self, op, local_path):
        """Decide how to resolve a save conflict: 'local' or 'server'.

        Maps the user's existing overwrite-behavior preference:
          - "Always prefer local"          -> 'local'
          - "Always download from server"  -> 'server'
          - "Smart (prefer newer)"         -> newer of local mtime vs server_updated_at
          - "Ask each time"                -> modal dialog (GTK); 'server' if unavailable
        Defaults to 'server' (safe: local is always backed up by the caller).
        """
        pref = "Smart (prefer newer)"
        if self.parent_window and hasattr(self.parent_window, 'get_overwrite_behavior'):
            try:
                pref = self.parent_window.get_overwrite_behavior()
            except Exception:
                pass

        if pref == "Always prefer local":
            return 'local'
        if pref == "Always download from server":
            return 'server'
        if pref == "Ask each time":
            return self._ask_conflict_dialog(op, local_path)

        # Smart (prefer newer): compare local mtime to server's updated_at.
        import datetime as _dt
        try:
            local_ts = Path(local_path).stat().st_mtime
            sv = op.get('server_updated_at')
            server_ts = 0.0
            if sv:
                sdt = _dt.datetime.fromisoformat(sv.replace('Z', '+00:00'))
                if sdt.tzinfo is None:
                    sdt = sdt.replace(tzinfo=_dt.timezone.utc)
                server_ts = sdt.timestamp()
            return 'local' if local_ts >= server_ts else 'server'
        except Exception:
            return 'server'

    def _ask_conflict_dialog(self, op, local_path):
        """Prompt the user to resolve a save conflict on the main thread.

        Returns 'local' or 'server'. Falls back to 'server' when GTK/Adw is not
        available (e.g. headless / Decky), since the local copy is backed up.
        """
        try:
            from gi.repository import Adw as _Adw
        except Exception:
            return 'server'

        import threading as _th
        done = _th.Event()
        choice = ['server']
        name = Path(local_path).name
        server_when = op.get('server_updated_at') or 'unknown'

        def ask():
            try:
                dialog = _Adw.AlertDialog.new(
                    "Save Conflict",
                    f"Both this device and the server changed “{name}” since the last sync.\n\n"
                    f"Server version: {server_when}\n\n"
                    f"Your local copy has been backed up either way. Which version should win?"
                )
                dialog.add_response("server", "Use Server")
                dialog.add_response("local", "Keep Local")
                dialog.set_default_response("server")

                def on_response(_d, response):
                    choice[0] = response if response in ('local', 'server') else 'server'
                    done.set()

                dialog.connect('response', on_response)
                parent = getattr(self.parent_window, 'window', None) or self.parent_window
                dialog.present(parent)
            except Exception:
                done.set()

        _idle_add(ask)
        # Avoid blocking forever if the UI never responds.
        done.wait(timeout=120)
        return choice[0]

    def build_sync_inventory(self):
        """Build the local save inventory for RomM 4.9.0's /api/sync/negotiate.

        Returns a list of ClientSaveState dicts:
            {rom_id, file_name, slot, emulator, content_hash, updated_at}
        one per local save file that maps to a known ROM. The save-sync engine
        is SAVES-ONLY (states stay on the legacy flow), so only the 'saves'
        bucket from RetroArchInterface.get_save_files() is walked.

        updated_at is the file mtime as a UTC ISO-8601 string; slot/content_hash
        mirror exactly what the server keys/compares on.
        """
        import datetime as _dt

        inventory = []
        save_files = self.retroarch.get_save_files() or {}
        for entry in save_files.get('saves', []):
            path = Path(entry['path'])
            rom_id = self.find_rom_id_for_save_file(path)
            if not rom_id:
                # Unmatched local save — server can't pair it; skip rather than
                # uploading against a guessed ROM.
                continue

            slot, _autocleanup, _limit = RomMClient.get_slot_info(path)
            content_hash = RomMClient.compute_content_hash(path)
            stat = path.stat()
            updated_at = _dt.datetime.fromtimestamp(
                entry.get('modified') or stat.st_mtime,
                tz=_dt.timezone.utc,
            ).isoformat()

            inventory.append({
                'rom_id': rom_id,
                'file_name': entry['name'],
                'slot': slot,
                'emulator': entry.get('retroarch_emulator'),
                'content_hash': content_hash,
                'updated_at': updated_at,
                'file_size_bytes': stat.st_size,
                # Local-only fields (stripped before negotiate; used by the executor)
                '_path': entry['path'],
                '_autocleanup': _autocleanup,
                '_autocleanup_limit': _limit,
            })

        return inventory

    def trigger_session_save_sync(self, reason=""):
        """Run a session-based save-sync in the background (coalesced).

        RomM's protocol is session-based ("sync once per session, not per save"),
        so instead of negotiating on every individual file write we run one
        full-inventory negotiate at session boundaries (RetroArch close, connect)
        and as a debounced safety net from the file watcher.

        Coalesced via a non-blocking lock: if a sync is already in flight it
        already captures current on-disk state, so overlapping triggers are
        dropped rather than queued.
        """
        if not (self.romm_client and self.romm_client.authenticated):
            return

        def _run():
            if not self._session_sync_lock.acquire(blocking=False):
                logging.debug("Session save-sync already running; coalescing trigger")
                return
            try:
                if reason:
                    self.log(f"🔄 Save-sync session ({reason})")
                self.run_negotiated_save_sync()
            except Exception as e:
                self.log(f"❌ Session save-sync failed: {e}")
            finally:
                self._session_sync_lock.release()

        threading.Thread(target=_run, daemon=True, name="romm-session-sync").start()

    def run_negotiated_save_sync(self, conflict_resolver=None):
        """Run one save-sync cycle via RomM 4.9.0's /negotiate engine (SAVES ONLY).

        Builds the local inventory, negotiates with the server, then executes the
        returned operations:
          - upload   -> push the local file (with session_id for attribution)
          - download -> pull the server save into the RetroArch saves dir
          - conflict -> defer to conflict_resolver(op) -> 'local'|'server'|'skip'
                        (default: 'skip' — never auto-clobber; collected for UI)
          - no_op    -> nothing
        Finally marks the session complete.

        Returns a summary dict: {uploaded, downloaded, conflicts, no_op, errors, session_id}.
        """
        summary = {'uploaded': 0, 'downloaded': 0, 'conflicts': [], 'no_op': 0,
                   'errors': 0, 'session_id': None}

        if not self.romm_client or not self.romm_client.authenticated:
            self.log("⚠️ Save-sync: not connected")
            return summary

        device_id = self.settings.get('Device', 'device_id', '') or None
        if not device_id:
            self.log("⚠️ Save-sync: no device_id registered")
            return summary

        inventory = self.build_sync_inventory()
        session_id, operations = self.romm_client.negotiate_sync(device_id, inventory)
        summary['session_id'] = session_id
        if session_id is None:
            self.log("⚠️ Save-sync: negotiate failed")
            return summary

        # Lookup local inventory entry by (rom_id, slot) for upload operations.
        inv_by_key = {(e['rom_id'], e['slot']): e for e in inventory}
        saves_dir = self.retroarch.save_dirs.get('saves')

        for op in operations:
            action = op.get('action')
            rom_id = op.get('rom_id')
            slot = op.get('slot')
            try:
                if action == 'no_op':
                    summary['no_op'] += 1

                elif action == 'upload':
                    entry = inv_by_key.get((rom_id, slot))
                    if not entry:
                        self.log(f"⚠️ Save-sync: no local file for upload op rom={rom_id} slot={slot!r}")
                        summary['errors'] += 1
                        continue
                    ok = self.romm_client.upload_save(
                        rom_id, 'saves', entry['_path'],
                        emulator=entry.get('emulator'), device_id=device_id,
                        slot=slot, autocleanup=entry.get('_autocleanup', False),
                        autocleanup_limit=entry.get('_autocleanup_limit'),
                        session_id=session_id,
                    )
                    if ok is True:
                        summary['uploaded'] += 1
                    else:
                        summary['errors'] += 1

                elif action == 'download':
                    target = self._resolve_download_target(op, saves_dir)
                    ok = self.romm_client.download_save_by_id(
                        op.get('save_id'), 'saves', target,
                        device_id=device_id, session_id=session_id,
                    )
                    if ok:
                        summary['downloaded'] += 1
                    else:
                        summary['errors'] += 1

                elif action == 'conflict':
                    # Resolve via an explicit resolver, else the user's
                    # overwrite-behavior preference.
                    entry = inv_by_key.get((rom_id, slot))
                    if conflict_resolver:
                        choice = conflict_resolver(op)
                    elif entry:
                        choice = self._resolve_save_conflict(op, entry['_path'])
                    else:
                        choice = 'skip'
                    if choice == 'local':
                        if entry and self.romm_client.upload_save(
                            rom_id, 'saves', entry['_path'], emulator=entry.get('emulator'),
                            device_id=device_id, slot=slot, overwrite=True,
                            autocleanup=entry.get('_autocleanup', False),
                            autocleanup_limit=entry.get('_autocleanup_limit'),
                            session_id=session_id,
                        ) is True:
                            summary['uploaded'] += 1
                        else:
                            summary['errors'] += 1
                    elif choice == 'server':
                        # Back up local before overwriting it — whatever chose
                        # 'server' (resolver or preference), never lose local work.
                        if entry:
                            try:
                                src = Path(entry['_path'])
                                shutil.copy2(src, src.with_suffix(
                                    src.suffix + f".local-{datetime.datetime.now().strftime('%Y%m%d-%H%M%S')}"))
                            except Exception:
                                pass
                        target = self._resolve_download_target(op, saves_dir)
                        if self.romm_client.download_save_by_id(
                            op.get('save_id'), 'saves', target,
                            device_id=device_id, session_id=session_id):
                            summary['downloaded'] += 1
                        else:
                            summary['errors'] += 1
                    else:
                        # Defer — record for UI resolution, leave both sides untouched.
                        summary['conflicts'].append(op)
            except Exception as e:
                self.log(f"❌ Save-sync op {action} rom={rom_id} failed: {e}")
                summary['errors'] += 1

        completed = summary['uploaded'] + summary['downloaded'] + summary['no_op']
        self.romm_client.complete_sync_session(
            session_id,
            operations_completed=completed,
            operations_failed=summary['errors'],
        )
        self.log(f"🔄 Save-sync: {summary['uploaded']} up, {summary['downloaded']} down, "
                 f"{len(summary['conflicts'])} conflict(s), {summary['no_op']} in-sync, "
                 f"{summary['errors']} error(s)")
        return summary

    def _resolve_download_target(self, op, saves_dir):
        """Resolve the local RetroArch path for a server save download operation.

        Places the file in the saves dir, inside the emulator's core subdirectory
        when that emulator maps to a known directory, with the filename converted
        back to RetroArch's expected form.
        """
        target_dir = Path(saves_dir)
        emulator = op.get('emulator')
        if emulator:
            mapped = self.retroarch.emulator_directory_map.get(emulator.lower())
            if mapped:
                target_dir = target_dir / mapped
        target_dir.mkdir(parents=True, exist_ok=True)
        local_name = self.retroarch.convert_to_retroarch_filename(
            op.get('file_name', ''), 'saves', target_dir, op.get('slot')
        )
        return target_dir / local_name

    def find_rom_id_for_save_file(self, file_path):
        """Find ROM ID by matching save filename to game library"""
        try:
            games = self.get_games()
            if not games:
                return None

            # RetroArch auto-savestate "<content>.state.auto" is a two-part suffix;
            # file_path.stem only drops ".auto", leaving ".state" which would never
            # match fs_name_no_ext. Strip the full suffix for that case.
            if file_path.name.lower().endswith('.state.auto'):
                save_basename = file_path.name[:-len('.state.auto')]
            else:
                save_basename = file_path.stem

            # Remove timestamps and clean up filename
            import re
            clean_basename = re.sub(r'\s*\[.*?\]', '', save_basename)

            # DEBUG: Log what we're trying to match
            

            # TIER 1: Try exact match with fs_name_no_ext
            for game in games:
                if not game.get('rom_id') or not game.get('romm_data'):
                    continue

                rom_data = game['romm_data']
                fs_name_no_ext = rom_data.get('fs_name_no_ext', '')

                if fs_name_no_ext and (fs_name_no_ext == save_basename or fs_name_no_ext == clean_basename):
                    return game['rom_id']
                
                # Check regional variants (_sibling_files)
                if game.get('_sibling_files'):
                    for sibling in game['_sibling_files']:
                        sibling_fs_name = sibling.get('fs_name', '')
                        sibling_fs_extension = sibling.get('fs_extension', '')
                        
                        # Build filename and stem
                        if sibling_fs_name:
                            if sibling_fs_extension and not sibling_fs_name.lower().endswith(f'.{sibling_fs_extension.lower()}'):
                                sibling_filename = f"{sibling_fs_name}.{sibling_fs_extension}"
                            else:
                                sibling_filename = sibling_fs_name
                        else:
                            sibling_filename = sibling.get('name', 'Unknown')
                        
                        # Use fs_name_no_ext if available, otherwise stem
                        sibling_fs_name_no_ext = sibling.get('fs_name_no_ext') or (Path(sibling_filename).stem if sibling_filename else '')
                        
                        if sibling_fs_name_no_ext and (sibling_fs_name_no_ext == save_basename or sibling_fs_name_no_ext == clean_basename):
                            # Return the variant's ROM ID, not the parent's
                            return sibling.get('id')

            # TIER 2: Try region-aware matching (NEW)
            # Extract region tag from save filename
            save_region = self._extract_region_tag(clean_basename)

            if save_region:
                

                # Get base name (without region tag) from save file
                save_base_name = re.sub(r'\s*\(.*?\)', '', clean_basename).strip()

                # Find all games matching base name
                region_candidates = []
                for game in games:
                    if not game.get('rom_id') or not game.get('romm_data'):
                        continue

                    rom_data = game['romm_data']
                    fs_name_no_ext = rom_data.get('fs_name_no_ext', '')

                    if not fs_name_no_ext:
                        continue

                    # Get base name from game (without region tags)
                    game_base_name = re.sub(r'\s*\(.*?\)', '', fs_name_no_ext).strip()

                    # If base names match (case-insensitive), this is a candidate
                    if game_base_name.lower() == save_base_name.lower():
                        game_region = self._extract_region_tag(fs_name_no_ext)
                        region_candidates.append({
                            'game': game,
                            'region': game_region,
                            'fs_name_no_ext': fs_name_no_ext,
                            'rom_id': game['rom_id']
                        })
                
                # Also check regional variants for region matching
                if game.get('_sibling_files'):
                    for sibling in game['_sibling_files']:
                        sibling_fs_name = sibling.get('fs_name', '')
                        sibling_fs_name_no_ext = sibling.get('fs_name_no_ext') or (Path(sibling_fs_name).stem if sibling_fs_name else '')
                        
                        if not sibling_fs_name_no_ext:
                            continue
                        
                        # Get base name from variant (without region tags)
                        variant_base_name = re.sub(r'\s*\(.*?\)', '', sibling_fs_name_no_ext).strip()
                        
                        # If base names match, this variant is a candidate
                        if variant_base_name.lower() == save_base_name.lower():
                            variant_region = self._extract_region_tag(sibling_fs_name_no_ext)
                            region_candidates.append({
                                'game': game,
                                'region': variant_region,
                                'fs_name_no_ext': sibling_fs_name_no_ext,
                                'rom_id': sibling.get('id')  # Use variant's ROM ID
                            })

                # If we have candidates, prefer region match
                if region_candidates:
                    # First pass: exact region match
                    for candidate in region_candidates:
                        if candidate['region'] == save_region:
                            return candidate['rom_id']  # Return variant ROM ID if matched

                    # Second pass: if no exact region match, use first candidate
                    return region_candidates[0]['rom_id']  # Return variant ROM ID if matched

            # TIER 3: Fuzzy match fallback (unchanged)
            for game in games:
                if not game.get('rom_id') or not game.get('romm_data'):
                    continue

                rom_data = game['romm_data']
                fs_name_no_ext = rom_data.get('fs_name_no_ext', '')

                # Remove all parenthetical content for fuzzy matching
                clean_game_name = re.sub(r'\s*\(.*?\)', '', fs_name_no_ext).strip()
                clean_save_name = re.sub(r'\s*\(.*?\)', '', clean_basename).strip()

                

                if clean_game_name and clean_game_name.lower() == clean_save_name.lower():
                    return game['rom_id']

            return None

        except Exception as e:
            self.log(f"ROM matching error: {e}")
            return None

    def _extract_region_tag(self, filename):
        """
        Extract region tag from ROM/save filename.

        Recognizes patterns like:
        - (USA)
        - (Europe)
        - (Japan)
        - (World)
        - (Europe) (En,Fr,De,Es,It)  # Takes first region tag
        - (USA) (Rev 1)               # Takes first region tag
        - (USA, Europe)               # Takes first region

        Returns: Normalized region string (e.g., 'USA', 'Europe', 'Japan')
                 or None if no recognized region tag found
        """
        import re

        # Extract all parenthetical groups
        paren_groups = re.findall(r'\(([^)]+)\)', filename)

        if not paren_groups:
            return None

        # Known region tags (case-insensitive)
        known_regions = {
            'usa': 'USA',
            'europe': 'Europe',
            'japan': 'Japan',
            'world': 'World',
            'asia': 'Asia',
            'china': 'China',
            'korea': 'Korea',
            'brazil': 'Brazil',
            'australia': 'Australia',
            'germany': 'Germany',
            'france': 'France',
            'spain': 'Spain',
            'italy': 'Italy',
            'netherlands': 'Netherlands',
            'sweden': 'Sweden',
            'uk': 'UK',
        }

        # Check each parenthetical group for region tags
        for group in paren_groups:
            # Split on comma to handle "(USA, Europe)" style
            parts = [p.strip() for p in group.split(',')]

            for part in parts:
                part_lower = part.lower()
                if part_lower in known_regions:
                    return known_regions[part_lower]

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
        normalized = romm_emulator.lower().replace('-', '_')
        return core_to_platform.get(normalized, romm_emulator)

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

    def _resolve_core_dir(self, base_dir, game, romm_emulator):
        """For core mode: when the emulator field doesn't map to an existing directory
        (e.g. it came from a content-mode upload on another device), find the correct
        core directory by checking which known cores for this platform exist on disk.
        Falls back to the first mapped core dir even if it doesn't exist yet (mkdir will create it)."""
        platform_slug = game.get('platform_slug', '')
        platform_name = game.get('platform', '')
        candidates = (self.retroarch.platform_core_map.get(platform_slug) or
                      self.retroarch.platform_core_map.get(platform_name) or [])
        first_mapped = None
        for core in candidates:
            mapped = self.retroarch.emulator_directory_map.get(core.lower())
            if mapped:
                candidate_dir = base_dir / mapped
                if first_mapped is None:
                    first_mapped = candidate_dir  # Remember as fallback if none exist on disk
                if candidate_dir.exists():
                    self.log(f"  [DEBUG] core fallback: {romm_emulator!r} → using existing dir {mapped!r}")
                    return candidate_dir
        if first_mapped is not None:
            self.log(f"  [DEBUG] core fallback: {romm_emulator!r} → using first mapped dir {first_mapped.name!r} (will be created)")
            return first_mapped
        return None

    def download_saves_for_specific_game(self, game):
        """Download only the LATEST saves/states for a specific game from RomM with smart overwrite protection"""
        try:
            from gi.repository import Adw as _Adw
        except ImportError:
            _Adw = None

        try:
            from urllib.parse import urljoin
            import datetime
            
            # Use variant ROM ID if matched, otherwise use parent ROM ID
            rom_id = game.get('_matched_variant_rom_id') or game['rom_id']
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
                    
                    for save in device_saves:
                        self.log(f"   Save ID: {save.get('id')}, file: {save.get('file_name')}, updated: {save.get('updated_at')}")

                    device_states = self.romm_client.get_saves_by_device(
                        self.parent_window.device_id,
                        save_type='states',
                        rom_id=rom_id,
                        limit=50
                    )
                    device_states_to_skip = {s.get('id') for s in device_states if s.get('id')}
                    
                    for state in device_states:
                        self.log(f"   State ID: {state.get('id')}, file: {state.get('file_name')}, updated: {state.get('updated_at')}")

                    if device_saves_to_skip or device_states_to_skip:
                        self.log(f"🔄 Optimistic sync: {len(device_saves_to_skip)} saves, {len(device_states_to_skip)} states already on device")
                except Exception as e:
                    print(f"Could not query device saves: {e}")
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
            skipped_count = 0
            
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
                """Determine if we should download.

                Content hash (RomM 4.9.0's save-sync signal) is checked FIRST: if the
                local file and the server's content_hash are identical, the save is
                already in sync — skip regardless of timestamps. This is essential
                because RomM 4.9.0's content-hash recompute bumped every save's
                updated_at to the upgrade date, which would otherwise make the legacy
                timestamp comparison think the server is always newer and clobber
                every local save on launch. Only when content differs do we fall back
                to timestamp comparison / user preference.
                """
                if not local_path.exists():
                    return True, f"Local {file_type} doesn't exist"

                # Content-hash short-circuit (mirrors negotiate's "Content is identical").
                server_hash = server_file.get('content_hash') if isinstance(server_file, dict) else None
                if server_hash:
                    local_hash = RomMClient.compute_content_hash(local_path)
                    if local_hash and local_hash == server_hash:
                        self.log(f"     → Content identical (hash match), skipping {file_type}")
                        return False, f"{file_type} content is identical (hash match)"

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

            # Derive the ROM's content directory (parent folder name) for content-mode path resolution.
            # In RetroArch content mode, saves/states go into a subdir named after the ROM's
            # immediate parent folder — e.g. roms/nds/Pokémon HeartGold Version/game.nds → subdir
            # "Pokémon HeartGold Version".  This is more reliable than mapping the stored emulator
            # name, which reflects the uploading device's mode and may differ across devices.
            #
            # Note: local_path in available_games is always set as download_dir/platform/file,
            # so it won't reflect a subfolder path for variant ROMs.  We scan the platform dir
            # on disk to find the actual parent folder name.
            _content_dir = None
            _rom_file_name = game.get('file_name', '')
            _platform_slug = game.get('platform_slug', '')
            if _rom_file_name and _platform_slug:
                try:
                    _rom_dir = Path(self.settings.get('Download', 'rom_directory',
                                                       '~/RomMSync/roms')).expanduser()
                    _platform_dir = _rom_dir / _platform_slug
                    if _platform_dir.exists():
                        # Case 1: file_name is itself a folder (container ROM like "Pokémon HeartGold Version")
                        # → content dir IS that folder name
                        if (_platform_dir / _rom_file_name).is_dir():
                            _content_dir = _rom_file_name
                        else:
                            # Case 2: variant file inside a subfolder — scan to find which one
                            for _subdir in _platform_dir.iterdir():
                                if _subdir.is_dir() and (_subdir / _rom_file_name).exists():
                                    _content_dir = _subdir.name
                                    break
                except Exception:
                    pass
            # If still not found (flat ROM), fall back to local_path parent
            if not _content_dir:
                _local_path = game.get('local_path')
                if _local_path:
                    _candidate = Path(_local_path).parent.name
                    if _candidate and _candidate != _platform_slug:
                        _content_dir = _candidate
            self.log(f"  [DEBUG] content_dir resolved: {_content_dir!r} for {_rom_file_name!r}")

            # Process saves
            if 'saves' in self.retroarch.save_dirs:
                save_base_dir = self.retroarch.save_dirs['saves']
                user_saves = rom_details.get('user_saves', [])

                latest_save = get_latest_file(user_saves, "save")

                if latest_save:
                    original_filename = latest_save.get('file_name', '')
                    romm_emulator = latest_save.get('emulator', 'unknown')

                    # Compute local path to check if file exists before skipping
                    final_path = None
                    emulator_save_dir = None
                    if original_filename:
                        subdir_mode = self.retroarch.get_save_subdir_mode('saves')
                        if subdir_mode == 'core':
                            emulator_save_dir = save_base_dir / self.retroarch.get_retroarch_directory_name(romm_emulator)
                            # If romm_emulator has no direct core mapping it's likely a content-dir
                            # name from a content-mode upload on another device. Fall back to known
                            # cores for this platform regardless of whether the directory exists.
                            if not self.retroarch.emulator_directory_map.get(romm_emulator.lower() if romm_emulator else ''):
                                emulator_save_dir = self._resolve_core_dir(
                                    save_base_dir, game, romm_emulator) or emulator_save_dir
                        elif subdir_mode == 'content':
                            # Use the ROM's actual parent folder name (what RetroArch uses as content dir)
                            # rather than the stored emulator field, which may be from a different device/mode.
                            subdir_name = _content_dir or self.get_platform_slug_from_emulator(romm_emulator)
                            emulator_save_dir = save_base_dir / subdir_name
                        else:
                            emulator_save_dir = save_base_dir
                        if emulator_save_dir:
                            retroarch_filename = self.retroarch.convert_to_retroarch_filename(
                                original_filename, 'saves', emulator_save_dir
                            )
                            final_path = emulator_save_dir / retroarch_filename

                    # Only skip download if the local file actually exists AND
                    # the API confirms this device has the current version.
                    # Do NOT skip based on device_saves_to_skip — that only tracks
                    # what this device uploaded and misses newer versions from other devices.
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
                            skipped_count += 1

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
                                    # Record fingerprint so upload worker treats this as already-uploaded,
                                    # preventing the download→re-upload loop until emulator modifies it.
                                    _auto_sync = self if hasattr(self, 'upload_debounce') else (
                                        getattr(getattr(self, 'parent_window', None), 'auto_sync', None))
                                    if _auto_sync and final_path.exists():
                                        _fp = final_path.stat()
                                        _auto_sync.last_uploaded[str(final_path)] = (_fp.st_size, _fp.st_mtime)
                                        _auto_sync.upload_debounce[str(final_path)] = time.time() + 30
                                    # Notification sent once at end of sync (see summary block below)
                                except Exception as e:
                                    self.log(f"  ❌ Failed to rename save: {e}")

            # Process states — download latest state from each slot
            if 'states' in self.retroarch.save_dirs:
                state_base_dir = self.retroarch.save_dirs['states']

                # Note: /api/states/summary endpoint doesn't exist in RomM API
                # Group user_states by slot locally instead
                slot_states = []
                if not slot_states:
                    user_states = rom_details.get('user_states', [])
                    if user_states:
                        slot_groups = {}
                        for state in user_states:
                            if not isinstance(state, dict):
                                continue
                            slot = state.get('slot')
                            if not slot and state.get('file_name'):
                                slot, _, _ = RomMClient.get_slot_info(state['file_name'])
                            slot_key = slot or 'quicksave'
                            if slot_key not in slot_groups:
                                slot_groups[slot_key] = []
                            slot_groups[slot_key].append(state)
                        for slot_key, states in slot_groups.items():
                            latest = get_latest_file(states, f"state/{slot_key}")
                            if latest:
                                slot_states.append(latest)

                for latest_state in slot_states:
                    state_id = latest_state.get('id')
                    original_filename = latest_state.get('file_name', '')
                    romm_emulator = latest_state.get('emulator', 'unknown')
                    state_slot = latest_state.get('slot')

                    # Infer slot from filename extension if not provided by API
                    if not state_slot and original_filename:
                        state_slot, _, _ = RomMClient.get_slot_info(original_filename)

                    # Compute local path
                    final_path = None
                    emulator_state_dir = None
                    if original_filename:
                        subdir_mode = self.retroarch.get_save_subdir_mode('states')
                        self.log(f"  [DEBUG] states subdir_mode={subdir_mode!r}, romm_emulator={romm_emulator!r}, content_dir={_content_dir!r}")
                        if subdir_mode == 'core':
                            emulator_state_dir = state_base_dir / self.retroarch.get_retroarch_directory_name(romm_emulator)
                            # If romm_emulator has no direct core mapping it's likely a content-dir
                            # name from a content-mode upload on another device. Fall back to known
                            # cores for this platform regardless of whether the directory exists.
                            if not self.retroarch.emulator_directory_map.get(romm_emulator.lower() if romm_emulator else ''):
                                emulator_state_dir = self._resolve_core_dir(
                                    state_base_dir, game, romm_emulator) or emulator_state_dir
                        elif subdir_mode == 'content':
                            # Use the ROM's actual parent folder name (what RetroArch uses as content dir)
                            subdir_name = _content_dir or self.get_platform_slug_from_emulator(romm_emulator)
                            emulator_state_dir = state_base_dir / subdir_name
                        else:
                            emulator_state_dir = state_base_dir
                        if emulator_state_dir:
                            retroarch_filename = self.retroarch.convert_to_retroarch_filename(
                                original_filename, 'states', emulator_state_dir, slot=state_slot
                            )
                            final_path = emulator_state_dir / retroarch_filename
                            self.log(f"  [DEBUG] state final_path={final_path}")

                    # Skip logic — only skip if API confirms this device has current version.
                    # Do NOT skip based on device_states_to_skip (uploaded-by-this-device set):
                    # another device may have uploaded a newer version of the same state ID.
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
                            skipped_count += 1

                    if not skip and original_filename and emulator_state_dir and final_path:
                        downloads_attempted += 1
                        emulator_state_dir.mkdir(parents=True, exist_ok=True)

                        # Conflict detection
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

                            device_id = self.settings.get('Device', 'device_id', '') or None

                            fallback_url = latest_state.get('download_path')
                            if self.romm_client.download_save_by_id(state_id, 'states', temp_path, device_id, fallback_url=fallback_url):
                                try:
                                    if temp_path != final_path:
                                        temp_path.rename(final_path)
                                    downloads_successful += 1
                                    self.log(f"  ✅ State ready: {retroarch_filename}")
                                    # Record fingerprint so upload worker treats this as already-uploaded,
                                    # preventing the download→re-upload loop until emulator modifies it.
                                    _auto_sync = self if hasattr(self, 'upload_debounce') else (
                                        getattr(getattr(self, 'parent_window', None), 'auto_sync', None))
                                    if _auto_sync and final_path.exists():
                                        _fp = final_path.stat()
                                        _auto_sync.last_uploaded[str(final_path)] = (_fp.st_size, _fp.st_mtime)
                                        _auto_sync.upload_debounce[str(final_path)] = time.time() + 30
                                    # Notification sent once at end of sync (see summary block below)

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
                            else:
                                self.log(f"  ❌ download_save_by_id failed for state {state_id}")

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
                    self.retroarch.send_notification(f"Synced: {game_name} ({downloads_successful} file{'s' if downloads_successful != 1 else ''})")
                elif conflicts_detected > 0:
                    self.log(f"🛡️ {game_name} local saves/states protected from overwrite")
                else:
                    self.log(f"✅ {game_name} saves/states already up to date")
            elif skipped_count > 0:
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
            if path.suffix.lower() in self.extensions:
                return True
            # RetroArch auto-savestate is "<content>.state.auto"; Path.suffix is
            # ".auto" so it won't match the numbered-slot set above — match by name.
            if self.save_type == 'states' and path.name.lower().endswith('.state.auto'):
                return True
            return False
        except Exception:
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
                      bios_tracking=None, steam_manager=None):
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
                            # Use file-based count (expands multi-disc ROMs to individual files)
                            file_counts = getattr(collection_sync, 'collection_file_counts', {})
                            total_roms      = file_counts.get(collection_name, len(cached_rom_ids))
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

                # Steam sync status per collection
                if steam_manager:
                    steam_collections = steam_manager.get_steam_sync_collections()
                    collection_data['steam_sync'] = collection_name in steam_collections
                    if collection_data['steam_sync']:
                        collection_data['steam_shortcut_count'] = steam_manager.get_collection_shortcut_count(collection_name)

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

    # Include Steam integration availability
    if steam_manager:
        status['steam_available'] = steam_manager.is_available()

    return status

class CollectionSyncManager:
    """Manages collection synchronization"""
    
    def __init__(self, romm_client, settings, selected_collections, sync_interval, available_games, log_callback, steam_manager=None):
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
        # File-based count per collection — counts individual disc files for multi-disc ROMs
        self.collection_file_counts = {}  # {collection_name: int}
        # Per-collection download progress — read directly by get_service_status()
        self.download_progress = {}  # {collection_name: {'downloaded': int, 'downloaded_pct': float, 'total': int, 'speed': float}}
        # Last removal event per collection — for frontend notification
        self.last_removals = {}  # {collection_name: {'removed_count': int, 'deleted_count': int, 'timestamp': float}}
        # Steam shortcut manager (optional)
        self.steam_manager = steam_manager

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
                self.collection_file_counts.pop(removed, None)
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
    
    @staticmethod
    def _count_rom_files(roms):
        """Count total files, expanding multi-file ROMs (e.g. multi-disc) by their file count."""
        total = 0
        for rom in roms:
            files = rom.get('files', [])
            if len(files) > 1:
                total += len(files)
            else:
                total += 1
        return total

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
                    self.collection_file_counts[collection_name] = self._count_rom_files(collection_roms)
                    self.log(f"Added collection to sync: {collection_name} ({self.collection_file_counts[collection_name]} files)")
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
                    self.collection_file_counts[collection_name] = self._count_rom_files(collection_roms)
                    self.log(f"Initialized cache for '{collection_name}': {self.collection_file_counts[collection_name]} files")

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
        # Total collection size is ALL files in the collection (multi-disc ROMs expand to their disc count)
        total_collection_size = self._count_rom_files(collection_roms)

        for rom in collection_roms:
            rom_file_count = len(rom.get('files', [])) if len(rom.get('files', [])) > 1 else 1
            if rom.get('id') not in added_rom_ids:
                # This ROM is not newly added, but check if it exists locally to count it
                platform_slug = rom.get('platform_slug', 'Unknown')
                file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
                local_path = download_dir / platform_slug / file_name
                if is_path_validly_downloaded(local_path):
                    existing_roms_count += rom_file_count
                continue

            # This ROM is newly added - check if we need to download it
            platform_slug = rom.get('platform_slug', 'Unknown')
            file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
            local_path = download_dir / platform_slug / file_name
            if is_path_validly_downloaded(local_path):
                existing_roms_count += rom_file_count
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
            rom_file_count = len(rom.get('files', [])) if len(rom.get('files', [])) > 1 else 1

            # Create directories
            platform_dir.mkdir(parents=True, exist_ok=True)

            # Progress callback: frac (0–1) is scaled across this ROM's file count
            # so multi-disc games advance the bar proportionally through their disc slots.
            _completed_before_this = existing_roms_count + downloaded_count

            def _chunk_progress(info, _coll=collection_name, _base=_completed_before_this,
                                 _total=total_collection_size, _fcount=rom_file_count):
                frac  = info.get('progress', 0.0)   # 0.0–1.0 within this ROM
                speed = info.get('speed',    0.0)   # bytes/sec
                overall_pct = round((_base + frac * _fcount) / _total * 100.0, 1) if _total > 0 else 0.0
                if _coll in self.download_progress:
                    self.download_progress[_coll]['downloaded']     = _base + 1
                    self.download_progress[_coll]['downloaded_pct'] = overall_pct
                    self.download_progress[_coll]['speed']          = speed

            # Download the ROM
            self.log(f"  ⬇️ Downloading {rom.get('name')}...")

            # Update progress immediately to show we're starting this ROM
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

            # Child-file variants (regional ROMs inside a parent folder) cannot be
            # downloaded via their own ROM ID — the API returns 404.
            # Fall back to downloading via the parent folder ROM + file_id,
            # using the siblings list from the collection API response.
            if not success and 'HTTP 404' in (message or '') and rom.get('fs_extension'):
                self.log(f"  ↩ Direct 404; trying via parent folder ROM...")
                parent_success, parent_path = self._download_via_siblings(
                    rom, file_name, platform_dir, _chunk_progress
                )
                if parent_success:
                    success = True
                    if parent_path:
                        local_path = parent_path

            if success:
                self.log(f"  ✅ Downloaded {rom.get('name')}")
                downloaded_count += rom_file_count

                # Update progress to exact file count after completed download
                current_local_count = existing_roms_count + downloaded_count
                self.download_progress[collection_name]['downloaded'] = current_local_count
                logging.info(f"[PROGRESS] Updated download_progress for {collection_name}: {current_local_count}/{total_collection_size}")

                # Update available_games with collection info
                for game in self.available_games:
                    if game.get('rom_id') == rom.get('id'):
                        game['collection'] = collection_name
                        game['is_downloaded'] = True
                        game['local_path'] = str(local_path)
                        game['local_size'] = local_path.stat().st_size if local_path.exists() else 0
                        break
            else:
                self.log(f"  ❌ Failed to download {rom.get('name')}: {message}")
        
        # Clear progress — get_service_status() will show 'synced' on next poll.
        if collection_name in self.download_progress:
            logging.info(f"[PROGRESS] Clearing download_progress for {collection_name}")
            del self.download_progress[collection_name]

        if downloaded_count > 0:
            self.log(f"Auto-downloaded {downloaded_count} new games from '{collection_name}'")

        # Sync Steam shortcuts if enabled for this collection
        self._sync_steam_if_enabled(collection_name, collection_roms, download_dir)

    def _download_via_siblings(self, rom, file_name, platform_dir, progress_callback=None):
        """Download a child-file variant via its parent folder ROM's endpoint + file_id.

        Called when a direct download returns HTTP 404 (variant ROM IDs are not
        individually downloadable — they live inside a parent folder ROM).

        Returns (success, actual_path) where actual_path is the Path the file
        was saved to, or (False, None) if no suitable parent could be found.
        """
        from urllib.parse import urljoin

        siblings = rom.get('sibling_roms', [])
        if not siblings:
            # No sibling data — try fetching ROM details to find the parent
            try:
                resp = self.romm_client.session.get(
                    urljoin(self.romm_client.base_url, f"/api/roms/{rom.get('id')}"),
                    timeout=10
                )
                if resp.status_code == 200:
                    siblings = resp.json().get('sibling_roms', [])
            except Exception:
                pass

        for sib in siblings:
            sib_id = sib.get('id') if isinstance(sib, dict) else sib
            if not sib_id:
                continue
            try:
                resp = self.romm_client.session.get(
                    urljoin(self.romm_client.base_url, f"/api/roms/{sib_id}"),
                    timeout=10
                )
                if resp.status_code != 200:
                    continue
                sib_details = resp.json()
            except Exception:
                continue

            # Only interested in folder ROMs (no extension, has files)
            if sib_details.get('fs_extension', ''):
                continue
            parent_files = sib_details.get('files', [])
            if not parent_files:
                continue

            # Find this file in the parent's file list
            matching = next(
                (f for f in parent_files
                 if (f.get('filename') or f.get('file_name', '')) == file_name),
                None
            )
            if not matching or not matching.get('id'):
                continue

            file_id = matching['id']
            parent_folder_name = sib_details.get('fs_name') or sib_details.get('name', str(sib_id))
            actual_path = platform_dir / parent_folder_name / file_name

            self.log(f"  ↩ Downloading via parent ROM {sib_id} (file_id={file_id})")
            success, message = self.romm_client.download_rom(
                sib_id, file_name, platform_dir / file_name,
                progress_callback=progress_callback,
                file_ids=str(file_id)
            )
            if success:
                # Per-disc fallback downloads land in a parent folder with no
                # playlist; generate one once 2+ discs are present so multi-disc
                # games boot and swap discs (idempotent — no-op for single disc).
                try:
                    RetroArchInterface.ensure_m3u_for_disc_folder(actual_path.parent, parent_folder_name)
                except Exception as e:
                    logging.debug(f"m3u generation skipped for {actual_path.parent}: {e}")
                return True, actual_path

        return False, None

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

        # Sync Steam shortcuts if enabled for this collection
        download_dir = Path(self.settings.get('Download', 'rom_directory'))
        # Re-fetch current collection ROMs for accurate sync
        try:
            if self.romm_client and collection_name in self.collection_caches:
                cached_rom_ids = self.collection_caches[collection_name]
                # Build current rom list from available_games
                current_roms = [g for g in self.available_games
                                if g.get('rom_id') in cached_rom_ids]
                self._sync_steam_if_enabled(collection_name, current_roms, download_dir)
        except Exception as e:
            logging.debug(f"Steam sync after removal failed: {e}")

    def _sync_steam_if_enabled(self, collection_name, collection_roms, download_dir):
        """Sync Steam shortcuts if steam_manager is set and collection has Steam sync enabled."""
        if not self.steam_manager:
            return
        steam_collections = self.steam_manager.get_steam_sync_collections()
        if collection_name not in steam_collections:
            return
        try:
            added, removed = self.steam_manager.sync_collection_shortcuts(
                collection_name, collection_roms, download_dir)
            if added or removed:
                self.log(f"Steam shortcuts updated for '{collection_name}': +{added} -{removed}")
        except Exception as e:
            self.log(f"Steam shortcut sync error: {e}")


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


# ==============================================================================
# Steam Shortcut Integration
# ==============================================================================

import struct
import zlib
import tempfile

class SteamVDFHandler:
    """Minimal binary VDF parser/writer for Steam's shortcuts.vdf.

    The binary VDF format used by shortcuts.vdf:
      0x00 <key>\0  — start of sub-dict
      0x01 <key>\0 <value>\0  — string field
      0x02 <key>\0 <int32_le>  — 32-bit integer field
      0x08  — end of current dict
    """

    # Type markers
    TYPE_DICT   = 0x00
    TYPE_STRING = 0x01
    TYPE_INT32  = 0x02
    TYPE_END    = 0x08

    @staticmethod
    def read_shortcuts(file_path):
        """Parse shortcuts.vdf into a list of shortcut dicts.

        Returns an empty list if the file doesn't exist or is empty.
        """
        file_path = Path(file_path)
        if not file_path.exists():
            return []

        try:
            data = file_path.read_bytes()
        except (OSError, IOError) as e:
            logging.error(f"Failed to read shortcuts.vdf: {e}")
            return []

        if len(data) < 3:
            return []

        shortcuts = []
        try:
            pos = [0]  # mutable for nested reads

            def read_string():
                end = data.index(b'\x00', pos[0])
                s = data[pos[0]:end].decode('utf-8', errors='replace')
                pos[0] = end + 1
                return s

            def read_int32():
                val = struct.unpack_from('<i', data, pos[0])[0]
                pos[0] += 4
                return val

            def read_dict():
                result = {}
                while pos[0] < len(data):
                    type_byte = data[pos[0]]
                    pos[0] += 1

                    if type_byte == SteamVDFHandler.TYPE_END:
                        break
                    elif type_byte == SteamVDFHandler.TYPE_DICT:
                        key = read_string()
                        result[key] = read_dict()
                    elif type_byte == SteamVDFHandler.TYPE_STRING:
                        key = read_string()
                        result[key] = read_string()
                    elif type_byte == SteamVDFHandler.TYPE_INT32:
                        key = read_string()
                        result[key] = read_int32()
                    else:
                        logging.warning(f"Unknown VDF type byte 0x{type_byte:02x} at pos {pos[0]-1}")
                        break
                return result

            root = read_dict()
            # Root is usually {'shortcuts': {'0': {...}, '1': {...}, ...}}
            shortcuts_dict = root.get('shortcuts', root)
            for key in sorted(shortcuts_dict.keys(), key=lambda k: int(k) if k.isdigit() else 0):
                shortcuts.append(shortcuts_dict[key])

        except Exception as e:
            logging.error(f"Failed to parse shortcuts.vdf: {e}")
            return []

        return shortcuts

    @staticmethod
    def write_shortcuts(file_path, shortcuts):
        """Write a list of shortcut dicts to shortcuts.vdf.

        Creates a backup before writing and uses atomic rename.
        """
        file_path = Path(file_path)

        # Backup existing file
        if file_path.exists():
            backup_path = file_path.with_suffix('.vdf.bak')
            try:
                shutil.copy2(str(file_path), str(backup_path))
            except (OSError, IOError) as e:
                logging.warning(f"Failed to backup shortcuts.vdf: {e}")

        def write_string(buf, key, value):
            buf.append(struct.pack('B', SteamVDFHandler.TYPE_STRING))
            buf.append(key.encode('utf-8') + b'\x00')
            buf.append(str(value).encode('utf-8') + b'\x00')

        def write_int32(buf, key, value):
            buf.append(struct.pack('B', SteamVDFHandler.TYPE_INT32))
            buf.append(key.encode('utf-8') + b'\x00')
            buf.append(struct.pack('<i', value))

        def write_dict_start(buf, key):
            buf.append(struct.pack('B', SteamVDFHandler.TYPE_DICT))
            buf.append(key.encode('utf-8') + b'\x00')

        def write_dict_end(buf):
            buf.append(struct.pack('B', SteamVDFHandler.TYPE_END))

        def write_shortcut(buf, index, shortcut):
            write_dict_start(buf, str(index))
            for key, value in shortcut.items():
                if key == 'tags':
                    write_dict_start(buf, 'tags')
                    if isinstance(value, dict):
                        for tag_key, tag_val in value.items():
                            write_string(buf, str(tag_key), tag_val)
                    elif isinstance(value, list):
                        for i, tag_val in enumerate(value):
                            write_string(buf, str(i), tag_val)
                    write_dict_end(buf)
                elif isinstance(value, int):
                    write_int32(buf, key, value)
                else:
                    write_string(buf, key, str(value))
            write_dict_end(buf)

        buf = []
        write_dict_start(buf, 'shortcuts')
        for i, shortcut in enumerate(shortcuts):
            write_shortcut(buf, i, shortcut)
        write_dict_end(buf)  # Close 'shortcuts' dict
        write_dict_end(buf)  # Close root/file

        binary_data = b''.join(buf)

        # Atomic write via temp file
        file_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=str(file_path.parent), suffix='.vdf.tmp')
        try:
            os.write(fd, binary_data)
            os.close(fd)
            os.replace(tmp_path, str(file_path))
        except Exception:
            os.close(fd) if not os.get_inheritable(fd) else None
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
            raise

    @staticmethod
    def calculate_appid(exe, app_name):
        """Calculate Steam shortcut appid from exe and app name.

        Steam uses: crc32(utf8(exe + app_name)) | 0x80000000, stored as signed int32.
        """
        crc = zlib.crc32((exe + app_name).encode('utf-8')) & 0xFFFFFFFF
        unsigned = crc | 0x80000000
        # Convert to signed int32
        if unsigned >= 0x80000000:
            return unsigned - 0x100000000
        return unsigned


class SteamShortcutManager:
    """High-level manager for adding/removing Steam shortcuts for ROM collections.

    Works with both GTK app and Decky plugin via sync_core.py.
    """

    MANAGED_TAG = 'romm-sync'  # Tag used to identify our shortcuts

    def __init__(self, retroarch_interface, settings, log_callback=None, cover_manager=None):
        self.retroarch = retroarch_interface
        self.settings = settings
        self.log = log_callback or (lambda msg: logging.info(msg))
        self._userdata_path_cache = None
        self.cover_manager = cover_manager

    def is_available(self):
        """Check if Steam userdata is accessible."""
        return self.find_steam_userdata_path() is not None

    def find_steam_userdata_path(self):
        """Find the Steam userdata directory containing shortcuts.vdf.

        Checks settings for manual override first, then auto-detects.
        Returns the path to the specific user's config dir, or None.
        """
        if self._userdata_path_cache is not None:
            return self._userdata_path_cache

        # Check settings for manual override
        manual_path = self.settings.get('Steam', 'userdata_path', '').strip()
        if manual_path:
            p = Path(manual_path)
            if p.exists():
                self._userdata_path_cache = p
                return p

        # Auto-detect Steam userdata
        steam_roots = [
            Path.home() / '.steam' / 'steam' / 'userdata',
            Path.home() / '.local' / 'share' / 'Steam' / 'userdata',
            Path.home() / '.var' / 'app' / 'com.valvesoftware.Steam' / 'data' / 'Steam' / 'userdata',
        ]

        for root in steam_roots:
            if not root.exists():
                continue
            # Find user directories (numeric IDs)
            user_dirs = [d for d in root.iterdir() if d.is_dir() and d.name.isdigit()]
            if not user_dirs:
                continue

            # Pick the one with the most recent shortcuts.vdf
            best = None
            best_mtime = 0
            for user_dir in user_dirs:
                shortcuts_file = user_dir / 'config' / 'shortcuts.vdf'
                config_dir = user_dir / 'config'
                if shortcuts_file.exists():
                    mtime = shortcuts_file.stat().st_mtime
                    if mtime > best_mtime:
                        best = config_dir
                        best_mtime = mtime
                elif config_dir.exists():
                    # Config dir exists but no shortcuts.vdf yet — valid target
                    if best is None:
                        best = config_dir

            if best:
                self._userdata_path_cache = best
                self.log(f"Found Steam userdata: {best}")
                return best

        return None

    def _get_shortcuts_path(self):
        """Get the full path to shortcuts.vdf."""
        userdata = self.find_steam_userdata_path()
        if not userdata:
            return None
        return userdata / 'shortcuts.vdf'

    def _build_launch_command(self, rom_path, platform_name):
        """Build the exe and launch options for a ROM.

        Returns (exe, launch_options) tuple, or (None, None) if no core found.
        """
        if not self.retroarch:
            return None, None

        # RetroDECK: detect via is_retrodeck_installation() which uses directory/flatpak
        # checks as fallbacks when retroarch_executable is None or lacks 'retrodeck'.
        # Use '"flatpak"' (quoted short name, no full path) to match the exact exe format
        # that Steam itself uses for flatpak shortcuts. Steam's overlay recalculates the
        # artwork appid from exe+name at display time, so the exe format here must match
        # what Steam expects — using the full path produces a different appid and the
        # overlay can't find the grid images.
        if self.retroarch.is_retrodeck_installation():
            self.log(f"  RetroDECK detected, using: \"flatpak\" run net.retrodeck.retrodeck")
            return '"flatpak"', f'run net.retrodeck.retrodeck "{rom_path}"'

        if not self.retroarch.retroarch_executable:
            return None, None

        exe = self.retroarch.retroarch_executable

        # Find the right core
        core_name, core_path = self.retroarch.suggest_core_for_platform(platform_name)
        if not core_name:
            return None, None

        # Build based on install type
        if 'flatpak' in exe:
            return 'flatpak', f'run org.libretro.RetroArch -L "{core_path}" "{rom_path}"'
        elif 'snap' in exe:
            return 'snap', f'run retroarch -L "{core_path}" "{rom_path}"'
        else:
            return f'"{exe}"', f'-L "{core_path}" "{rom_path}"'

    def build_shortcut_entry(self, rom_name, rom_path, platform_name, collection_name, rom_id=None, platform_slug=None, cover_url=None):
        """Build a single shortcut dict for a ROM.

        Creates a shortcut even if no core is found (with placeholder launch command).
        Returns the shortcut dict.

        Args:
            rom_name: Display name of the ROM
            rom_path: Path to the ROM file
            platform_name: Platform name
            collection_name: Collection name
            rom_id: Optional ROM ID for cover art download
            platform_slug: Optional platform slug for cover art organization
            cover_url: Optional cover URL from RomM API (path_cover_l or path_cover_s)
        """
        exe, launch_options = self._build_launch_command(rom_path, platform_name)

        # If no core found, create placeholder shortcut
        if not exe:
            self.log(f"  ⚠️  No core for {rom_name} ({platform_name}) - creating placeholder shortcut")
            # Use a simple placeholder that will show an error when launched
            exe = '/usr/bin/echo'
            launch_options = f'No RetroArch core found for platform: {platform_name}. Please install a compatible core.'
            # Add a tag to identify shortcuts missing cores
            missing_core_tag = 'romm-sync-missing-core'
        else:
            missing_core_tag = None

        app_name = f"{rom_name}"
        appid = SteamVDFHandler.calculate_appid(exe, app_name)

        # StartDir: directory containing the ROM
        start_dir = str(Path(rom_path).parent)

        # Build tags list
        tags = [self.MANAGED_TAG, collection_name, platform_name]
        if missing_core_tag:
            tags.append(missing_core_tag)

        # Icon will be set after cover processing if available
        icon_path = ''

        shortcut = {
            'appid': appid,
            'AppName': app_name,
            'Exe': exe,
            'StartDir': start_dir,
            'icon': icon_path,
            'ShortcutPath': '',
            'LaunchOptions': launch_options,
            'IsHidden': 0,
            'AllowDesktopConfig': 1,
            'AllowOverlay': 1,
            'OpenVR': 0,
            'Devkit': 0,
            'DevkitGameID': '',
            'DevkitOverrideAppID': 0,
            'LastPlayTime': 0,
            'FlatpakAppID': '',
            'tags': tags,
        }

        # Generate Steam grid artwork if enabled
        if self.cover_manager and rom_id and cover_url:
            artwork_enabled = self.settings.get('Steam', 'artwork_enabled', 'true') == 'true'

            if artwork_enabled:
                # Download cover art
                success, cover_path, msg = self.cover_manager.download_cover(
                    rom_id, platform_slug or 'unknown', cover_url
                )

                if success and cover_path:
                    # Generate Steam grid images
                    userdata_path = self.find_steam_userdata_path()
                    if userdata_path:
                        grid_dir = userdata_path / 'grid'
                        grid_success, count, grid_msg = SteamGridImageGenerator.generate_grid_images(
                            cover_path, grid_dir, appid
                        )

                        if grid_success:
                            self.log(f"  🎨 Generated {count} grid images for {rom_name}")
                        else:
                            # Non-fatal - log but continue
                            logging.debug(f"Grid generation failed for {rom_name}: {grid_msg}")

                        # Generate square icon for Steam
                        icon_dir = Path.home() / 'RomMSync' / 'icons'
                        icon_dir.mkdir(parents=True, exist_ok=True)
                        icon_file = icon_dir / f"{rom_id}.png"

                        icon_success, icon_msg = SteamGridImageGenerator.generate_square_icon(
                            cover_path, icon_file, size=256
                        )

                        if icon_success:
                            # Update shortcut icon field with absolute path
                            shortcut['icon'] = str(icon_file.absolute())
                            logging.debug(f"Generated icon for {rom_name}: {icon_file}")
                        else:
                            logging.debug(f"Icon generation failed for {rom_name}: {icon_msg}")
                    else:
                        logging.debug(f"Steam userdata not found, skipping grid generation for {rom_name}")
                else:
                    # Cover download failed - non-fatal
                    logging.debug(f"Cover download skipped for {rom_name}: {msg}")

        return shortcut

    def _is_managed_shortcut(self, shortcut, collection_name=None):
        """Check if a shortcut was created by us (optionally for a specific collection)."""
        tags = shortcut.get('tags', {})
        if isinstance(tags, dict):
            tag_values = set(tags.values())
        elif isinstance(tags, list):
            tag_values = set(tags)
        else:
            return False

        if self.MANAGED_TAG not in tag_values:
            return False
        if collection_name and collection_name not in tag_values:
            return False
        return True

    def _detect_multi_disc_from_api(self, rom):
        """Detect if a ROM is multi-disc and extract disc files from raw API data.
        
        This is a simplified version of the multi-disc detection logic from romm_sync_app.py
        that works with raw API data without requiring full game processing.
        
        Args:
            rom: ROM dict from RomM API
            
        Returns:
            (is_multi, disc_files) tuple where:
            - is_multi: True if this is a multi-disc game
            - disc_files: List of disc file dicts or strings
        """
        # First check if already processed (has is_multi_disc and discs)
        if rom.get('is_multi_disc', False):
            discs = rom.get('discs', [])
            if discs:
                return True, discs
        
        # Check for multi flag
        if rom.get('multi', False):
            files = rom.get('files', [])
            if files:
                return True, files
        
        # Analyze files array for disc patterns
        files = rom.get('files', [])
        if len(files) <= 1:
            return False, []
        
        disc_pattern = re.compile(r'(\(|\[|_|-|\s)(disc|disk|cd|dvd)(\s|_|-)?(\d+)(\)|\]|_|-|\s)', re.IGNORECASE)
        track_pattern = re.compile(r'(track|tr)(\s|_|-)?(\d+)', re.IGNORECASE)
        
        # Extract disc numbers from filenames (only count files, not tracks)
        disc_numbers = set()
        disc_files_map = {}  # Map disc number to file
        
        for file_obj in files:
            # Handle both string filenames and dict objects
            if isinstance(file_obj, str):
                file_name = file_obj
            elif isinstance(file_obj, dict):
                file_name = file_obj.get('filename', file_obj.get('file_name', file_obj.get('name', '')))
            else:
                continue
            
            # Skip if this is a track indicator (not a disc)
            if track_pattern.search(file_name):
                continue
            
            # Check if this file has a disc indicator
            match = disc_pattern.search(file_name)
            if match:
                disc_num = match.group(4)  # The disc number
                disc_numbers.add(disc_num)
                if disc_num not in disc_files_map:
                    disc_files_map[disc_num] = file_obj
        
        # Only treat as multi-disc if we have multiple different disc numbers
        if len(disc_numbers) > 1:
            # Return disc files sorted by disc number
            disc_files = [disc_files_map[num] for num in sorted(disc_numbers, key=int)]
            return True, disc_files
        
        return False, []

    def add_collection_shortcuts(self, collection_name, roms, download_dir):
        """Add Steam shortcuts for all downloaded ROMs in a collection.

        Args:
            collection_name: Name of the RomM collection
            roms: List of ROM dicts from RomM API
            download_dir: Path to ROM download directory

        Returns:
            (added_count, message)
        """
        shortcuts_path = self._get_shortcuts_path()
        if not shortcuts_path:
            return 0, "Steam userdata not found"

        # Load existing shortcuts
        shortcuts = SteamVDFHandler.read_shortcuts(shortcuts_path)

        # Remove any existing shortcuts for this collection first
        shortcuts = [s for s in shortcuts if not self._is_managed_shortcut(s, collection_name)]

        added = 0
        download_dir = Path(download_dir)

        self.log(f"[Steam] add_collection_shortcuts: {len(roms)} ROMs, download_dir={download_dir}")

        for rom in roms:
            # Folder-container ROM (no extension, has child files): the individual
            # variant files live in a subdirectory named after the container's fs_name.
            # Expand them here so both the GTK app (which pre-expands) and the Decky
            # plugin (which passes raw API data) get shortcuts for downloaded variants.
            if not rom.get('fs_extension', '') and rom.get('files', []):
                parent_folder = rom.get('fs_name', '')
                self.log(f"[Steam] container ROM: fs_name={rom.get('fs_name')!r} ext={rom.get('fs_extension')!r} files={len(rom.get('files', []))}")
                if not parent_folder:
                    continue
                _platform_slug = rom.get('platform_slug', 'Unknown')
                _platform_name = rom.get('platform_name', rom.get('platform_slug', 'Unknown'))
                _cover_url = rom.get('path_cover_large') or rom.get('path_cover_small')
                _rom_id = rom.get('id')
                for file_obj in rom.get('files', []):
                    if isinstance(file_obj, str):
                        file_name = file_obj
                    elif isinstance(file_obj, dict):
                        file_name = file_obj.get('filename') or file_obj.get('file_name', '')
                    else:
                        continue
                    if not file_name:
                        self.log(f"[Steam] container file_obj has no filename: {file_obj}")
                        continue
                    local_path = download_dir / _platform_slug / parent_folder / file_name
                    self.log(f"[Steam] variant path check: {local_path} exists={local_path.exists()}")
                    if not is_path_validly_downloaded(local_path):
                        continue
                    variant_name = Path(file_name).stem
                    entry = self.build_shortcut_entry(
                        variant_name, str(local_path), _platform_name, collection_name,
                        rom_id=_rom_id, platform_slug=_platform_slug, cover_url=_cover_url
                    )
                    if entry:
                        shortcuts.append(entry)
                        added += 1
                continue

            self.log(f"[Steam] regular ROM: fs_name={rom.get('fs_name')!r} ext={rom.get('fs_extension')!r} platform_slug={rom.get('platform_slug')!r}")

            rom_id = rom.get('id')
            fs_name = rom.get('fs_name', '')
            # Use filename stem as display name (includes region tag for variants),
            # matching the process_single_rom convention so all variants get unique names.
            rom_name = Path(fs_name).stem if fs_name else rom.get('name', 'Unknown')
            platform_name = rom.get('platform_name', rom.get('platform_slug', 'Unknown'))
            platform_slug = rom.get('platform_slug', 'Unknown')

            # Get cover URL (prefer large, fallback to small)
            cover_url = rom.get('path_cover_large') or rom.get('path_cover_small')

            # Detect multi-disc game from API data
            is_multi, disc_files = self._detect_multi_disc_from_api(rom)

            if is_multi and disc_files:
                # Multi-disc: one shortcut per disc
                for disc_idx, disc_file in enumerate(disc_files, 1):
                    # Handle both string filenames and dict objects
                    if isinstance(disc_file, str):
                        disc_name = disc_file
                    elif isinstance(disc_file, dict):
                        disc_name = disc_file.get('filename', disc_file.get('file_name', f'disc_{disc_idx}'))
                    else:
                        disc_name = f'disc_{disc_idx}'

                    local_path = download_dir / platform_slug / rom.get('fs_name', rom_name) / disc_name
                    if not is_path_validly_downloaded(local_path):
                        continue
                    disc_display = f"{rom_name} (Disc {disc_idx})"
                    entry = self.build_shortcut_entry(
                        disc_display, str(local_path), platform_name, collection_name,
                        rom_id=rom_id, platform_slug=platform_slug, cover_url=cover_url
                    )
                    if entry:
                        shortcuts.append(entry)
                        added += 1
            else:
                # Single ROM (including regional variants stored in a parent subfolder)
                file_name = fs_name or f"{rom_name}.rom"
                platform_dir = download_dir / platform_slug
                local_path = platform_dir / file_name
                self.log(f"[Steam] single ROM flat check: {local_path} exists={local_path.exists()}")
                if not is_path_validly_downloaded(local_path):
                    # Regional variant files land inside a parent-named subdirectory.
                    # Scan one level of subdirectories before giving up.
                    found_in_sub = False
                    if platform_dir.exists():
                        try:
                            for sub in platform_dir.iterdir():
                                if sub.is_dir():
                                    candidate = sub / file_name
                                    if is_path_validly_downloaded(candidate):
                                        local_path = candidate
                                        found_in_sub = True
                                        self.log(f"[Steam] found variant in subdir: {local_path}")
                                        break
                        except (OSError, PermissionError):
                            pass
                    if not found_in_sub:
                        self.log(f"[Steam] not found anywhere, skipping: {file_name}")
                        continue
                entry = self.build_shortcut_entry(
                    rom_name, str(local_path), platform_name, collection_name,
                    rom_id=rom_id, platform_slug=platform_slug, cover_url=cover_url
                )
                if entry:
                    shortcuts.append(entry)
                    added += 1

        # Write back
        try:
            SteamVDFHandler.write_shortcuts(shortcuts_path, shortcuts)

            # Collect appids of the shortcuts we just added for Steam collections
            appids_added = []
            for s in shortcuts:
                if self._is_managed_shortcut(s, collection_name):
                    appids_added.append(s['appid'])

            # Add to Steam collection (category)
            if appids_added:
                self.update_steam_collections(collection_name, appids_added)

            msg = f"Added {added} shortcuts for '{collection_name}'"
            self.log(msg)
            return added, msg
        except Exception as e:
            msg = f"Failed to write shortcuts.vdf: {e}"
            self.log(msg)
            return 0, msg

    def _cleanup_shortcut_artwork(self, shortcut):
        """Clean up grid images and icons for a shortcut.

        Args:
            shortcut: Shortcut dict containing appid and icon path
        """
        appid = shortcut.get('appid')
        if not appid:
            return

        # Convert to unsigned for filenames
        unsigned_appid = appid if appid >= 0 else appid + 0x100000000

        # Delete grid images
        userdata_path = self.find_steam_userdata_path()
        if userdata_path:
            grid_dir = userdata_path / 'grid'

            # Delete all variants (portrait, landscape, hero)
            for suffix in ['p.png', '.png', '_hero.png']:
                grid_file = grid_dir / f"{unsigned_appid}{suffix}"
                if grid_file.exists():
                    try:
                        grid_file.unlink()
                        logging.debug(f"Deleted grid image: {grid_file.name}")
                    except Exception as e:
                        logging.warning(f"Failed to delete {grid_file}: {e}")

        # Delete icon if it exists
        icon_path = shortcut.get('icon', '')
        if icon_path:
            icon_file = Path(icon_path)
            if icon_file.exists():
                try:
                    icon_file.unlink()
                    logging.debug(f"Deleted icon: {icon_file.name}")
                except Exception as e:
                    logging.warning(f"Failed to delete icon {icon_file}: {e}")

    def remove_collection_shortcuts(self, collection_name):
        """Remove all Steam shortcuts for a collection.

        Returns:
            (removed_count, message)
        """
        shortcuts_path = self._get_shortcuts_path()
        if not shortcuts_path:
            return 0, "Steam userdata not found"

        shortcuts = SteamVDFHandler.read_shortcuts(shortcuts_path)

        # Find shortcuts to remove and clean up their artwork
        shortcuts_to_remove = [s for s in shortcuts if self._is_managed_shortcut(s, collection_name)]

        if not shortcuts_to_remove:
            return 0, f"No shortcuts found for '{collection_name}'"

        # Clean up grid images and icons before removing
        for shortcut in shortcuts_to_remove:
            self._cleanup_shortcut_artwork(shortcut)

        # Remove shortcuts from list
        shortcuts = [s for s in shortcuts if not self._is_managed_shortcut(s, collection_name)]
        removed = len(shortcuts_to_remove)

        try:
            SteamVDFHandler.write_shortcuts(shortcuts_path, shortcuts)

            # Also remove the Steam collection itself
            self.remove_steam_collection(collection_name)

            msg = f"Removed {removed} shortcuts for '{collection_name}'"
            self.log(msg)
            return removed, msg
        except Exception as e:
            msg = f"Failed to write shortcuts.vdf: {e}"
            self.log(msg)
            return 0, msg

    def sync_collection_shortcuts(self, collection_name, current_roms, download_dir):
        """Sync Steam shortcuts to match the current state of a collection.

        Compares existing managed shortcuts with current ROM list,
        adds missing ones and removes stale ones.

        Returns:
            (added_count, removed_count)
        """
        shortcuts_path = self._get_shortcuts_path()
        if not shortcuts_path:
            return 0, 0

        shortcuts = SteamVDFHandler.read_shortcuts(shortcuts_path)
        download_dir = Path(download_dir)

        # Separate our shortcuts from user's shortcuts
        user_shortcuts = [s for s in shortcuts if not self._is_managed_shortcut(s, collection_name)]
        managed_shortcuts = [s for s in shortcuts if self._is_managed_shortcut(s, collection_name)]

        # Build set of existing managed AppNames for comparison
        existing_names = {s.get('AppName', '') for s in managed_shortcuts}

        # Build desired shortcuts from current ROM list
        desired = []
        desired_names = set()

        for rom in current_roms:
            # Folder-container ROM (no extension, has child files): expand variants.
            if not rom.get('fs_extension', '') and rom.get('files', []):
                parent_folder = rom.get('fs_name', '')
                if not parent_folder:
                    continue
                _platform_slug = rom.get('platform_slug', 'Unknown')
                _platform_name = rom.get('platform_name', rom.get('platform_slug', 'Unknown'))
                _cover_url = rom.get('path_cover_large') or rom.get('path_cover_small')
                _rom_id = rom.get('id')
                for file_obj in rom.get('files', []):
                    if isinstance(file_obj, str):
                        file_name = file_obj
                    elif isinstance(file_obj, dict):
                        file_name = file_obj.get('filename') or file_obj.get('file_name', '')
                    else:
                        continue
                    if not file_name:
                        continue
                    local_path = download_dir / _platform_slug / parent_folder / file_name
                    if not is_path_validly_downloaded(local_path):
                        continue
                    variant_name = Path(file_name).stem
                    entry = self.build_shortcut_entry(
                        variant_name, str(local_path), _platform_name, collection_name,
                        rom_id=_rom_id, platform_slug=_platform_slug, cover_url=_cover_url
                    )
                    if entry:
                        desired.append(entry)
                        desired_names.add(entry['AppName'])
                continue

            rom_id = rom.get('id')
            fs_name = rom.get('fs_name', '')
            rom_name = Path(fs_name).stem if fs_name else rom.get('name', 'Unknown')
            platform_name = rom.get('platform_name', rom.get('platform_slug', 'Unknown'))
            platform_slug = rom.get('platform_slug', 'Unknown')

            # Get cover URL (prefer large, fallback to small)
            cover_url = rom.get('path_cover_large') or rom.get('path_cover_small')

            # Detect multi-disc game from API data
            is_multi, disc_files = self._detect_multi_disc_from_api(rom)

            if is_multi and disc_files:
                for disc_idx, disc_file in enumerate(disc_files, 1):
                    # Handle both string filenames and dict objects
                    if isinstance(disc_file, str):
                        disc_name = disc_file
                    elif isinstance(disc_file, dict):
                        disc_name = disc_file.get('filename', disc_file.get('file_name', f'disc_{disc_idx}'))
                    else:
                        disc_name = f'disc_{disc_idx}'

                    local_path = download_dir / platform_slug / rom.get('fs_name', rom_name) / disc_name
                    if not is_path_validly_downloaded(local_path):
                        continue
                    disc_display = f"{rom_name} (Disc {disc_idx})"
                    entry = self.build_shortcut_entry(
                        disc_display, str(local_path), platform_name, collection_name,
                        rom_id=rom_id, platform_slug=platform_slug, cover_url=cover_url
                    )
                    if entry:
                        desired.append(entry)
                        desired_names.add(entry['AppName'])
            else:
                # Single ROM (including regional variants stored in a parent subfolder)
                file_name = fs_name or f"{rom_name}.rom"
                platform_dir = download_dir / platform_slug
                local_path = platform_dir / file_name
                if not is_path_validly_downloaded(local_path):
                    # Regional variant files land inside a parent-named subdirectory.
                    # Scan one level of subdirectories before giving up.
                    found_in_sub = False
                    if platform_dir.exists():
                        try:
                            for sub in platform_dir.iterdir():
                                if sub.is_dir():
                                    candidate = sub / file_name
                                    if is_path_validly_downloaded(candidate):
                                        local_path = candidate
                                        found_in_sub = True
                                        break
                        except (OSError, PermissionError):
                            pass
                    if not found_in_sub:
                        continue
                entry = self.build_shortcut_entry(
                    rom_name, str(local_path), platform_name, collection_name,
                    rom_id=rom_id, platform_slug=platform_slug, cover_url=cover_url
                )
                if entry:
                    desired.append(entry)
                    desired_names.add(entry['AppName'])

        # Calculate delta
        to_add = [s for s in desired if s['AppName'] not in existing_names]
        to_keep = [s for s in managed_shortcuts if s.get('AppName', '') in desired_names]
        to_remove = [s for s in managed_shortcuts if s.get('AppName', '') not in desired_names]
        removed_count = len(to_remove)

        # Clean up artwork for removed shortcuts
        for shortcut in to_remove:
            self._cleanup_shortcut_artwork(shortcut)

        # Rebuild full list
        new_shortcuts = user_shortcuts + to_keep + to_add

        if to_add or removed_count > 0:
            try:
                SteamVDFHandler.write_shortcuts(shortcuts_path, new_shortcuts)
                if to_add:
                    self.log(f"Steam: added {len(to_add)} shortcuts for '{collection_name}'")
                if removed_count > 0:
                    self.log(f"Steam: removed {removed_count} shortcuts for '{collection_name}'")
            except Exception as e:
                self.log(f"Steam: failed to sync shortcuts: {e}")
                return 0, 0

        return len(to_add), removed_count

    def get_collection_shortcut_count(self, collection_name):
        """Get the number of Steam shortcuts for a collection."""
        shortcuts_path = self._get_shortcuts_path()
        if not shortcuts_path:
            return 0
        shortcuts = SteamVDFHandler.read_shortcuts(shortcuts_path)
        return sum(1 for s in shortcuts if self._is_managed_shortcut(s, collection_name))

    def get_steam_sync_collections(self):
        """Get the set of collection names that have Steam sync enabled."""
        raw = self.settings.get('Steam', 'collections', '').strip()
        if not raw:
            return set()
        return set(c.strip() for c in raw.split('|') if c.strip())

    def set_steam_sync_collections(self, collections):
        """Save the set of collection names that have Steam sync enabled."""
        value = '|'.join(sorted(collections))
        if not self.settings.config.has_section('Steam'):
            self.settings.config.add_section('Steam')
        self.settings.config.set('Steam', 'collections', value)
        self.settings.save_settings()

    def _get_sharedconfig_path(self):
        """Get the path to Steam's sharedconfig.vdf (text VDF with collections)."""
        userdata = self.find_steam_userdata_path()
        if not userdata:
            return None
        # Navigate up from config/ to userdata/USERID/, then to 7/remote/
        user_id_dir = userdata.parent
        sharedconfig = user_id_dir / '7' / 'remote' / 'sharedconfig.vdf'
        return sharedconfig if sharedconfig.exists() else None

    def update_steam_collections(self, collection_name, shortcut_appids):
        """Add shortcuts to a Steam collection (category).

        Args:
            collection_name: Name of the RomM collection (will be the Steam category name)
            shortcut_appids: List of appids to add to this collection
        """
        import json
        import hashlib

        # Get paths (find_steam_userdata_path returns the config directory)
        config_path = self.find_steam_userdata_path()
        if not config_path:
            self.log("Steam userdata not found - collections not updated")
            return False

        localconfig_path = config_path / 'localconfig.vdf'
        cloud_storage_path = config_path / 'cloudstorage' / 'cloud-storage-namespace-1.json'

        if not localconfig_path.exists():
            self.log("localconfig.vdf not found - collections not updated")
            return False

        try:
            # Convert appids to unsigned
            unsigned_appids = [
                appid if appid >= 0 else appid + 0x100000000
                for appid in shortcut_appids
            ]

            # Generate a deterministic collection ID based on collection name
            collection_id = f"romm-{hashlib.md5(collection_name.encode()).hexdigest()[:12]}"

            # Update localconfig.vdf
            with open(localconfig_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()

            # Find the user-collections line
            import re
            # Match the full line - VDF format is "key"\t\t"value"
            match = re.search(r'(\s*)"user-collections"(\s+)"([^"\\]*(?:\\.[^"\\]*)*)"', content)
            if match:
                indent = match.group(1)
                whitespace = match.group(2)
                json_str = match.group(3)
                # Unescape the JSON (VDF escapes quotes as \" and backslashes as \\)
                json_str = json_str.replace('\\\\', '\x00')  # Temporarily replace \\ with null byte
                json_str = json_str.replace('\\"', '"')      # Replace \" with "
                json_str = json_str.replace('\x00', '\\')    # Restore \\ as \
                collections = json.loads(json_str)

                # Add or update our collection
                collections[collection_id] = {
                    'id': collection_id,
                    'added': unsigned_appids,
                    'removed': []
                }

                # Re-serialize
                new_json = json.dumps(collections, separators=(',', ':'))
                # Escape for VDF
                new_json_escaped = new_json.replace('\\', '\\\\').replace('"', '\\"')

                # Replace in content
                new_line = f'{indent}"user-collections"{whitespace}"{new_json_escaped}"'
                content = re.sub(
                    r'\s*"user-collections"\s+"[^"\\]*(?:\\.[^"\\]*)*"',
                    new_line,
                    content
                )

                # Backup and write
                backup_path = localconfig_path.with_suffix('.vdf.bak')
                shutil.copy2(localconfig_path, backup_path)

                with open(localconfig_path, 'w', encoding='utf-8') as f:
                    f.write(content)

                self.log(f"Updated localconfig.vdf with {len(unsigned_appids)} games in collection '{collection_name}'")
            else:
                self.log("user-collections not found in localconfig.vdf")
                return False

            # Update cloud storage (if exists)
            if cloud_storage_path.exists():
                try:
                    with open(cloud_storage_path, 'r', encoding='utf-8') as f:
                        cloud_data = json.load(f)

                    # Find existing entry or create new one
                    collection_entry = None
                    for i, entry in enumerate(cloud_data):
                        if entry[0] == f'user-collections.{collection_id}':
                            collection_entry = entry
                            break

                    # Create collection metadata
                    timestamp = int(time.time())
                    collection_value = {
                        'id': collection_id,
                        'name': collection_name,
                        'added': unsigned_appids,
                        'removed': []
                    }

                    new_entry = [
                        f'user-collections.{collection_id}',
                        {
                            'key': f'user-collections.{collection_id}',
                            'timestamp': timestamp,
                            'value': json.dumps(collection_value),
                            'version': str(timestamp),
                            'conflictResolutionMethod': 'custom',
                            'strMethodId': 'union-collections'
                        }
                    ]

                    if collection_entry:
                        # Update existing
                        idx = cloud_data.index(collection_entry)
                        cloud_data[idx] = new_entry
                    else:
                        # Add new
                        cloud_data.append(new_entry)

                    # Backup and write
                    backup_cloud = cloud_storage_path.with_suffix('.json.bak')
                    shutil.copy2(cloud_storage_path, backup_cloud)

                    with open(cloud_storage_path, 'w', encoding='utf-8') as f:
                        json.dump(cloud_data, f, separators=(',', ':'))

                    self.log(f"Updated cloud storage with collection '{collection_name}'")
                except Exception as e:
                    self.log(f"Failed to update cloud storage (non-fatal): {e}")

            return True

        except Exception as e:
            self.log(f"Failed to update Steam collections: {e}")
            logging.error(f"Steam collection update error: {e}", exc_info=True)
            return False

    def remove_steam_collection(self, collection_name):
        """Remove a Steam collection (category) completely.

        Args:
            collection_name: Name of the RomM collection to remove from Steam
        """
        import json
        import hashlib

        config_path = self.find_steam_userdata_path()
        if not config_path:
            return False

        localconfig_path = config_path / 'localconfig.vdf'
        cloud_storage_path = config_path / 'cloudstorage' / 'cloud-storage-namespace-1.json'

        if not localconfig_path.exists():
            return False

        try:
            # Generate collection ID (same as in update_steam_collections)
            collection_id = f"romm-{hashlib.md5(collection_name.encode()).hexdigest()[:12]}"

            # Remove from localconfig.vdf
            with open(localconfig_path, 'r', encoding='utf-8', errors='replace') as f:
                content = f.read()

            import re
            match = re.search(r'(\s*)"user-collections"(\s+)"([^"\\]*(?:\\.[^"\\]*)*)"', content)
            if match:
                indent = match.group(1)
                whitespace = match.group(2)
                json_str = match.group(3)

                # Unescape JSON
                json_str = json_str.replace('\\\\', '\x00')
                json_str = json_str.replace('\\"', '"')
                json_str = json_str.replace('\x00', '\\')
                collections = json.loads(json_str)

                # Remove our collection if it exists
                if collection_id in collections:
                    del collections[collection_id]

                    # Re-serialize
                    new_json = json.dumps(collections, separators=(',', ':'))
                    new_json_escaped = new_json.replace('\\', '\\\\').replace('"', '\\"')

                    # Replace in content
                    new_line = f'{indent}"user-collections"{whitespace}"{new_json_escaped}"'
                    content = re.sub(
                        r'\s*"user-collections"\s+"[^"\\]*(?:\\.[^"\\]*)*"',
                        new_line,
                        content
                    )

                    # Backup and write
                    backup_path = localconfig_path.with_suffix('.vdf.bak')
                    shutil.copy2(localconfig_path, backup_path)

                    with open(localconfig_path, 'w', encoding='utf-8') as f:
                        f.write(content)

                    self.log(f"Removed collection '{collection_name}' from localconfig.vdf")

            # Remove from cloud storage
            if cloud_storage_path.exists():
                try:
                    with open(cloud_storage_path, 'r', encoding='utf-8') as f:
                        cloud_data = json.load(f)

                    # Find and remove collection entry
                    key_to_remove = f'user-collections.{collection_id}'
                    cloud_data = [entry for entry in cloud_data if entry[0] != key_to_remove]

                    # Backup and write
                    backup_cloud = cloud_storage_path.with_suffix('.json.bak')
                    shutil.copy2(cloud_storage_path, backup_cloud)

                    with open(cloud_storage_path, 'w', encoding='utf-8') as f:
                        json.dump(cloud_data, f, separators=(',', ':'))

                    self.log(f"Removed collection '{collection_name}' from cloud storage")
                except Exception as e:
                    self.log(f"Failed to remove from cloud storage (non-fatal): {e}")

            return True

        except Exception as e:
            self.log(f"Failed to remove Steam collection: {e}")
            logging.error(f"Steam collection removal error: {e}", exc_info=True)
            return False
