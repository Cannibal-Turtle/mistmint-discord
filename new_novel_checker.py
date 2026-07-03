#!/usr/bin/env python3
"""
new_novel_checker.py (for thread servers)

Announce a brand new novel when it FIRST becomes available for free/public reading.

Usage:
  python new_novel_checker.py --feed free

Behavior:
- For each novel in HOSTING_SITE_DATA that has a free_feed:
    - Parse the free feed (RSS).
    - Find an entry for that novel whose chapter looks like the first drop:
        "Chapter 1", "Ch 1", "Prologue", or "1.1".
    - If we haven't announced this novel before:
        - Build a launch message (sparkle text).
        - Build an embed (translator, clickable title, cleaned description,
          cover image, footer with host + timestamp).
        - Post both to the novel's thread (per-novel secret).
        - Write launch_free info into state.json so we never post it again.

Thread routing:
  - Thread IDs are resolved from thread_id_map.json using novel short_code.

Notes:
- SHORTCODE is taken from HOSTING_SITE_DATA.novels[...]['short_code'] if present.
  If missing, it is derived from the title: uppercase and non-alnum → underscore.
"""

import argparse
import json
import os
import sys
import re
import html
import feedparser
import requests
from datetime import datetime, timezone
import time

from message_renderer import render_message, to_discord_api_payload
from git_state_commit import commit_state_update

from novel_mappings import (
    HOSTING_SITE_DATA,
)

# ─── CONFIG ────────────────────────────────────────────────────────────────────
from config_loader import (
    THREAD_ID_MAP,
    THREAD_MAP_FILE,
    env_bool,
    require_file_value,
    require_server_value,
    server_value,
)

STATE_PATH    = require_file_value("state_path")
BOT_TOKEN_ENV = "DISCORD_BOT_TOKEN"

HOST_TARGET = require_server_value("host_target")
TRANSLATOR_URL = str(server_value("translator_url", "") or "").strip()

USE_UNARCHIVE = env_bool("USE_UNARCHIVE", False)
DEFAULT_AUTO_ARCHIVE_MINUTES = int(require_server_value("default_auto_archive_minutes"))
_THREAD_GUILD_ID_CACHE = {}
# ───────────────────────────────────────────────────────────────────────────────

def get_entry_translator_url(entry) -> str:
    for key in ("translator_url", "translatorUrl", "translatorurl"):
        value = entry.get(key)
        if value:
            return str(value).strip()
    return ""


