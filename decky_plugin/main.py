import json
import logging
import sys
import threading
import time
from pathlib import Path

# Add py_modules to path so sync_core is importable
sys.path.insert(0, str(Path(__file__).parent / "py_modules"))

try:
    from sync_core import (
        SettingsManager, RomMClient, RetroArchInterface,
        AutoSyncManager, DaemonCollectionSync,
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

    # -----------------------------------------------------------------------
    # Lifecycle
    # -----------------------------------------------------------------------

    async def _main(self):
        self._available_games = []
        self._romm_collections = None
        self._connection_attempted = False
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
        logging.info("Sync started")

    def _stop_sync(self):
        if self._stop_event:
            self._stop_event.set()
        if self._retry_thread:
            self._retry_thread.join(timeout=5)

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
        """Create and start DaemonCollectionSync from current settings."""
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
        self._collection_sync = DaemonCollectionSync(
            romm_client=self._romm_client,
            settings=self._settings,
            selected_collections=selected_collections,
            sync_interval=sync_interval,
            available_games=self._available_games,
            log_callback=lambda msg: logging.info(f"[COLLECTION-SYNC] {msg}"),
        )
        self._collection_sync.start()

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
                    continue  # enabled — count comes from DaemonCollectionSync cache
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
        or retry the connection if disconnected.  No status building — that happens
        on-demand in get_service_status()."""
        self._connect_to_romm()
        self._connection_attempted = True

        while not self._stop_event.is_set():
            self._stop_event.wait(300)  # sleep 5 minutes (or until _stop_sync wakes us)
            if self._stop_event.is_set():
                break
            try:
                if self._romm_client and self._romm_client.authenticated:
                    self._romm_collections = self._romm_client.get_collections()
                    logging.debug(f"Refreshed collection list: {len(self._romm_collections)} collections")
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
            )

            game_count             = status.get('game_count', 0)
            collections            = status.get('collections', [])
            collection_count       = status.get('collection_count', 0)
            actively_syncing_count = status.get('actively_syncing_count', 0)

            message = f"{game_count} games, {collection_count} collections"

            return {
                'status':                  'connected',
                'message':                 message,
                'details':                 status,
                'collections':             collections,
                'collection_count':        collection_count,
                'actively_syncing_count':  actively_syncing_count,
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
                    threading.Thread(
                        target=old_sync.stop,
                        daemon=True,
                        name="romm-collection-stop",
                    ).start()
            elif enabled and self._romm_client and self._romm_client.authenticated:
                # First collection enabled — create DaemonCollectionSync now
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
            if needs_save:
                settings.set('Download', 'rom_directory',  rom_directory)
                settings.set('Download', 'save_directory', save_directory)
                logging.info(f"Auto-configured RetroDECK paths: ROMs={rom_directory}, "
                             f"saves={save_directory}")

            import socket
            try:
                hostname = socket.gethostname() or 'Steam Deck'
            except Exception:
                hostname = 'Steam Deck'

            ds = load_decky_settings()
            needs_onboarding = ds.get('needs_onboarding', False)

            return {
                'url':                url,
                'username':           username,
                'has_password':       has_password,
                'rom_directory':      rom_directory,
                'save_directory':     save_directory,
                'device_name':        settings.get('Device', 'device_name'),
                'device_name_default': hostname,
                'configured':         bool(url and username and has_password) and not needs_onboarding,
                'retrodeck_detected': retrodeck is not None,
            }
        except Exception as e:
            logging.error(f"get_config error: {e}", exc_info=True)
            return {'configured': False, 'error': str(e)}

    async def save_config(self, url: str, username: str, password: str,
                          rom_directory: str, save_directory: str, device_name: str):
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
        """Delete all downloaded ROMs from synced collections and reset sync state.
        Credentials (URL/username/password) are preserved."""
        import configparser, shutil
        config_dir   = Path.home() / '.config' / 'romm-retroarch-sync'
        ini_path     = config_dir / 'settings.ini'

        try:
            self._stop_sync()
            logging.info("Reset: sync stopped")

            config = configparser.ConfigParser()
            config.read(ini_path)

            download_dir = Path(config.get('Download', 'rom_directory',
                                           fallback='~/RomMSync/roms')).expanduser()
            actively_syncing_str = config.get('Collections', 'actively_syncing', fallback='')
            synced_collections   = [c for c in actively_syncing_str.split('|') if c]

            deleted_roms = 0
            if synced_collections and SYNC_CORE_AVAILABLE and download_dir.exists():
                romm_url = config.get('RomM', 'url',      fallback='')
                username = config.get('RomM', 'username', fallback='')
                password = config.get('RomM', 'password', fallback='')
                if all([romm_url, username, password]):
                    try:
                        client = RomMClient(romm_url, username, password)
                        if client.authenticated:
                            all_collections = client.get_collections()
                            col_id_map      = {c.get('name'): c.get('id') for c in all_collections}
                            for col_name in synced_collections:
                                col_id = col_id_map.get(col_name)
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

            logging.info(f"Reset complete: {deleted_roms} ROM file(s) deleted")
            return {'success': True, 'deleted_roms': deleted_roms}

        except Exception as e:
            logging.error(f"reset_all_settings error: {e}", exc_info=True)
            return {'success': False, 'error': str(e)}

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
