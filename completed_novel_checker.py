#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
completed_novel_checker.py (for thread servers)

Posts completion announcements for novels into their per-novel threads (no fallback channel).

Usage:
  python completed_novel_checker.py --feed paid
  python completed_novel_checker.py --feed free
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime

import feedparser
import requests
from dateutil.relativedelta import relativedelta

from message_renderer import render_message, to_discord_api_payload
from git_state_commit import commit_state_update
from guid_state import entry_guid_identity, format_seen_guid, seen_guid_identities
from announcement_banner import build_announcement_banner

try:
    from novel_mappings import HOSTING_SITE_DATA
except Exception as e:
    print(f"⚠️ novel_mappings not available ({e}); using empty HOSTING_SITE_DATA.")
    HOSTING_SITE_DATA = {}

try:
    from novel_mappings import get_translator_url
except Exception:
    def get_translator_url(host, novel_title=""):
        return ""
  

# ─── CONFIG ────────────────────────────────────────────────────────────────────
from config_loader import (
    THREAD_ID_MAP,
    THREAD_MAP_FILE,
    env_bool,
    require_feed_value,
    require_file_value,
    require_server_value,
    server_value,
)

STATE_PATH     = require_file_value("state_path")
RSS_STATE_PATH = require_file_value("rss_state_path")
BOT_TOKEN_ENV  = "DISCORD_BOT_TOKEN"

HOST_NAME_TARGET = require_server_value("host_target")
GLOBAL_MENTION   = require_server_value("global_mention")
TRANSLATOR_URL = str(server_value("translator_url", "") or "").strip()

USE_UNARCHIVE = env_bool("USE_UNARCHIVE", False)
DEFAULT_AUTO_ARCHIVE_MINUTES = int(require_server_value("default_auto_archive_minutes"))
# ────────────────────────────────────────────────────────────────────────────────

COMPLETED_NOVELS_TEMPLATE_PATH = "message_templates/completed_novels.toml"
DEFAULT_COMPLETION_BANNER_SETTINGS = {
    "enabled": True,
    "ratio": "8:3",
    "crop": "auto",
}
DEFAULT_COMPLETION_BANNER_SIZE = (1600, 600)


