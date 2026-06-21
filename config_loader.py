# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent


def repo_path(relative_path: str | Path) -> Path:
    path = Path(relative_path)
    return path if path.is_absolute() else BASE_DIR / path


def load_json(relative_path: str | Path, default: Any = None) -> Any:
    path = repo_path(relative_path)

    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        print(f"❌ Missing {relative_path}; using default.")
    except json.JSONDecodeError as e:
        print(f"❌ Invalid JSON in {relative_path}: {e}; using default.")

    return {} if default is None else default


SERVER = load_json("config/server.json", {})
ROLES = load_json("config/roles.json", {})
FEEDS = load_json("config/feeds.json", {})
FILES = load_json("config/files.json", {})

THREAD_MAP_FILE = FILES.get("thread_map_file", "config/thread_id_map.json")
THREAD_ID_MAP = load_json(THREAD_MAP_FILE, {})


def server_value(key: str, default: Any = None) -> Any:
    return SERVER.get(key, default)


def role_value(key: str, default: Any = None) -> Any:
    return ROLES.get(key, default)


def feed_value(feed_name: str, key: str, default: Any = None) -> Any:
    return FEEDS.get(feed_name, {}).get(key, default)


def file_value(key: str, default: Any = None) -> Any:
    return FILES.get(key, default)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default

    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}
