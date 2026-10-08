"""Persistent settings, saved folders, statistics and history.

Config is shared by the GUI and the scheduled CLI runner, so every write is a
locked read-modify-write of the file on disk.
"""
from __future__ import annotations

import fcntl
import json
import os
import secrets
import threading
import time
from contextlib import contextmanager

CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "shreder")
DATA_DIR = os.path.join(os.environ.get("XDG_DATA_HOME", os.path.expanduser("~/.local/share")), "shreder")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
LOCK_PATH = os.path.join(CONFIG_DIR, ".lock")
HISTORY_PATH = os.path.join(DATA_DIR, "history.jsonl")
HISTORY_LIMIT = 500

DEFAULT_SETTINGS = {
    "method": "dod3",
    "verify": True,
    "rename_passes": 3,
    "notify": True,
    "wipe_elevated": False,
}

SCHEDULES = {"off": 0, "daily": 86400, "weekly": 7 * 86400}


def _default_config() -> dict:
    return {
        "version": 1,
        "settings": dict(DEFAULT_SETTINGS),
        "folders": [],
        "stats": {"files": 0, "bytes_data": 0, "bytes_written": 0, "operations": 0, "wipes": 0},
        "pending_cleanup": [],
    }


def new_folder(path: str, label: str | None = None) -> dict:
    return {
        "id": secrets.token_hex(6),
        "path": os.path.abspath(path),
        "label": label or os.path.basename(os.path.abspath(path).rstrip("/")) or path,
        "method": None,            # None -> use the default method
        "keep_root": True,         # shred contents, keep the folder
        "older_than_days": 0,
        "include_hidden": True,
        "schedule": "off",
        "last_run": None,
        "added": time.time(),
    }


class Store:
    def __init__(self):
        os.makedirs(CONFIG_DIR, exist_ok=True)
        os.makedirs(DATA_DIR, exist_ok=True)
        self._lock = threading.RLock()
        self._listeners = []
        self.data = self._read()

    # ------------------------------------------------------------- plumbing
    def connect(self, callback):
        self._listeners.append(callback)

    def _emit(self):
        for cb in list(self._listeners):
            cb()

    @contextmanager
    def _file_lock(self):
        with open(LOCK_PATH, "a") as lf:
            fcntl.flock(lf, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lf, fcntl.LOCK_UN)

    def _read(self) -> dict:
        data = _default_config()
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                loaded = json.load(f)
            if isinstance(loaded, dict):
                for key in ("folders", "pending_cleanup"):
                    if isinstance(loaded.get(key), list):
                        data[key] = loaded[key]
                for key in ("settings", "stats"):
                    if isinstance(loaded.get(key), dict):
                        data[key].update(loaded[key])
        except (OSError, ValueError):
            pass
        for folder in data["folders"]:
            for k, v in new_folder(folder.get("path", "/")).items():
                folder.setdefault(k, v)
        return data

    def _write(self, data: dict):
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, CONFIG_PATH)

    def update(self, fn):
        """Apply fn(data) to the freshest on-disk config and persist it."""
        with self._lock, self._file_lock():
            data = self._read()
            fn(data)
            self._write(data)
            self.data = data
        self._emit()

    def reload(self):
        with self._lock:
            self.data = self._read()
        self._emit()

    # ------------------------------------------------------------- settings
    @property
    def settings(self) -> dict:
        return self.data["settings"]

    def set_setting(self, key, value):
        self.update(lambda d: d["settings"].__setitem__(key, value))

    # ------------------------------------------------------------- folders
    @property
    def folders(self) -> list:
        return self.data["folders"]

    def folder(self, folder_id: str) -> dict | None:
        return next((f for f in self.folders if f["id"] == folder_id), None)

    def add_folder(self, path: str) -> dict | None:
        path = os.path.abspath(path)
        if any(f["path"] == path for f in self.folders):
            return None
        folder = new_folder(path)
        self.update(lambda d: d["folders"].append(folder))
        return folder

    def save_folder(self, folder: dict):
        def apply(d):
            for i, f in enumerate(d["folders"]):
                if f["id"] == folder["id"]:
                    d["folders"][i] = folder
                    return
            d["folders"].append(folder)
        self.update(apply)

    def remove_folder(self, folder_id: str):
        self.update(lambda d: d.__setitem__("folders", [f for f in d["folders"] if f["id"] != folder_id]))

    def mark_folders_run(self, ids, when=None):
        when = when or time.time()

        def apply(d):
            for f in d["folders"]:
                if f["id"] in ids:
                    f["last_run"] = when
        self.update(apply)

    def due_folders(self, now=None) -> list:
        now = now or time.time()
        due = []
        for f in self.folders:
            interval = SCHEDULES.get(f.get("schedule", "off"), 0)
            if not interval:
                continue
            last = f.get("last_run") or 0
            if now - last >= interval - 60:
                due.append(f)
        return due

    def any_scheduled(self) -> bool:
        return any(SCHEDULES.get(f.get("schedule", "off")) for f in self.folders)

    # ------------------------------------------------------------- crash recovery
    def track_workdir(self, path: str, active: bool):
        def apply(d):
            items = [p for p in d["pending_cleanup"] if p != path]
            if active:
                items.append(path)
            d["pending_cleanup"] = items
        self.update(apply)

    # ------------------------------------------------------------- stats & history
    @property
    def stats(self) -> dict:
        return self.data["stats"]

    def record(self, entry: dict):
        stats = entry.get("stats", {})

        def apply(d):
            s = d["stats"]
            s["operations"] = s.get("operations", 0) + 1
            s["files"] = s.get("files", 0) + stats.get("files_shredded", 0)
            s["bytes_data"] = s.get("bytes_data", 0) + stats.get("bytes_data", 0)
            s["bytes_written"] = s.get("bytes_written", 0) + stats.get("bytes_written", 0)
            if entry.get("kind") == "wipe":
                s["wipes"] = s.get("wipes", 0) + 1
        with self._lock:
            try:
                with open(HISTORY_PATH, "a", encoding="utf-8") as f:
                    f.write(json.dumps(entry) + "\n")
            except OSError:
                pass
        self.update(apply)
        self._trim_history()

    def _trim_history(self):
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                lines = f.readlines()
            if len(lines) > HISTORY_LIMIT * 1.2:
                with open(HISTORY_PATH, "w", encoding="utf-8") as f:
                    f.writelines(lines[-HISTORY_LIMIT:])
        except OSError:
            pass

    def history(self, limit: int = 200) -> list:
        entries = []
        try:
            with open(HISTORY_PATH, encoding="utf-8") as f:
                for line in f:
                    try:
                        entries.append(json.loads(line))
                    except ValueError:
                        continue
        except OSError:
            pass
        return list(reversed(entries[-limit:]))

    def clear_history(self):
        try:
            os.unlink(HISTORY_PATH)
        except OSError:
            pass
        self._emit()

    def reset_stats(self):
        self.update(lambda d: d.__setitem__("stats", _default_config()["stats"]))
