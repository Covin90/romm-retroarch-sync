#!/usr/bin/env python3
"""
Extract sync_core.py from romm_sync_app.py and update the original.
"""

import sys
import os

SRC_DIR = "/home/covin/romm-retroarch-sync/src"
ORIGINAL = os.path.join(SRC_DIR, "romm_sync_app.py")
SYNC_CORE = os.path.join(SRC_DIR, "sync_core.py")

# ---------------------------------------------------------------------------
# Read original file
# ---------------------------------------------------------------------------
with open(ORIGINAL, "r", encoding="utf-8") as f:
    lines = f.readlines()

total = len(lines)
print(f"Original file: {total} lines")

# ---------------------------------------------------------------------------
# Build sync_core.py content
# ---------------------------------------------------------------------------
HEADER = '''\
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
os.environ[\'REQUESTS_CA_BUNDLE\'] = \'/etc/ssl/certs/ca-certificates.crt\'
os.environ[\'SSL_CERT_FILE\'] = \'/etc/ssl/certs/ca-certificates.crt\'

# GLib is optional - used for GUI thread scheduling when GTK is available.
# In headless/Decky mode, callbacks are called directly instead.
try:
    from gi.repository import GLib
    def _idle_add(f, *a): GLib.idle_add(f, *a)
except ImportError:
    def _idle_add(f, *a): f(*a)  # call directly in headless mode

'''

# Slices (0-indexed, Python slice notation)
# 1-indexed lines 505-1094  => lines[504:1094]
# 1-indexed lines 6478-9924 => lines[6477:9923]  (note: end is exclusive in slice, so lines[6477:9924] gives lines 6478..9924 inclusive)
# 1-indexed lines 15579-17710 => lines[15578:17710]

chunk1 = lines[504:1094]
chunk2 = lines[6477:9924]
chunk3 = lines[15578:17710]

sync_core_content = HEADER + "".join(chunk1) + "".join(chunk2) + "".join(chunk3)

# ---------------------------------------------------------------------------
# Apply text substitutions
# ---------------------------------------------------------------------------

# Sub 1 - Remove local GLib/Adw import in AutoSyncManager.download_saves_for_specific_game
OLD1 = "        from gi.repository import GLib, Adw\n"
NEW1 = ("        try:\n"
        "            from gi.repository import Adw as _Adw\n"
        "        except ImportError:\n"
        "            _Adw = None\n")
count1 = sync_core_content.count(OLD1)
sync_core_content = sync_core_content.replace(OLD1, NEW1)
print(f"Sub 1 replacements: {count1}")

# Sub 2 - Make ask_user headless-safe
OLD2 = ("                    def ask_user():\n"
        "                        dialog = Adw.AlertDialog.new(")
NEW2 = ("                    def ask_user():\n"
        "                        if _Adw is None:\n"
        "                            download_choice[0] = False\n"
        "                            user_choice.set()\n"
        "                            return\n"
        "                        dialog = _Adw.AlertDialog.new(")
count2 = sync_core_content.count(OLD2)
sync_core_content = sync_core_content.replace(OLD2, NEW2)
print(f"Sub 2 replacements: {count2}")

# Sub 3 - Replace GLib.idle_add with _idle_add in that block
OLD3 = "                    GLib.idle_add(ask_user)\n"
NEW3 = "                    _idle_add(ask_user)\n"
count3 = sync_core_content.count(OLD3)
sync_core_content = sync_core_content.replace(OLD3, NEW3)
print(f"Sub 3 replacements: {count3}")

# Sub 4 - Modify run_daemon_mode signature
OLD4 = "def run_daemon_mode():\n"
NEW4 = "def run_daemon_mode(stop_event=None):\n"
count4 = sync_core_content.count(OLD4)
sync_core_content = sync_core_content.replace(OLD4, NEW4)
print(f"Sub 4 replacements: {count4}")

# Sub 5 - Guard signal.signal() registrations
OLD5 = ("    # Handle signals gracefully\n"
        "    signal.signal(signal.SIGTERM, signal_handler)\n"
        "    signal.signal(signal.SIGINT, signal_handler)\n"
        "    signal.signal(signal.SIGUSR1, reload_handler)  # Add USR1 handler for immediate reload\n")
