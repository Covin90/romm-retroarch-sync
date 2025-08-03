import subprocess
import json
import logging
from pathlib import Path

# Set up logging
log_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'decky_debug.log'
log_file.parent.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    filename=str(log_file),
    level=logging.DEBUG,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

class Plugin:
    async def _main(self):
        logging.info("RomM Sync Monitor starting...")
        return await self.get_service_status()
    
    async def get_service_status(self):
        """Check if the RomM sync service is running"""
        try:
            # Check systemd service status
            result = subprocess.run(
                ['systemctl', '--user', 'is-active', 'romm-retroarch-sync.service'],
                capture_output=True,
                text=True
            )
            
            service_running = result.returncode == 0 and 'active' in result.stdout
            
            # Try to read additional status from the app's status file
            status_file = Path.home() / '.config' / 'romm-retroarch-sync' / 'status.json'
            app_status = {}
            
            if status_file.exists():
                try:
                    with open(status_file, 'r') as f:
                        app_status = json.load(f)
                    logging.info(f"Status file read: {app_status}")
                except Exception as e:
                    logging.error(f"Failed to read status file: {e}")
            
            # Combine service and app status
            if service_running:
                if app_status.get('connected', False):
                    return {
                        'status': 'connected',
                        'message': f"🟢 Connected ({app_status.get('game_count', 0)} games)",
                        'details': app_status
                    }
                elif app_status.get('running', False):
                    return {
                        'status': 'running',
                        'message': "🟡 Running (not connected)",
                        'details': app_status
                    }
                else:
                    return {
                        'status': 'service_only',
                        'message': "🔵 Service active",
                        'details': {}
                    }
            else:
                return {
                    'status': 'stopped',
                    'message': "🔴 Service stopped",
                    'details': {}
                }
                
        except Exception as e:
            logging.error(f"Status check error: {e}")
            return {
                'status': 'error',
                'message': f"❌ Error: {str(e)[:50]}",
                'details': {}
            }
    
    async def start_service(self):
        """Start the RomM sync service"""
        try:
            result = subprocess.run(
                ['systemctl', '--user', 'start', 'romm-retroarch-sync.service'],
                capture_output=True,
                text=True
            )
            return result.returncode == 0
        except:
            return False
    
    async def stop_service(self):
        """Stop the RomM sync service"""
        try:
            result = subprocess.run(
                ['systemctl', '--user', 'stop', 'romm-retroarch-sync.service'],
                capture_output=True,
                text=True
            )
            return result.returncode == 0
        except:
            return False