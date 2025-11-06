#!/usr/bin/env python3
"""
BIOS Manager for RomM-RetroArch Sync
Handles BIOS detection, verification, and synchronization
"""

from pathlib import Path
import hashlib
import json
import threading
from typing import Dict, List, Tuple, Optional

# Comprehensive BIOS requirements database
BIOS_DATABASE = {
    'Sony - PlayStation': {
        'cores': ['beetle_psx', 'beetle_psx_hw', 'pcsx_rearmed', 'swanstation', 'duckstation'],
        'bios_files': [
            {'file': 'scph5500.bin', 'md5': '8dd7d5296a650fac7319bce665a6a53c', 'region': 'JP', 'required': True},
            {'file': 'scph5501.bin', 'md5': '490f666e1afb15b7362b406ed1cea246', 'region': 'US', 'required': True},
            {'file': 'scph5502.bin', 'md5': '32736f17079d0b2b7024407c39bd3050', 'region': 'EU', 'required': True},
            {'file': 'scph1001.bin', 'md5': '924e392ed05558ffdb115408c263dccf', 'region': 'US', 'required': False},
            {'file': 'scph7001.bin', 'md5': '1e68c231d0896b7eadcad1d7d8e76129', 'region': 'US', 'required': False},
            {'file': 'scph101.bin', 'md5': '6e3735ff4c7dc899ee98981385f6f3d0', 'region': 'US', 'required': False},
        ]
    },
    'Sony - PlayStation 2': {
        'cores': ['pcsx2', 'play'],
        'bios_files': [
            {'file': 'ps2-0230a-20080220.bin', 'required': True, 'desc': 'PS2 USA BIOS'},
            {'file': 'ps2-0230e-20080220.bin', 'required': True, 'desc': 'PS2 Europe BIOS'},
            {'file': 'ps2-0230j-20080220.bin', 'required': True, 'desc': 'PS2 Japan BIOS'},
            {'file': 'SCPH-70012_BIOS_V12_USA_200.BIN', 'required': False},
            {'file': 'SCPH-70004_BIOS_V12_EUR_200.BIN', 'required': False},
        ]
    },
    'Sega - Saturn': {
        'cores': ['beetle_saturn', 'kronos', 'yabause'],
        'bios_files': [
            {'file': 'sega_101.bin', 'md5': '85ec9ca47d8f6807718151cbcca8b964', 'required': True},
            {'file': 'mpr-17933.bin', 'md5': '3240872c70984b6cbfda1586cab68dbe', 'required': True},
            {'file': 'saturn_bios.bin', 'md5': 'af5828fdff51384f99b3c4926be27762', 'required': False},
        ]
    },
    'Sega - Mega-CD - Sega CD': {
        'cores': ['genesis_plus_gx', 'picodrive'],
        'bios_files': [
            {'file': 'bios_CD_U.bin', 'md5': '2efd74e3232ff260e371b99f84024f7f', 'region': 'US', 'required': True},
            {'file': 'bios_CD_E.bin', 'md5': 'e66fa1dc5820d254611fdcdba0662372', 'region': 'EU', 'required': True},
            {'file': 'bios_CD_J.bin', 'md5': '278a9397d192149e84e820ac621a8edd', 'region': 'JP', 'required': True},
        ]
    },
    'Sega - Dreamcast': {
        'cores': ['flycast', 'redream'],
        'bios_files': [
            {'file': 'dc_boot.bin', 'md5': 'e10c53c2f8b90bab96ead2d368858623', 'required': True},
            {'file': 'dc_flash.bin', 'md5': '0a93f7940c455905bea6e392dfde92a4', 'required': True},
            {'file': 'dc_nvmem.bin', 'required': False},
        ]
    },
    'SNK - Neo Geo': {
        'cores': ['fbneo', 'fbalpha', 'mame'],
        'bios_files': [
            {'file': 'neogeo.zip', 'required': True, 'desc': 'Neo Geo BIOS package'},
            {'file': 'uni-bios.rom', 'required': False, 'desc': 'UniBIOS (optional)'},
        ]
    },
    'Nintendo - Nintendo DS': {
        'cores': ['desmume', 'melonds'],
        'bios_files': [
            {'file': 'bios7.bin', 'size': 16384, 'required': True, 'md5': 'df692a80a5b1bc90728bc3dfc76cd948'},
            {'file': 'bios9.bin', 'size': 4096, 'required': True, 'md5': 'a392174eb3e572fed6447e956bde4b25'},
            {'file': 'firmware.bin', 'size': 262144, 'required': True},
        ]
    },
    'Nintendo - Game Boy Advance': {
        'cores': ['mgba', 'vba_next', 'vbam', 'gpsp'],
        'bios_files': [
            {'file': 'gba_bios.bin', 'md5': 'a860e8c0b6d573d191e4ec7db1b1e4f6', 'required': False},
            {'file': 'gb_bios.bin', 'md5': '32fbbd84168d3482956eb3c5051637f5', 'required': False},
            {'file': 'gbc_bios.bin', 'md5': 'dbfce9db9deaa2567f6a84fde55f9680', 'required': False},
            {'file': 'sgb_bios.bin', 'md5': 'd574d4f9c12f305074798f54c091a8b4', 'required': False},
        ]
    },
    'NEC - PC Engine - TurboGrafx 16': {
        'cores': ['beetle_pce', 'beetle_pce_fast', 'beetle_supergrafx'],
        'bios_files': [
            {'file': 'syscard3.pce', 'md5': '38179df8f4ac870017db21ebcbf53114', 'required': True},
            {'file': 'syscard2.pce', 'md5': '3cdd6614a918616bfc41c862e889dd79', 'required': False},
            {'file': 'syscard1.pce', 'md5': '2b7ccb3d86baa18f6402c176f3065082', 'required': False},
            {'file': 'gexpress.pce', 'md5': '51a12d90b2a7a6fbd6509e0a38b1c120', 'required': False},
        ]
    },
    'Atari - 7800': {
        'cores': ['prosystem'],
        'bios_files': [
            {'file': '7800 BIOS (U).rom', 'md5': '0763f1ffb006ddbe32e52d497ee848ae', 'required': False},
            {'file': '7800 BIOS (E).rom', 'required': False},
        ]
    },
    'Atari - Lynx': {
        'cores': ['handy', 'beetle_lynx'],
        'bios_files': [
            {'file': 'lynxboot.img', 'md5': 'fcd403db69f54290b51035d82f835e7b', 'required': True},
        ]
    },
    '3DO': {
        'cores': ['opera', '4do'],
        'bios_files': [
            {'file': 'panafz1.bin', 'md5': 'f47264dd47fe30f73ab3c010015c155b', 'required': True},
            {'file': 'panafz10.bin', 'md5': '51f2f43ae2f3508a14d9f56597e2d3ce', 'required': False},
            {'file': 'goldstar.bin', 'md5': '8639fd5e549bd6238cfee79e3e749114', 'required': False},
        ]
    },
    'Microsoft - MSX': {
        'cores': ['bluemsx', 'fmsx'],
        'bios_files': [
            {'file': 'MSX.ROM', 'required': True},
            {'file': 'MSX2.ROM', 'required': False},
            {'file': 'MSX2EXT.ROM', 'required': False},
            {'file': 'MSX2P.ROM', 'required': False},
            {'file': 'MSX2PEXT.ROM', 'required': False},
        ]
    },
    'Commodore - Amiga': {
        'cores': ['puae', 'fsuae'],
        'bios_files': [
            {'file': 'kick33180.A500', 'md5': '85ad74194e87c08904327de1a9443b7a', 'required': True},
            {'file': 'kick34005.A500', 'md5': '82a21c1890cae844b3df741f2762d48d', 'required': True},
            {'file': 'kick37175.A500', 'md5': 'dc10d7bdd1b6f450773dfb558477c230', 'required': False},
            {'file': 'kick40063.A600', 'md5': 'e40a5dfb3d017ba8779faba30cbd1c8e', 'required': False},
        ]
    },
    'Sony - PlayStation Portable': {
        'cores': ['ppsspp'],
        'bios_files': [
            # PPSSPP generates its own PSP firmware files
            {'file': 'PPSSPP', 'required': False, 'desc': 'PPSSPP handles firmware internally'},
        ]
    },
    'Nintendo - Nintendo 3DS': {
        'cores': ['citra'],
        'bios_files': [
            {'file': 'boot9.bin', 'required': True},
            {'file': 'boot11.bin', 'required': True},
            {'file': 'sysdata', 'required': True, 'desc': 'System save data folder'},
        ]
    }
}

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
            
            self.log(f"📋 Found {len(self.installed_bios)} files in system directory")
            
        except Exception as e:
            self.log(f"Error scanning BIOS directory: {e}")
    
    def normalize_platform_name(self, platform_name):
        """Normalize platform name to match BIOS database"""
        if not platform_name:
            return None
        
        # Convert to lowercase for comparison
        platform_lower = platform_name.lower().replace('_', '-')
        
        # Check aliases
        if platform_lower in self.platform_aliases:
            return self.platform_aliases[platform_lower]
        
        # Check if it's already a proper name
        for db_platform in BIOS_DATABASE.keys():
            if db_platform.lower() == platform_lower:
                return db_platform
        
        # Partial matching for complex names
        for db_platform in BIOS_DATABASE.keys():
            if platform_lower in db_platform.lower() or db_platform.lower() in platform_lower:
                return db_platform
        
        return platform_name  # Return original if no match found
    
    def check_platform_bios(self, platform_name):
        """Check BIOS status for a specific platform"""
        platform_name = self.normalize_platform_name(platform_name)
        
        if platform_name not in BIOS_DATABASE:
            return [], []  # No BIOS requirements known
        
        platform_bios = BIOS_DATABASE[platform_name]
        present = []
        missing = []
        
        for bios_info in platform_bios['bios_files']:
            bios_file = bios_info['file']
            
            # Check if file exists
            if bios_file in self.installed_bios:
                installed = self.installed_bios[bios_file]
                
                # Verify MD5 if specified
                if 'md5' in bios_info:
                    # Calculate MD5 if not already done
                    if installed['md5'] is None:
                        installed['md5'] = self.calculate_md5(Path(installed['path']))
                    
                    if installed['md5'] == bios_info['md5']:
                        present.append({**bios_info, 'status': 'verified'})
                    else:
                        missing.append({**bios_info, 'status': 'md5_mismatch', 
                                      'actual_md5': installed['md5']})
                
                # Verify size if specified
                elif 'size' in bios_info:
                    if installed['size'] == bios_info['size']:
                        present.append({**bios_info, 'status': 'size_ok'})
                    else:
                        missing.append({**bios_info, 'status': 'size_mismatch',
                                      'actual_size': installed['size']})
                else:
                    # No verification criteria, assume OK
                    present.append({**bios_info, 'status': 'present'})
            
            elif not bios_info.get('optional', False):
                # Required file is missing
                missing.append({**bios_info, 'status': 'missing'})
        
        return present, missing
    
    def get_all_platforms_status(self):
        """Get BIOS status for all platforms"""
        status = {}
        
        for platform_name in BIOS_DATABASE.keys():
            present, missing = self.check_platform_bios(platform_name)
            
            # Only include platforms that need BIOS files
            if present or missing:
                status[platform_name] = {
                    'present': present,
                    'missing': missing,
                    'complete': len(missing) == 0,
                    'required_count': len([b for b in missing if not b.get('optional', False)])
                }
        
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
                    'Nintendo - Game Boy Advance': ['Game Boy Advance', 'GBA', 'Nintendo Game Boy Advance'],
                    'Nintendo - Game Boy': ['Game Boy', 'GB', 'Nintendo Game Boy'],
                    'Nintendo - Game Boy Color': ['Game Boy Color', 'GBC', 'Nintendo Game Boy Color']
                }
                
                possible_names = platform_mappings.get(platform_name, [platform_name])
                
                for platform in platforms:
                    platform_name_check = platform.get('name', '')
                    
                    if any(name.lower() in platform_name_check.lower() or 
                        platform_name_check.lower() in name.lower() 
                        for name in possible_names):
                        
                        self.log(f"🔍 Found platform: {platform_name_check}")
                        firmware_list = platform.get('firmware', [])
                        
                        for firmware in firmware_list:
                            if firmware.get('file_name') == bios_filename:
                                firmware_id = firmware.get('id')
                                self.log(f"🔍 Found BIOS: {bios_filename} (ID: {firmware_id})")

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
                                    
                                    self.log(f"✅ Downloaded {bios_filename}")
                                    return True
                                else:
                                    self.log(f"❌ Download failed with status code: {file_response.status_code}")
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
            self.log(f"✅ Downloaded all {success_count} BIOS files for {platform_name}")
            return True
        elif success_count > 0:
            self.log(f"⚠️ Downloaded {success_count}/{len(required_missing)} BIOS files for {platform_name}")
            return True
        else:
            self.log(f"❌ Could not download any BIOS files for {platform_name}")
            return False