def _truthy(value, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
    return default


def load_completion_banner_settings() -> dict:
    template = load_toml(COMPLETED_NOVELS_TEMPLATE_PATH, required=False, default={})
    settings = ((template.get("settings") or {}).get("banner") or {})

    return {
        "enabled": _truthy(settings.get("enabled"), DEFAULT_COMPLETION_BANNER_SETTINGS["enabled"]),
        "ratio": str(settings.get("ratio") or DEFAULT_COMPLETION_BANNER_SETTINGS["ratio"]).strip(),
        "crop": str(settings.get("crop") or DEFAULT_COMPLETION_BANNER_SETTINGS["crop"]).strip().lower(),
    }


def parse_banner_ratio_to_size(ratio: str) -> tuple[int, int] | None:
    ratio_text = str(ratio or "").strip()

    if ratio_text.casefold() == "original":
        return None

    try:
        width_text, height_text = ratio_text.split(":", 1)
        ratio_width = float(width_text.strip())
        ratio_height = float(height_text.strip())
        if ratio_width <= 0 or ratio_height <= 0:
            raise ValueError

        output_width = DEFAULT_COMPLETION_BANNER_SIZE[0]
        output_height = max(1, int(round(output_width * ratio_height / ratio_width)))
        return output_width, output_height
    except Exception:
        print(
            f"⚠️ Invalid completed_novels banner ratio {ratio_text!r}; "
            f"falling back to {DEFAULT_COMPLETION_BANNER_SETTINGS['ratio']}.",
            file=sys.stderr,
        )
        return DEFAULT_COMPLETION_BANNER_SIZE


COMPLETION_BANNER_SETTINGS = load_completion_banner_settings()


# ─── STATE IO ──────────────────────────────────────────────────────────────────
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


# ─── DISCORD SENDER ────────────────────────────────────────────────────────────
def send_bot_message(
    bot_token: str,
    channel_or_thread_id: str,
    message_payload: dict,
    attachment: tuple[str, bytes, str] | None = None,
):
    """
    POST rendered TOML payload via bot token to the given channel/thread ID.
    Threads are also channels in Discord API, so same endpoint works.
    """
    url = f"https://discord.com/api/v10/channels/{channel_or_thread_id}/messages"
    payload = to_discord_api_payload(message_payload)

    if attachment:
        filename, _, _ = attachment
        payload = dict(payload)
        payload["attachments"] = [{"id": 0, "filename": filename}]

    def _send():
        if attachment:
            filename, file_bytes, content_type = attachment
            return requests.post(
                url,
                headers={"Authorization": f"Bot {bot_token}"},
                data={"payload_json": json.dumps(payload, ensure_ascii=False)},
                files={"files[0]": (filename, file_bytes, content_type)},
                timeout=30,
            )

        return requests.post(
            url,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=20,
        )

    # Preflight: join thread is always safe; unarchive only when allowed
    ensure_bot_in_thread(bot_token, channel_or_thread_id)
    if USE_UNARCHIVE:
        unarchive_thread(
            bot_token,
            channel_or_thread_id,
            unlock=True,
            auto_archive_minutes=DEFAULT_AUTO_ARCHIVE_MINUTES,
        )

    r = _send()

    # Fix archived / missing access once, then retry
    if r.status_code in (400, 403):
        try:
            body = r.json()
        except Exception:
            body = {"message": r.text}

        msg = (body.get("message") or "").lower()
        code = body.get("code")

        recovered = False

        if "archiv" in msg and USE_UNARCHIVE:
            recovered = unarchive_thread(
                bot_token,
                channel_or_thread_id,
                unlock=True,
                auto_archive_minutes=DEFAULT_AUTO_ARCHIVE_MINUTES,
            )

        if not recovered and (code in (50001, 50013) or "missing access" in msg):
            recovered = ensure_bot_in_thread(bot_token, channel_or_thread_id)

        if recovered:
            time.sleep(0.8)
            r = _send()

    # Rate limit: respect header, else body, then retry once
    if r.status_code == 429:
        wait = None
        reset_after = (
            r.headers.get("X-RateLimit-Reset-After")
            or r.headers.get("x-ratelimit-reset-after")
        )

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

        time.sleep(max(0.0, min(wait or 1.0, 5.0)))
        r = _send()

    if not r.ok:
        r.raise_for_status()
      
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

def ensure_bot_in_thread(bot_token: str, thread_id: str) -> bool:
    try:
        h = {"Authorization": f"Bot {bot_token}"}
        r = requests.get(f"https://discord.com/api/v10/channels/{thread_id}/thread-members/@me",
                         headers=h, timeout=15)
        if r.status_code == 200:
            return True
        j = requests.put(f"https://discord.com/api/v10/channels/{thread_id}/thread-members/@me",
                         headers=h, timeout=15)
        return j.status_code in (200, 204)
    except requests.RequestException:
        return False

def safe_send_bot(
    bot_token: str,
    channel_or_thread_id: str,
    message_payload: dict,
    attachment: tuple[str, bytes, str] | None = None,
) -> bool:
    try:
        send_bot_message(
            bot_token,
            channel_or_thread_id,
            message_payload,
            attachment=attachment,
        )
        return True

    except requests.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        body   = e.response.text if e.response else ""
        print(f"⚠️ Bot send failed ({status}) to {channel_or_thread_id}:\n{body}", file=sys.stderr)
        return False
    except requests.RequestException as e:
        print(f"⚠️ Bot send error to {channel_or_thread_id}: {e}", file=sys.stderr)
        return False
      
    except requests.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        body   = e.response.text if e.response else ""
        print(f"⚠️ Bot send failed ({status}) to {channel_or_thread_id}:\n{body}", file=sys.stderr)
        return False
    except requests.RequestException as e:
        print(f"⚠️ Bot send error to {channel_or_thread_id}: {e}", file=sys.stderr)
        return False


# ─── HELPERS ───────────────────────────────────────────────────────────────────

def get_duration(start_date_str: str, end_date: datetime) -> str:
    """
    Converts a start date (DD/MM/YYYY) to a human-readable duration vs end_date.
    Returns "" if no valid start_date_str was provided.
    """
    if not start_date_str:
        return ""

    try:
        day, month, year = map(int, start_date_str.split("/"))
        start = datetime(year, month, day)
    except Exception:
        # invalid format → give up on duration entirely
        return ""

    delta = relativedelta(end_date, start)

    years = delta.years
    months = delta.months
    days = delta.days

    if years > 0:
        if months > 0:
            return (
                f"{'a' if years == 1 else years} year{'s' if years > 1 else ''} "
                f"and {'a' if months == 1 else months} month{'s' if months > 1 else ''}"
            )
        return f"{'a' if years == 1 else years} year{'s' if years > 1 else ''}"

    if months > 0:
        return f"{'a' if months == 1 else months} month{'s' if months > 1 else ''}"

    weeks = days // 7
    remaining_days = days % 7
    if weeks > 0:
        return f"{weeks} week{'s' if weeks != 1 else ''}"
    if remaining_days > 0:
        return "more than a week"
    return "less than a week"


def sanitize_shortcode_from_title(title: str) -> str:
    """
    Build an env-safe fallback key from the novel title.
    """
    up = (title or "").upper()
    return re.sub(r"[^A-Z0-9]+", "_", up).strip("_")


def resolve_thread_id(novel_title: str, details: dict) -> str | None:
    short_code = (details.get("short_code") or "").strip() or sanitize_shortcode_from_title(novel_title)
    key = re.sub(r"[^A-Z0-9]+", "_", short_code.upper())

    val = THREAD_ID_MAP.get(key)
    return str(val) if val else None


def get_entry_translator_url(entry) -> str:
    for key in ("translator_url", "translatorUrl", "translatorurl"):
        value = entry.get(key)
        if value:
            return str(value).strip()
    return ""


def build_completion_attachment(novel: dict):
    if not COMPLETION_BANNER_SETTINGS["enabled"]:
        return None

    featured_image = (novel.get("featured_image") or "").strip()
    if not featured_image:
        print(f"⚠️ No featured image for {novel.get('novel_title', 'novel')}; sending text only.")
        return None

    output_size = parse_banner_ratio_to_size(COMPLETION_BANNER_SETTINGS["ratio"])
    crop_position = COMPLETION_BANNER_SETTINGS["crop"] or DEFAULT_COMPLETION_BANNER_SETTINGS["crop"]

    try:
        return build_announcement_banner(
            featured_image,
            output_size=output_size,
            crop_position=crop_position,
        )
    except Exception as exc:
        print(
            f"⚠️ Could not prepare completion banner for {novel.get('novel_title', 'novel')}: {exc}. "
            "Sending text only.",
            file=sys.stderr,
        )
        return None


def _entry_chapter_text(entry) -> str:
    """Text used for chapter matching."""
    parts = []
    for key in ("chapter", "chaptername", "volume"):
        value = (entry.get(key) or "").replace("\u00A0", " ").strip()
        if value:
            parts.append(value)
    return " ".join(parts).strip()


def _entry_display_text(entry, *, prefer_full: bool = False) -> str:
    base = (entry.get("chapter") or "").replace("\u00A0", " ").strip()
    full = _entry_chapter_text(entry)
    if prefer_full and full:
        return full
    return base or full


def _entry_sort_key(entry, fallback_index: int = 0):
    """Best-effort newest sorting for RSS entries."""
    parsed = entry.get("published_parsed") or entry.get("updated_parsed")
    if parsed:
        try:
            return datetime(*parsed[:6]).timestamp()
        except Exception:
            pass
    # feed entries are normally newest -> oldest, so earlier index is newer
    return -fallback_index


def _matching_title_entries(feed, novel_id: str):
    """Keep only entries for this novel, but allow feeds that omit title."""
    out = []
    for idx, entry in enumerate(feed.entries):
        entry_title = (entry.get("title") or "").strip()
        if entry_title and entry_title != novel_id:
            continue
        out.append((idx, entry))
    return out


def _extract_bonus_totals(chapter_count: str) -> tuple[int, int]:
    """Return (extras_total, side_stories_total) from chapter_count text."""
    text = chapter_count or ""
    m_ex = re.search(r"(\d+)\s*extras?", text, re.IGNORECASE)
    m_ss = re.search(r"(\d+)\s*(?:side story|side stories)", text, re.IGNORECASE)
    return (int(m_ex.group(1)) if m_ex else 0, int(m_ss.group(1)) if m_ss else 0)


def _find_bonus_entries(indexed_entries, raw_kw: str):
    """Return [(number, entry, index)] for entries like Extra 1 / Side Story 2."""
    if not raw_kw:
        return []

    if raw_kw.lower() == "side story":
        keyword = r"side\s+stor(?:y|ies)"
    else:
        keyword = rf"{re.escape(raw_kw)}s?"

    pattern = re.compile(rf"(?i)\b{keyword}\b.*?(\d+)")
    found = []
    for idx, entry in indexed_entries:
        for field in ("chapter", "chaptername", "volume"):
            value = entry.get(field, "") or ""
            match = pattern.search(value)
            if match:
                found.append((int(match.group(1)), entry, idx))
                break
    return found


def _latest_entry(items):
    if not items:
        return None
    # items are (number, entry, index)
    return max(items, key=lambda item: _entry_sort_key(item[1], item[2]))[1]


def _completion_target_entry(feed, novel: dict, feed_type: str):
    """
    Decide which feed entry is allowed to trigger completion.

    The configured last_chapter in novel_mappings.py is the source of truth.
    If the real end is Extra 3 / Side Story 2, set last_chapter to that value.
    Completion will then wait until that exact RSS entry exists and has already
    been posted by the chapter bot.
    """
    novel_id = novel["novel_title"]
    indexed_entries = _matching_title_entries(feed, novel_id)

    last_chap = novel.get("last_chapter") or ""
    for _idx, entry in indexed_entries:
        if last_chap and last_chap in _entry_chapter_text(entry):
            return entry, "chapter", ""

    return None, "chapter", f"→ no configured last_chapter found yet for {novel_id} in {feed_type} feed"


def _rss_seen_identities(feed_type: str) -> set[str]:
    rss_state = load_state(RSS_STATE_PATH)
    seen_key = require_feed_value(feed_type, "seen_key")
    return seen_guid_identities(rss_state.get(seen_key, []))


def _entry_was_already_announced(feed_type: str, entry, label: str) -> bool:
    guid_key = entry_guid_identity(entry)
    if not guid_key:
        print(f"⏳ Holding {label}: target RSS entry has no GUID, so chapter-send state cannot be verified.")
        return False

    if guid_key in _rss_seen_identities(feed_type):
        return True

    print(
        f"⏳ Holding {label}: {format_seen_guid(entry, default_host=HOST_NAME_TARGET)} "
        f"is not in state_rss.json::{require_feed_value(feed_type, 'seen_key')} yet."
    )
    return False


def build_completion_context(novel, chap_field, chap_link, duration: str = "") -> dict:
    translator_url = (
        novel.get("feed_translator_url", "")
        or novel.get("translator_url", "")
        or get_translator_url(novel.get("host", ""), novel.get("novel_title", ""))
        or TRANSLATOR_URL
    )

    return {
        "global_mention": GLOBAL_MENTION,
        "translator": novel.get("translator", ""),
        "translator_url": translator_url,
        "novel_title": novel.get("novel_title", ""),
        "novel_link": novel.get("novel_link", ""),
        "host": novel.get("host", ""),
        "chapter_count": novel.get("chapter_count", "the entire series"),
        "chapter_text": (chap_field or "").replace("\u00A0", " "),
        "chapter_link": chap_link,
        "duration": duration,
    }


def build_paid_completion(novel, chap_field, chap_link, duration: str):
    variant = "paid_with_duration" if duration else "paid_no_duration"
    ctx = build_completion_context(novel, chap_field, chap_link, duration)
    return render_message("completed_novels", ctx, variant=variant)


def build_free_completion(novel, chap_field, chap_link):
    ctx = build_completion_context(novel, chap_field, chap_link)
    return render_message("completed_novels", ctx, variant="free")


def build_only_free_completion(novel, chap_field, chap_link, duration: str):
    variant = "only_free_with_duration" if duration else "only_free_no_duration"
    ctx = build_completion_context(novel, chap_field, chap_link, duration)
    return render_message("completed_novels", ctx, variant=variant)
  

# ─── DATA LOAD ─────────────────────────────────────────────────────────────────
def load_novels() -> list[dict]:
    """
    Pull novels directly from HOSTING_SITE_DATA, but only include:
      - host == "Target Host"
      - last_chapter is defined
      - at least one feed present (free or paid)
    """
    novels = []
    for host, host_data in (HOSTING_SITE_DATA or {}).items():
        if host != HOST_NAME_TARGET:
            continue

        # default translator/profile for this host
        host_translator = host_data.get("translator", "")
        host_translator_url = host_data.get("translator_url", "")

        for title, details in host_data.get("novels", {}).items():
            last = details.get("last_chapter")
            if not last:
                continue
            free = details.get("free_feed")
            paid = details.get("paid_feed")
            if not (free or paid):
                continue

            novels.append({
                "novel_title":      title,
                "host":             host,
                "novel_link":       details.get("novel_url", ""),
                "featured_image":   details.get("featured_image", ""),
                "chapter_count":    details.get("chapter_count", ""),
                "last_chapter":     last,
                "start_date":       details.get("start_date", ""),
                "free_feed":        free,
                "paid_feed":        paid,
                "short_code":       details.get("short_code", ""),  # used for thread env
                # per-novel override, else host-level default
                "translator":       details.get("translator") or host_translator,
                "translator_url":   details.get("translator_url") or host_translator_url,
            })
    return novels


# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--feed", choices=["paid", "free"], required=True)
    args = parser.parse_args()

    bot_token = os.getenv(BOT_TOKEN_ENV)
    if not bot_token:
        sys.exit("❌ Missing DISCORD_BOT_TOKEN")

    state  = load_state()
    novels = load_novels()

    for novel in reversed(novels):
        novel_id  = novel["novel_title"]
        last_chap = novel.get("last_chapter")
        if not last_chap:
            continue

        # route: per-novel thread id (required; no fallback)
        thread_id = resolve_thread_id(novel_id, novel)
        if not thread_id:
            sc = (novel.get("short_code") or sanitize_shortcode_from_title(novel_id)).upper()
            print(
                f"❌ No thread id for {novel_id}. "
                f"Add \"{sc}\" to {THREAD_MAP_FILE}."
            )
            continue

        feed_type = args.feed              # "paid" or "free"
        feed_key  = f"{feed_type}_feed"    # "paid_feed" or "free_feed"
        url       = novel.get(feed_key)
        if not url:
            # Skip if this novel lacks the requested feed type
            continue

        # Generic skip check before parsing
        completion_key = "paid_completion" if feed_type == "paid" else "free_completion"
        if state.get(novel_id, {}).get(completion_key):
            print(f"→ skipping {novel_id} ({completion_key}) — already notified")
            continue

        # Fetch + parse RSS
        try:
            resp = requests.get(url, timeout=20)
            resp.raise_for_status()
        except requests.RequestException as e:
            print(f"⚠️ Failed to fetch feed for {novel_id}: {e}")
            continue

        feed = feedparser.parse(resp.text)
        print(f"Parsing {feed_key} for {novel_id}: {len(feed.entries)} entries")

        entry, target_kind, hold_reason = _completion_target_entry(feed, novel, feed_type)
        if not entry:
            print(hold_reason)
            continue

        label = f"{feed_type} completion for {novel_id}"
        if not _entry_was_already_announced(feed_type, entry, label):
            continue

        prefer_full_display = target_kind == "bonus"
        chap_display = _entry_display_text(entry, prefer_full=prefer_full_display)
        chapter_link = entry.get("link", "")
        novel_for_message = dict(novel, feed_translator_url=get_entry_translator_url(entry))
        completion_attachment = build_completion_attachment(novel)

        # compute a chapter timestamp for duration
        if entry.get("published_parsed"):
            chap_date = datetime(*entry.published_parsed[:6])
        elif entry.get("updated_parsed"):
            chap_date = datetime(*entry.updated_parsed[:6])
        else:
            chap_date = datetime.now()

        # ONLY-FREE (series with no paid feed at all)
        if feed_type == "free" and not novel.get("paid_feed"):
            if state.get(novel_id, {}).get("only_free_completion"):
                print(f"→ skipping {novel_id} (only_free_completion) — already notified")
                continue

            duration = get_duration(novel.get("start_date", ""), chap_date)
            msg = build_only_free_completion(novel_for_message, chap_display, chapter_link, duration)
            print(f"→ Built message of {len(msg.get('content', ''))} characters")

            if safe_send_bot(
                bot_token,
                thread_id,
                msg,
                attachment=completion_attachment,
            ):
                print(f"✔️ Sent only-free completion announcement for {novel_id} → thread {thread_id}")
                state.setdefault(novel_id, {})["only_free_completion"] = {
                    "chapter": chap_display,
                    "sent_at": datetime.now().isoformat()
                }
                save_state(state)
                commit_state_update(STATE_PATH)
            else:
                print(f"→ Not marking {novel_id} as only_free_completion (send failed)")

        # PAID completion
        elif feed_type == "paid":
            if state.get(novel_id, {}).get("paid_completion"):
                print(f"→ skipping {novel_id} (paid_completion) — already notified")
                continue

            duration = get_duration(novel.get("start_date", ""), chap_date)
            msg = build_paid_completion(novel_for_message, chap_display, chapter_link, duration)
            print(f"→ Built message of {len(msg.get('content', ''))} characters")

            if safe_send_bot(
                bot_token,
                thread_id,
                msg,
                attachment=completion_attachment,
            ):
                print(f"✔️ Sent paid-completion announcement for {novel_id} → thread {thread_id}")
                state.setdefault(novel_id, {})["paid_completion"] = {
                    "chapter": chap_display,
                    "sent_at": datetime.now().isoformat()
                }
                save_state(state)
                commit_state_update(STATE_PATH)
            else:
                print(f"→ Not marking {novel_id} as paid_completion (send failed)")

        # STANDARD FREE completion (series that also had a paid feed)
        elif feed_type == "free":
            if state.get(novel_id, {}).get("free_completion"):
                print(f"→ skipping {novel_id} (free_completion) — already notified")
                continue

            msg = build_free_completion(novel_for_message, chap_display, chapter_link)
            print(f"→ Built message of {len(msg.get('content', ''))} characters")

            if safe_send_bot(
                bot_token,
                thread_id,
                msg,
                attachment=completion_attachment,
            ):
                print(f"✔️ Sent free-completion announcement for {novel_id} → thread {thread_id}")
                state.setdefault(novel_id, {})["free_completion"] = {
                    "chapter": chap_display,
                    "sent_at": datetime.now().isoformat()
                }
                save_state(state)
                commit_state_update(STATE_PATH)
            else:
                print(f"→ Not marking {novel_id} as free_completion (send failed)")


if __name__ == "__main__":
    main()
