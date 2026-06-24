# -*- coding: utf-8 -*-
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

BASE_DIR = Path(__file__).resolve().parent

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib


def repo_path(relative_path: str | Path) -> Path:
    path = Path(relative_path)
    return path if path.is_absolute() else BASE_DIR / path
    

def load_toml(relative_path: str | Path, *, required: bool = True, default: Any = None) -> Any:
    path = repo_path(relative_path)

    try:
        return tomllib.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if required:
            raise RuntimeError(f"Missing required TOML config file: {relative_path}")
        return {} if default is None else default


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


def require_feed_url(feed_name: str) -> str:
    feed_key = require_feed_value(feed_name, "feed_key")

    try:
        from novel_mappings import get_output_feed_url
    except Exception as e:
        raise RuntimeError(
            "Could not import get_output_feed_url from rss-feed/novel_mappings. "
            "Make sure the latest rss-feed package is installed."
        ) from e

    url = get_output_feed_url(feed_key)

    if not url:
        raise RuntimeError(
            f"Missing output feed URL for {feed_key!r}. "
            "Check rss-feed/mappings/output_feeds.toml."
        )

    return url


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


def get_novel_color_from_short_code(short_code: str) -> str:
    """
    Returns the novel's theme/Discord color from rss-feed mappings.

    Looks for:
      theme_color
      discord_color

    Returns "" if not found.
    """
    short_code = (short_code or "").strip().upper()

    if not short_code:
        return ""

    try:
        from novel_mappings import get_novel_details_by_short_code
    except Exception:
        return ""

    try:
        _host, _title, details = get_novel_details_by_short_code(short_code)
    except Exception:
        return ""

    if not details:
        return ""

    return str(
        details.get("theme_color")
        or details.get("discord_color")
        or ""
    ).strip()


def embed_color(
    key: str,
    default: str,
    *,
    short_code: str = "",
    novel_color: str = "",
) -> int:
    """
    Resolves an embed color.

    Supports fixed hex config:
      "paid_chapter": "A87676"

    Supports novel-specific config:
      "paid_chapter": "novel"

    In "novel" mode, it uses:
      1. explicit novel_color if passed
      2. theme_color / discord_color from rss-feed using short_code
      3. default fallback
    """
    configured = embed_color_hex(key, default)
    configured_key = str(configured or "").strip().casefold()

    if configured_key in {"novel", "theme", "theme_color", "discord_color"}:
        configured = (
            novel_color
            or get_novel_color_from_short_code(short_code)
            or default
        )

    configured = str(configured or default).strip().lstrip("#")
    return int(configured, 16)
    

def env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default

    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


THREAD_MAP_FILE = require_file_value("thread_map_file")
THREAD_ID_MAP = load_json(THREAD_MAP_FILE, required=False, default={})