def load_state(path=STATE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
            if not raw:
                # empty file → treat as empty state
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # malformed JSON → ignore and start fresh in-memory
                print(f"⚠️ {path} contained invalid JSON; using empty state.", file=sys.stderr)
                return {}
    except FileNotFoundError:
        return {}


def save_state(state, path=STATE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def parsed_time_to_aware(struct_t, fallback_now):
    if not struct_t:
        return fallback_now
    try:
        aware_utc = datetime(
            struct_t.tm_year,
            struct_t.tm_mon,
            struct_t.tm_mday,
            struct_t.tm_hour,
            struct_t.tm_min,
            struct_t.tm_sec,
            tzinfo=timezone.utc,
        )
        return aware_utc.astimezone()
    except Exception:
        return fallback_now

def unarchive_thread(bot_token: str, thread_id: str, *, unlock: bool = True, auto_archive_minutes: int | None = None) -> bool:
    if auto_archive_minutes is None:
        auto_archive_minutes = DEFAULT_AUTO_ARCHIVE_MINUTES
      
    url = f"https://discord.com/api/v10/channels/{thread_id}"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    payload = {"archived": False}
    if unlock:
        payload["locked"] = False
    if auto_archive_minutes:
        payload["auto_archive_duration"] = auto_archive_minutes  # 60, 1440, 4320, 10080
    r = requests.patch(url, headers=headers, json=payload, timeout=15)
    if not r.ok:
        print(f"⚠️ Unarchive failed {r.status_code}: {r.text}")
    return r.ok
  
def _ensure_bot_in_thread(bot_token: str, thread_id: str) -> bool:
    try:
        h = {"Authorization": f"Bot {bot_token}"}
        r = requests.get(f"https://discord.com/api/v10/channels/{thread_id}/thread-members/@me", headers=h, timeout=15)
        if r.status_code == 200:
            return True
        j = requests.put(f"https://discord.com/api/v10/channels/{thread_id}/thread-members/@me", headers=h, timeout=15)
        return j.status_code in (200, 204)
    except requests.RequestException:
        return False

def send_bot_payload(bot_token: str, channel_or_thread_id: str, message_payload: dict):
    url = f"https://discord.com/api/v10/channels/{channel_or_thread_id}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type": "application/json",
    }

    payload = to_discord_api_payload(message_payload)

    def _post():
        return requests.post(url, headers=headers, json=payload, timeout=20)

    r = _post()

    # If archived/membership issues, try to fix once and post again.
    if r.status_code in (400, 403):
        body = {}
        try:
            body = r.json()
        except Exception:
            body = {"message": r.text}

        msg  = (body.get("message") or "").lower()
        code = body.get("code")

        recovered = False

        if "archiv" in msg:
            if USE_UNARCHIVE:
                recovered = unarchive_thread(
                    bot_token,
                    channel_or_thread_id,
                    unlock=True,
                    auto_archive_minutes=DEFAULT_AUTO_ARCHIVE_MINUTES,
                )
            else:
                print("ℹ️ Thread is archived and USE_UNARCHIVE=0; not patching.")

        if not recovered and (("missing access" in msg) or code in (50001, 50013)):
            recovered = _ensure_bot_in_thread(bot_token, channel_or_thread_id)

        if recovered:
            time.sleep(0.8)
            r = _post()

    # Respect rate limit once.
    if r.status_code == 429:
        wait = None
        reset_after = r.headers.get("X-RateLimit-Reset-After")

        if reset_after:
            try:
                wait = float(reset_after)
            except (TypeError, ValueError):
                pass

        if wait is None:
            try:
                wait = float(r.json().get("retry_after", 1.0))
            except Exception:
                wait = 1.0

        time.sleep(min(max(wait or 1.0, 0.0), 5.0))
        r = _post()

    if not r.ok:
        raise requests.HTTPError(f"{r.status_code} {r.text}", response=r)


def safe_send_bot_payload(bot_token: str, channel_or_thread_id: str, message_payload: dict) -> bool:
    try:
        send_bot_payload(bot_token, channel_or_thread_id, message_payload)
        return True

    except requests.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        body   = e.response.text if e.response else ""
        print(f"⚠️ Bot send failed ({status}): {body}", file=sys.stderr)

        if "locked" in body.lower():
            print("ℹ️ Thread appears LOCKED. Unlock or grant the bot Manage Threads.", file=sys.stderr)

        return False

    except requests.RequestException as e:
        print(f"⚠️ Bot send error: {e}", file=sys.stderr)
        return False
      

def is_first_chapter_name(chapter_field: str) -> bool:
    if not chapter_field:
        return False

    text = chapter_field.lower().strip()

    if re.search(r"\bch(?:apter)?\.?\s*0*1\b", text):
        return True

    if re.search(r"\bep(?:isode)?\.?\s*0*1\b", text):
        return True

    if "prologue" in text:
        return True

    if re.search(r"\b1[．\.]\s*0*1\b", text):
        return True

    return False


def clean_feed_description(raw_html: str) -> str:
    if not raw_html:
        return ""

    parts = re.split(r"(?i)<hr[^>]*>", raw_html, maxsplit=1)
    main_part = parts[0]

    no_tags = re.sub(r"(?s)<[^>]+>", "", main_part)

    text = html.unescape(no_tags)

    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = text.strip()

    if len(text) > 4000:
        text = text[:4000].rstrip() + "…"

    return text


def format_site_genres(value) -> str:
    """Normalize a host-specific TOML genre value for Discord display."""
    if not value:
        return ""

    if isinstance(value, str):
        # Accept either "Horror, Yaoi" or a single plain string.
        raw_items = re.split(r",\s*", value)
    elif isinstance(value, (list, tuple, set)):
        raw_items = list(value)
    else:
        raw_items = [value]

    genres: list[str] = []
    seen: set[str] = set()

    for item in raw_items:
        if isinstance(item, dict):
            item = item.get("name", "")

        genre = str(item or "").strip()
        if not genre:
            continue

        key = genre.casefold()
        if key in seen:
            continue

        seen.add(key)
        genres.append(genre)

    return ", ".join(genres)


def get_site_genre(details: dict) -> str:
    """
    Return site-native genres from novel TOML.
    """
    return format_site_genres(
        details.get("site_genres")
        or details.get("genres")
    )


# ──────────────────────── Thread helpers ────────────────────────

def sanitize_shortcode_from_title(title: str) -> str:
    """Fallback SHORTCODE from title (A–Z/0–9 only)."""
    return re.sub(r"[^A-Z0-9]+", "_", (title or "").upper()).strip("_")
  
  
def resolve_thread_id(novel_title: str, details: dict) -> str | None:
    short_code = (details.get("short_code") or "").strip() or sanitize_shortcode_from_title(novel_title)
    key = re.sub(r"[^A-Z0-9]+", "_", short_code.upper())

    val = THREAD_ID_MAP.get(key)
    if not val:
        print(
            f"❌ No thread id for {novel_title}. "
            f"Add \"{key}\" to {THREAD_MAP_FILE}."
        )
        return None

    return str(val)


def get_guild_id_for_thread(bot_token: str, thread_id: str) -> str:
    if thread_id in _THREAD_GUILD_ID_CACHE:
        return _THREAD_GUILD_ID_CACHE[thread_id]

    headers = {"Authorization": f"Bot {bot_token}"}

    r = requests.get(
        f"https://discord.com/api/v10/channels/{thread_id}",
        headers=headers,
        timeout=15,
    )
    r.raise_for_status()

    guild_id = str(r.json().get("guild_id") or "").strip()

    if not guild_id:
        raise RuntimeError(f"Could not resolve guild_id for thread {thread_id}")

    _THREAD_GUILD_ID_CACHE[thread_id] = guild_id
    return guild_id


def build_thread_url(bot_token: str, thread_id: str) -> str:
    guild_id = get_guild_id_for_thread(bot_token, thread_id)
    return f"https://discord.com/channels/{guild_id}/{thread_id}"


def load_novels_from_mapping():
    novels = []
    for host_name, host_data in HOSTING_SITE_DATA.items():
        host_logo    = host_data.get("host_logo", "")
        novels_block = host_data.get("novels", {})
        for novel_title, details in novels_block.items():
            free_feed_url = details.get("free_feed")
            if not free_feed_url:
                continue
              
            novels.append({
                "host":             host_name,
                "translator":       details.get("translator") or host_data.get("translator", ""),
                "translator_url":   details.get("translator_url") or host_data.get("translator_url", ""),
                "host_logo":        host_logo,
                "novel_title":      novel_title,
                "novel_url":        details.get("novel_url", ""),
                "featured_image":   details.get("featured_image", ""),
                "free_feed":        free_feed_url,
                "short_code":       (details.get("short_code", "") or "").strip().upper(),
                "site_genre":       get_site_genre(details),
            })
    return novels


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--feed",
        choices=["free"],
        required=True,
        help="We only announce once free/public chapters are available."
    )
    _ = parser.parse_args()

    bot_token = os.getenv(BOT_TOKEN_ENV)
    if not bot_token:
        sys.exit("❌ Missing DISCORD_BOT_TOKEN")

    state  = load_state()
    novels = load_novels_from_mapping()

    now_local = datetime.now(timezone.utc).astimezone()

    for novel in reversed(novels):
        if novel["host"] != HOST_TARGET:
            continue
        novel_title = novel["novel_title"]
        host_name   = novel["host"]

        # only announce for first free
        if state.get(novel_title, {}).get("launch_free"):
            print(f"→ skipping {novel_title} (launch_free) — already launched")
            continue

        # route to per-novel thread via THREAD_ID_MAP or <SHORTCODE>_THREAD_ID
        thread_id = resolve_thread_id(novel_title, novel)
        if not thread_id:
            # resolve_thread_id already printed the helpful hint
            continue

        follow_url = build_thread_url(bot_token, thread_id)

        feed_url = novel.get("free_feed")
        if not feed_url:
            continue

        print(f"Fetching free feed for {novel_title} from {feed_url}")
        resp = requests.get(feed_url, timeout=20)
        resp.raise_for_status()
        feed = feedparser.parse(resp.text)
        print(
            f"Parsed {len(feed.entries)} entries "
            f"(Content-Type: {resp.headers.get('Content-Type')})"
        )

        for entry in feed.entries:
            entry_title = (entry.get("title") or "").strip()
            if entry_title != novel_title:
                continue

            chap_field = entry.get("chapter") or ""
          
            if not is_first_chapter_name(chap_field):
                continue

            chap_link = entry.link

            raw_desc_html = (
                entry.get("description")
                or entry.get("summary")
                or ""
            )
            desc_text = clean_feed_description(raw_desc_html)

            chap_dt_local = parsed_time_to_aware(
                entry.get("published_parsed") or entry.get("updated_parsed"),
                now_local
            )

            chap_display = chap_field.replace("\u00A0", " ").strip()
            pub_date_iso = chap_dt_local.astimezone(timezone.utc).isoformat()

            ctx = {
                "title": novel_title,
                "novel_title": novel_title,
                "novel_url": novel.get("novel_url", ""),
                "chapter": chap_display,
                "chapter_link": chap_link,
                "host": host_name,
                "translator": novel.get("translator", ""),
                "translator_url": (
                    get_entry_translator_url(entry)
                    or novel.get("translator_url", "")
                    or TRANSLATOR_URL
                ),
                "description": desc_text,
                "featured_image_url": novel.get("featured_image", ""),
                "host_logo_url": novel.get("host_logo", ""),
                "pub_date_iso": pub_date_iso,
                "short_code": novel.get("short_code", ""),
                "follow_thread_url": follow_url,
                "site_genre": novel.get("site_genre", ""),
                "site_genre_block": (
                    f"*Genres: {novel.get('site_genre', '')}*\n\n"
                    if novel.get("site_genre", "")
                    else ""
                ),
            }

            message_payload = render_message("new_novels", ctx)

            print(
                f"→ Built launch message for {novel_title} "
                f"({len(message_payload.get('content', ''))} chars + "
                f"{len(message_payload.get('embeds', []))} embed)"
            )

            # Ensure bot can post by joining the thread.
            _ensure_bot_in_thread(bot_token, thread_id)

            ok = safe_send_bot_payload(
                bot_token=bot_token,
                channel_or_thread_id=thread_id,
                message_payload=message_payload,
            )

            if ok:
                print(f"✔️ Sent launch announcement for {novel_title} → thread {thread_id}")
                state.setdefault(novel_title, {})["launch_free"] = {
                    "chapter": chap_field,
                    "sent_at": datetime.now().isoformat()
                }
                save_state(state)
                commit_state_update(STATE_PATH)
            else:
                print("→ Send failed; not updating state.json")

            break


if __name__ == "__main__":
    main()
