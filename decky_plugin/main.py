import subprocess
import json
import logging
import os
from pathlib import Path

# Set up logging
log_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'decky_debug.log'
log_file.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(log_file),
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

def get_systemd_env():
    """Get proper environment variables for systemd user commands on Steam Deck"""
    env = os.environ.copy()

    # Ensure XDG_RUNTIME_DIR is set (required for systemd --user)
    if 'XDG_RUNTIME_DIR' not in env:
        uid = os.getuid()
        env['XDG_RUNTIME_DIR'] = f'/run/user/{uid}'

    # Try to get DBUS_SESSION_BUS_ADDRESS if not set
    if 'DBUS_SESSION_BUS_ADDRESS' not in env:
        xdg_runtime = env.get('XDG_RUNTIME_DIR', f'/run/user/{os.getuid()}')
        bus_path = Path(xdg_runtime) / 'bus'
        if bus_path.exists():
            env['DBUS_SESSION_BUS_ADDRESS'] = f'unix:path={bus_path}'

    return env

class Plugin:
    async def _main(self):
        logging.info("RomM Sync Monitor starting...")
        return await self.get_service_status()
    
    async def get_service_status(self):
        """Check if the RomM sync service is running"""
        try:
            # Get proper environment for systemd commands
            env = get_systemd_env()

            # Check systemd service status with proper environment
            logging.debug(f"Checking service with env: XDG_RUNTIME_DIR={env.get('XDG_RUNTIME_DIR')}")
            result = subprocess.run(
                ['systemctl', '--user', 'is-active', 'romm-retroarch-sync.service'],
                capture_output=True,
                text=True,
                env=env
            )

            logging.debug(f"systemctl returncode: {result.returncode}, stdout: {result.stdout.strip()}, stderr: {result.stderr.strip()}")

            service_running = result.returncode == 0 and 'active' in result.stdout

            # Fallback: Check if process is running by looking for the status file being recently updated
            status_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'status.json'
            app_status = {}
            status_file_recent = False

            if status_file.exists():
                try:
                    with open(status_file, 'r') as f:
                        app_status = json.load(f)
                    logging.info(f"Status file read: {app_status}")

                    # Check if status file was updated recently (within last 120 seconds)
                    # Increased timeout to handle large collection sync calculations
                    import time
                    file_mtime = status_file.stat().st_mtime
                    status_file_recent = (time.time() - file_mtime) < 300
                    logging.debug(f"Status file age: {time.time() - file_mtime}s, recent: {status_file_recent}")
                except Exception as e:
                    logging.error(f"Failed to read status file: {e}")

            # If systemctl failed but status file is recent and shows running, trust the status file
            # This handles cases where systemctl --user doesn't work properly on Steam Deck
            if not service_running and status_file_recent and app_status.get('running', False):
                logging.info("systemctl check failed but status file indicates service is running")
                service_running = True

            # Combine service and app status
            if service_running:
                if app_status.get('connected', False):
                    # Build message with game count and collections
                    game_count = app_status.get('game_count', 0)
                    collections = app_status.get('collections', [])
                    collection_count = app_status.get('collection_count', 0)
                    actively_syncing_count = app_status.get('actively_syncing_count', 0)

                    message = f"Connected ({game_count} games"
                    if collection_count > 0:
                        message += f", {collection_count} collections"
                    message += ")"

                    return {
                        'status': 'connected',
                        'message': message,
                        'details': app_status,
                        'collections': collections,
                        'collection_count': collection_count,
                        'actively_syncing_count': actively_syncing_count
                    }
                elif app_status.get('running', False):
                    return {
                        'status': 'running',
                        'message': "Running (not connected)",
                        'details': app_status,
                        'collections': app_status.get('collections', []),
                        'collection_count': app_status.get('collection_count', 0),
                        'actively_syncing_count': app_status.get('actively_syncing_count', 0)
                    }
                else:
                    return {
                        'status': 'service_only',
                        'message': "Service active",
                        'details': {},
                        'collections': [],
                        'collection_count': 0
                    }
            else:
                return {
                    'status': 'stopped',
                    'message': "Service stopped",
                    'details': {},
                    'collections': [],
                    'collection_count': 0
                }

        except Exception as e:
            logging.error(f"Status check error: {e}", exc_info=True)
            return {
                'status': 'error',
                'message': f"Error: {str(e)[:50]}",
                'details': {},
                'collections': [],
                'collection_count': 0
            }
    
    async def start_service(self):
        """Start the RomM sync service"""
        try:
            env = get_systemd_env()
            result = subprocess.run(
                ['systemctl', '--user', 'start', 'romm-retroarch-sync.service'],
                capture_output=True,
                text=True,
                env=env
            )
            logging.info(f"start_service returncode: {result.returncode}, stderr: {result.stderr}")
            return result.returncode == 0
        except Exception as e:
            logging.error(f"start_service error: {e}")
            return False

    async def stop_service(self):
        """Stop the RomM sync service"""
        try:
            env = get_systemd_env()
            result = subprocess.run(
                ['systemctl', '--user', 'stop', 'romm-retroarch-sync.service'],
                capture_output=True,
                text=True,
                env=env
            )
            logging.info(f"stop_service returncode: {result.returncode}, stderr: {result.stderr}")
            return result.returncode == 0
        except Exception as e:
            logging.error(f"stop_service error: {e}")
            return False

    async def toggle_collection_sync(self, collection_name: str, enabled: bool):
        """Enable or disable auto-sync for a specific collection"""
        try:
            import configparser
            from pathlib import Path

            settings_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'settings.ini'

            if not settings_file.exists():
                logging.error("Settings file not found")
                return False

            config = configparser.ConfigParser()
            config.read(settings_file)

            # Ensure Collections section exists
            if not config.has_section('Collections'):
                config.add_section('Collections')

            # Get current actively syncing collections
            actively_syncing = config.get('Collections', 'actively_syncing', fallback='')
            sync_set = set(actively_syncing.split('|')) if actively_syncing else set()
            sync_set.discard('')  # Remove empty strings

            # Toggle the collection
            if enabled:
                sync_set.add(collection_name)
                logging.info(f"Enabling auto-sync for collection: {collection_name}")
            else:
                sync_set.discard(collection_name)
                logging.info(f"Disabling auto-sync for collection: {collection_name}")

            # Update settings
            config.set('Collections', 'actively_syncing', '|'.join(sorted(sync_set)))

            # Enable auto_sync_enabled if at least one collection is syncing
            if sync_set:
                config.set('Collections', 'auto_sync_enabled', 'true')
            else:
                config.set('Collections', 'auto_sync_enabled', 'false')

            # Write back to file
            with open(settings_file, 'w') as f:
                config.write(f)

            logging.info(f"Updated settings: actively_syncing={config.get('Collections', 'actively_syncing')}")

            # Trigger immediate daemon reload by creating a flag file
            try:
                reload_flag = Path.home() / '.config' / 'romm-retroarch-sync' / '.reload_trigger'
                reload_flag.touch()
                logging.info(f"✅ Created reload trigger file: {reload_flag}")
            except Exception as e:
                logging.error(f"Error creating reload trigger: {e}", exc_info=True)

            return True

        except Exception as e:
            logging.error(f"toggle_collection_sync error: {e}", exc_info=True)
            return False

    async def delete_collection_roms(self, collection_name: str):
        """Delete all ROMs for a specific collection to allow re-sync from scratch"""
        try:
            import configparser
            logging.info(f"🗑️ Starting ROM deletion for collection: {collection_name}")

            # Load settings to get download directory
            settings_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'settings.ini'
            if not settings_file.exists():
                logging.error("Settings file not found")
                return False

            config = configparser.ConfigParser()
            config.read(settings_file)

            # Get download directory (from Download section, fallback to ~/retrodeck/roms)
            download_dir = config.get('Download', 'rom_directory', fallback='~/retrodeck/roms')
            download_dir = Path(download_dir).expanduser()

            if not download_dir.exists():
                logging.error(f"Download directory not found: {download_dir}")
                return False

            # Read status file to get collection ID
            status_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'status.json'
            if not status_file.exists():
                logging.error("Status file not found")
                return False

            with open(status_file, 'r') as f:
                status_data = json.load(f)

            # Find the collection by name
            collection_id = None
            for col in status_data.get('collections', []):
                if col['name'] == collection_name:
                    collection_id = col['id']
                    break

            if collection_id is None:
                logging.error(f"Collection '{collection_name}' not found in status")
                return False

            # Use urllib (standard library) to fetch collection ROMs from RomM API
            import urllib.request
            import urllib.error
            import base64

            # Get RomM credentials
            romm_url = config.get('RomM', 'url', fallback='')
            username = config.get('RomM', 'username', fallback='')
            password = config.get('RomM', 'password', fallback='')

            if not all([romm_url, username, password]):
                logging.error("Missing RomM credentials in settings")
                return False

            # Fetch collection ROMs using urllib
            api_url = f"{romm_url.rstrip('/')}/api/roms?collection_id={collection_id}"
            logging.info(f"Fetching ROMs from: {api_url}")

            try:
                # Create SSL context that doesn't verify certificates (like the daemon does)
                import ssl
                ssl_context = ssl.create_default_context()
                ssl_context.check_hostname = False
                ssl_context.verify_mode = ssl.CERT_NONE

                # Create request with Basic Auth
                credentials = base64.b64encode(f"{username}:{password}".encode()).decode()
                req = urllib.request.Request(api_url)
                req.add_header('Authorization', f'Basic {credentials}')

                with urllib.request.urlopen(req, context=ssl_context) as response:
                    data = json.loads(response.read().decode())
                    collection_roms = data.get('items', [])

                deleted_count = 0

                # Delete each ROM file that belongs to this collection only
                for rom in collection_roms:
                    platform_slug = rom.get('platform_slug', '')
                    file_name = rom.get('fs_name') or rom.get('file_name', '')

                    if platform_slug and file_name:
                        platform_dir = download_dir / platform_slug
                        rom_path = platform_dir / file_name

                        if rom_path.exists() and rom_path.is_file():
                            try:
                                rom_path.unlink()
                                deleted_count += 1
                                logging.info(f"  🗑️ Deleted: {rom_path}")
                            except Exception as e:
                                logging.error(f"  ❌ Failed to delete {rom_path}: {e}")

                logging.info(f"✅ Deleted {deleted_count} ROM(s) from collection '{collection_name}'")

            except urllib.error.HTTPError as e:
                logging.error(f"HTTP error fetching collection ROMs: {e.code} - {e.reason}")
                return False
            except Exception as e:
                logging.error(f"Error fetching collection ROMs: {e}")
                return False

            # Disable auto-sync for this collection
            if not config.has_section('Collections'):
                config.add_section('Collections')

            actively_syncing = config.get('Collections', 'actively_syncing', fallback='')
            sync_set = set(actively_syncing.split('|')) if actively_syncing else set()
            sync_set.discard('')  # Remove empty strings
            sync_set.discard(collection_name)  # Remove this collection

            # Update settings
            config.set('Collections', 'actively_syncing', '|'.join(sorted(sync_set)))

            # Disable auto_sync_enabled if no collections are syncing
            if not sync_set:
                config.set('Collections', 'auto_sync_enabled', 'false')

            # Write back to file
            with open(settings_file, 'w') as f:
                config.write(f)

            logging.info(f"🔴 Disabled auto-sync for collection: {collection_name}")

            # Trigger daemon reload
            try:
                reload_flag = Path.home() / '.config' / 'romm-retroarch-sync' / '.reload_trigger'
                reload_flag.touch()
                logging.info(f"✅ Created reload trigger file")
            except Exception as e:
                logging.error(f"Error creating reload trigger: {e}")

            return True

        except Exception as e:
            logging.error(f"delete_collection_roms error: {e}", exc_info=True)
            return False