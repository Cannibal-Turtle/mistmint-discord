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


def load_json(relative_path: str | Path, *, required: bool = True, default: Any = None) -> Any:
    path = repo_path(relative_path)

    try:
        with path.open(encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        if required:
            raise RuntimeError(f"Missing required config file: {relative_path}")
        print(f"❌ Missing {relative_path}; using default.")
    except json.JSONDecodeError as e:
        if required:
            raise RuntimeError(f"Invalid JSON in required config file {relative_path}: {e}")
        print(f"❌ Invalid JSON in {relative_path}: {e}; using default.")

    return {} if default is None else default


SERVER = load_json("config/server.json")
FEEDS = load_json("config/feeds.json")
FILES = load_json("config/files.json")
EMBEDS = load_json("config/embeds.json", required=False, default={})


def require_value(source: dict, key: str, label: str) -> Any:
    value = source.get(key)
    if value in (None, ""):
        raise RuntimeError(f"Missing required config value: {label}.{key}")
    return value


def require_server_value(key: str) -> Any:
    return require_value(SERVER, key, "server")


def require_feeds_value(key: str) -> Any:
    return require_value(FEEDS, key, "feeds")


def require_feed_value(feed_name: str, key: str) -> Any:
    feed = FEEDS.get(feed_name)
    if not isinstance(feed, dict):
        raise RuntimeError(f"Missing required feed config: feeds.{feed_name}")
    return require_value(feed, key, f"feeds.{feed_name}")


def require_file_value(key: str) -> Any:
    return require_value(FILES, key, "files")


def server_value(key: str, default: Any = None) -> Any:
    return SERVER.get(key, default)


def file_value(key: str, default: Any = None) -> Any:
    return FILES.get(key, default)
    

def embed_value(key: str, default: Any = None) -> Any:
    return EMBEDS.get(key, default)


def embed_color_hex(key: str, default: str) -> str:
    colors = EMBEDS.get("colors", {})

    if isinstance(colors, dict):
        value = colors.get(key)
    else:
        value = None

    value = value or EMBEDS.get(key) or default
    return str(value).strip().lstrip("#")


def embed_color(key: str, default: str) -> int:
    return int(embed_color_hex(key, default), 16)


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default

    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


THREAD_MAP_FILE = require_file_value("thread_map_file")
THREAD_ID_MAP = load_json(THREAD_MAP_FILE, required=False, default={})
