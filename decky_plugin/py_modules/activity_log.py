"""Persistent recent-activity feed for the Settings UI.

A small, curated event list — one entry per user-visible outcome (download
finished, save-sync ran, collection synced, …), NOT a log-line firehose.
Kept module-level so both main.py (RPC threads) and sync_core's background
threads can record without plumbing a handle through every manager.

Entries persist to activity.json so the feed survives plugin reloads and
Deck restarts. Bounded to the newest MAX_ENTRIES; oldest drop first.
"""

import json
import logging
import threading
import time
from collections import deque
from pathlib import Path

MAX_ENTRIES = 50

_FILE = Path.home() / '.config' / 'romm-retroarch-sync' / 'activity.json'
_lock = threading.Lock()
_entries = deque(maxlen=MAX_ENTRIES)
_loaded = False


def _load_locked():
    global _loaded
    if _loaded:
        return
    _loaded = True
    try:
        if _FILE.exists():
            data = json.loads(_FILE.read_text())
            for e in data if isinstance(data, list) else []:
                if isinstance(e, dict) and e.get('title'):
                    _entries.append(e)
    except Exception as e:
        logging.debug(f"activity_log load failed: {e}")


def _save_locked():
    try:
        _FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = _FILE.with_suffix('.json.tmp')
        tmp.write_text(json.dumps(list(_entries)))
        tmp.replace(_FILE)
    except Exception as e:
        logging.debug(f"activity_log save failed: {e}")


def record(kind, title, detail=''):
    """Append one activity entry.

    kind: free-form tag the UI maps to an icon —
          'download' | 'sync' | 'save' | 'delete' | 'update' | 'account' | 'error'
    """
    with _lock:
        _load_locked()
        _entries.append({
            'kind': str(kind),
            'title': str(title),
            'detail': str(detail or ''),
            'timestamp': time.time(),
        })
        _save_locked()


def get_recent(limit=MAX_ENTRIES):
    """Return entries newest-first."""
    with _lock:
        _load_locked()
        return list(_entries)[::-1][:max(1, int(limit))]


def clear():
    with _lock:
        _load_locked()
        _entries.clear()
        _save_locked()
