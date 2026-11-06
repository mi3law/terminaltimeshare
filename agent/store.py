"""Durable on-disk state for the kiosk agent: atomic JSON files and the event outbox.

No tkinter. Files live next to agent.py (never the CWD) — see agent.py for paths. Writes go to a
temp file and are swapped in with os.replace, retried briefly because antivirus/indexers on
Windows can hold a file open for a moment (SPEC §6: keep the install out of OneDrive).
"""
from __future__ import annotations

import json
import os
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional


def load_json(path: Path, default: Any) -> Any:
    """Parsed JSON, or `default` if the file is missing or corrupt (corrupt files are kept aside)."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except (OSError, ValueError):
        try:
            os.replace(path, path.with_suffix(path.suffix + ".corrupt"))
        except OSError:
            pass
        return default


def save_json(path: Path, data: Any, retries: int = 5) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    payload = json.dumps(data, indent=1, sort_keys=True, default=str)
    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            with open(tmp, "w", encoding="utf-8") as f:
                f.write(payload)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp, path)
            return
        except OSError as e:
            last_err = e
            time.sleep(0.05 * (attempt + 1))
    raise last_err  # type: ignore[misc]


class Outbox:
    """Thread-safe durable queue of events awaiting the backend. Every mutation hits disk
    before returning, so a crash or power cut never loses a queued event."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()
        raw = load_json(path, [])
        self._items: list[dict] = [x for x in raw if isinstance(x, dict) and isinstance(x.get("event"), dict)] \
            if isinstance(raw, list) else []

    def add(self, events: list, now: Optional[datetime] = None) -> None:
        if not events:
            return
        stamp = (now or datetime.now()).isoformat(timespec="seconds")
        with self._lock:
            known = {x["event"].get("eventId") for x in self._items}
            for ev in events:
                if ev.get("eventId") not in known:
                    self._items.append({"event": ev, "queuedAt": stamp})
            save_json(self.path, self._items)

    def batch(self, limit: int = 50) -> list[dict]:
        with self._lock:
            return [dict(x["event"]) for x in self._items[:limit]]

    def remove(self, event_ids) -> None:
        ids = set(event_ids)
        if not ids:
            return
        with self._lock:
            self._items = [x for x in self._items if x["event"].get("eventId") not in ids]
            save_json(self.path, self._items)

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)

    def oldest_queued_at(self) -> Optional[datetime]:
        with self._lock:
            if not self._items:
                return None
            try:
                return datetime.fromisoformat(self._items[0]["queuedAt"])
            except (KeyError, ValueError, TypeError):
                return None
