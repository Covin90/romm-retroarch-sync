import asyncio
import base64
import hashlib
import json
import logging
import mimetypes
import sys
import threading
import time
import os
import ctypes
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urljoin

# Add py_modules to path so sync_core is importable
sys.path.insert(0, str(Path(__file__).parent / "py_modules"))

# Preload Pillow shared libraries for bundled wheel
# This is needed because the .so files in the wheel need to find their dependencies
pillow_libs_dir = Path(__file__).parent / "py_modules" / "pillow.libs"
if pillow_libs_dir.exists():
    try:
        # Preload all shared libraries from pillow.libs
        for lib_file in sorted(pillow_libs_dir.glob("*.so*")):
            try:
                ctypes.CDLL(str(lib_file))
                logging.debug(f"[PIL] Preloaded library: {lib_file.name}")
            except Exception as e:
                logging.debug(f"[PIL] Could not preload {lib_file.name}: {e}")
    except Exception as e:
        logging.warning(f"[PIL] Error preloading pillow libraries: {e}")

# Import PIL early to ensure C extensions are loaded correctly
# This must be done before sync_core imports it at module level
try:
    from PIL import Image
    PIL_AVAILABLE = True
    logging.info(f"[PIL] PIL imported successfully from: {Image.__file__}")
except ImportError as e:
    PIL_AVAILABLE = False
    logging.error(f"[PIL] PIL import failed: {e}")

try:
    from sync_core import (
        SettingsManager, RomMClient, RetroArchInterface,
        AutoSyncManager, CollectionSyncManager,
        BiosTrackingManager,
        SteamShortcutManager, CoverArtManager,
        build_sync_status, is_path_validly_downloaded, detect_retrodeck,
    )
    SYNC_CORE_AVAILABLE = True
except ImportError as e:
    logging.warning(f"sync_core not available: {e}")
    SYNC_CORE_AVAILABLE = False

# ---------------------------------------------------------------------------
# Logging setup
# ---------------------------------------------------------------------------
log_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'decky_debug.log'
log_file.parent.mkdir(parents=True, exist_ok=True)
settings_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'decky_settings.json'

# Persisted library snapshot — the last successful fetch of games + collections +
# platform mappings. Hydrated on cold start so the Game Browser populates offline,
# overwritten (write-through) after every successful fetch. schema-versioned and
# tagged with the server_url so a server switch invalidates it. See _persist_snapshot /
# _load_snapshot. SCHEMA bumps when the persisted game/collection shape changes.
snapshot_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'library_snapshot.json'
SNAPSHOT_SCHEMA = 1


def load_decky_settings():
    try:
        if settings_file.exists():
            with open(settings_file, 'r') as f:
                return json.load(f)
    except Exception as e:
        print(f"Failed to load decky settings: {e}")
    return {'logging_enabled': True}


def save_decky_settings(settings):
    try:
        settings_file.parent.mkdir(parents=True, exist_ok=True)
        with open(settings_file, 'w') as f:
            json.dump(settings, f, indent=2)
        return True
    except Exception as e:
        print(f"Failed to save decky settings: {e}")
        return False


decky_settings = load_decky_settings()
logging_enabled = decky_settings.get('logging_enabled', True)

_root_logger = logging.getLogger()
_file_handler = None

if logging_enabled:
    _file_handler = logging.FileHandler(str(log_file))
    _file_handler.setLevel(logging.DEBUG)
    _file_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
    _root_logger.addHandler(_file_handler)
    _root_logger.setLevel(logging.DEBUG)

# Suppress noisy third-party loggers
logging.getLogger('watchdog').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# File extensions considered launchable discs — must match Plugin._LAUNCHABLE_DISC_EXTS.
_LAUNCHABLE_DISC_EXTS = ('.m3u', '.chd', '.cue', '.iso', '.pbp',
                          '.ccd', '.gdi', '.cdi', '.nrg')

# Auxiliary (non-game) files that may sit alongside ROMs inside a download
# folder: playlists, cover art, saves/states, and metadata. Used to isolate the
# actual standalone game files when classifying a multi-FILE ROM (regional
# variants) — must match Plugin._NON_GAME_EXTS.
_NON_GAME_EXTS = (
    '.m3u', '.png', '.jpg', '.jpeg', '.webp', '.gif', '.bmp',
    '.srm', '.sav', '.dsv', '.mcr', '.eep', '.fla', '.mpk', '.sra',
    '.state', '.auto', '.txt', '.nfo', '.xml', '.dat', '.json', '.cue',
)


def _list_standalone_games(folder):
    """Return the standalone game files inside a folder (regional/multi-file ROM).

    A RomM multi-FILE ROM (e.g. one entry whose member files are per-region
    cartridge dumps like .nds) extracts to a folder containing the game files
    plus a RomM-generated .m3u. Those files are NOT discs — each is a complete,
    independently bootable game — so the .m3u must never be launched (it would
    hand the emulator two full games as "discs"). This isolates the real games:
    every file that is not auxiliary (playlist/cover/save/metadata) and not a
    disc image. Returns a name-sorted list of Path objects.
    """
    games = []
    for f in folder.rglob('*'):
        if not f.is_file():
            continue
        ext = f.suffix.lower()
        if ext in _NON_GAME_EXTS or ext in _LAUNCHABLE_DISC_EXTS:
            continue
        # State slots are ".state", ".state1", ".state.auto", etc.
        if ext.startswith('.state'):
            continue
        games.append(f)
    return sorted(games, key=lambda x: x.name.lower())


def _detect_multi_disc(local_path, is_downloaded):
    """Return (is_multi_disc, disc_count) for the downloaded game.

    Covers both flavours of "multiple bootable files in one folder":
      - true multi-DISC games (disc images / an .m3u playlist), and
      - multi-FILE ROMs whose members are regional cartridge variants.
    Both are surfaced through the same picker; region-vs-disc labelling is
    resolved later from the on-disk entries (see get_local_discs).
    """
    if not is_downloaded:
        return False, 0
    p = Path(local_path)
    if not p.exists():
        return False, 0
    if p.is_dir():
        launchable = [f for f in p.rglob('*')
                      if f.is_file() and f.suffix.lower() in _LAUNCHABLE_DISC_EXTS]
        if len(launchable) > 1:
            return True, len(launchable)
        # No disc set — fall back to standalone game files (regional variants).
        games = _list_standalone_games(p)
        return len(games) > 1, len(games)
    return False, 0


# ---------------------------------------------------------------------------
# Plugin
# ---------------------------------------------------------------------------