NEW5 = ("    # Handle signals gracefully (only works from main thread)\n"
        "    import threading as _th\n"
        "    if _th.current_thread() is _th.main_thread():\n"
        "        signal.signal(signal.SIGTERM, signal_handler)\n"
        "        signal.signal(signal.SIGINT, signal_handler)\n"
        "        signal.signal(signal.SIGUSR1, reload_handler)\n")
count5 = sync_core_content.count(OLD5)
sync_core_content = sync_core_content.replace(OLD5, NEW5)
print(f"Sub 5 replacements: {count5}")

# Sub 6 - Add stop_event check to daemon loop
# The loop is inside def daemon_loop():
OLD6 = "        while running:\n"
NEW6 = "        while running and (stop_event is None or not stop_event.is_set()):\n"
count6 = sync_core_content.count(OLD6)
sync_core_content = sync_core_content.replace(OLD6, NEW6)
print(f"Sub 6 replacements: {count6}")

# ---------------------------------------------------------------------------
# Write sync_core.py
# ---------------------------------------------------------------------------
with open(SYNC_CORE, "w", encoding="utf-8") as f:
    f.write(sync_core_content)

sc_lines = sync_core_content.count("\n")
print(f"sync_core.py written: {sc_lines} lines")

# ---------------------------------------------------------------------------
# Build updated romm_sync_app.py
# ---------------------------------------------------------------------------
# Keep:
#   lines[0:504]         -> original lines 1-504
#   insert import line
#   lines[1094:6477]     -> original lines 1095-6477
#   lines[9924:15578]    -> original lines 9925-15578
#   lines[17710:17749]   -> original lines 17711-17749

import_line = "\nfrom sync_core import *\n\n"

new_app_lines = (
    lines[0:504]
    + [import_line]
    + lines[1094:6477]
    + lines[9924:15578]
    + lines[17710:17749]
)

new_app_content = "".join(new_app_lines)

with open(ORIGINAL, "w", encoding="utf-8") as f:
    f.write(new_app_content)

app_lines = new_app_content.count("\n")
print(f"romm_sync_app.py written: {app_lines} lines")

# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------
print("\n--- Verification ---")
failures = []

# Check sync_core.py line count
with open(SYNC_CORE, "r", encoding="utf-8") as f:
    sc_text = f.read()
sc_line_count = sc_text.count("\n")
print(f"sync_core.py line count: {sc_line_count}")

# Check romm_sync_app.py line count
with open(ORIGINAL, "r", encoding="utf-8") as f:
    app_text = f.read()
app_line_count = app_text.count("\n")
print(f"romm_sync_app.py line count: {app_line_count}")

# sync_core.py starts with header
if not sc_text.startswith("#!/usr/bin/env python3\n"):
    failures.append("sync_core.py does not start with shebang")
else:
    print("OK: sync_core.py starts with shebang")

# sync_core.py contains _idle_add definition
if "_idle_add" in sc_text and "def _idle_add" in sc_text:
    print("OK: _idle_add is defined in sync_core.py header")
else:
    failures.append("_idle_add not defined in sync_core.py")

# sync_core.py contains SettingsManager class
if "class SettingsManager" in sc_text:
    print("OK: SettingsManager class found in sync_core.py")
else:
    failures.append("SettingsManager class NOT found in sync_core.py")

# sync_core.py contains run_daemon_mode(stop_event=None)
if "def run_daemon_mode(stop_event=None):" in sc_text:
    print("OK: run_daemon_mode(stop_event=None) found in sync_core.py")
else:
    failures.append("run_daemon_mode(stop_event=None) NOT found in sync_core.py")

# romm_sync_app.py contains SyncWindow
if "class SyncWindow" in app_text:
    print("OK: class SyncWindow found in romm_sync_app.py")
else:
    failures.append("class SyncWindow NOT found in romm_sync_app.py")

# romm_sync_app.py contains the import
if "from sync_core import *" in app_text:
    print("OK: 'from sync_core import *' found in romm_sync_app.py")
else:
    failures.append("'from sync_core import *' NOT found in romm_sync_app.py")

# romm_sync_app.py does NOT contain SettingsManager definition (it's moved out)
if "class SettingsManager" in app_text:
    failures.append("class SettingsManager still present in romm_sync_app.py (should have been removed)")
else:
    print("OK: SettingsManager NOT duplicated in romm_sync_app.py")

print()
if failures:
    print("FAILURES:")
    for f in failures:
        print(f"  - {f}")
else:
    print("SUCCESS")
