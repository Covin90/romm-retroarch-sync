import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

# Add py_modules to path so sync_core is importable
sys.path.insert(0, str(Path(__file__).parent / "py_modules"))

try:
    from sync_core import (
        SettingsManager, RomMClient, RetroArchInterface,
        AutoSyncManager, CollectionSyncManager,
        GameListPollingManager, BiosTrackingManager,
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

    # New manager instances (handle polling and BIOS tracking)
    _game_polling: 'GameListPollingManager' = None
    _bios_tracking: 'BiosTrackingManager' = None

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def _main(self):
        self._available_games = []
        self._romm_collections = None
        self._connection_attempted = False
        self._platform_slug_to_name = {}
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
        logging.info(f"RetroArch interface created, has bios_manager: {hasattr(self._retroarch, 'bios_manager')}")
        if hasattr(self._retroarch, 'bios_manager'):
            logging.info(f"bios_manager value: {self._retroarch.bios_manager}")
        if self._available_games is None:
            self._available_games = []
        self._connection_attempted = False

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
        if self._game_polling:
            self._game_polling.stop()
            self._game_polling = None
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
        self._connection_attempted = False
        self._disabled_collection_counts.clear()

        logging.info("Sync stopped")

    def _connect_to_romm(self):
        """Connect to RomM, load game list, and start AutoSyncManager."""
        url      = self._settings.get('RomM', 'url')
        username = self._settings.get('RomM', 'username')
        password = self._settings.get('RomM', 'password')
        remember     = self._settings.get('RomM', 'remember_credentials') == 'true'
        auto_connect = self._settings.get('RomM', 'auto_connect') == 'true'

        if not (url and username and password and remember and auto_connect):
            logging.info("Auto-connect disabled or credentials missing")
            return False

        try:
            logging.info(f"Connecting to RomM at {url}...")
            self._romm_client = RomMClient(url, username, password)
            if not self._romm_client.authenticated:
                logging.error("RomM authentication failed")
                return False

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
                    self._available_games.append({
                        'name':          Path(file_name).stem if file_name else rom.get('name', 'Unknown'),
                        'rom_id':        rom.get('id'),
                        'platform':      rom.get('platform_name', 'Unknown'),
                        'platform_slug': platform_slug,
                        'file_name':     file_name,
                        'is_downloaded': is_downloaded,
                        'local_path':    str(local_path) if is_downloaded else None,
                        'local_size':    local_size,
                        'romm_data': {
                            'fs_name':         rom.get('fs_name'),
                            'fs_name_no_ext':  rom.get('fs_name_no_ext'),
                            'fs_size_bytes':   rom.get('fs_size_bytes', 0),
                            'platform_id':     rom.get('platform_id'),
                            'platform_slug':   rom.get('platform_slug'),
                        },
                    })
                logging.info(f"Loaded {len(self._available_games)} games")

                # Set initial timestamp for efficient future polling
                self._last_full_fetch_time = datetime.now(timezone.utc).isoformat()

                # Initialize GameListPollingManager
                self._game_polling = GameListPollingManager(
                    romm_client=self._romm_client,
                    settings=self._settings,
                    available_games_list=self._available_games,
                    platform_slug_to_name=self._platform_slug_to_name,
                    log_callback=lambda msg: logging.info(f"[POLLING] {msg}"),
                    update_callback=None,  # Optional: add callback if needed
                )
                self._game_polling.set_last_poll_time(self._last_full_fetch_time)
                self._game_polling.start()

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
                self._disabled_collection_counts[name] = {'rom_ids': rom_ids, 'total': len(roms)}
                logging.debug(f"Fetched disabled count for '{name}': {len(roms)} total")
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
                    new_collections = self._romm_client.get_collections(updated_after=updated_after)

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
            except Exception as e:
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

            connected = bool(self._romm_client and self._romm_client.authenticated)

            if not connected:
                # _connection_attempted becomes True once _connect_to_romm() finishes.
                # Before that we're still starting; after that we genuinely failed.
                # The frontend uses details.last_update to decide whether to show
                # the "not connected / retry" warning (same key as before).
                details = {'last_update': time.time()} if self._connection_attempted else {}
                return {
                    'status':                  'running',
                    'message':                 "Connecting to RomM...",
                    'details':                 details,
                    'collections':             [],
                    'collection_count':        0,
                    'actively_syncing_count':  0,
                }

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
            )

            game_count             = status.get('game_count', 0)
            collections            = status.get('collections', [])
            collection_count       = status.get('collection_count', 0)
            actively_syncing_count = status.get('actively_syncing_count', 0)
            bios_status            = status.get('bios_status', {})

            # Show "Fetching games..." until initial fetch completes
            if self._last_full_fetch_time is None:
                message = "Fetching games..."
            else:
                message = f"{game_count} games, {collection_count} collections"

            return {
                'status':                  'connected',
                'message':                 message,
                'details':                 status,
                'collections':             collections,
                'collection_count':        collection_count,
                'actively_syncing_count':  actively_syncing_count,
                'bios_status':             bios_status,
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
                                'romm_data': {
                                    'fs_name': rom.get('fs_name'),
                                    'fs_name_no_ext': rom.get('fs_name_no_ext'),
                                    'fs_size_bytes': rom.get('fs_size_bytes', 0),
                                    'platform_id': rom.get('platform_id'),
                                    'platform_slug': rom.get('platform_slug'),
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

                        self._available_games.append({
                            'name': Path(file_name).stem if file_name else rom.get('name', 'Unknown'),
                            'rom_id': rom.get('id'),
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
                        })
                    logging.info(f"Full refresh: loaded {len(self._available_games)} games")

            # Update timestamp for both local tracking and polling manager
            self._last_full_fetch_time = current_time
            if self._game_polling:
                self._game_polling.set_last_poll_time(current_time)

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
                    self._disabled_collection_counts[collection_name] = {
                        'rom_ids': set(rom_ids),
                        'total':   len(rom_ids),
                    }

            config.set('Collections', 'actively_syncing',  '|'.join(sorted(sync_set)))
            config.set('Collections', 'selected_for_sync', '|'.join(sorted(sync_set)))
            config.set('Collections', 'auto_sync_enabled', 'true' if sync_set else 'false')

            with open(ini_path, 'w') as f:
                config.write(f)

            # Update in-memory settings so the heartbeat sees the change immediately
            if self._settings:
                self._settings.load_settings()

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

    async def delete_collection_roms(self, collection_name: str):
        """Delete all local ROM files for a collection.

        toggle_collection_sync already handled settings + sync object updates
        before this is called, so this method only does the file deletion.
        Uses the existing authenticated client and in-memory caches to avoid
        redundant API calls.
        """
        try:
            import shutil
            logging.info(f"Starting ROM deletion for collection: {collection_name}")

            # Use the already-authenticated client — no new login needed
            client = self._romm_client
            if not client or not client.authenticated:
                logging.error("RomM client not available for ROM deletion")
                return False

            download_dir = Path(self._settings.get('Download', 'rom_directory',
                                                    '~/RomMSync/roms')).expanduser()
            if not download_dir.exists():
                logging.info(f"Download directory not found, nothing to delete: {download_dir}")
                return True

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

            return {
                'url':                url,
                'username':           username,
                'has_password':       has_password,
                'rom_directory':      rom_directory,
                'save_directory':     save_directory,
                'bios_directory':     bios_directory,
                'device_name':        settings.get('Device', 'device_name'),
                'device_name_default': hostname,
                'configured':         bool(url and username and has_password) and not needs_onboarding,
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

    async def get_logging_enabled(self):
        try:
            return load_decky_settings().get('logging_enabled', True)
        except Exception as e:
            logging.error(f"get_logging_enabled error: {e}")
            return True

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