class Plugin:
    # Sync objects — owned directly
    _settings: 'SettingsManager' = None
    _retroarch = None
    _romm_client = None
    _auto_sync = None
    _collection_sync = None
    _available_games: list = None

    # Background retry thread (reconnect + collection-list refresh every 5 min)
    _stop_event: threading.Event = None
    _retry_thread: threading.Thread = None

    # Cached collection list — refreshed on connect and every 5 min in _retry_loop.
    # Passed to build_sync_status so get_service_status() makes zero API calls.
    _romm_collections: list = None

    # Cached virtual (autogenerated) collections — browse/download only, never
    # auto-synced. Keyed by their opaque base64 `id`.
    _romm_virtual_collections: list = None

    # True once the first _connect_to_romm() attempt has completed (even on failure),
    # used by get_service_status() to distinguish "still starting" from "failed".
    _connection_attempted: bool = False

    # Snapshot of ROM counts for collections that have been disabled.
    # Keyed by collection name; value is the total count from the cache at disable time.
    # Cleared when deletion completes or the collection is re-enabled.
    _disabled_collection_counts: dict = {}

    # Platform mapping (slug -> name)
    _platform_slug_to_name: dict = None  # {'psx': 'Sony - PlayStation', ...}

    # Timestamp for efficient polling with updated_after parameter
    _last_full_fetch_time: str = None  # ISO 8601 datetime of last full data fetch

    # ISO 8601 fetched_at of the data currently in memory, sourced either from the
    # persisted snapshot (cold start) or the last live fetch. Drives the "library
    # from N ago" copy in the offline banner. None until hydrated/fetched.
    _snapshot_fetched_at: str = None

    # Reachability latch, distinct from RomMClient.authenticated (which is sticky).
    # True while the server is responding; flipped False on any connected-branch
    # failure. A False→True transition triggers an offline save flush. Starts None
    # so the first successful probe doesn't count as a "reconnect".
    _online: bool = None

    _bios_tracking: 'BiosTrackingManager' = None
    _steam_manager: 'SteamShortcutManager' = None

    # Collections currently running a Steam shortcut sync (add/remove in progress)
    _syncing_steam_collections: set = None

    # Game Browser: base64 cover-art cache {(rom_id, large): data_uri} and a
    # rom_id -> cover_path map for games not in _available_games (collection view).
    _cover_cache: dict = {}
    _cover_paths: dict = {}

    # Live per-game download progress {rom_id: {percent, downloaded, total, speed,
    # eta, state}} populated by download_game's progress_callback and polled by the
    # frontend via get_download_progress() for the cover/button fill UI.
    _download_progress: dict = {}

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def _main(self):
        self._available_games = []
        self._romm_collections = None
        self._romm_virtual_collections = None
        self._connection_attempted = False
        self._platform_slug_to_name = {}
        self._syncing_steam_collections = set()
        logging.info("RomM Sync Monitor starting...")
        self._start_sync()
        return await self.get_service_status()

    async def _unload(self):
        logging.info("RomM Sync Monitor unloading...")
        self._stop_sync()

    # -----------------------------------------------------------------------
    # Internal sync management
    # -----------------------------------------------------------------------

    def _start_sync(self):
        if not SYNC_CORE_AVAILABLE:
            logging.error("sync_core not available, cannot start sync")
            return
        if self._retry_thread and self._retry_thread.is_alive():
            return

        self._settings = SettingsManager()
        self._retroarch = RetroArchInterface()
        self._steam_manager = SteamShortcutManager(
            retroarch_interface=self._retroarch,
            settings=self._settings,
            log_callback=lambda msg: logging.info(f"[STEAM] {msg}"),
            cover_manager=None  # Will be set after romm_client is created
        )
        logging.info(f"Steam shortcut manager created, available: {self._steam_manager.is_available()}")
        logging.info(f"RetroArch interface created, has bios_manager: {hasattr(self._retroarch, 'bios_manager')}")
        if hasattr(self._retroarch, 'bios_manager'):
            logging.info(f"bios_manager value: {self._retroarch.bios_manager}")
        if self._available_games is None:
            self._available_games = []
        self._connection_attempted = False

        # Cold-start hydration: seed the library from the last persisted snapshot
        # so the Game Browser is populated immediately — before the retry thread
        # connects, and even if it never does (offline). A successful fetch later
        # overwrites this in place.
        if not self._available_games:
            self._hydrate_from_snapshot()

        self._stop_event = threading.Event()
        self._retry_thread = threading.Thread(
            target=self._retry_loop,
            daemon=True,
            name="romm-sync-retry",
        )
        self._retry_thread.start()

        logging.info("Sync started (retry thread only, managers start on connect)")

    def _stop_sync(self):
        if self._stop_event:
            self._stop_event.set()
        if self._retry_thread:
            self._retry_thread.join(timeout=5)

        # Stop managers
        if self._bios_tracking:
            # No explicit stop needed (downloads run to completion)
            self._bios_tracking = None

        if self._auto_sync and self._auto_sync.enabled:
            try:
                self._auto_sync.stop_auto_sync()
            except Exception:
                pass
        if self._collection_sync:
            try:
                self._collection_sync.stop()
            except Exception:
                pass

        self._retry_thread = None
        self._stop_event = None
        self._romm_client = None
        self._auto_sync = None
        self._collection_sync = None
        self._romm_collections = None
        self._romm_virtual_collections = None
        self._connection_attempted = False
        self._disabled_collection_counts.clear()

        logging.info("Sync stopped")

    # -----------------------------------------------------------------------
    # Library snapshot (offline-first persistence)
    # -----------------------------------------------------------------------

    def _pending_save_count(self):
        """Best-effort count of local save changes not yet pushed, for the UI.
        Zero when auto-sync hasn't started (e.g. cold-start offline)."""
        try:
            if self._auto_sync:
                return self._auto_sync.count_pending_saves()
        except Exception as e:
            logging.debug(f"pending save count failed: {e}")
        return 0

    def _note_reachable(self):
        """Mark the server reachable. On an offline→online edge (was False),
        flush any save changes made while offline. No-op on the first-ever probe
        (was None) so startup doesn't masquerade as a reconnect."""
        was = self._online
        self._online = True
        if was is False and self._auto_sync:
            try:
                logging.info("Server reachable again — flushing offline save changes")
                self._auto_sync.flush_after_reconnect()
            except Exception as e:
                logging.warning(f"Reconnect save flush failed: {e}")

    def _persist_snapshot(self):
        """Write-through the live library to disk so a cold start can hydrate it
        offline. Called after every successful fetch. Best-effort: never raises
        into the caller (a failed snapshot must not break a working session)."""
        try:
            url = self._settings.get('RomM', 'url') if self._settings else ''
            data = {
                'schema': SNAPSHOT_SCHEMA,
                'fetched_at': datetime.now(timezone.utc).isoformat(),
                'server_url': url,
                'games': self._available_games or [],
                'collections': self._romm_collections or [],
                'virtual_collections': self._romm_virtual_collections or [],
                'platform_slug_to_name': self._platform_slug_to_name or {},
            }
            tmp = snapshot_file.with_suffix('.tmp')
            tmp.parent.mkdir(parents=True, exist_ok=True)
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(data, f, separators=(',', ':'))
            tmp.rename(snapshot_file)
            logging.info(f"Persisted library snapshot ({len(data['games'])} games)")
        except Exception as e:
            logging.warning(f"Failed to persist library snapshot: {e}")

    def _hydrate_from_snapshot(self):
        """Load the persisted snapshot into live state on cold start so the Game
        Browser populates before (or without) a network connection. Discards a
        snapshot from a different server or an incompatible schema. Returns the
        ISO fetched_at timestamp on success, else None."""
        try:
            if not snapshot_file.exists():
                return None
            with open(snapshot_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
            if data.get('schema') != SNAPSHOT_SCHEMA:
                logging.info("Library snapshot schema mismatch; ignoring")
                return None
            configured_url = self._settings.get('RomM', 'url') if self._settings else ''
            if configured_url and data.get('server_url') and data['server_url'] != configured_url:
                logging.info("Library snapshot is for a different server; ignoring")
                return None
            self._available_games = data.get('games') or []
            self._romm_collections = data.get('collections') or None
            self._romm_virtual_collections = data.get('virtual_collections') or None
            self._platform_slug_to_name = data.get('platform_slug_to_name') or {}
            self._snapshot_fetched_at = data.get('fetched_at')
            logging.info(f"Hydrated {len(self._available_games)} games from snapshot "
                         f"(fetched {self._snapshot_fetched_at})")
            return self._snapshot_fetched_at
        except Exception as e:
            logging.warning(f"Failed to hydrate library snapshot: {e}")
            return None

    def _connect_to_romm(self):
        """Connect to RomM, load game list, and start AutoSyncManager."""
        url      = self._settings.get('RomM', 'url')
        username = self._settings.get('RomM', 'username')
        password = self._settings.get('RomM', 'password')
        client_token = self._settings.get('RomM', 'client_token', '')
        remember     = self._settings.get('RomM', 'remember_credentials') == 'true'
        auto_connect = self._settings.get('RomM', 'auto_connect') == 'true'

        # A paired Client API Token is sufficient on its own (RomM's recommended
        # companion-app auth); otherwise fall back to stored username/password.
        if not (url and auto_connect and (client_token or (username and password and remember))):
            logging.info("Auto-connect disabled or credentials missing")
            return False

        try:
            logging.info(f"Connecting to RomM at {url}...")
            self._romm_client = RomMClient(url, username, password, client_token=client_token or None)
            if not self._romm_client.authenticated:
                logging.error("RomM authentication failed")
                return False

            # Initialize cover art manager for Steam grid images
            self._romm_client.cover_manager = CoverArtManager(self._settings, self._romm_client)

            # Update steam manager with cover manager
            if self._steam_manager:
                self._steam_manager.cover_manager = self._romm_client.cover_manager

            logging.info("Connected to RomM successfully")

            # Fetch and cache platform mappings (slug -> name)
            try:
                platforms = self._romm_client.get_platforms()
                self._platform_slug_to_name.clear()
                for platform in platforms:
                    slug = platform.get('slug')
                    name = platform.get('name')
                    if slug and name:
                        self._platform_slug_to_name[slug] = name
                logging.info(f"Cached {len(self._platform_slug_to_name)} platform mappings")
            except Exception as e:
                logging.warning(f"Failed to fetch platforms: {e}")

            # Cache collection list for zero-latency heartbeat rebuilds
            self._romm_collections = self._romm_client.get_collections()
            logging.info(f"Cached {len(self._romm_collections)} collections")

            # Virtual collections (autogenerated, browse/download only).
            try:
                self._romm_virtual_collections = self._romm_client.get_virtual_collections()
                logging.info(f"Cached {len(self._romm_virtual_collections)} virtual collections")
            except Exception as e:
                logging.warning(f"Failed to fetch virtual collections: {e}")
                self._romm_virtual_collections = []

            # Fetch ROM counts for already-disabled collections in background so
            # get_service_status() can show "X / Y ROMs locally" even after restart.
            threading.Thread(target=self._fetch_disabled_counts,
                             daemon=True, name="romm-disabled-counts").start()

            # Load game list
            roms_result = self._romm_client.get_roms()
            if roms_result and len(roms_result) == 2:
                raw_games, _ = roms_result
                self._available_games.clear()
                download_dir = Path(self._settings.get('Download', 'rom_directory',
                                                        '~/RomMSync/roms')).expanduser()
                for rom in raw_games:
                    platform_slug = rom.get('platform_slug', 'Unknown')
                    file_name     = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
                    local_path    = download_dir / platform_slug / file_name
                    is_downloaded = is_path_validly_downloaded(local_path)
                    local_size    = 0
                    if is_downloaded and local_path.exists():
                        if local_path.is_dir():
                            local_size = sum(f.stat().st_size
                                             for f in local_path.rglob('*') if f.is_file())
                        else:
                            local_size = local_path.stat().st_size
                    is_md = _detect_multi_disc(local_path, is_downloaded)
                    self._available_games.append({
                        'name':            Path(file_name).stem if file_name else rom.get('name', 'Unknown'),
                        'rom_id':          rom.get('id'),
                        'platform':        rom.get('platform_name', 'Unknown'),
                        'platform_slug':   platform_slug,
                        'file_name':       file_name,
                        'is_downloaded':   is_downloaded,
                        'is_multi_disc':   is_md[0],
                        'disc_count':      is_md[1],
                        'local_path':      str(local_path) if is_downloaded else None,
                        'local_size':      local_size,
                        'cover_path': rom.get('path_cover_small'),
                        'cover_path_large': rom.get('path_cover_large'),
                        'created_at':      rom.get('created_at'),
                        '_sibling_files':  rom.get('_sibling_files', []),
                        'sibling_roms':    rom.get('sibling_roms', []),
                        'romm_data': {
                            'fs_name':         rom.get('fs_name'),
                            'fs_name_no_ext':  rom.get('fs_name_no_ext'),
                            'fs_size_bytes':   rom.get('fs_size_bytes', 0),
                            'platform_id':     rom.get('platform_id'),
                            'platform_slug':   rom.get('platform_slug'),
                            # Member files (multi-disc / multi-FILE regional ROMs);
                            # the save-sync matcher maps a launched member back to
                            # this parent ROM. Requires with_files=true in get_roms.
                            'files':           rom.get('files', []),
                        },
                    })
                logging.info(f"Loaded {len(self._available_games)} games")

                self._last_full_fetch_time = datetime.now(timezone.utc).isoformat()
                self._snapshot_fetched_at = self._last_full_fetch_time
                self._persist_snapshot()
                # Library is fetched on connect and on manual refresh (the reference
                # client's on-demand model) — no background polling.

                # Initialize BiosTrackingManager
                self._bios_tracking = BiosTrackingManager(
                    retroarch=self._retroarch,
                    romm_client=self._romm_client,
                    collection_sync=self._collection_sync,  # May be None initially
                    available_games_list=self._available_games,
                    platform_slug_to_name=self._platform_slug_to_name,
                    log_callback=lambda msg: logging.info(f"[BIOS] {msg}"),
                )
                self._bios_tracking.scan_library_bios()

            # Register this device with RomM so save-sync (the /negotiate engine)
            # has a device_id. Without it the session sync can't run and battery
            # saves never sync. Mirrors the GTK app's initialize_device().
            self._ensure_device_registered()

            # AutoSyncManager (save/state sync)
            if self._auto_sync is None:
                self._auto_sync = AutoSyncManager(
                    romm_client=self._romm_client,
                    retroarch=self._retroarch,
                    settings=self._settings,
                    log_callback=lambda msg: logging.info(f"[AUTO-SYNC] {msg}"),
                    get_games_callback=lambda: self._available_games,
                    parent_window=None,
                )
            else:
                self._auto_sync.romm_client = self._romm_client

            if self._settings.get('AutoSync', 'auto_enable_on_connect') == 'true':
                self._auto_sync.upload_enabled   = True
                self._auto_sync.download_enabled = True
                try:
                    upload_delay = int(self._settings.get('AutoSync', 'sync_delay', '3'))
                except (ValueError, TypeError):
                    upload_delay = 3
                self._auto_sync.upload_delay = upload_delay
                self._auto_sync.start_auto_sync()
                logging.info("Auto-sync (saves/states) enabled")
                # Collection sync runs in its own thread
                threading.Thread(target=self._init_collection_sync,
                                 daemon=True, name="romm-collection-init").start()

            return True

        except Exception as e:
            logging.error(f"Connection error: {e}", exc_info=True)
            return False

    def _init_collection_sync(self):
        """Create and start CollectionSyncManager from current settings."""
        if not (self._romm_client and self._romm_client.authenticated):
            return

        selected_str = self._settings.get('Collections', 'actively_syncing', '')
        if not selected_str:
            selected_str = self._settings.get('Collections', 'selected_for_sync', '')
        auto_sync_enabled = self._settings.get('Collections', 'auto_sync_enabled', 'false') == 'true'

        if not (selected_str and auto_sync_enabled):
            return

        try:
            sync_interval = int(self._settings.get('Collections', 'sync_interval', '120'))
        except (ValueError, TypeError):
            sync_interval = 120

        selected_collections = {c for c in selected_str.split('|') if c}
        logging.info(f"Starting collection sync for: {selected_collections}")
        self._collection_sync = CollectionSyncManager(
            romm_client=self._romm_client,
            settings=self._settings,
            selected_collections=selected_collections,
            sync_interval=sync_interval,
            available_games=self._available_games,
            log_callback=lambda msg: logging.info(f"[COLLECTION-SYNC] {msg}"),
            steam_manager=self._steam_manager,
        )
        self._collection_sync.start()

        # Update BIOS tracking manager's collection_sync reference
        if self._bios_tracking:
            self._bios_tracking.collection_sync = self._collection_sync

    def _fetch_disabled_counts(self):
        """Fetch ROM counts for collections that are not currently being auto-synced.

        Called once in a background thread on connect so that get_service_status()
        can show "X / Y ROMs locally" for disabled collections even after a restart.
        Only fetches collections not already in _disabled_collection_counts (avoids
        redundant calls if this runs more than once due to reconnect).
        """
        try:
            actively_syncing_str = self._settings.get('Collections', 'actively_syncing', '')
            actively_syncing = {c for c in actively_syncing_str.split('|') if c}

            for collection in (self._romm_collections or []):
                name   = collection.get('name', '')
                col_id = collection.get('id')
                if not (name and col_id):
                    continue
                if name in actively_syncing:
                    continue  # enabled — count comes from CollectionSyncManager cache
                if name in self._disabled_collection_counts:
                    continue  # already populated (e.g. from a live toggle this session)
                roms = self._romm_client.get_collection_roms(col_id)
                rom_ids = {r.get('id') for r in roms if r.get('id')}
                from sync_core import CollectionSyncManager
                file_count = CollectionSyncManager._count_rom_files(roms)
                self._disabled_collection_counts[name] = {'rom_ids': rom_ids, 'total': file_count}
                logging.debug(f"Fetched disabled count for '{name}': {file_count} files ({len(roms)} ROMs)")
        except Exception as e:
            logging.error(f"_fetch_disabled_counts error: {e}", exc_info=True)

    def _retry_loop(self):
        """Connect on startup, then every 5 minutes refresh the collection list
        or retry the connection if disconnected. Uses updated_after for efficiency.

        On initial startup, retries every 15s until connected (handles DNS not
        ready after boot). Once connected, switches to 5-minute refresh interval.
        """
        connected = self._connect_to_romm()
        self._connection_attempted = True
        if connected:
            self._note_reachable()

        # If initial connection failed, retry quickly (DNS may not be ready yet)
        if not connected:
            for attempt in range(24):  # up to ~2 minutes of retries
                self._stop_event.wait(5)
                if self._stop_event.is_set():
                    break
                logging.info(f"Startup retry {attempt + 1}/24: attempting to connect...")
                if self._connect_to_romm():
                    logging.info("Startup retry succeeded")
                    break

        while not self._stop_event.is_set():
            self._stop_event.wait(300)  # sleep 5 minutes (or until _stop_sync wakes us)
            if self._stop_event.is_set():
                break
            try:
                if self._romm_client and self._romm_client.authenticated:
                    # Use updated_after for efficient collection refresh if we have a timestamp
                    updated_after = self._last_full_fetch_time
                    # This GET is also our reachability probe: RomMClient.authenticated
                    # is sticky (a network drop never flips it), so a raised exception
                    # here is how we learn the server went away, and a success after a
                    # failure is how we learn it came back.
                    new_collections = self._romm_client.get_collections(updated_after=updated_after)
                    self._note_reachable()

                    if updated_after and new_collections:
                        # Merge updated collections with existing ones
                        existing_map = {c['id']: c for c in (self._romm_collections or [])}
                        for col in new_collections:
                            existing_map[col['id']] = col
                        self._romm_collections = list(existing_map.values())
                        logging.debug(f"5-min poll: merged {len(new_collections)} updated collections")
                    elif new_collections or not updated_after:
                        # Full refresh or first fetch
                        self._romm_collections = new_collections
                        logging.debug(f"5-min poll: loaded {len(new_collections)} collections")
                else:
                    logging.info("Attempting to reconnect to RomM...")
                    if self._connect_to_romm():
                        logging.info("Reconnected successfully")
                        self._note_reachable()
            except Exception as e:
                # Treat any failure in the connected branch as a reachability loss
                # so the next success triggers an offline→online flush.
                self._online = False
                logging.error(f"Retry loop error: {e}", exc_info=True)

        logging.info("Retry loop exited")

    # -----------------------------------------------------------------------
    # Public callables
    # -----------------------------------------------------------------------

    async def get_service_status(self):
        """Build and return current sync status directly from live object state."""
        try:
            if not (self._retry_thread and self._retry_thread.is_alive()):
                return {
                    'status':           'stopped',
                    'message':          "Service stopped",
                    'details':          {},
                    'collections':      [],
                    'collection_count': 0,
                }

            # RomMClient.authenticated is sticky — a network drop never flips it —
            # so also consult the reachability latch (_online). When it has
            # explicitly latched False (the retry loop saw the server go away),
            # treat the session as not-connected so the offline-aware branch below
            # reports 'offline_cached' instead of a false 'online'. _online is None
            # until the first probe, which must NOT demote a healthy connection.
            connected = bool(self._romm_client and self._romm_client.authenticated
                             and self._online is not False)

            if not connected:
                # _connection_attempted becomes True once _connect_to_romm() finishes.
                # Before that we're still starting; after that we genuinely failed.
                # The frontend uses details.last_update to decide whether to show
                # the "not connected / retry" warning (same key as before).
                details = {'last_update': time.time()} if self._connection_attempted else {}
                # Tri-state for the offline-aware UI: if we have a hydrated/cached
                # library the user can still browse + launch downloaded games
                # ('offline_cached'); with nothing to show it's a true cold
                # 'disconnected'. Before the first attempt completes we're still
                # 'connecting'. 'connection' is additive — legacy keys unchanged.
                has_library = bool(self._available_games)
                if not self._connection_attempted:
                    conn_state = 'connecting'
                    msg = "Connecting to RomM..."
                elif has_library:
                    conn_state = 'offline_cached'
                    msg = "Offline — showing your saved library"
                else:
                    conn_state = 'disconnected'
                    msg = "Not connected to RomM"
                return {
                    'status':                  'running',
                    'connection':              conn_state,
                    'snapshot_fetched_at':     self._snapshot_fetched_at,
                    'pending_saves':           self._pending_save_count(),
                    'message':                 msg,
                    'details':                 details,
                    'collections':             [],
                    'collection_count':        0,
                    'actively_syncing_count':  0,
                }

            # Auto-enable RetroArch settings if disabled (Option B: always-on approach)
            if self._retroarch:
                try:
                    network_ok, _ = self._retroarch.check_network_commands_config()
                    if not network_ok:
                        self._retroarch.enable_retroarch_setting('network_commands')
                        logging.info("Auto-enabled network commands")

                    thumbnail_ok, _ = self._retroarch.check_savestate_thumbnail_config()
                    if not thumbnail_ok:
                        self._retroarch.enable_retroarch_setting('savestate_thumbnails')
                        logging.info("Auto-enabled save state thumbnails")
                except Exception as e:
                    logging.debug(f"Auto-enable settings error: {e}")

            # Build status directly from live in-memory objects — zero API calls,
            # always up-to-date, no race condition with a background thread.
            status = build_sync_status(
                romm_client=self._romm_client,
                collection_sync=self._collection_sync,
                auto_sync=self._auto_sync,
                available_games=self._available_games or [],
                known_collections=self._romm_collections,
                disabled_collection_counts=self._disabled_collection_counts,
                retroarch=self._retroarch,
                bios_tracking=self._bios_tracking,
                steam_manager=self._steam_manager,
            )

            game_count             = status.get('game_count', 0)
            collections            = status.get('collections', [])
            collection_count       = status.get('collection_count', 0)
            actively_syncing_count = status.get('actively_syncing_count', 0)
            bios_status            = status.get('bios_status', {})

            syncing_set = self._syncing_steam_collections or set()
            for col in collections:
                col['is_syncing_steam'] = col.get('name') in syncing_set

            # Show "Fetching games..." until initial fetch completes
            if self._last_full_fetch_time is None:
                message = "Fetching games..."
            else:
                message = f"{game_count} games, {collection_count} collections"

            return {
                'status':                  'connected',
                'connection':              'online',
                'snapshot_fetched_at':     self._snapshot_fetched_at,
                'pending_saves':           self._pending_save_count(),
                'message':                 message,
                'details':                 status,
                'collections':             collections,
                'collection_count':        collection_count,
                'actively_syncing_count':  actively_syncing_count,
                'bios_status':             bios_status,
                'steam_available':         status.get('steam_available', False),
            }

        except Exception as e:
            logging.error(f"Status check error: {e}", exc_info=True)
            return {
                'status':           'error',
                'message':          f"Error: {str(e)[:50]}",
                'details':          {},
                'collections':      [],
                'collection_count': 0,
            }

    async def drain_notifications(self):
        """Return and clear queued notification events from the sync engine.

        The frontend polls this on its status tick and toasts each event
        verbatim. Events are produced at the exact moment a sync/removal
        happens (see CollectionSyncManager.push_notification), so there's no
        frontend-side diffing or transition inference.
        """
        try:
            if self._collection_sync:
                return {'events': self._collection_sync.drain_notifications()}
        except Exception as e:
            logging.debug(f"drain_notifications error: {e}")
        return {'events': []}

    async def refresh_from_romm(self, force_full_refresh: bool = False):
        """Refresh data from RomM server (collections and games).

        Uses updated_after parameter for efficient incremental updates unless
        force_full_refresh is True.

        Args:
            force_full_refresh: If True, fetch all data regardless of timestamps

        Returns:
            dict with status and updated game/collection info
        """
        try:
            if not (self._romm_client and self._romm_client.authenticated):
                return {
                    'success': False,
                    'message': 'Not connected to RomM',
                    'status': await self.get_service_status()
                }

            # Get current timestamp in ISO 8601 format with timezone
            current_time = datetime.now(timezone.utc).isoformat()

            # Determine whether to do incremental or full refresh
            use_incremental = (
                not force_full_refresh and
                self._last_full_fetch_time is not None
            )

            updated_after = self._last_full_fetch_time if use_incremental else None

            logging.info(f"Refreshing from RomM (incremental={use_incremental}, "
                        f"updated_after={updated_after})")

            # Fetch collections (with updated_after if available)
            new_collections = self._romm_client.get_collections(updated_after=updated_after)

            if use_incremental and new_collections:
                # Merge new collections with existing ones
                existing_map = {c['id']: c for c in (self._romm_collections or [])}
                for col in new_collections:
                    existing_map[col['id']] = col
                self._romm_collections = list(existing_map.values())
                logging.info(f"Incremental: merged {len(new_collections)} updated collections")
            elif new_collections or not use_incremental:
                # Full refresh or first fetch
                self._romm_collections = new_collections
                logging.info(f"Full refresh: loaded {len(new_collections)} collections")

            # Fetch ROMs
            if use_incremental:
                # Incremental fetch - only get updated ROMs
                new_roms_data = self._romm_client.get_roms(
                    limit=10000,  # High limit for incremental updates
                    offset=0,
                    updated_after=updated_after
                )

                if new_roms_data and len(new_roms_data) == 2:
                    new_roms, _ = new_roms_data

                    if new_roms:
                        # Update existing games list
                        download_dir = Path(self._settings.get('Download', 'rom_directory',
                                                                '~/RomMSync/roms')).expanduser()

                        # Create a map for fast lookup by rom_id
                        existing_games_map = {g['rom_id']: g for g in self._available_games if 'rom_id' in g}

                        for rom in new_roms:
                            rom_id = rom.get('id')
                            platform_slug = rom.get('platform_slug', 'Unknown')
                            file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
                            local_path = download_dir / platform_slug / file_name
                            is_downloaded = is_path_validly_downloaded(local_path)
                            local_size = 0

                            if is_downloaded and local_path.exists():
                                if local_path.is_dir():
                                    local_size = sum(f.stat().st_size
                                                   for f in local_path.rglob('*') if f.is_file())
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
                                'cover_path': rom.get('path_cover_small'),
                                'cover_path_large': rom.get('path_cover_large'),
                                'romm_data': {
                                    'fs_name': rom.get('fs_name'),
                                    'fs_name_no_ext': rom.get('fs_name_no_ext'),
                                    'fs_size_bytes': rom.get('fs_size_bytes', 0),
                                    'platform_id': rom.get('platform_id'),
                                    'platform_slug': rom.get('platform_slug'),
                                    'files': rom.get('files', []),
                                },
                            }

                            # Update or add the game
                            existing_games_map[rom_id] = game_data

                        self._available_games = list(existing_games_map.values())
                        logging.info(f"Incremental: processed {len(new_roms)} updated ROMs, "
                                   f"total games: {len(self._available_games)}")
                    else:
                        logging.info("Incremental: no new/updated ROMs found")
            else:
                # Full refresh - fetch all games
                roms_result = self._romm_client.get_roms()
                if roms_result and len(roms_result) == 2:
                    raw_games, _ = roms_result
                    self._available_games.clear()
                    download_dir = Path(self._settings.get('Download', 'rom_directory',
                                                            '~/RomMSync/roms')).expanduser()

                    for rom in raw_games:
                        platform_slug = rom.get('platform_slug', 'Unknown')
                        file_name = rom.get('fs_name') or f"{rom.get('name', 'unknown')}.rom"
                        local_path = download_dir / platform_slug / file_name
                        is_downloaded = is_path_validly_downloaded(local_path)
                        local_size = 0

                        if is_downloaded and local_path.exists():
                            if local_path.is_dir():
                                local_size = sum(f.stat().st_size
                                               for f in local_path.rglob('*') if f.is_file())
                            else:
                                local_size = local_path.stat().st_size

                        is_md = _detect_multi_disc(local_path, is_downloaded)

                        self._available_games.append({
                            'name':            Path(file_name).stem if file_name else rom.get('name', 'Unknown'),
                            'rom_id':          rom.get('id'),
                            'platform':        rom.get('platform_name', 'Unknown'),
                            'platform_slug':   platform_slug,
                            'file_name':       file_name,
                            'is_downloaded':   is_downloaded,
                            'is_multi_disc':   is_md[0],
                            'disc_count':      is_md[1],
                            'local_path':      str(local_path) if is_downloaded else None,
                            'local_size':      local_size,
                            'cover_path': rom.get('path_cover_small'),
                            'cover_path_large': rom.get('path_cover_large'),
                            '_sibling_files':  rom.get('_sibling_files', []),
                            'sibling_roms':    rom.get('sibling_roms', []),
                            'romm_data': {
                                'fs_name': rom.get('fs_name'),
                                'fs_name_no_ext': rom.get('fs_name_no_ext'),
                                'fs_size_bytes': rom.get('fs_size_bytes', 0),
                                'platform_id': rom.get('platform_id'),
                                'platform_slug': rom.get('platform_slug'),
                                'files': rom.get('files', []),
                            },
                        })
                    logging.info(f"Full refresh: loaded {len(self._available_games)} games")

            self._last_full_fetch_time = current_time
            self._snapshot_fetched_at = current_time
            # Write-through the freshly merged library so a later cold start /
            # offline session sees this data.
            self._persist_snapshot()

            # Get updated status
            status = await self.get_service_status()

            return {
                'success': True,
                'message': f"Refreshed: {status.get('message', '')}",
                'incremental': use_incremental,
                'status': status
            }

        except Exception as e:
            logging.error(f"refresh_from_romm error: {e}", exc_info=True)
            return {
                'success': False,
                'message': f'Refresh failed: {str(e)[:100]}',
                'status': await self.get_service_status()
            }

    async def toggle_collection_sync(self, collection_name: str, enabled: bool):
        """Enable or disable auto-sync for a specific collection."""
        try:
            import configparser
            ini_path = Path.home() / '.config' / 'romm-retroarch-sync' / 'settings.ini'
            if not ini_path.exists():
                logging.error("Settings file not found")
                return False

            config = configparser.ConfigParser()
            config.read(ini_path)
            if not config.has_section('Collections'):
                config.add_section('Collections')

            actively_syncing = config.get('Collections', 'actively_syncing', fallback='')
            sync_set = {c for c in actively_syncing.split('|') if c}

            if enabled:
                sync_set.add(collection_name)
                logging.info(f"Enabling auto-sync for: {collection_name}")
                # Clear any stale disabled-count so build_sync_status uses live cache
                self._disabled_collection_counts.pop(collection_name, None)

                # Trigger BIOS downloads for this collection's platforms
                if self._bios_tracking:
                    self._bios_tracking.download_for_collection(collection_name)
            else:
                sync_set.discard(collection_name)
                logging.info(f"Disabling auto-sync for: {collection_name}")
                # Snapshot rom_ids from cache so build_sync_status can compute
                # downloaded dynamically (stays accurate as _available_games updates)
                if self._collection_sync:
                    rom_ids = self._collection_sync.collection_caches.get(collection_name, set())
                    file_counts = getattr(self._collection_sync, 'collection_file_counts', {})
                    self._disabled_collection_counts[collection_name] = {
                        'rom_ids': set(rom_ids),
                        'total':   file_counts.get(collection_name, len(rom_ids)),
                    }

            config.set('Collections', 'actively_syncing',  '|'.join(sorted(sync_set)))
            config.set('Collections', 'selected_for_sync', '|'.join(sorted(sync_set)))
            config.set('Collections', 'auto_sync_enabled', 'true' if sync_set else 'false')

            with open(ini_path, 'w') as f:
                config.write(f)

            # Update in-memory settings so the heartbeat sees the change immediately
            if self._settings:
                self._settings.load_settings()

            # When disabling collection sync, also disable Steam sync if it was active
            if not enabled and self._steam_manager and self._steam_manager.is_available():
                steam_collections = self._steam_manager.get_steam_sync_collections()
                if collection_name in steam_collections:
                    import asyncio
                    try:
                        asyncio.create_task(
                            self.toggle_collection_steam_sync(collection_name, False)
                        )
                        logging.info(f"Steam sync disabled for: {collection_name} (collection sync turned off)")
                    except Exception as e:
                        logging.warning(f"Could not disable Steam sync: {e}")

            # Update collection sync directly — no trigger file needed
            if self._collection_sync:
                if sync_set:
                    self._collection_sync.update_collections(sync_set)
                else:
                    # Detach immediately so get_service_status() sees no active sync,
                    # then stop the worker thread in the background (avoids blocking
                    # on join while check_for_changes() finishes its API call).
                    old_sync = self._collection_sync
                    self._collection_sync = None
                    # Update BIOS tracking manager's collection_sync reference
                    if self._bios_tracking:
                        self._bios_tracking.collection_sync = None
                    threading.Thread(
                        target=old_sync.stop,
                        daemon=True,
                        name="romm-collection-stop",
                    ).start()
            elif enabled and self._romm_client and self._romm_client.authenticated:
                # First collection enabled — create CollectionSyncManager now
                threading.Thread(target=self._init_collection_sync,
                                 daemon=True, name="romm-collection-init").start()

            # No status patching needed — get_service_status() builds status
            # on-demand from live objects, so the next frontend poll is always fresh.
            return True

        except Exception as e:
            logging.error(f"toggle_collection_sync error: {e}", exc_info=True)
            return False

    async def delete_collection_roms(self, collection_name: str, mode: str = 'collection'):
        """Delete all local ROM files for a group (collection or platform).

        Local-only: removes downloaded files under the download dir. It never
        touches the RomM server. toggle_collection_sync already handled
        settings + sync object updates before this is called (collections
        only), so this method only does the file deletion. Uses the existing
        authenticated client and in-memory caches to avoid redundant API calls.
        """
        try:
            import shutil
            logging.info(f"Starting ROM deletion for {mode}: {collection_name}")

            download_dir = Path(self._settings.get('Download', 'rom_directory',
                                                    '~/RomMSync/roms')).expanduser()
            if not download_dir.exists():
                logging.info(f"Download directory not found, nothing to delete: {download_dir}")
                return True

            # Platform groups aren't tracked by CollectionSyncManager — delete
            # straight from the in-memory game list (keyed by resolved platform
            # name, matching get_library_games).
            if mode == 'platform':
                deleted_count = 0
                for game in (self._available_games or []):
                    if self._platform_name_for(game) != collection_name:
                        continue
                    if not game.get('is_downloaded') or not game.get('local_path'):
                        continue
                    rom_path = Path(game['local_path'])
                    if rom_path.exists():
                        try:
                            if rom_path.is_file():
                                rom_path.unlink()
                            else:
                                shutil.rmtree(rom_path)
                            deleted_count += 1
                            logging.info(f"  Deleted: {rom_path}")
                        except Exception as e:
                            logging.error(f"  Failed to delete {rom_path}: {e}")
                    game['is_downloaded'] = False
                    game['local_path'] = None
                logging.info(f"Deleted {deleted_count} ROM(s) from platform '{collection_name}'")
                return True

            # Use the already-authenticated client — no new login needed
            client = self._romm_client
            if not client or not client.authenticated:
                logging.error("RomM client not available for ROM deletion")
                return False

            # Get collection ID from cached collection list
            collection_id = None
            for col in (self._romm_collections or []):
                if col.get('name') == collection_name:
                    collection_id = col.get('id')
                    break

            if collection_id is None:
                logging.error(f"Collection '{collection_name}' not found in status")
                return False

            # Fetch ROM list for this collection (need fs_name / platform_slug for paths)
            collection_roms = client.get_collection_roms(collection_id)
            logging.info(f"Fetched {len(collection_roms)} ROMs for '{collection_name}'")

            # Protect ROMs shared with other still-synced collections.
            # Use collection_caches (in-memory) — no extra API calls needed.
            protected_rom_ids: set = set()
            if self._collection_sync:
                for other_name, rom_ids in self._collection_sync.collection_caches.items():
                    if other_name != collection_name:
                        protected_rom_ids.update(rom_ids)
                        logging.info(f"  Protecting {len(rom_ids)} ROM(s) from '{other_name}'")

            deleted_count = 0
            skipped_count = 0
            deleted_rom_ids: set = set()
            for rom in collection_roms:
                rom_id        = rom.get('id')
                platform_slug = rom.get('platform_slug', '')
                file_name     = rom.get('fs_name') or rom.get('file_name', '')
                if not (platform_slug and file_name):
                    continue
                if rom_id and rom_id in protected_rom_ids:
                    skipped_count += 1
                    continue
                rom_path = download_dir / platform_slug / file_name
                if rom_path.exists():
                    try:
                        if rom_path.is_file():
                            rom_path.unlink()
                        else:
                            shutil.rmtree(rom_path)
                        deleted_count += 1
                        if rom_id:
                            deleted_rom_ids.add(rom_id)
                        logging.info(f"  Deleted: {rom_path}")
                    except Exception as e:
                        logging.error(f"  Failed to delete {rom_path}: {e}")

            # Mark deleted ROMs as not downloaded in available_games so the
            # dynamic count in build_sync_status drops to 0 immediately.
            # We keep the _disabled_collection_counts entry so the UI shows
            # "0 / N ROMs locally" rather than "Auto-sync disabled".
            if deleted_rom_ids and self._available_games:
                for game in self._available_games:
                    if game.get('rom_id') in deleted_rom_ids:
                        game['is_downloaded'] = False
                        game['local_path']    = None

            logging.info(f"Deleted {deleted_count} ROM(s) from '{collection_name}' "
                         f"({skipped_count} skipped, shared with other collections)")
            return True

        except Exception as e:
            logging.error(f"delete_collection_roms error: {e}", exc_info=True)
            return False

    async def get_config(self):
        """Get current RomM configuration (never returns the raw password)."""
        try:
            if not SYNC_CORE_AVAILABLE:
                return {'configured': False, 'error': 'sync_core not available'}
            settings = SettingsManager()
            url      = settings.get('RomM', 'url')
            username = settings.get('RomM', 'username')
            has_password = bool(settings.get('RomM', 'password'))
            has_token = bool(settings.get('RomM', 'client_token'))  # set by pair_device

            rom_directory  = settings.get('Download', 'rom_directory')
            save_directory = settings.get('Download', 'save_directory')
            bios_directory = settings.get('BIOS', 'custom_path', '')

            _default_rom  = str(Path.home() / 'RomMSync' / 'roms')
            _default_save = str(Path.home() / 'RomMSync' / 'saves')
            retrodeck  = detect_retrodeck()
            needs_save = False
            if retrodeck:
                if not rom_directory or rom_directory == _default_rom:
                    rom_directory = retrodeck['rom_directory']
                    needs_save    = True
                if not save_directory or save_directory == _default_save:
                    save_directory = retrodeck['save_directory']
                    needs_save     = True
                if not bios_directory:
                    bios_directory = str(Path.home() / 'retrodeck' / 'bios')
                    settings.set('BIOS', 'custom_path', bios_directory)
                    needs_save = True
            if needs_save:
                settings.set('Download', 'rom_directory',  rom_directory)
                settings.set('Download', 'save_directory', save_directory)
                logging.info(f"Auto-configured RetroDECK paths: ROMs={rom_directory}, "
                             f"saves={save_directory}, BIOS={bios_directory}")

            import socket
            try:
                hostname = socket.gethostname() or 'SteamOS'
            except Exception:
                hostname = 'SteamOS'

            ds = load_decky_settings()
            needs_onboarding = ds.get('needs_onboarding', False)

            # Self-heal: once we have working credentials (password auth or a
            # paired token), onboarding is complete. Clear a stale flag so the
            # setup wizard can never trap the user on the "Get Started" panel.
            has_creds = bool(url and ((username and has_password) or has_token))
            if has_creds and needs_onboarding:
                ds.pop('needs_onboarding', None)
                save_decky_settings(ds)
                needs_onboarding = False

            return {
                'url':                url,
                'username':           username,
                'has_password':       has_password,
                'rom_directory':      rom_directory,
                'save_directory':     save_directory,
                'bios_directory':     bios_directory,
                'device_name':        settings.get('Device', 'device_name'),
                'device_name_default': hostname,
                # Configured if we have either password auth OR a paired client
                # token — pairing stores only a token (no username/password), so
                # requiring a password would wrongly keep the setup wizard
                # re-opening after a successful pair.
                'configured':         bool(url and ((username and has_password) or has_token)) and not needs_onboarding,
                'retrodeck_detected': retrodeck is not None,
            }
        except Exception as e:
            logging.error(f"get_config error: {e}", exc_info=True)
            return {'configured': False, 'error': str(e)}

    async def save_config(self, url: str, username: str, password: str,
                          rom_directory: str, save_directory: str, device_name: str,
                          bios_directory: str = ''):
        """Save RomM configuration and restart sync to pick up new settings."""
        try:
            if not SYNC_CORE_AVAILABLE:
                return {'success': False, 'error': 'sync_core not available'}
            settings = SettingsManager()
            settings.set('RomM', 'url',      url.strip().rstrip('/'))
            settings.set('RomM', 'username', username.strip())
            if password:
                settings.set('RomM', 'password', password)
            settings.set('RomM', 'remember_credentials', 'true')
            settings.set('RomM', 'auto_connect',         'true')
            if rom_directory:
                settings.set('Download', 'rom_directory',  rom_directory.strip())
            if save_directory:
                settings.set('Download', 'save_directory', save_directory.strip())
            if device_name:
                settings.set('Device', 'device_name', device_name.strip())
            settings.set('BIOS', 'custom_path', bios_directory.strip() if bios_directory else '')

            ds = load_decky_settings()
            ds.pop('needs_onboarding', None)
            save_decky_settings(ds)

            self._stop_sync()
            time.sleep(0.5)
            self._start_sync()
            return {'success': True}
        except Exception as e:
            logging.error(f"save_config error: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def test_connection(self, url: str, username: str, password: str):
        """Test connection to RomM with the given credentials."""
        try:
            if not SYNC_CORE_AVAILABLE:
                return {'success': False, 'message': 'sync_core not available'}

            actual_password = password
            if not actual_password:
                settings        = SettingsManager()
                actual_password = settings.get('RomM', 'password')
            if not actual_password:
                return {'success': False, 'message': 'Password is required to test the connection.'}

            client = RomMClient(url.strip().rstrip('/'), username.strip(), actual_password)
            if client.authenticated:
                collections = client.get_collections()
                return {
                    'success':     True,
                    'message':     f'Connected! Found {len(collections)} collection(s).',
                    'collections': [{'id': c.get('id'), 'name': c.get('name', '')}
                                    for c in collections],
                }
            else:
                return {'success': False,
                        'message': 'Authentication failed — check URL, username and password.'}
        except Exception as e:
            logging.error(f"test_connection error: {e}", exc_info=True)
            return {'success': False, 'message': f'Connection error: {str(e)[:150]}'}

    async def get_account_username(self):
        """Return the human-readable RomM account name for the connected user,
        fetched live from /api/users/me. Used by the in-app Settings page so it
        never displays a stored credential/token. Empty string when offline."""
        try:
            client = self._romm_client
            if client is None or not getattr(client, 'authenticated', False):
                return {'username': ''}
            user = client.get_current_user() or {}
            name = user.get('username') or user.get('display_name') or ''
            return {
                'username': name,
                'role': user.get('role') or '',
                'avatar_path': user.get('avatar_path') or '',
                'updated_at': user.get('updated_at') or '',
            }
        except Exception as e:
            logging.error(f"get_account_username error: {e}")
            return {'username': ''}

    async def get_plugin_stats(self):
        """Plugin-local stats for the user-menu Stats page. Computed live from
        in-memory state (no API calls): library size, what's downloaded on this
        device, disk usage, platforms, and collection sync state."""
        try:
            games = self._available_games or []
            downloaded = [g for g in games if g.get('is_downloaded')]
            size_on_disk = sum(int(g.get('local_size') or 0) for g in downloaded)
            platforms = {g.get('platform_slug') for g in games if g.get('platform_slug')}

            # Per-platform breakdown for the Platforms section. rom_count is the
            # library total for the platform; fs_size_bytes is the on-disk size of
            # its downloaded games, so the bars/percentages reflect disk usage
            # (consistent with the Size-on-disk summary total).
            plat_map = {}
            for g in games:
                slug = (g.get('platform_slug')
                        or (g.get('romm_data') or {}).get('platform_slug')
                        or 'unknown')
                p = plat_map.setdefault(slug, {
                    'slug': slug,
                    'fs_slug': slug,
                    'name': self._platform_name_for(g),
                    'rom_count': 0,
                    'downloaded': 0,
                    'fs_size_bytes': 0,
                })
                p['rom_count'] += 1
                if g.get('is_downloaded'):
                    p['downloaded'] += 1
                    p['fs_size_bytes'] += int(g.get('local_size') or 0)
            platforms_breakdown = sorted(
                plat_map.values(), key=lambda p: p['fs_size_bytes'], reverse=True)

            collections_total = 0
            collections_synced = 0
            try:
                if self._romm_client and self._romm_client.authenticated:
                    status = build_sync_status(
                        romm_client=self._romm_client,
                        collection_sync=self._collection_sync,
                        auto_sync=self._auto_sync,
                        available_games=games,
                        known_collections=self._romm_collections,
                        disabled_collection_counts=self._disabled_collection_counts,
                        retroarch=self._retroarch,
                        bios_tracking=self._bios_tracking,
                        steam_manager=self._steam_manager,
                    )
                    cols = status.get('collections', [])
                    collections_total = len(cols)
                    collections_synced = sum(
                        1 for c in cols
                        if c.get('auto_sync') or c.get('sync_state') == 'synced'
                    )
            except Exception as e:
                logging.debug(f"get_plugin_stats collections error: {e}")

            return {
                'games_total':         len(games),
                'games_downloaded':    len(downloaded),
                'size_on_disk':        size_on_disk,
                'platforms':           len(platforms),
                'collections_total':   collections_total,
                'collections_synced':  collections_synced,
                'platforms_breakdown': platforms_breakdown,
            }
        except Exception as e:
            logging.error(f"get_plugin_stats error: {e}", exc_info=True)
            return {
                'games_total': 0, 'games_downloaded': 0, 'size_on_disk': 0,
                'platforms': 0, 'collections_total': 0, 'collections_synced': 0,
                'platforms_breakdown': [],
            }

    async def get_logging_enabled(self):
        try:
            return load_decky_settings().get('logging_enabled', True)
        except Exception as e:
            logging.error(f"get_logging_enabled error: {e}")
            return True

    async def get_retrodeck_button_enabled(self):
        """Whether the Game Browser shows the 'Launch RetroDECK' header button.
        Defaults off; only meaningful when RetroDECK is actually installed."""
        try:
            return load_decky_settings().get('retrodeck_button_enabled', False)
        except Exception as e:
            logging.error(f"get_retrodeck_button_enabled error: {e}")
            return False

    async def set_retrodeck_button_enabled(self, enabled: bool):
        try:
            settings = load_decky_settings()
            settings['retrodeck_button_enabled'] = bool(enabled)
            return save_decky_settings(settings)
        except Exception as e:
            logging.error(f"set_retrodeck_button_enabled error: {e}")
            return False

    async def get_core_mappings(self):
        """For the core-mapping settings page: one row per platform in the
        user's library, showing how the launch core resolves and the options
        available to override it.

        Returns {success, available_cores: [...], mappings: [
          {slug, platform_name, resolved_core, source, override,
           retrodeck_default, retrodeck_choices: [...]}
        ]}.
        """
        try:
            ra = self._retroarch
            if not ra:
                return {'success': False, 'mappings': [], 'available_cores': [],
                        'message': 'RetroArch interface unavailable'}
            available = sorted(ra.get_available_cores().keys())
            # Unique platforms (slug + label) that have at least one DOWNLOADED
            # game — the core choice only matters for games you can actually
            # launch locally, so platforms with nothing downloaded are hidden.
            seen, rows = set(), []
            for g in (self._available_games or []):
                if not g.get('is_downloaded'):
                    continue
                slug = g.get('platform_slug') or (g.get('romm_data') or {}).get('platform_slug')
                if not slug or slug in seen:
                    continue
                seen.add(slug)
                label = self._platform_name_for(g)
                info = ra.describe_core_resolution(label, system_slug=slug)
                info.update({'slug': slug, 'platform_name': label})
                rows.append(info)
            rows.sort(key=lambda r: (r['platform_name'] or '').lower())
            return {'success': True, 'available_cores': available, 'mappings': rows}
        except Exception as e:
            logging.error(f"get_core_mappings error: {e}", exc_info=True)
            return {'success': False, 'mappings': [], 'available_cores': [], 'message': str(e)}

    async def set_core_override(self, slug: str, core: str = ''):
        """Pin (core non-empty) or clear (core empty) the launch core for a
        platform slug. Returns the refreshed resolution for that row."""
        try:
            ra = self._retroarch
            if not ra:
                return {'success': False, 'message': 'RetroArch interface unavailable'}
            ra.set_core_override(slug, core or '')
            # Recompute so the UI reflects the new source/resolved core.
            label = slug
            for g in (self._available_games or []):
                if (g.get('platform_slug') or (g.get('romm_data') or {}).get('platform_slug')) == slug:
                    label = self._platform_name_for(g)
                    break
            info = ra.describe_core_resolution(label, system_slug=slug)
            info.update({'slug': slug, 'platform_name': label})
            return {'success': True, 'mapping': info}
        except Exception as e:
            logging.error(f"set_core_override error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    async def reset_all_settings(self):
        """Delete all downloaded ROMs from ALL collections, delete downloaded
        BIOS files, and reset sync state.  Credentials are preserved."""
        import configparser, shutil
        config_dir   = Path.home() / '.config' / 'romm-retroarch-sync'
        ini_path     = config_dir / 'settings.ini'

        try:
            # Grab BIOS system_dir before stopping sync (needs retroarch ref)
            bios_system_dir = None
            if self._retroarch and hasattr(self._retroarch, 'bios_manager') and self._retroarch.bios_manager:
                bios_system_dir = self._retroarch.bios_manager.system_dir

            self._stop_sync()
            logging.info("Reset: sync stopped")

            config = configparser.ConfigParser()
            config.read(ini_path)

            download_dir = Path(config.get('Download', 'rom_directory',
                                           fallback='~/RomMSync/roms')).expanduser()

            # Delete ROMs from ALL collections (not just actively syncing)
            deleted_roms = 0
            if SYNC_CORE_AVAILABLE and download_dir.exists():
                romm_url = config.get('RomM', 'url',      fallback='')
                username = config.get('RomM', 'username', fallback='')
                password = config.get('RomM', 'password', fallback='')
                if all([romm_url, username, password]):
                    try:
                        client = RomMClient(romm_url, username, password)
                        if client.authenticated:
                            all_collections = client.get_collections()
                            for col in all_collections:
                                col_id   = col.get('id')
                                col_name = col.get('name', '')
                                if col_id is None:
                                    continue
                                for rom in client.get_collection_roms(col_id):
                                    platform_slug = rom.get('platform_slug', '')
                                    file_name     = rom.get('fs_name') or rom.get('file_name', '')
                                    if not (platform_slug and file_name):
                                        continue
                                    rom_path = download_dir / platform_slug / file_name
                                    if rom_path.exists():
                                        try:
                                            if rom_path.is_file():
                                                rom_path.unlink()
                                            else:
                                                shutil.rmtree(rom_path)
                                            deleted_roms += 1
                                        except Exception as e:
                                            logging.error(f"Reset: failed to delete {rom_path}: {e}")
                                logging.info(f"Reset: deleted ROMs from '{col_name}'")
                        else:
                            logging.warning("Reset: could not authenticate — ROM files not deleted")
                    except Exception as e:
                        logging.error(f"Reset: ROM deletion error: {e}", exc_info=True)

            # Delete downloaded BIOS files from the system directory
            deleted_bios = 0
            if bios_system_dir and bios_system_dir.exists():
                try:
                    from bios_manager import BIOS_DATABASE
                    known_bios_files = set()
                    for platform_info in BIOS_DATABASE.values():
                        for bios_entry in platform_info.get('bios_files', []):
                            fname = bios_entry.get('file')
                            if fname:
                                known_bios_files.add(fname)

                    for bios_file in known_bios_files:
                        bios_path = bios_system_dir / bios_file
                        if bios_path.exists():
                            try:
                                bios_path.unlink()
                                deleted_bios += 1
                                logging.info(f"Reset: deleted BIOS file {bios_file}")
                            except Exception as e:
                                logging.error(f"Reset: failed to delete BIOS {bios_file}: {e}")

                    logging.info(f"Reset: deleted {deleted_bios} BIOS file(s)")
                except ImportError:
                    logging.warning("Reset: bios_manager module not available, skipping BIOS deletion")
                except Exception as e:
                    logging.error(f"Reset: BIOS deletion error: {e}", exc_info=True)

            # Clear all collection settings (disable all sync collections)
            if config.has_section('Collections'):
                config.set('Collections', 'actively_syncing',  '')
                config.set('Collections', 'selected_for_sync', '')
                config.set('Collections', 'auto_sync_enabled', 'false')
                with open(ini_path, 'w') as f:
                    config.write(f)

            self._romm_collections = None
            self._romm_virtual_collections = None

            cache_dir = config_dir / 'cache'
            if cache_dir.exists():
                shutil.rmtree(cache_dir)

            ds = load_decky_settings()
            ds['needs_onboarding'] = True
            save_decky_settings(ds)

            logging.info(f"Reset complete: {deleted_roms} ROM(s), {deleted_bios} BIOS file(s) deleted")
            return {'success': True, 'deleted_roms': deleted_roms, 'deleted_bios': deleted_bios}

        except Exception as e:
            logging.error(f"reset_all_settings error: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    async def logout(self, wipe_data: bool = False):
        """Log out of RomM. Always clears stored credentials and stops sync so
        get_config() reports unconfigured and the setup wizard takes over.

        When wipe_data is True, also deletes downloaded ROMs/BIOS and clears
        sync state first (the old "reset to new user" behaviour). When False,
        downloaded files are kept so logging back in is non-destructive.
        """
        try:
            if not SYNC_CORE_AVAILABLE:
                return {'success': False, 'error': 'sync_core not available'}

            result = {'success': True, 'deleted_roms': 0, 'deleted_bios': 0}
            if wipe_data:
                # reset_all_settings deletes ROMs/BIOS, clears sync state, and
                # sets needs_onboarding; it also stops sync.
                result = await self.reset_all_settings()
            else:
                self._stop_sync()

            # Clear credentials so configured == False. Keep url/username so the
            # wizard can pre-fill the server for a quick log back in.
            settings = SettingsManager()
            settings.set('RomM', 'password', '')
            settings.set('RomM', 'client_token', '')
            settings.set('RomM', 'auto_connect', 'false')

            self._romm_client = None

            ds = load_decky_settings()
            ds['needs_onboarding'] = True
            save_decky_settings(ds)

            logging.info(f"Logged out of RomM (wipe_data={wipe_data})")
            result['success'] = True
            return result
        except Exception as e:
            logging.error(f"logout error: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

    def _ensure_device_registered(self):
        """Ensure this device is registered with RomM and device_id is stored.

        Save-sync (/negotiate) needs a device_id; without it the session sync
        can't run and battery saves never sync. Mirrors the GTK app's
        initialize_device(): reuse an existing valid registration, else register
        a new device. New devices default to sync_enabled on the server.
        """
        if not (self._romm_client and self._romm_client.authenticated and self._settings):
            return None
        try:
            existing = self._settings.get('Device', 'device_id', '')
            if existing and self._romm_client.get_device(existing):
                logging.info(f"[DEVICE] verified on server: {existing}")
                return existing

            import socket as _socket
            device_id = self._romm_client.register_device(
                device_name=self._settings.get('Device', 'device_name', _socket.gethostname()),
                platform=self._settings.get('Device', 'device_platform', 'SteamOS'),
                client=self._settings.get('Device', 'client', 'RomM-RetroArch-Sync-Decky'),
                client_version=self._settings.get('Device', 'client_version', '1.5'),
            )
            if device_id:
                self._settings.set('Device', 'device_id', device_id)
                logging.info(f"[DEVICE] registered: {device_id}")
                return device_id
            logging.warning("[DEVICE] registration failed; save-sync will be disabled")
            return None
        except Exception as e:
            logging.error(f"[DEVICE] registration error: {e}", exc_info=True)
            return None

    async def pair_device(self, url: str, code: str):
        """Pair with RomM using an 8-digit Client API Token code (no password).

        Exchanges the code for a token, stores it, and connects. This is RomM's
        recommended companion-app auth — far better UX on a Steam Deck than
        typing a username/password.
        """
        try:
            url = (url or self._settings.get('RomM', 'url', '')).strip().rstrip('/')
            if not url:
                return {'success': False, 'message': 'RomM URL is required'}
            if not code or not str(code).strip():
                return {'success': False, 'message': 'Pairing code is required'}

            token = RomMClient(url).exchange_pair_code(str(code).strip())
            if not token:
                return {'success': False, 'message': 'Invalid or expired pairing code'}

            self._settings.set('RomM', 'url', url)
            self._settings.set('RomM', 'client_token', token)
            self._settings.set('RomM', 'auto_connect', 'true')
            # Clear the onboarding flag so get_config() reports configured — pairing
            # is a complete setup path (save_config does the same for password auth).
            ds = load_decky_settings()
            ds.pop('needs_onboarding', None)
            save_decky_settings(ds)
            logging.info("Paired with RomM via Client API Token")

            connected = self._connect_to_romm()
            return {'success': bool(connected),
                    'message': 'Paired and connected' if connected
                    else 'Paired, but connection failed'}
        except Exception as e:
            logging.error(f"pair_device error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    async def delete_device(self):
        """Unregister the current device from the server and clear local device ID."""
        try:
            device_id = self._settings.get('Device', 'device_id', '') if self._settings else ''
            if not device_id:
                return {'success': False, 'message': 'No device registered'}

            if not self._romm_client or not self._romm_client.authenticated:
                return {'success': False, 'message': 'Not connected to RomM'}

            if self._romm_client.delete_device(device_id):
                self._settings.set('Device', 'device_id', '')
                logging.info(f"Device {device_id} unregistered")
                return {'success': True, 'message': f'Device {device_id} deleted'}
            else:
                return {'success': False, 'message': 'Server rejected device deletion'}

        except Exception as e:
            logging.error(f"delete_device error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    async def set_logging_enabled(self, enabled: bool):
        try:
            settings = load_decky_settings()
            settings['logging_enabled'] = enabled
            result = save_decky_settings(settings)

            if result:
                global _file_handler
                root = logging.getLogger()
                if enabled:
                    if _file_handler is None:
                        _file_handler = logging.FileHandler(str(log_file))
                        _file_handler.setLevel(logging.DEBUG)
                        _file_handler.setFormatter(
                            logging.Formatter('%(asctime)s - %(levelname)s - %(message)s'))
                        root.addHandler(_file_handler)
                    root.setLevel(logging.DEBUG)
                    logging.info("Logging enabled")
                else:
                    logging.info("Logging disabled")
                    if _file_handler is not None:
                        root.removeHandler(_file_handler)
                        _file_handler.close()
                        _file_handler = None

            return result
        except Exception as e:
            logging.error(f"set_logging_enabled error: {e}")
            return False

    async def enable_retroarch_setting(self, setting_type: str):
        """Enable a RetroArch setting (network_commands or savestate_thumbnails)."""
        try:
            if not SYNC_CORE_AVAILABLE:
                return {'success': False, 'message': 'sync_core not available'}

            if not self._retroarch:
                return {'success': False, 'message': 'RetroArch interface not initialized'}

            success, message = self._retroarch.enable_retroarch_setting(setting_type)
            logging.info(f"enable_retroarch_setting({setting_type}): {message}")
            return {'success': success, 'message': message}

        except Exception as e:
            logging.error(f"enable_retroarch_setting error: {e}", exc_info=True)
            return {'success': False, 'message': f'Error: {str(e)}'}

    # -----------------------------------------------------------------------
    # Save History (browse / restore server save & state versions)
    # -----------------------------------------------------------------------

    async def get_downloaded_games(self):
        """Return the list of locally-downloaded games (for the history picker)."""
        try:
            slug_map = self._platform_slug_to_name or {}

            def _platform(g):
                p = g.get('platform')
                if p and p != 'Unknown':
                    return p
                slug = g.get('platform_slug') or (g.get('romm_data') or {}).get('platform_slug')
                if slug:
                    return slug_map.get(slug) or slug
                return 'Unknown'

            games = [
                {
                    'rom_id':   g.get('rom_id'),
                    'name':     g.get('name'),
                    'platform': _platform(g),
                }
                for g in (self._available_games or [])
                if g.get('is_downloaded') and g.get('rom_id')
            ]
            games.sort(key=lambda g: (g.get('platform') or '', (g.get('name') or '').lower()))
            return {'success': True, 'games': games}
        except Exception as e:
            logging.error(f"get_downloaded_games error: {e}", exc_info=True)
            return {'success': False, 'games': [], 'message': str(e)}

    @staticmethod
    def _serialize_history_entry(entry, save_type):
        """Flatten a RomM save/state version into a frontend-friendly dict."""
        ds = entry.get('device_syncs') or entry.get('deviceSyncs')
        device = None
        if isinstance(ds, list) and ds and isinstance(ds[0], dict):
            device = ds[0].get('device_name') or ds[0].get('name')
        return {
            'id':            entry.get('id'),
            'slot':          entry.get('slot'),
            'save_type':     save_type,
            'file_name':     entry.get('file_name', ''),
            'updated_at':    entry.get('updated_at') or entry.get('created_at'),
            'size_bytes':    entry.get('size_bytes') or entry.get('file_size_bytes'),
            'device':        device,
            'has_screenshot': bool(entry.get('screenshot')),
        }

    async def get_save_history(self, rom_id: int):
        """Return all server save/state versions for a ROM (newest first)."""
        try:
            if not (self._romm_client and self._romm_client.authenticated):
                return {'success': False, 'message': 'Not connected to RomM',
                        'saves': [], 'states': []}
            saves, states = self._romm_client.get_save_history(rom_id)

            def _key(e):
                return e.get('updated_at') or e.get('created_at') or ''
            saves = sorted(saves, key=_key, reverse=True)
            states = sorted(states, key=_key, reverse=True)
            return {
                'success': True,
                'saves':  [self._serialize_history_entry(e, 'saves') for e in saves],
                'states': [self._serialize_history_entry(e, 'states') for e in states],
            }
        except Exception as e:
            logging.error(f"get_save_history error: {e}", exc_info=True)
            return {'success': False, 'message': str(e), 'saves': [], 'states': []}

    async def get_save_screenshot(self, rom_id: int, save_id: int, save_type: str):
        """Return a base64 data URI for a state's screenshot, or None.

        The frontend <img> cannot authenticate to RomM, so the backend (which
        holds the session) fetches the bytes and inlines them. Re-fetches the
        ROM history to obtain the entry's full screenshot metadata (the list
        endpoint may not embed download_path), falling back to the per-entry
        detail endpoint inside fetch_screenshot_bytes.
        """
        try:
            if not (self._romm_client and self._romm_client.authenticated):
                return {'success': False, 'data_uri': None}
            saves, states = self._romm_client.get_save_history(rom_id)
            pool = states if save_type == 'states' else saves
            entry = next((e for e in pool if e.get('id') == save_id), {'id': save_id})
            sd = entry.get('screenshot')
            logging.info(f"get_save_screenshot rom={rom_id} {save_type} id={save_id} "
                         f"screenshot_meta={'yes' if sd else 'no'}")
            data = self._romm_client.fetch_screenshot_bytes(entry, save_type)
            if not data:
                logging.info(f"get_save_screenshot id={save_id}: no image bytes returned")
                return {'success': True, 'data_uri': None}
            import base64
            b64 = base64.b64encode(data).decode('ascii')
            return {'success': True, 'data_uri': f'data:image/png;base64,{b64}'}
        except Exception as e:
            logging.error(f"get_save_screenshot error: {e}", exc_info=True)
            return {'success': False, 'data_uri': None, 'message': str(e)}

    async def restore_save_version(self, rom_id: int, save_id: int,
                                   save_type: str, as_copy: bool = False):
        """Restore a server save/state version to local disk.

        Re-fetches the ROM's history and looks up the entry by id (so we use the
        authoritative server metadata, not stale frontend data), then delegates
        to the shared RetroArchInterface.restore_save_version. The running file
        watcher auto-uploads the restored file as a new server version.
        """
        try:
            if not (self._romm_client and self._romm_client.authenticated):
                return {'success': False, 'message': 'Not connected to RomM'}
            if not self._retroarch:
                return {'success': False, 'message': 'RetroArch interface not initialized'}

            saves, states = self._romm_client.get_save_history(rom_id)
            pool = states if save_type == 'states' else saves
            entry = next((e for e in pool if e.get('id') == save_id), None)
            if entry is None:
                return {'success': False, 'message': f'Version {save_id} not found'}

            result = self._retroarch.restore_save_version(
                self._romm_client, None, entry, save_type, as_copy,
                log=lambda m: logging.info(m))
            return {
                'success':  result.get('success', False),
                'message':  result.get('error') or 'Restored',
                'tgt_name': result.get('tgt_name'),
            }
        except Exception as e:
            logging.error(f"restore_save_version error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    # -----------------------------------------------------------------------
    # Game Browser (controller-first library: browse platforms/collections,
    # per-game cover/detail/download/delete). Styled in RomM v2 on the frontend.
    # -----------------------------------------------------------------------

    def _games_index(self):
        """rom_id -> game dict, from the in-memory library."""
        return {g.get('rom_id'): g for g in (self._available_games or []) if g.get('rom_id')}

    # File types RetroArch can boot directly. .m3u is the multi-disc playlist
    # (RomM generates one inside the download zip); listing it lets the user
    # launch "all discs" with in-game disc swapping. .bin is intentionally
    # excluded because it is paired with a .cue (we launch the .cue).
    _LAUNCHABLE_DISC_EXTS = ('.m3u', '.chd', '.cue', '.iso', '.pbp',
                             '.ccd', '.gdi', '.cdi', '.nrg')

    # Auxiliary (non-game) extensions — must match module-level _NON_GAME_EXTS.
    _NON_GAME_EXTS = _NON_GAME_EXTS

    def _list_local_discs(self, local_path):
        """Return launchable entries inside a downloaded multi-file folder.

        Each entry: {name, path, is_m3u, is_region}. Returns [] for a single-file
        ROM or when nothing launchable is found.

        Two folder shapes are handled:
          - true multi-DISC games: disc images + an .m3u playlist. The .m3u sorts
            first so it reads as the natural "all discs" default, and is_region is
            False on every entry.
          - multi-FILE regional ROMs: standalone game files (e.g. per-region
            .nds). Here the .m3u is a RomM artefact that would mis-boot the
            emulator, so it is excluded entirely and each game is a region entry
            (is_region True). The caller boots one variant, never a playlist.
        """
        try:
            p = Path(local_path)
            if not p.is_dir():
                return []
            discs = []
            for f in sorted(p.rglob('*'), key=lambda x: x.name.lower()):
                if f.is_file() and f.suffix.lower() in self._LAUNCHABLE_DISC_EXTS:
                    discs.append({'name': f.name, 'path': str(f),
                                  'is_m3u': f.suffix.lower() == '.m3u',
                                  'is_region': False})
            # A real disc set has 2+ disc images (the .m3u doesn't count). When
            # there is no such set, treat the folder as a regional multi-file ROM
            # and surface the standalone game files instead of the stray .m3u.
            disc_images = [d for d in discs if not d['is_m3u']]
            if len(disc_images) < 2:
                games = _list_standalone_games(p)
                if len(games) > 1:
                    return [{'name': f.name, 'path': str(f),
                             'is_m3u': False, 'is_region': True} for f in games]
            # Float the .m3u to the top so it is the default "all discs" entry.
            discs.sort(key=lambda d: (not d['is_m3u'], d['name'].lower()))
            return discs
        except Exception as e:
            logging.warning(f"_list_local_discs error: {e}")
            return []

    def _resolve_launch_path(self, local_path, disc=None):
        """Resolve a game's local_path (file or folder) to the file to boot.

        - Single-file ROM: returns local_path unchanged.
        - Folder + explicit `disc` filename: returns that disc/region's path.
        - Folder, no disc: for a true disc set, prefers the .m3u playlist
          (in-game swap); for a regional multi-file ROM, the first variant
          (there is no playlist — booting it would mis-launch).
        Returns None when nothing launchable is present.
        """
        p = Path(local_path)
        if p.is_file():
            return str(p)
        discs = self._list_local_discs(local_path)
        if not discs:
            return None
        if disc:
            for d in discs:
                if d['name'] == disc:
                    return d['path']
            return None
        return discs[0]['path']  # .m3u sorts first, else first disc

    def _get_last_disc(self, rom_id):
        """Remembered disc filename for a ROM, or '' if none/invalid."""
        try:
            return self._settings.get('LastDisc', str(rom_id), '') if self._settings else ''
        except Exception:
            return ''

    async def get_local_discs(self, rom_id: int):
        """Disc list for the Play-button picker (downloaded multi-disc games).

        Also returns `last` — the remembered disc filename (what a plain Play
        will boot), so the UI can mark the current choice.
        """
        try:
            g = self._games_index().get(rom_id)
            if not g or not g.get('is_downloaded') or not g.get('local_path'):
                return {'success': True, 'discs': [], 'last': ''}
            discs = self._list_local_discs(g['local_path'])
            last = self._get_last_disc(rom_id)
            # Drop a stale remembered name if that disc no longer exists.
            if last and not any(d['name'] == last for d in discs):
                last = ''
            # Regional multi-file ROM (variants, no playlist) vs true disc set:
            # lets the UI title the picker "region" and skip "all discs".
            is_region = bool(discs) and all(d.get('is_region') for d in discs)
            return {'success': True, 'discs': discs, 'last': last,
                    'is_region': is_region}
        except Exception as e:
            logging.error(f"get_local_discs error: {e}", exc_info=True)
            return {'success': False, 'discs': [], 'last': '', 'message': str(e)}

    async def get_sibling_roms(self, rom_id: int):
        """Return regional variants for a ROM."""
        try:
            if not (self._romm_client and self._romm_client.authenticated):
                return {'success': False, 'siblings': [], 'message': 'Not connected to RomM'}
            r = self._romm_client.session.get(
                urljoin(self._romm_client.base_url, f'/api/roms/{rom_id}'), timeout=15)
            if r.status_code != 200:
                return {'success': False, 'siblings': [], 'message': f'HTTP {r.status_code}'}
            d = r.json()
            raw = d.get('sibling_roms') or []
            siblings = [
                {'rom_id': s.get('id'), 'name': s.get('name') or s.get('fs_name_no_ext') or 'Variant',
                 'fs_name': s.get('fs_name'), 'regions': s.get('regions', [])}
                for s in raw
            ]
            return {'success': True, 'siblings': siblings}
        except Exception as e:
            logging.error(f"get_sibling_roms error: {e}", exc_info=True)
            return {'success': False, 'siblings': [], 'message': str(e)}

    async def get_local_siblings(self, rom_id: int):
        """Return which siblings of a ROM are downloaded locally."""
        try:
            idx = self._games_index()
            g = idx.get(rom_id)
            if not g:
                return {'success': True, 'downloaded_ids': []}
            sibling_roms = g.get('sibling_roms', [])
            ids = [rom_id] + [s.get('id') for s in sibling_roms if s.get('id')]
            downloaded = [rid for rid in ids if idx.get(rid, {}).get('is_downloaded')]
            return {'success': True, 'downloaded_ids': downloaded}
        except Exception as e:
            logging.error(f"get_local_siblings error: {e}", exc_info=True)
            return {'success': False, 'downloaded_ids': [], 'message': str(e)}

    def _platform_name_for(self, g):
        p = g.get('platform')
        if p and p != 'Unknown':
            return p
        slug = g.get('platform_slug') or (g.get('romm_data') or {}).get('platform_slug')
        if slug:
            return (self._platform_slug_to_name or {}).get(slug) or slug
        return 'Unknown'

    async def get_library_groups(self, mode: str = 'platform'):
        """Return library entry groups for the chosen mode ('platform'|'collection').

        Each group: {key, label, count, downloaded}. 'key' is what to pass back to
        get_library_games.
        """
        try:
            if mode == 'collection':
                actively = (self._settings.get('Collections', 'actively_syncing', '')
                            if self._settings else '')
                synced_set = {c for c in actively.split('|') if c}
                groups = []
                for col in (self._romm_collections or []):
                    name = col.get('name')
                    if not name:
                        continue
                    count = col.get('rom_count')
                    if count is None:
                        count = len(col.get('roms') or col.get('rom_ids') or [])
                    # Kind drives the badge (favorite/smart/virtual) and section,
                    # mirroring RomM's collection index.
                    if col.get('is_favorite'):
                        kind = 'favorite'
                    elif col.get('is_smart'):
                        kind = 'smart'
                    elif col.get('is_virtual'):
                        kind = 'virtual'
                    else:
                        kind = 'collection'
                    # Up to 4 sample cover paths for the 2×2 mosaic tile.
                    covers = (col.get('path_covers_small')
                              or col.get('path_covers_large') or [])[:4]
                    groups.append({'key': name, 'label': name,
                                   'count': count, 'downloaded': None,
                                   'kind': kind, 'covers': covers,
                                   'synced': name in synced_set})
                groups.sort(key=lambda x: (x['label'] or '').lower())

                # Virtual (autogenerated) collections render as their own
                # section. Keyed by their opaque base64 id; browse/download
                # only (no auto-sync), so the frontend disables the Y toggle.
                vgroups = []
                for col in (self._romm_virtual_collections or []):
                    name = col.get('name')
                    vid = col.get('id')
                    if not name or not vid:
                        continue
                    count = col.get('rom_count')
                    if count is None:
                        count = len(col.get('rom_ids') or [])
                    covers = (col.get('path_covers_small')
                              or col.get('path_covers_large') or [])[:4]
                    vgroups.append({'key': vid, 'label': name,
                                    'count': count, 'downloaded': None,
                                    'kind': 'virtual', 'covers': covers,
                                    'virtual': True, 'synced': False})
                vgroups.sort(key=lambda x: (x['label'] or '').lower())
                groups.extend(vgroups)
                return {'success': True, 'mode': mode, 'groups': groups}

            # default: platform
            agg = {}
            for g in (self._available_games or []):
                label = self._platform_name_for(g)
                slug = g.get('platform_slug') or (g.get('romm_data') or {}).get('platform_slug')
                a = agg.setdefault(label, {'key': label, 'label': label, 'count': 0,
                                           'downloaded': 0, 'slug': slug, 'fs_slug': slug})
                a['count'] += 1
                if not a.get('slug') and slug:
                    a['slug'] = slug; a['fs_slug'] = slug
                if g.get('is_downloaded'):
                    a['downloaded'] += 1
            groups = sorted(agg.values(), key=lambda x: (x['label'] or '').lower())
            return {'success': True, 'mode': 'platform', 'groups': groups}
        except Exception as e:
            logging.error(f"get_library_groups error: {e}", exc_info=True)
            return {'success': False, 'groups': [], 'message': str(e)}

    @staticmethod
    def _serialize_game(g, is_downloaded=None):
        dl = g.get('is_downloaded') if is_downloaded is None else is_downloaded
        s = {
            'rom_id':        g.get('rom_id') or g.get('id'),
            'name':          g.get('name') or g.get('fs_name_no_ext') or g.get('fs_name') or 'Unknown',
            'platform':      g.get('platform'),
            'is_downloaded': dl,
            'has_cover':     bool(g.get('cover_path') or g.get('path_cover_small')),
            'platform_slug': g.get('platform_slug') or (g.get('romm_data') or {}).get('platform_slug'),
        }
        if g.get('is_multi_disc'):
            s['is_multi_disc'] = True
            s['disc_count'] = g.get('disc_count', 0)
        if g.get('sibling_roms'):
            s['sibling_roms'] = [
                {'rom_id': sib.get('id'), 'name': sib.get('name') or sib.get('fs_name_no_ext') or 'Variant'}
                for sib in g['sibling_roms']
            ]
            s['region_count'] = len(g['sibling_roms']) + 1
        return s

    async def get_library_games(self, mode: str, key: str):
        """Return the games for a group (platform name or collection name)."""
        try:
            if mode == 'collection':
                col_id = None
                for col in (self._romm_collections or []):
                    if col.get('name') == key:
                        col_id = col.get('id'); break
                if col_id is not None:
                    roms = self._romm_client.get_collection_roms(col_id) or []
                else:
                    # Fall back to virtual collections (keyed by opaque base64 id).
                    is_virtual = any(c.get('id') == key
                                     for c in (self._romm_virtual_collections or []))
                    if not is_virtual:
                        return {'success': False, 'games': [], 'message': 'Collection not found'}
                    roms = self._romm_client.get_virtual_collection_roms(key) or []
                idx = self._games_index()
                games = []
                for r in roms:
                    rid = r.get('id')
                    local = idx.get(rid)
                    entry = {
                        'rom_id': rid,
                        'name': (local or {}).get('name') or r.get('fs_name_no_ext') or r.get('name') or 'Unknown',
                        'platform': (local or {}).get('platform') or r.get('platform_name'),
                        'is_downloaded': bool(local and local.get('is_downloaded')),
                        'has_cover': bool(r.get('path_cover_small') or (local or {}).get('cover_path')),
                        'platform_slug': (local or {}).get('platform_slug') or r.get('platform_slug'),
                    }
                    if local and local.get('is_downloaded') and local.get('local_path'):
                        is_md, dc = _detect_multi_disc(local['local_path'], True)
                        entry['is_multi_disc'] = is_md
                        entry['disc_count'] = dc
                    sibs = r.get('sibling_roms') or (local or {}).get('sibling_roms') or []
                    if sibs:
                        entry['sibling_roms'] = [
                            {'rom_id': s.get('id'), 'name': s.get('name') or 'Variant'}
                            for s in sibs
                        ]
                        entry['region_count'] = len(sibs) + 1
                    games.append(entry)
                    if rid and rid not in idx and r.get('path_cover_small'):
                        self._cover_paths[rid] = r.get('path_cover_small')
                games.sort(key=lambda x: (x.get('name') or '').lower())
                return {'success': True, 'games': games}

            # platform
            games = []
            for g in (self._available_games or []):
                if self._platform_name_for(g) != key:
                    continue
                sg = self._serialize_game(g)
                sg['platform'] = key  # resolved label, not the raw (maybe-Unknown) field
                games.append(sg)
            games.sort(key=lambda x: (x.get('name') or '').lower())
            return {'success': True, 'games': games}
        except Exception as e:
            logging.error(f"get_library_games error: {e}", exc_info=True)
            return {'success': False, 'games': [], 'message': str(e)}

    async def search_games(self, query: str):
        """Search the whole library, mirroring RomM's Search view.

        Uses the server's `search_term` (matches filename + metadata, not just
        the display name), then merges in local download state. Falls back to an
        in-memory name filter if the server query fails or the client is down.
        """
        q = (query or '').strip()
        if not q:
            # Mirror RomM's Search view: with no term, show the whole library.
            try:
                out = []
                for g in (self._available_games or []):
                    sg = self._serialize_game(g)
                    sg['platform'] = self._platform_name_for(g)
                    out.append(sg)
                out.sort(key=lambda x: (x.get('name') or '').lower())
                return {'success': True, 'games': out}
            except Exception as e:
                logging.error(f"search_games (browse) error: {e}", exc_info=True)
                return {'success': False, 'games': [], 'message': str(e)}
        try:
            roms = []
            if self._romm_client:
                try:
                    roms = self._romm_client.search_roms(q) or []
                except Exception as e:
                    logging.warning(f"server search failed, falling back: {e}")
                    roms = []
            if roms:
                idx = self._games_index()
                out = []
                for r in roms:
                    rid = r.get('id')
                    local = idx.get(rid)
                    entry = {
                        'rom_id': rid,
                        'name': (local or {}).get('name') or r.get('fs_name_no_ext') or r.get('name') or 'Unknown',
                        'platform': (local or {}).get('platform') or r.get('platform_name'),
                        'is_downloaded': bool(local and local.get('is_downloaded')),
                        'has_cover': bool(r.get('path_cover_small') or (local or {}).get('cover_path')),
                        'platform_slug': (local or {}).get('platform_slug') or r.get('platform_slug'),
                    }
                    if local and local.get('is_downloaded') and local.get('local_path'):
                        is_md, dc = _detect_multi_disc(local['local_path'], True)
                        entry['is_multi_disc'] = is_md
                        entry['disc_count'] = dc
                    sibs = r.get('sibling_roms') or (local or {}).get('sibling_roms') or []
                    if sibs:
                        entry['sibling_roms'] = [
                            {'rom_id': s.get('id'), 'name': s.get('name') or 'Variant'}
                            for s in sibs
                        ]
                        entry['region_count'] = len(sibs) + 1
                    out.append(entry)
                    if rid and rid not in idx and r.get('path_cover_small'):
                        self._cover_paths[rid] = r.get('path_cover_small')
                out.sort(key=lambda x: (x.get('name') or '').lower())
                return {'success': True, 'games': out}

            # Fallback: in-memory name filter (offline / search error).
            ql = q.lower()
            out = []
            for g in (self._available_games or []):
                if ql in (g.get('name') or '').lower():
                    sg = self._serialize_game(g)
                    sg['platform'] = self._platform_name_for(g)
                    out.append(sg)
            out.sort(key=lambda x: (x.get('name') or '').lower())
            return {'success': True, 'games': out[:200]}
        except Exception as e:
            logging.error(f"search_games error: {e}", exc_info=True)
            return {'success': False, 'games': [], 'message': str(e)}

    # -----------------------------------------------------------------------
    # Cover-art disk cache  (~/.config/romm-retroarch-sync/cover_cache/)
    #
    # Persists decoded cover/screenshot bytes across plugin reloads so art is
    # never re-fetched from RomM after the first load — and is served even
    # before the RomM client re-authenticates. Stores RAW image bytes (compact);
    # the data URI is rebuilt on read. Keyed by sha1 of a stable string key.
    # -----------------------------------------------------------------------
    # Per-image timing+source to the plugin log — how we tell apart a slow
    # network fetch from a slow disk read, a big payload, or event-loop
    # contention when covers feel laggy. Off by default; set ROMM_COVER_TRACE=1
    # to re-enable. Only logs non-memory paths so volume stays low.
    _cover_trace = bool(os.environ.get('ROMM_COVER_TRACE'))

    def _trace_cover(self, kind: str, key, src: str, t0: float, nbytes: int = 0):
        if not self._cover_trace:
            return
        try:
            ms = (time.monotonic() - t0) * 1000.0
            logging.info(f"[cover] {kind} {key} src={src} {ms:.0f}ms "
                         f"{nbytes/1024:.0f}KB mem={len(self._cover_cache)}")
        except Exception:
            pass

    # Grid covers render at ~150px; serving the full ~200KB RomM art means
    # ~16MB of base64 hits the websocket + UI-thread JSON.parse in one burst
    # when a platform opens (the freeze). Downscale to a small JPEG thumbnail
    # (~15KB) before caching/serving. Width chosen for 2x retina of the cell.
    _THUMB_W = 360
    _THUMB_W_LARGE = 640  # detail-page hero uses a bigger cover

    def _cover_cache_put(self, ck, uri):
        """Insert into the in-memory cache, enforcing a bound on EVERY insert
        (the old code only capped on the network path, so disk hits grew it
        unbounded — 800+ thumbnails ≈ hundreds of MB resident)."""
        if len(self._cover_cache) > 600:
            self._cover_cache.clear()
        self._cover_cache[ck] = uri

    def _store_thumb(self, ck, thumb_key, raw, mime, large):
        """Downscale raw bytes → thumbnail, persist under thumb_key, mem-cache
        and return the data URI."""
        tb, tmime = self._make_thumb(raw, large)
        mime = tmime or mime or 'image/jpeg'
        uri = f"data:{mime};base64,{base64.b64encode(tb).decode('ascii')}"
        self._disk_cover_put(thumb_key, mime, tb)
        self._cover_cache_put(ck, uri)
        return uri

    def _make_thumb(self, content: bytes, large: bool = False):
        """Compact an image for grid display. Returns (bytes, mime).

        RomM 'small' covers are already ~small resolution but ship as ~200KB
        PNGs, so the real win is re-encoding to JPEG (≈30KB), not resizing.
        We downscale only when wider than the target, then always try a JPEG
        re-encode and keep it when it actually shrinks the payload. Small
        images with real transparency (platform icons) are left untouched so
        they don't get a black background. Falls back to the original bytes if
        PIL is missing or anything fails."""
        if not PIL_AVAILABLE:
            return content, None
        try:
            import io
            max_w = self._THUMB_W_LARGE if large else self._THUMB_W
            im = Image.open(io.BytesIO(content))
            # Preserve small transparent assets (icons) as-is.
            has_alpha = ('A' in im.getbands())
            if has_alpha and len(content) < 60_000:
                return content, None
            if im.width > max_w:
                h = round(im.height * (max_w / im.width))
                im = im.resize((max_w, h), Image.LANCZOS)
            out = io.BytesIO()
            im.convert('RGB').save(out, format='JPEG', quality=82, optimize=True)
            jpeg = out.getvalue()
            # Only adopt the JPEG if it's a real win (it nearly always is for
            # the big PNG covers; guards against bloating already-tiny art).
            if len(jpeg) < len(content):
                return jpeg, 'image/jpeg'
            return content, None
        except Exception as e:
            logging.debug(f"_make_thumb failed: {e}")
            return content, None

    def _cover_dir(self):
        d = Path.home() / '.config' / 'romm-retroarch-sync' / 'cover_cache'
        try:
            d.mkdir(parents=True, exist_ok=True)
        except Exception:
            pass
        return d

    def _disk_cover_get(self, key: str):
        """Return a data URI from disk for `key`, or None if not cached."""
        try:
            stem = hashlib.sha1(key.encode()).hexdigest()
            d = self._cover_dir()
            for f in d.glob(stem + '.*'):
                data = f.read_bytes()
                if not data:
                    return None
                mime = mimetypes.guess_type(f.name)[0] or 'image/jpeg'
                try:
                    os.utime(f, None)  # bump mtime → cheap LRU for pruning
                except Exception:
                    pass
                return f"data:{mime};base64,{base64.b64encode(data).decode('ascii')}"
        except Exception:
            pass
        return None

    def _disk_cover_get_bytes(self, key: str):
        """Return (raw_bytes, mime) from disk for `key`, or (None, None)."""
        try:
            stem = hashlib.sha1(key.encode()).hexdigest()
            for f in self._cover_dir().glob(stem + '.*'):
                data = f.read_bytes()
                if not data:
                    return None, None
                return data, (mimetypes.guess_type(f.name)[0] or 'image/jpeg')
        except Exception:
            pass
        return None, None

    def _disk_cover_put(self, key: str, mime: str, content: bytes):
        """Write raw cover bytes to disk for `key`; prune occasionally."""
        try:
            ext = mimetypes.guess_extension(mime) or '.jpg'
            if ext == '.jpe':
                ext = '.jpg'
            stem = hashlib.sha1(key.encode()).hexdigest()
            (self._cover_dir() / (stem + ext)).write_bytes(content)
            self._prune_cover_dir()
        except Exception:
            pass

    def _prune_cover_dir(self, cap: int = 4000):
        """Keep the cache bounded: when over `cap` files, drop the oldest 20%.
        Runs ~1 call in 40 (writes) to keep the stat cost negligible."""
        try:
            import random
            if random.randint(0, 39) != 0:
                return
            files = list(self._cover_dir().iterdir())
            if len(files) <= cap:
                return
            files.sort(key=lambda p: p.stat().st_mtime)
            for p in files[: max(1, len(files) // 5)]:
                try:
                    p.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    async def clear_cover_cache(self):
        """Wipe the cover-art caches (memory + disk). Use after RomM art changes."""
        try:
            self._cover_cache = {}
            removed = 0
            d = self._cover_dir()
            for f in d.iterdir():
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass
            logging.info(f"Cleared cover cache ({removed} files)")
            return {'success': True, 'removed': removed}
        except Exception as e:
            logging.error(f"clear_cover_cache error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    async def get_game_cover(self, rom_id: int, large: bool = False):
        """Return a base64 data URI for a game's cover art (cached).

        The in-memory hit is served inline; disk reads + RomM HTTP fetches are
        blocking, so they run in a worker thread to avoid freezing the asyncio
        event loop (which would stall every other plugin RPC and the whole UI).
        """
        ck = (rom_id, large)
        if ck in self._cover_cache:
            return {'success': True, 'data_uri': self._cover_cache[ck]}
        return await asyncio.to_thread(self._get_game_cover_blocking, rom_id, large)

    def _get_game_cover_blocking(self, rom_id: int, large: bool = False):
        t0 = time.monotonic()
        try:
            ck = (rom_id, large)
            if ck in self._cover_cache:
                return {'success': True, 'data_uri': self._cover_cache[ck]}
            # Thumbnail disk cache (v2). The compact downscaled art.
            tkey = f"covt2:{rom_id}:{large}"
            disk = self._disk_cover_get(tkey)
            if disk:
                self._cover_cache_put(ck, disk)
                self._trace_cover('cover', rom_id, 'disk', t0, len(disk))
                return {'success': True, 'data_uri': disk}
            # Reuse a full-size cover already on disk (legacy v1 cache) →
            # downscale locally instead of re-downloading from RomM.
            raw, mime = self._disk_cover_get_bytes(f"cover:{rom_id}:{large}")
            if raw:
                uri = self._store_thumb(ck, tkey, raw, mime, large)
                self._trace_cover('cover', rom_id, 'disk-orig', t0, len(uri))
                return {'success': True, 'data_uri': uri}
            if not (self._romm_client and self._romm_client.authenticated):
                return {'success': False, 'data_uri': None}
            idx = self._games_index()
            g = idx.get(rom_id) or {}
            path = (g.get('cover_path_large') if large else g.get('cover_path')) \
                or g.get('cover_path') or self._cover_paths.get(rom_id)
            if not path:
                # fall back to the ROM detail endpoint
                r = self._romm_client.session.get(
                    urljoin(self._romm_client.base_url, f'/api/roms/{rom_id}'), timeout=10)
                if r.status_code == 200:
                    d = r.json()
                    path = (d.get('path_cover_large') if large else d.get('path_cover_small')) \
                        or d.get('path_cover_small') or d.get('path_cover_large')
            if not path:
                return {'success': True, 'data_uri': None}
            resp = self._romm_client.session.get(
                urljoin(self._romm_client.base_url, path), timeout=20)
            if resp.status_code != 200 or not resp.content:
                return {'success': True, 'data_uri': None}
            mime = resp.headers.get('content-type') or mimetypes.guess_type(path)[0] or 'image/jpeg'
            uri = self._store_thumb(ck, tkey, resp.content, mime, large)
            self._trace_cover('cover', rom_id, 'net', t0, len(uri))
            return {'success': True, 'data_uri': uri}
        except Exception as e:
            logging.error(f"get_game_cover error: {e}", exc_info=True)
            return {'success': False, 'data_uri': None}

    async def get_image(self, path: str):
        """Return a base64 data URI for an arbitrary RomM resource path (cached).

        Used by collection mosaic tiles, whose cover paths come from the
        collection object (path_covers_small) rather than a single rom_id.
        In-memory hit served inline; blocking disk/HTTP offloaded to a thread
        so the event loop (and the whole UI) never stalls on image I/O.
        """
        if not path:
            return {'success': True, 'data_uri': None}
        ck = ('img', path)
        if ck in self._cover_cache:
            return {'success': True, 'data_uri': self._cover_cache[ck]}
        return await asyncio.to_thread(self._get_image_blocking, path)

    def _get_image_blocking(self, path: str):
        t0 = time.monotonic()
        try:
            ck = ('img', path)
            if ck in self._cover_cache:
                return {'success': True, 'data_uri': self._cover_cache[ck]}
            tkey = f"imgt2:{path}"
            disk = self._disk_cover_get(tkey)
            if disk:
                self._cover_cache_put(ck, disk)
                self._trace_cover('img', path, 'disk', t0, len(disk))
                return {'success': True, 'data_uri': disk}
            # Downscale a full-size image already on disk (legacy v1) in place.
            raw, mime = self._disk_cover_get_bytes(f"img:{path}")
            if raw:
                uri = self._store_thumb(ck, tkey, raw, mime, False)
                self._trace_cover('img', path, 'disk-orig', t0, len(uri))
                return {'success': True, 'data_uri': uri}
            if not (self._romm_client and self._romm_client.authenticated):
                return {'success': False, 'data_uri': None}
            resp = self._romm_client.session.get(
                urljoin(self._romm_client.base_url, path), timeout=20)
            if resp.status_code != 200 or not resp.content:
                return {'success': True, 'data_uri': None}
            mime = resp.headers.get('content-type') or mimetypes.guess_type(path)[0] or 'image/png'
            uri = self._store_thumb(ck, tkey, resp.content, mime, False)
            self._trace_cover('img', path, 'net', t0, len(uri))
            return {'success': True, 'data_uri': uri}
        except Exception as e:
            logging.error(f"get_image error: {e}", exc_info=True)
            return {'success': False, 'data_uri': None}

    async def get_plugin_logo(self):
        """Return the plugin's bundled logo as a base64 data URI + raw base64.

        Used by the frontend to paint custom artwork on the optional 'RomM'
        Steam library shortcut (SteamClient.SetCustomArtworkForApp wants raw
        base64, while <img> wants a data URI).
        """
        try:
            import base64
            logo = Path(__file__).parent / "assets" / "logo.png"
            if not logo.exists():
                return {'success': False, 'b64': None, 'data_uri': None}
            raw = base64.b64encode(logo.read_bytes()).decode('ascii')
            return {'success': True, 'b64': raw, 'data_uri': f"data:image/png;base64,{raw}", 'ext': 'png'}
        except Exception as e:
            logging.error(f"get_plugin_logo error: {e}", exc_info=True)
            return {'success': False, 'b64': None, 'data_uri': None}

    async def get_romm_artwork(self):
        """Return RomM-branded Steam artwork for every asset type as raw base64.

        Keys are Steam's eAppArtworkAssetType values:
            0 grid (portrait), 1 hero, 2 logo (transparent), 3 header, 4 icon.
        The frontend paints these onto the optional 'RomM' library shortcut via
        SetCustomArtworkForApp. PNGs are pre-rendered (scripts/gen_romm_artwork.py)
        and bundled, so no SVG/PIL work happens at runtime.
        """
        try:
            import base64
            assets = Path(__file__).parent / "assets"
            files = {0: "romm-grid.png", 1: "romm-hero.png", 2: "romm-logo.png",
                     3: "romm-header.png", 4: "romm-icon.png"}
            out = {}
            for atype, fname in files.items():
                p = assets / fname
                if p.exists():
                    out[str(atype)] = base64.b64encode(p.read_bytes()).decode('ascii')
            return {'success': bool(out), 'art': out, 'ext': 'png'}
        except Exception as e:
            logging.error(f"get_romm_artwork error: {e}", exc_info=True)
            return {'success': False, 'art': {}}

    async def get_romm_logo(self):
        """Return RomM's bundled isotipo (brand mark) as an SVG data URI.

        Bundled in the plugin assets so the setup wizard can show the real RomM
        logo before any server connection exists (server assets need auth).
        """
        try:
            import base64
            iso = Path(__file__).parent / "assets" / "romm-isotipo.svg"
            if not iso.exists():
                return {'success': False, 'data_uri': None}
            raw = base64.b64encode(iso.read_bytes()).decode('ascii')
            return {'success': True, 'data_uri': f"data:image/svg+xml;base64,{raw}"}
        except Exception as e:
            logging.error(f"get_romm_logo error: {e}", exc_info=True)
            return {'success': False, 'data_uri': None}

    async def get_retrodeck_logo(self):
        """Return RetroDECK's bundled brand mark as an SVG data URI for the
        optional 'Launch RetroDECK' button in the top bar."""
        try:
            import base64
            svg = Path(__file__).parent / "assets" / "retrodeck.svg"
            if not svg.exists():
                return {'success': False, 'data_uri': None}
            raw = base64.b64encode(svg.read_bytes()).decode('ascii')
            return {'success': True, 'data_uri': f"data:image/svg+xml;base64,{raw}"}
        except Exception as e:
            logging.error(f"get_retrodeck_logo error: {e}", exc_info=True)
            return {'success': False, 'data_uri': None}

    async def get_ra_earned(self, ra_id: int):
        """Earned achievement badge ids for a game's ra_id, fetched on its own so
        the game detail can render immediately and fill in earned state after.
        Returns {'earned': [badge_id, ...]}."""
        try:
            if not (ra_id and self._romm_client and self._romm_client.authenticated):
                return {'earned': []}
            mr = self._romm_client.session.get(
                urljoin(self._romm_client.base_url, '/api/users/me'), timeout=10)
            me = mr.json() if mr.status_code == 200 else {}
            for prog in ((me.get('ra_progression') or {}).get('results') or []):
                if prog.get('rom_ra_id') == ra_id:
                    return {'earned': [str(e.get('id'))
                                       for e in (prog.get('earned_achievements') or [])
                                       if e.get('id') is not None]}
        except Exception as e:
            logging.debug(f"get_ra_earned error: {e}")
        return {'earned': []}

    async def get_game_detail(self, rom_id: int):
        """Return IGDB-style metadata + files + local state for a game."""
        try:
            if not (self._romm_client and self._romm_client.authenticated):
                return {'success': False, 'message': 'Not connected to RomM'}
            r = self._romm_client.session.get(
                urljoin(self._romm_client.base_url, f'/api/roms/{rom_id}'), timeout=15)
            if r.status_code != 200:
                return {'success': False, 'message': f'HTTP {r.status_code}'}
            d = r.json()
            meta = d.get('metadatum') or d.get('igdb_metadata') or {}

            def _names(val):
                out = []
                for x in (val or []):
                    if isinstance(x, dict):
                        out.append(x.get('name') or x.get('slug'))
                    elif x:
                        out.append(str(x))
                return [x for x in out if x]

            local = self._games_index().get(rom_id) or {}
            files = []
            for f in (d.get('files') or []):
                files.append({'name': f.get('file_name') or f.get('fs_name'),
                              'size': f.get('file_size_bytes') or f.get('size_bytes')})

            # RetroAchievements — mirror RomM's GameDetails wiring: the rom's
            # merged_ra_metadata.achievements list, with each achievement marked
            # earned when its badge_id is in the user's progression set for this
            # game (located by rom_ra_id == rom.ra_id). Badge art uses the public
            # RetroAchievements CDN URLs so the frontend <img> can load directly.
            # Earned state is fetched separately (get_ra_earned) so this response
            # never blocks on the extra /api/users/me round-trip. Achievements
            # render immediately as not-earned; the frontend fills in earned after.
            achievements = []
            ra_id = d.get('ra_id')
            earned_ids = set()
            ra_meta = d.get('merged_ra_metadata') or {}
            raw_ach = sorted((ra_meta.get('achievements') or []),
                             key=lambda a: (a.get('display_order') if a.get('display_order') is not None else 1e9))
            for a in raw_ach:
                bid = a.get('badge_id')
                achievements.append({
                    'ra_id': a.get('ra_id'),
                    'title': a.get('title') or '',
                    'description': a.get('description') or '',
                    'points': a.get('points') or 0,
                    'type': a.get('type') or '',
                    'badge_id': str(bid) if bid is not None else None,
                    'badge_url': a.get('badge_url'),
                    'badge_url_lock': a.get('badge_url_lock'),
                    'earned': bool(bid is not None and str(bid) in earned_ids),
                })

            # ── HLTB durations (RomM HLTBStrip) ──────────────────────────────
            hltb_src = d.get('hltb_metadata') or meta.get('hltb_metadata') or {}
            hltb = {
                'main_story':            hltb_src.get('main_story'),
                'main_story_count':      hltb_src.get('main_story_count'),
                'main_plus_extra':       hltb_src.get('main_plus_extra'),
                'main_plus_extra_count': hltb_src.get('main_plus_extra_count'),
                'completionist':         hltb_src.get('completionist'),
                'completionist_count':   hltb_src.get('completionist_count'),
                'all_styles':            hltb_src.get('all_styles'),
                'all_styles_count':      hltb_src.get('all_styles_count'),
            } if hltb_src else None

            # ── Age ratings (RomM AgeRatingBadges) ───────────────────────────
            # Resolve each merged rating string to {category, rating, icon_url},
            # recovering the IGDB icon URL from the igdb/ss provider lists or by
            # the "CATEGORY:RATING" convention. Mirrors AgeRatingBadges.vue.
            _CAT_SLUG = {'ESRB': 'esrb', 'PEGI': 'pegi', 'CERO': 'cero', 'USK': 'usk',
                         'GRAC': 'grac', 'CLASS_IND': 'class_ind', 'ACB': 'acb'}
            def _igdb_icon(category, rating):
                slug = _CAT_SLUG.get((category or '').strip().upper())
                if not slug or not rating:
                    return None
                norm = str(rating).lower().replace('+', '')
                return f"https://www.igdb.com/icons/rating_icons/{slug}/{slug}_{norm}.png"
            igdb_meta = d.get('igdb_metadata') or {}
            ss_meta = d.get('ss_metadata') or {}
            _igdb_by = {str(r.get('rating')).strip(): r for r in (igdb_meta.get('age_ratings') or []) if isinstance(r, dict)}
            _ss_by = {str(r.get('rating')).strip(): r for r in (ss_meta.get('age_ratings') or []) if isinstance(r, dict)}
            age_ratings = []
            for entry in (meta.get('age_ratings') or []):
                if not isinstance(entry, str):
                    continue
                e = entry.strip()
                if ':' in e:
                    cat, _, rat = e.partition(':')
                    cat, rat = cat.strip(), rat.strip()
                    age_ratings.append({'category': cat, 'rating': rat, 'icon_url': _igdb_icon(cat, rat)})
                elif e in _igdb_by:
                    m = _igdb_by[e]
                    age_ratings.append({'category': m.get('category') or '', 'rating': m.get('rating') or e,
                                        'icon_url': m.get('rating_cover_url') or _igdb_icon(m.get('category'), m.get('rating'))})
                elif e in _ss_by:
                    m = _ss_by[e]
                    age_ratings.append({'category': m.get('category') or '', 'rating': m.get('rating') or e,
                                        'icon_url': _igdb_icon(m.get('category'), m.get('rating'))})
                else:
                    age_ratings.append({'category': '', 'rating': e, 'icon_url': None})

            # ── Related games (RomM RelatedGamesGrid) ────────────────────────
            def _related(key):
                out = []
                for g in (igdb_meta.get(key) or []):
                    if not isinstance(g, dict):
                        continue
                    out.append({'id': g.get('id'), 'name': g.get('name') or '',
                                'slug': g.get('slug'), 'cover_url': g.get('cover_url')})
                return out
            related = {
                'expansions': _related('expansions'),
                'dlcs':       _related('dlcs'),
                'remakes':    _related('remakes'),
                'remasters':  _related('remasters'),
                'similar':    _related('similar_games'),
            }

            # ── Provider ids + verification (RomM MetadataTab / providers.ts) ─
            providers = {k: d.get(k) for k in (
                'igdb_id', 'moby_id', 'ss_id', 'ra_id', 'sgdb_id',
                'launchbox_id', 'hasheous_id', 'flashpoint_id', 'hltb_id')}
            hashes = {'crc': d.get('crc_hash'), 'md5': d.get('md5_hash'),
                      'sha1': d.get('sha1_hash'), 'ra': d.get('ra_hash')}
            hh = d.get('hasheous_metadata') or {}
            verifications = [
                {'label': 'TOSEC',    'match': bool(hh.get('tosec_match'))},
                {'label': 'No-Intro', 'match': bool(hh.get('nointro_match'))},
                {'label': 'Redump',   'match': bool(hh.get('redump_match'))},
                {'label': 'FBNeo',    'match': bool(hh.get('fbneo_match'))},
                {'label': 'MAME',     'match': bool(hh.get('mame_arcade_match') or hh.get('mame_mess_match'))},
                {'label': 'RA',       'match': bool(d.get('ra_id'))},
            ]

            return {
                'success': True,
                'rom_id': rom_id,
                'name': d.get('name') or local.get('name') or 'Unknown',
                'fs_name': d.get('fs_name') or local.get('file_name'),
                'platform': (d.get('platform_display_name') or d.get('platform_custom_name')
                             or d.get('platform_name') or d.get('platform_slug') or local.get('platform')),
                'summary': d.get('summary') or meta.get('summary') or '',
                'genres': _names(d.get('genres') or meta.get('genres')),
                'franchises': _names(d.get('franchises') or meta.get('franchises')),
                'companies': _names(d.get('companies') or meta.get('companies')),
                'release_date': d.get('first_release_date') or meta.get('first_release_date'),
                'rating': meta.get('total_rating') or d.get('total_rating'),
                # Header chips + Overview extras (RomM GameHeader / OverviewTab).
                'regions':      _names(d.get('regions')),
                'languages':    _names(d.get('languages')),
                'tags':         _names(d.get('tags')),
                'collections':  _names(meta.get('collections')),  # IGDB series (metadatum)
                'user_collections': _names(d.get('user_collections')),  # RomM collections this ROM is in
                'player_count': (meta.get('player_count') or '').strip() if isinstance(meta.get('player_count'), str) else meta.get('player_count'),
                'last_played':  (d.get('rom_user') or {}).get('last_played'),
                'verified':     bool(d.get('crc_hash')),
                'hltb':         hltb,
                'age_ratings':  age_ratings,
                'related':      related,
                'providers':    providers,
                'hashes':       hashes,
                'verifications': verifications,
                'files': files,
                'screenshots': [s for s in (d.get('merged_screenshots') or []) if s],
                'achievements': achievements,
                'ra_id': ra_id,
                'fs_size_bytes': d.get('fs_size_bytes') or 0,
                'is_downloaded': bool(local.get('is_downloaded')),
                'has_cover': bool(d.get('path_cover_small') or local.get('cover_path')),
            }
        except Exception as e:
            logging.error(f"get_game_detail error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    async def download_game(self, rom_id: int):
        """Download a single ROM into the library (handles archive extraction)."""
        try:
            if not (self._romm_client and self._romm_client.authenticated):
                return {'success': False, 'message': 'Not connected to RomM'}
            idx = self._games_index()
            g = idx.get(rom_id)
            download_dir = Path(self._settings.get('Download', 'rom_directory',
                                                   '~/RomMSync/roms')).expanduser()
            if g:
                platform_slug = g.get('platform_slug') or (g.get('romm_data') or {}).get('platform_slug')
                file_name = g.get('file_name') or (g.get('romm_data') or {}).get('fs_name')
                name = g.get('name')
            else:
                r = self._romm_client.session.get(
                    urljoin(self._romm_client.base_url, f'/api/roms/{rom_id}'), timeout=15)
                d = r.json() if r.status_code == 200 else {}
                platform_slug = d.get('platform_slug', 'Unknown')
                file_name = d.get('fs_name') or f"{d.get('name', 'rom')}.rom"
                name = d.get('name')
            if not (platform_slug and file_name):
                return {'success': False, 'message': 'Could not resolve ROM path'}
            dest = download_dir / platform_slug / file_name

            # Decky serves RPC calls sequentially on one socket, so if this method
            # blocked until the download finished, the frontend's get_download_progress
            # polls would queue behind it and never update mid-download. Instead we
            # run the download in a background thread and return immediately; the
            # frontend polls get_download_progress() to drive the fill UI and to learn
            # when it finished (state done/error).
            cur = self._download_progress.get(rom_id)
            if cur and cur.get('state') == 'downloading':
                return {'success': True, 'started': True, 'message': 'Already downloading'}

            self._download_progress[rom_id] = {
                'percent': 0, 'downloaded': 0, 'total': 0,
                'speed': 0, 'eta': 0, 'state': 'downloading', 'message': '',
            }

            def _worker():
                def _on_progress(info):
                    try:
                        prog = info.get('progress')  # 0..1
                        self._download_progress[rom_id] = {
                            'percent': int(round((prog or 0) * 100)),
                            'downloaded': info.get('downloaded', 0),
                            'total': info.get('total', 0),
                            'speed': info.get('speed', 0),
                            'eta': info.get('eta', 0),
                            'state': 'downloading', 'message': '',
                        }
                    except Exception:
                        pass
                try:
                    ok, msg = self._romm_client.download_rom(rom_id, name, dest, _on_progress)
                    if ok and g:
                        g['is_downloaded'] = True
                        g['local_path'] = str(dest)
                        is_md, dc = _detect_multi_disc(str(dest), True)
                        g['is_multi_disc'] = is_md
                        g['disc_count'] = dc
                    elif ok and not g:
                        # Sibling ROM downloaded — add to the in-memory index so
                        # launch_game can find it. Safe under CPython's GIL (same
                        # assumption as the g[] mutations above).
                        is_md, dc = _detect_multi_disc(str(dest), True)
                        self._available_games.append({
                            'name': name or 'Unknown',
                            'rom_id': rom_id,
                            'platform': platform_slug,
                            'platform_slug': platform_slug,
                            'file_name': file_name,
                            'is_downloaded': True,
                            'is_multi_disc': is_md,
                            'disc_count': dc,
                            'local_path': str(dest),
                            'local_size': dest.stat().st_size if dest.exists() else 0,
                            'cover_path': None,
                            'cover_path_large': None,
                            '_sibling_files': [],
                            'sibling_roms': [],
                            'romm_data': {
                                'fs_name': file_name,
                                'fs_name_no_ext': Path(file_name).stem if file_name else None,
                                'fs_size_bytes': 0,
                                'platform_slug': platform_slug,
                            },
                        })
                    self._download_progress[rom_id] = {
                        'percent': 100 if ok else 0, 'downloaded': 0, 'total': 0,
                        'speed': 0, 'eta': 0, 'state': 'done' if ok else 'error',
                        'message': msg or ('Downloaded' if ok else 'Download failed'),
                    }
                except Exception as e:
                    logging.error(f"download_game worker error: {e}", exc_info=True)
                    self._download_progress[rom_id] = {
                        'percent': 0, 'downloaded': 0, 'total': 0,
                        'speed': 0, 'eta': 0, 'state': 'error', 'message': str(e),
                    }

            threading.Thread(target=_worker, daemon=True, name=f"romm-dl-{rom_id}").start()
            return {'success': True, 'started': True, 'message': 'Download started'}
        except Exception as e:
            self._download_progress[rom_id] = {
                'percent': 0, 'downloaded': 0, 'total': 0,
                'speed': 0, 'eta': 0, 'state': 'error', 'message': str(e),
            }
            logging.error(f"download_game error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    async def get_download_progress(self, rom_id: int):
        """Live progress for an in-flight download (polled by the cover/button fill UI).

        Returns {percent 0..100, downloaded, total, speed, eta, state, message} where
        state is one of downloading|done|error, or state 'idle' when nothing is tracked.
        """
        p = self._download_progress.get(rom_id)
        if not p:
            return {'percent': 0, 'state': 'idle', 'message': ''}
        return p

    async def debug_log(self, msg: str):
        """Temporary bridge so frontend logs land in the backend log file."""
        logging.info(f"[FE] {msg}")
        return True

    async def delete_game(self, rom_id: int):
        """Delete a single game's local files."""
        try:
            import shutil
            idx = self._games_index()
            g = idx.get(rom_id)
            download_dir = Path(self._settings.get('Download', 'rom_directory',
                                                   '~/RomMSync/roms')).expanduser()
            target = None
            if g and g.get('local_path'):
                target = Path(g['local_path'])
            elif g:
                platform_slug = g.get('platform_slug') or (g.get('romm_data') or {}).get('platform_slug')
                file_name = g.get('file_name') or (g.get('romm_data') or {}).get('fs_name')
                if platform_slug and file_name:
                    target = download_dir / platform_slug / file_name
            if not target or not target.exists():
                if g:
                    g['is_downloaded'] = False; g['local_path'] = None
                return {'success': True, 'message': 'Nothing to delete'}
            if target.is_file():
                target.unlink()
            else:
                shutil.rmtree(target)
            if g:
                g['is_downloaded'] = False; g['local_path'] = None
            return {'success': True, 'message': 'Deleted'}
        except Exception as e:
            logging.error(f"delete_game error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    def _fetch_continue_playing(self, limit=15):
        """Continue-playing list straight from RomM (per-user, cross-device).

        RomM tracks play state server-side as rom_user.last_played, so we ask
        /api/roms ordered by it rather than guessing from local save mtimes.
        Returns serialized LibGames, enriched with local download state when the
        rom is in our index.
        """
        client = self._romm_client
        if not client or not client.ensure_authenticated():
            return []
        try:
            resp = client.session.get(
                urljoin(client.base_url, '/api/roms'),
                params={
                    'order_by': 'last_played', 'order_dir': 'desc',
                    'last_played': 'true',  # server-side filter to played roms only
                    'limit': limit, 'offset': 0,
                    'fields': 'id,name,fs_name,platform_name,platform_slug,path_cover_small,merged_screenshots,rom_user',
                },
                timeout=30,
            )
            if resp.status_code != 200:
                logging.warning(f"continue-playing fetch HTTP {resp.status_code}")
                return []
            items = resp.json().get('items', [])
        except Exception as e:
            logging.warning(f"continue-playing fetch failed: {e}")
            return []

        idx = self._games_index()
        out = []
        for rom in items:
            # Only games actually played carry a last_played timestamp; the
            # ordering pushes null-played roms to the tail, so stop at the first.
            if not (rom.get('rom_user') or {}).get('last_played'):
                continue
            rid = rom.get('id')
            local = idx.get(rid)
            if local:
                item = self._serialize_game(local)
            else:
                item = {
                    'rom_id': rid,
                    'name': rom.get('name') or rom.get('fs_name') or 'Unknown',
                    'platform': rom.get('platform_name'),
                    'is_downloaded': False,
                    'has_cover': bool(rom.get('path_cover_small')),
                    'platform_slug': rom.get('platform_slug'),
                }
            # RomM's Home paints continue-playing cards with the game's
            # screenshot (landscape) and floats the box-art as a PIP. Carry the
            # first merged screenshot path so the frontend can do the same; the
            # frontend base64s it through get_image, like the screenshots tab.
            shots = [s for s in (rom.get('merged_screenshots') or []) if s]
            item['screenshot'] = shots[0] if shots else None
            out.append(item)
        return out

    async def get_home_data(self):
        """Home dashboard payload: library snapshot stats + a recently-added row.

        Modeled on RomM's v2 Home (WidgetBar + 'Continue playing' + 'Recently
        added' CardRows). Stats/recent come from the local library cache;
        continue-playing is pulled live from RomM (per-user, cross-device).
        """
        try:
            games = self._available_games or []
            total = len(games)
            downloaded = sum(1 for g in games if g.get('is_downloaded'))
            platforms = len({g.get('platform_slug') for g in games if g.get('platform_slug')})
            collections = len(self._romm_collections or [])
            # Recently added: newest created_at first; fall back to library order
            # (RomM returns roms newest-first) when timestamps are missing.
            def _key(g):
                return g.get('created_at') or ''
            has_dates = any(g.get('created_at') for g in games)
            ordered = sorted(games, key=_key, reverse=True) if has_dates else list(games)
            recent = [self._serialize_game(g) for g in ordered[:15]]
            continue_playing = await asyncio.to_thread(self._fetch_continue_playing, 15)
            return {
                'success': True,
                'stats': {
                    'games': total,
                    'downloaded': downloaded,
                    'platforms': platforms,
                    'collections': collections,
                },
                'recent': recent,
                'continue_playing': continue_playing,
            }
        except Exception as e:
            logging.error(f"get_home_data error: {e}", exc_info=True)
            return {'success': False, 'stats': {}, 'recent': [], 'continue_playing': [], 'message': str(e)}

    async def launch_game(self, rom_id: int, disc: str = None, sibling_rom_id: int = None):
        """Launch a downloaded game in RetroArch (A button on a downloaded card).

        `disc` optionally names a specific disc file inside a multi-disc folder
        (from get_local_discs). When omitted, the last disc the user launched is
        resumed; absent that, the .m3u playlist is preferred (in-game swap),
        falling back to the first disc.
        `sibling_rom_id` optionally overrides the ROM to launch — used when the
        user picks a regional variant from the region picker.
        """
        try:
            idx = self._games_index()
            effective_rom_id = sibling_rom_id or rom_id
            g = idx.get(effective_rom_id)
            if not g or not g.get('is_downloaded') or not g.get('local_path'):
                # Fallback: sibling ROM may be on disk but not in the index (e.g.,
                # downloaded in a previous session). Try resolving from the API.
                download_dir = Path(self._settings.get('Download', 'rom_directory',
                                                        '~/RomMSync/roms')).expanduser()
                try:
                    r = self._romm_client.session.get(
                        urljoin(self._romm_client.base_url, f'/api/roms/{effective_rom_id}'),
                        timeout=15)
                    if r.status_code == 200:
                        d = r.json()
                        slug = d.get('platform_slug', 'Unknown')
                        fn = d.get('fs_name') or f"{d.get('name', 'rom')}.rom"
                        candidate = download_dir / slug / fn
                        if is_path_validly_downloaded(candidate):
                            is_md, dc = _detect_multi_disc(str(candidate), True)
                            g = {
                                'rom_id': effective_rom_id,
                                'name': d.get('name') or 'Unknown',
                                'platform_slug': slug,
                                'file_name': fn,
                                'is_downloaded': True,
                                'local_path': str(candidate),
                                'is_multi_disc': is_md,
                                'disc_count': dc,
                                '_sibling_files': [],
                                'sibling_roms': [],
                            }
                            self._available_games.append(g)
                except Exception:
                    pass
            if not g or not g.get('is_downloaded') or not g.get('local_path'):
                return {'success': False, 'message': 'Game not downloaded'}
            if not self._retroarch:
                return {'success': False, 'message': 'RetroArch not available'}
            # A bare Play resumes the remembered disc; an explicit pick overrides.
            effective_disc = disc or self._get_last_disc(effective_rom_id) or None
            launch_path = self._resolve_launch_path(g['local_path'], effective_disc)
            if not launch_path and effective_disc:
                # Remembered disc vanished — fall back to the default resolution.
                effective_disc = None
                launch_path = self._resolve_launch_path(g['local_path'], None)
            if not launch_path:
                return {'success': False, 'message': 'No launchable file found'}
            # Pull down the latest saves/states from RomM before launching so the
            # session starts from the most recent progress (no-op if download is off).
            if self._auto_sync is not None:
                try:
                    await asyncio.to_thread(self._auto_sync.sync_before_launch, g)
                except Exception as e:
                    logging.warning(f"pre-launch sync failed (continuing): {e}")
            platform_name = self._platform_name_for(g)
            ok, msg = self._retroarch.launch_game(Path(launch_path), platform_name)
            # Remember an explicit disc choice so the next plain Play resumes it.
            if ok and disc:
                try:
                    self._settings.set('LastDisc', str(effective_rom_id), disc)
                except Exception as e:
                    logging.warning(f"could not persist last disc: {e}")
            return {'success': bool(ok), 'message': msg or ('Launched' if ok else 'Launch failed')}
        except Exception as e:
            logging.error(f"launch_game error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    async def prepare_steam_launch(self, rom_id: int, disc: str = None,
                                   sibling_rom_id: int = None):
        """Resolve a game's emulator argv and write a launch-spec for the Steam
        session-host tile (Steam Deck Gaming Mode only).

        On gamescope the Steam overlay only renders over a game Steam itself
        launched. So instead of spawning the emulator from the Decky daemon, the
        frontend triggers SteamClient.Apps.RunGame on the "RomM" tile, whose exe
        is bin/romm-session-host. This method does everything launch_game does
        *except* the final spawn: it runs the pre-launch save-sync and resolves
        the exact argv, then writes it to the spec file the host reads and execs.

        Returns {success, message, steam_host: bool}. When steam_host is False
        the caller should fall back to launch_game (e.g. not under gamescope, or
        the spec couldn't be written).
        """
        try:
            if not (self._retroarch and self._retroarch._gamescope_running()):
                return {'success': False, 'steam_host': False,
                        'message': 'Not running under gamescope'}
            idx = self._games_index()
            effective_rom_id = sibling_rom_id or rom_id
            g = idx.get(effective_rom_id)
            if not g or not g.get('is_downloaded') or not g.get('local_path'):
                return {'success': False, 'steam_host': False,
                        'message': 'Game not downloaded'}
            effective_disc = disc or self._get_last_disc(effective_rom_id) or None
            launch_path = self._resolve_launch_path(g['local_path'], effective_disc)
            if not launch_path and effective_disc:
                effective_disc = None
                launch_path = self._resolve_launch_path(g['local_path'], None)
            if not launch_path:
                return {'success': False, 'steam_host': False,
                        'message': 'No launchable file found'}
            if self._auto_sync is not None:
                try:
                    await asyncio.to_thread(self._auto_sync.sync_before_launch, g)
                except Exception as e:
                    logging.warning(f"pre-launch sync failed (continuing): {e}")
            platform_name = self._platform_name_for(g)
            cmd, err = self._retroarch.build_launch_command(Path(launch_path), platform_name)
            if err or not cmd:
                return {'success': False, 'steam_host': False,
                        'message': err or 'Could not resolve launch command'}
            # The host runs under Steam, so it already has the correct display
            # (:1), session vars and the real overlay LD_PRELOAD. We deliberately
            # pass NO env — overriding it would clobber Steam's overlay preload.
            spec = {'argv': [str(c) for c in cmd], 'rom_name': g.get('name', ''),
                    'ts': time.time()}
            spec_path = (Path.home() / '.config' / 'romm-retroarch-sync'
                         / 'session' / 'launch-spec.json')
            spec_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = spec_path.with_suffix('.json.tmp')
            tmp.write_text(json.dumps(spec), encoding='utf-8')
            os.replace(tmp, spec_path)
            # Remember an explicit disc choice so the next plain Play resumes it.
            if disc:
                try:
                    self._settings.set('LastDisc', str(effective_rom_id), disc)
                except Exception as e:
                    logging.warning(f"could not persist last disc: {e}")
            logging.info(f"prepare_steam_launch: wrote spec for '{g.get('name')}' "
                         f"argv={cmd}")
            return {'success': True, 'steam_host': True, 'message': 'Spec ready'}
        except Exception as e:
            logging.error(f"prepare_steam_launch error: {e}", exc_info=True)
            return {'success': False, 'steam_host': False, 'message': str(e)}

    async def get_session_host_path(self):
        """Absolute path to bin/romm-session-host — the exe the RomM Steam tile
        runs so the emulator launches as a child of a Steam-tracked game (overlay
        works). Returns {path} or {path: ''} if the script is missing.
        """
        try:
            host = Path(__file__).resolve().parent / 'bin' / 'romm-session-host'
            if host.is_file():
                try:
                    if not os.access(host, os.X_OK):
                        os.chmod(host, 0o755)
                except Exception:
                    pass
                return {'path': str(host)}
        except Exception as e:
            logging.error(f"get_session_host_path error: {e}")
        return {'path': ''}

    async def get_bios_status(self):
        """Get detailed BIOS download status for all platforms.

        Returns:
            dict with BIOS status including downloading, ready, and failed platforms
        """
        try:
            if self._bios_tracking:
                return self._bios_tracking.get_status()
            else:
                return {
                    'downloading_count': 0,
                    'ready_count': 0,
                    'failed_count': 0,
                    'downloading': [],
                    'ready': [],
                    'failures': {},
                    'platforms': {},
                    'total_platforms': 0,
                    'platforms_ready': 0,
                    'manual_platforms': 0,
                }
        except Exception as e:
            logging.error(f"get_bios_status error: {e}", exc_info=True)
            return {
                'downloading_count': 0,
                'ready_count': 0,
                'failed_count': 0,
                'downloading': [],
                'ready': [],
                'failures': {},
                'platforms': {},
                'total_platforms': 0,
                'platforms_ready': 0,
                'manual_platforms': 0,
                'error': str(e)
            }

    # -----------------------------------------------------------------------
    # Steam shortcut integration
    # -----------------------------------------------------------------------

    async def toggle_collection_steam_sync(self, collection_name: str, enabled: bool):
        """Enable or disable Steam shortcut sync for a specific collection.

        When enabled, creates Steam shortcuts for all downloaded ROMs in the
        collection. When disabled, removes the shortcuts. Auto-sync keeps
        shortcuts updated as ROMs are added/removed.
        """
        try:
            if not self._steam_manager:
                return {'success': False, 'message': 'Steam manager not available'}

            if not self._steam_manager.is_available():
                return {'success': False, 'message': 'Steam userdata not found'}

            # Update the settings
            steam_collections = self._steam_manager.get_steam_sync_collections()
            if enabled:
                steam_collections.add(collection_name)
            else:
                steam_collections.discard(collection_name)
            self._steam_manager.set_steam_sync_collections(steam_collections)

            if self._syncing_steam_collections is None:
                self._syncing_steam_collections = set()
            self._syncing_steam_collections.add(collection_name)
            try:
                if enabled:
                    # Find collection ID and fetch ROMs
                    collection_id = None
                    for col in (self._romm_collections or []):
                        if col.get('name') == collection_name:
                            collection_id = col.get('id')
                            break

                    if collection_id is None:
                        return {'success': False, 'message': f"Collection '{collection_name}' not found"}

                    client = self._romm_client
                    if not client or not client.authenticated:
                        return {'success': False, 'message': 'Not connected to RomM'}

                    collection_roms = client.get_collection_roms(collection_id)
                    download_dir = Path(self._settings.get('Download', 'rom_directory',
                                                            '~/RomMSync/roms')).expanduser()

                    added, msg = self._steam_manager.add_collection_shortcuts(
                        collection_name, collection_roms, str(download_dir))
                    logging.info(f"Steam sync enabled for '{collection_name}': {msg}")
                    return {'success': True, 'message': msg, 'shortcuts_added': added}
                else:
                    removed, msg = self._steam_manager.remove_collection_shortcuts(collection_name)
                    logging.info(f"Steam sync disabled for '{collection_name}': {msg}")
                    return {'success': True, 'message': msg, 'shortcuts_removed': removed}
            finally:
                self._syncing_steam_collections.discard(collection_name)

        except Exception as e:
            logging.error(f"toggle_collection_steam_sync error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}

    async def toggle_steam_integration(self, enabled: bool):
        """Enable or disable Steam integration globally.

        When enabled, adds Steam shortcuts for all currently synced collections.
        When disabled, removes all Steam shortcuts for all synced collections.
        """
        try:
            import configparser
            ini_path = Path.home() / '.config' / 'romm-retroarch-sync' / 'settings.ini'
            if not ini_path.exists():
                logging.error("Settings file not found")
                return {'success': False, 'message': 'Settings file not found'}

            config = configparser.ConfigParser()
            config.read(ini_path)
            if not config.has_section('Steam'):
                config.add_section('Steam')

            config.set('Steam', 'enabled', str(enabled).lower())

            total_added = 0
            total_removed = 0
            collections_count = 0

            if self._steam_manager and self._steam_manager.is_available():
                if enabled:
                    # When enabling, add Steam shortcuts for all currently synced collections
                    actively_syncing = config.get('Collections', 'actively_syncing', fallback='')
                    synced_collections = {c for c in actively_syncing.split('|') if c}

                    if synced_collections and self._romm_client and self._romm_client.authenticated:
                        for collection_name in synced_collections:
                            try:
                                # Enable Steam sync for this collection
                                steam_collections = self._steam_manager.get_steam_sync_collections()
                                steam_collections.add(collection_name)
                                self._steam_manager.set_steam_sync_collections(steam_collections)

                                # Find collection and add shortcuts
                                collection_id = None
                                for col in (self._romm_collections or []):
                                    if col.get('name') == collection_name:
                                        collection_id = col.get('id')
                                        break

                                if collection_id:
                                    collection_roms = self._romm_client.get_collection_roms(collection_id)
                                    download_dir = Path(self._settings.get('Download', 'rom_directory',
                                                                            '~/RomMSync/roms')).expanduser()
                                    added, msg = self._steam_manager.add_collection_shortcuts(
                                        collection_name, collection_roms, str(download_dir))
                                    total_added += added
                                    collections_count += 1
                                    logging.info(f"Added {added} shortcuts for '{collection_name}'")
                            except Exception as e:
                                logging.warning(f"Error adding shortcuts for {collection_name}: {e}")
                else:
                    # When disabling, clean up all Steam shortcuts
                    steam_collections = self._steam_manager.get_steam_sync_collections().copy()
                    if steam_collections:
                        for collection_name in steam_collections:
                            try:
                                removed, msg = self._steam_manager.remove_collection_shortcuts(collection_name)
                                total_removed += removed
                                collections_count += 1
                                logging.info(f"Removed {removed} shortcuts for '{collection_name}'")
                            except Exception as e:
                                logging.warning(f"Error removing shortcuts for {collection_name}: {e}")

                        # Clear the Steam sync collections list
                        self._steam_manager.set_steam_sync_collections(set())

            with open(ini_path, 'w') as f:
                config.write(f)

            # Update in-memory settings
            if self._settings:
                self._settings.load_settings()

            if enabled:
                if collections_count > 0:
                    message = f"Steam integration enabled — added {total_added} shortcuts from {collections_count} synced collections"
                else:
                    message = "Steam integration enabled — no collections currently synced"
            else:
                if collections_count > 0:
                    message = f"Steam integration disabled — removed {total_removed} shortcuts from {collections_count} collections"
                else:
                    message = "Steam integration disabled"

            logging.info(message)
            return {'success': True, 'message': message}

        except Exception as e:
            logging.error(f"toggle_steam_integration error: {e}", exc_info=True)
            return {'success': False, 'message': str(e)}
