#!/usr/bin/env python3
"""
BIOS Manager for RomM-RetroArch Sync
Handles BIOS detection, verification, and synchronization
"""

from pathlib import Path
import hashlib
import logging

class BiosManager:
    """Manages BIOS files for RetroArch cores"""
    
    def __init__(self, retroarch_interface, romm_client=None, log_callback=None, settings=None):
        self.retroarch = retroarch_interface
        self.romm_client = romm_client
        self.log = log_callback or print
        self.settings = settings  # Use passed settings instead of creating new one
        
        # Find system directory
        self.system_dir = self.find_system_directory()
        
        # Cache for installed BIOS files
        self.installed_bios = {}
        self.scan_installed_bios()
        
        # Platform name normalization map
        self.platform_aliases = {
            'playstation': 'Sony - PlayStation',
            'ps1': 'Sony - PlayStation',
            'psx': 'Sony - PlayStation',
            'playstation-2': 'Sony - PlayStation 2',
            'ps2': 'Sony - PlayStation 2',
            'sega-saturn': 'Sega - Saturn',
            'saturn': 'Sega - Saturn',
            'sega-cd': 'Sega - Mega-CD - Sega CD',
            'mega-cd': 'Sega - Mega-CD - Sega CD',
            'segacd': 'Sega - Mega-CD - Sega CD',
            'dreamcast': 'Sega - Dreamcast',
            'dc': 'Sega - Dreamcast',
            'neo-geo': 'SNK - Neo Geo',
            'neogeo': 'SNK - Neo Geo',
            'nintendo-ds': 'Nintendo - Nintendo DS',
            'nds': 'Nintendo - Nintendo DS',
            'game-boy-advance': 'Nintendo - Game Boy Advance',
            'gba': 'Nintendo - Game Boy Advance',
            'pc-engine': 'NEC - PC Engine - TurboGrafx 16',
            'turbografx': 'NEC - PC Engine - TurboGrafx 16',
            'turbografx-16': 'NEC - PC Engine - TurboGrafx 16',
            'pce': 'NEC - PC Engine - TurboGrafx 16',
            'atari-7800': 'Atari - 7800',
            'atari-lynx': 'Atari - Lynx',
            'lynx': 'Atari - Lynx',
            '3do': '3DO',
            'msx': 'Microsoft - MSX',
            'msx2': 'Microsoft - MSX',
            'amiga': 'Commodore - Amiga',
            'psp': 'Sony - PlayStation Portable',
            'playstation-portable': 'Sony - PlayStation Portable',
            '3ds': 'Nintendo - Nintendo 3DS',
            'nintendo-3ds': 'Nintendo - Nintendo 3DS',
        }

    def refresh_system_directory(self):
        """Refresh system directory path (useful when settings change)"""
        self.system_dir = self.find_system_directory()
        if self.system_dir:
            self.scan_installed_bios()
            self.log(f"📁 BIOS directory refreshed: {self.system_dir}")
        else:
            self.log("⚠️ No BIOS directory found after refresh")

    def find_system_directory(self):
        """Find RetroArch system/BIOS directory"""
        # Check for custom BIOS path override first
        if self.settings:
            custom_bios_path = self.settings.get('BIOS', 'custom_path', '').strip()
            if custom_bios_path:  # Only use if not empty
                custom_dir = Path(custom_bios_path)
                if custom_dir.exists():
                    self.log(f"📁 Using custom BIOS directory: {custom_dir}")
                    return custom_dir
                else:
                    # Try to create it
                    try:
                        custom_dir.mkdir(parents=True, exist_ok=True)
                        self.log(f"📁 Created custom BIOS directory: {custom_dir}")
                        return custom_dir
                    except Exception as e:
                        self.log(f"❌ Failed to create custom BIOS directory: {e}")
                        self.log("⚠️ Falling back to auto-detection")

        possible_dirs = [
            # RetroDECK
            Path.home() / 'retrodeck' / 'bios',
            Path.home() / '.var/app/net.retrodeck.retrodeck/config/retroarch/system',
            
            # Flatpak RetroArch
            Path.home() / '.var/app/org.libretro.RetroArch/config/retroarch/system',
            
            # Native installations
            Path.home() / '.config/retroarch/system',
            Path.home() / '.retroarch/system',
            
            # Steam installations
            Path.home() / '.steam/steam/steamapps/common/RetroArch/system',
            Path.home() / '.local/share/Steam/steamapps/common/RetroArch/system',
            Path.home() / '.var/app/com.valvesoftware.Steam/.local/share/Steam/steamapps/common/RetroArch/system',
            
            # Snap
            Path.home() / 'snap/retroarch/current/.config/retroarch/system',
            
            # AppImage
            Path.home() / '.retroarch-appimage/system',
        ]
        
        # Check for custom RetroArch path override
        if hasattr(self.retroarch, 'settings'):
            custom_path = self.retroarch.settings.get('RetroArch', 'custom_path', '').strip()
            if custom_path:
                custom_dir = Path(custom_path).parent
                possible_dirs.insert(0, custom_dir / 'system')
                possible_dirs.insert(0, custom_dir / 'bios')  # RetroDECK style
        
        for system_dir in possible_dirs:
            if system_dir.exists():
                self.log(f"📁 Found system/BIOS directory: {system_dir}")
                return system_dir
        
        # Create RetroDECK bios directory if RetroDECK is detected
        retrodeck_bios = Path.home() / 'retrodeck' / 'bios'
        if Path.home() / 'retrodeck' / 'roms' and not retrodeck_bios.exists():
            try:
                retrodeck_bios.mkdir(parents=True, exist_ok=True)
                self.log(f"📁 Created RetroDECK BIOS directory: {retrodeck_bios}")
                return retrodeck_bios
            except Exception as e:
                self.log(f"Failed to create RetroDECK BIOS directory: {e}")
        
        self.log("⚠️ No RetroArch system/BIOS directory found")
        return None
    
    def calculate_md5(self, file_path):
        """Calculate MD5 hash of a file"""
        md5 = hashlib.md5()
        try:
            with open(file_path, 'rb') as f:
                for chunk in iter(lambda: f.read(8192), b''):
                    md5.update(chunk)
            return md5.hexdigest()
        except Exception as e:
            self.log(f"Error calculating MD5 for {file_path}: {e}")
            return None
    
    def scan_installed_bios(self):
        """Scan for installed BIOS files"""
        self.installed_bios = {}
        
        if not self.system_dir:
            return
        
        try:
            # Scan all files in system directory
            for file_path in self.system_dir.rglob('*'):
                if file_path.is_file():
                    # Skip very large files (likely not BIOS)
                    if file_path.stat().st_size > 50 * 1024 * 1024:  # 50MB
                        continue
                    
                    relative_path = file_path.relative_to(self.system_dir)
                    self.installed_bios[str(relative_path)] = {
                        'path': str(file_path),
                        'size': file_path.stat().st_size,
                        'modified': file_path.stat().st_mtime,
                        'md5': None  # Calculate on demand to speed up scanning
                    }

        except Exception as e:
            self.log(f"Error scanning BIOS directory: {e}")
    
    def normalize_platform_name(self, platform_name):
        """Normalize platform name using aliases"""
        if not platform_name:
            return None

        # Convert to lowercase for comparison
        platform_lower = platform_name.lower().replace('_', '-')

        # Check aliases
        if platform_lower in self.platform_aliases:
            return self.platform_aliases[platform_lower]

        return platform_name  # Return original if no match found

    def get_server_firmware_for_platform(self, platform_name):
        """Query RomM server for available firmware files for a platform

        Args:
            platform_name: The platform name to query

        Returns:
            List of firmware file dictionaries with 'file_name' and 'id' fields,
            or None if server unavailable or platform not found
        """
        if not self.romm_client or not self.romm_client.authenticated:
            logging.debug("[BIOS] No RomM client or not authenticated")
            return None

        try:
            from urllib.parse import urljoin

            # Get all platforms from server
            logging.debug(f"[BIOS] Querying server for platform: {platform_name}")
            platforms_response = self.romm_client.session.get(
                urljoin(self.romm_client.base_url, '/api/platforms'),
                timeout=10
            )

            if platforms_response.status_code != 200:
                logging.debug(f"[BIOS] Server returned status {platforms_response.status_code}")
                return None

            platforms = platforms_response.json()
            logging.debug(f"[BIOS] Got {len(platforms)} platforms from server")

            # Platform name variations for matching
            platform_mappings = {
                'Sony - PlayStation': ['PlayStation', 'Sony PlayStation', 'PS1', 'PSX'],
                'Sony - PlayStation 2': ['PlayStation 2', 'Sony PlayStation 2', 'PS2'],
                'Nintendo - Game Boy Advance': ['Game Boy Advance', 'GBA', 'Nintendo Game Boy Advance'],
                'Nintendo - Game Boy': ['Game Boy', 'GB', 'Nintendo Game Boy'],
                'Nintendo - Game Boy Color': ['Game Boy Color', 'GBC', 'Nintendo Game Boy Color'],
                'Nintendo - Nintendo DS': ['Nintendo DS', 'DS', 'NDS'],
                'Nintendo - Nintendo 3DS': ['Nintendo 3DS', '3DS', 'N3DS'],
                'Sega - Saturn': ['Sega Saturn', 'Saturn', 'SS'],
                'Sega - Dreamcast': ['Sega Dreamcast', 'Dreamcast', 'DC'],
                'Sega - Mega-CD - Sega CD': ['Sega CD', 'Mega CD', 'Mega-CD'],
                'SNK - Neo Geo': ['Neo Geo', 'NeoGeo', 'Neo-Geo'],
                'NEC - PC Engine - TurboGrafx 16': ['PC Engine', 'TurboGrafx', 'TurboGrafx-16', 'TG-16', 'PCE'],
                'Atari - 7800': ['Atari 7800', '7800'],
                'Atari - Lynx': ['Atari Lynx', 'Lynx']
            }

            possible_names = platform_mappings.get(platform_name, [platform_name])
            logging.debug(f"[BIOS] Searching for matches: {possible_names}")

            # Find matching platform
            for platform in platforms:
                platform_name_check = platform.get('name', '')

                if any(name.lower() in platform_name_check.lower() or
                      platform_name_check.lower() in name.lower()
                      for name in possible_names):

                    firmware_list = platform.get('firmware', [])
                    logging.debug(f"[BIOS] Found platform '{platform_name_check}' with {len(firmware_list)} firmware files")
                    if firmware_list:
                        logging.debug(f"[BIOS] Firmware files: {[f.get('file_name') for f in firmware_list]}")
                    return firmware_list

            logging.debug(f"[BIOS] Platform '{platform_name}' not found on server")
            return None  # Platform not found

        except Exception as e:
            logging.warning(f"[BIOS] Error querying server firmware: {e}")
            import traceback
            logging.debug(traceback.format_exc())
            return None

    def check_platform_bios(self, platform_name):
        """Check BIOS status for a platform by querying RomM server"""
        platform_name = self.normalize_platform_name(platform_name)
        present = []
        missing = []

        # Query server for firmware list
        server_firmware = self.get_server_firmware_for_platform(platform_name)

        if server_firmware:
            # Check each firmware file from server against local files
            for firmware in server_firmware:
                file_name = firmware.get('file_name', '')

                if not file_name:
                    continue

                if file_name in self.installed_bios:
                    present.append({
                        'file': file_name,
                        'status': 'present'
                    })
                else:
                    # Mark as required so auto-download will fetch them
                    missing.append({
                        'file': file_name,
                        'status': 'missing',
                        'optional': False  # Treat as required for downloading
                    })

        return present, missing
    
    def get_all_platforms_status(self):
        """Get BIOS status for all platforms by querying RomM server"""
        status = {}

        if not self.romm_client or not self.romm_client.authenticated:
            return status

        try:
            from urllib.parse import urljoin

            # Get all platforms from server
            platforms_response = self.romm_client.session.get(
                urljoin(self.romm_client.base_url, '/api/platforms'),
                timeout=10
            )

            if platforms_response.status_code != 200:
                return status

            platforms = platforms_response.json()

            # Check BIOS status for each platform that has firmware
            for platform in platforms:
                platform_name = platform.get('name', '')
                firmware_list = platform.get('firmware', [])

                if not firmware_list:
                    continue  # Skip platforms with no firmware

                present, missing = self.check_platform_bios(platform_name)

                # Only include platforms that have BIOS files
                if present or missing:
                    status[platform_name] = {
                        'present': present,
                        'missing': missing,
                        'complete': len(missing) == 0,
                        'required_count': len([b for b in missing if not b.get('optional', False)])
                    }

        except Exception as e:
            self.log(f"⚠️ Error getting platforms status: {e}")

        return status
    
    def download_bios_from_romm(self, platform_name, bios_filename):
            """Download a specific BIOS file from RomM's firmware API"""
            if not self.romm_client or not self.romm_client.authenticated:
                self.log("❌ Not connected to RomM")
                return False
            
            if not self.system_dir:
                self.log("❌ No system directory found")
                return False
            
            try:
                from urllib.parse import urljoin
                
                # This part of your code is correct
                platforms_response = self.romm_client.session.get(
                    urljoin(self.romm_client.base_url, '/api/platforms'),
                    timeout=10
                )
                
                if platforms_response.status_code != 200:
                    self.log("❌ Failed to get platforms list from RomM.")
                    return False
                
                platforms = platforms_response.json()
                
                platform_mappings = {
                    'Sony - PlayStation': ['PlayStation', 'Sony PlayStation', 'PS1', 'PSX'],
                    'Sony - PlayStation 2': ['PlayStation 2', 'Sony PlayStation 2', 'PS2'],
                    'Nintendo - Game Boy Advance': ['Game Boy Advance', 'GBA', 'Nintendo Game Boy Advance'],
                    'Nintendo - Game Boy': ['Game Boy', 'GB', 'Nintendo Game Boy'],
                    'Nintendo - Game Boy Color': ['Game Boy Color', 'GBC', 'Nintendo Game Boy Color'],
                    'Nintendo - Nintendo DS': ['Nintendo DS', 'DS', 'NDS'],
                    'Nintendo - Nintendo 3DS': ['Nintendo 3DS', '3DS', 'N3DS'],
                    'Sega - Saturn': ['Sega Saturn', 'Saturn', 'SS'],
                    'Sega - Dreamcast': ['Sega Dreamcast', 'Dreamcast', 'DC'],
                    'Sega - Mega-CD - Sega CD': ['Sega CD', 'Mega CD', 'Mega-CD'],
                    'SNK - Neo Geo': ['Neo Geo', 'NeoGeo', 'Neo-Geo'],
                    'NEC - PC Engine - TurboGrafx 16': ['PC Engine', 'TurboGrafx', 'TurboGrafx-16', 'TG-16', 'PCE'],
                    'Atari - 7800': ['Atari 7800', '7800'],
                    'Atari - Lynx': ['Atari Lynx', 'Lynx']
                }
                
                possible_names = platform_mappings.get(platform_name, [platform_name])
                
                for platform in platforms:
                    platform_name_check = platform.get('name', '')
                    
                    if any(name.lower() in platform_name_check.lower() or 
                        platform_name_check.lower() in name.lower() 
                        for name in possible_names):
                        
                        logging.debug(f"[BIOS] Found platform: {platform_name_check}")
                        firmware_list = platform.get('firmware', [])

                        for firmware in firmware_list:
                            if firmware.get('file_name') == bios_filename:
                                firmware_id = firmware.get('id')
                                logging.debug(f"[BIOS] Found BIOS: {bios_filename} (ID: {firmware_id})")

                                # Construct the download URL using the firmware ID and filename
                                download_url = f'/api/firmware/{firmware_id}/content/{bios_filename}'

                                # STEP 1: Download the file from the constructed URL
                                file_response = self.romm_client.session.get(
                                    urljoin(self.romm_client.base_url, download_url),
                                    stream=True,
                                    timeout=60  # Increased timeout for larger files
                                )

                                # STEP 2: Check for a successful response and write the file
                                if file_response.status_code == 200:
                                    download_path = self.system_dir / bios_filename

                                    with open(download_path, 'wb') as f:
                                        for chunk in file_response.iter_content(chunk_size=8192):
                                            f.write(chunk)

                                    logging.debug(f"[BIOS] Downloaded {bios_filename}")
                                    return True
                                else:
                                    logging.warning(f"[BIOS] Download failed with status code: {file_response.status_code}")
                                    return False
                        
                        self.log(f"❌ {bios_filename} not found in {platform_name_check} firmware list on server.")
                        break # Stop searching after finding the correct platform
                
                self.log(f"❌ Platform matching '{platform_name}' not found on server.")
                return False
                
            except Exception as e:
                self.log(f"❌ Download error: {e}")
                import traceback
                self.log(traceback.format_exc()) # More detailed error for debugging
                return False
    
    def search_romm_for_bios(self, bios_filename):
        """Search RomM for a BIOS file"""
        if not self.romm_client:
            return None
        
        try:
            # Try searching via API
            search_endpoints = [
                f'/api/search?q={bios_filename}',
                f'/api/search?q={bios_filename}&type=resource',
                f'/api/search?q={bios_filename}&type=firmware',
                f'/api/resources?search={bios_filename}',
            ]
            
            for endpoint in search_endpoints:
                try:
                    response = self.romm_client.session.get(
                        urljoin(self.romm_client.base_url, endpoint),
                        timeout=10
                    )
                    
                    if response.status_code == 200:
                        results = response.json()
                        
                        if isinstance(results, list):
                            for result in results:
                                if isinstance(result, dict):
                                    filename = result.get('filename', result.get('name', ''))
                                    if filename.lower() == bios_filename.lower():
                                        return result
                        elif isinstance(results, dict):
                            items = results.get('items', results.get('results', []))
                            for item in items:
                                filename = item.get('filename', item.get('name', ''))
                                if filename.lower() == bios_filename.lower():
                                    return item
                                    
                except:
                    continue
                    
        except Exception as e:
            self.log(f"Search error: {e}")
        
        return None
    
    def download_romm_resource(self, resource_info):
        """Download a resource from RomM based on search result"""
        if not self.romm_client or not resource_info:
            return False
        
        try:
            # Extract download URL from resource info
            download_url = None
            
            if 'download_url' in resource_info:
                download_url = resource_info['download_url']
            elif 'url' in resource_info:
                download_url = resource_info['url']
            elif 'path' in resource_info:
                download_url = f"/api/resources/{resource_info['id']}/download"
            elif 'id' in resource_info:
                download_url = f"/api/resources/{resource_info['id']}/content"
            
            if download_url:
                response = self.romm_client.session.get(
                    urljoin(self.romm_client.base_url, download_url),
                    stream=True,
                    timeout=30
                )
                
                if response.status_code == 200:
                    filename = resource_info.get('filename', resource_info.get('name', 'unknown.bin'))
                    download_path = self.system_dir / filename
                    
                    with open(download_path, 'wb') as f:
                        for chunk in response.iter_content(chunk_size=8192):
                            if chunk:
                                f.write(chunk)
                    
                    self.log(f"✅ Downloaded {filename}")
                    return True
                    
        except Exception as e:
            self.log(f"Resource download error: {e}")
        
        return False

    def auto_download_missing_bios(self, platform_name):
        """Download missing BIOS for a specific platform"""
        # Rescan to get current state
        self.scan_installed_bios()
        
        present, missing = self.check_platform_bios(platform_name)
        
        # Only download required files that are missing
        required_missing = [b for b in missing if not b.get('optional', False)]
        
        if not required_missing:
            self.log(f"✅ All required BIOS present for {platform_name}")
            return True
        
        success_count = 0
        for bios_info in required_missing:
            bios_file = bios_info['file']
            
            # Double-check if file exists before downloading
            bios_path = self.system_dir / bios_file
            if bios_path.exists():
                self.log(f"⏭️ {bios_file} already exists, skipping")
                success_count += 1
                continue
                
            if self.download_bios_from_romm(platform_name, bios_file):
                success_count += 1
        
        # Rescan after downloads
        self.scan_installed_bios()
        
        if success_count == len(required_missing):
            logging.debug(f"[BIOS] Downloaded all {success_count} BIOS files for {platform_name}")
            return True
        elif success_count > 0:
            logging.warning(f"[BIOS] Downloaded {success_count}/{len(required_missing)} BIOS files for {platform_name}")
            return True
        else:
            logging.warning(f"[BIOS] Could not download any BIOS files for {platform_name}")
            return False