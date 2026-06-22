#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
completed_novel_checker.py (mistmint-discord)

Posts completion announcements for Mistmint Haven novels into their
per-novel threads (no fallback channel).

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
import subprocess
from datetime import datetime

import feedparser
import requests
from dateutil.relativedelta import relativedelta

from message_renderer import render_message, to_discord_api_payload

try:
    from novel_mappings import HOSTING_SITE_DATA
except Exception as e:
    print(f"⚠️ novel_mappings not available ({e}); using empty HOSTING_SITE_DATA.")
    HOSTING_SITE_DATA = {}
  

# ─── CONFIG ────────────────────────────────────────────────────────────────────
from config_loader import (
    THREAD_ID_MAP,
    THREAD_MAP_FILE,
    env_bool,
    require_file_value,
    require_server_value,
)

STATE_PATH     = require_file_value("state_path")
BOT_TOKEN_ENV  = "DISCORD_BOT_TOKEN"

HOST_NAME_TARGET = require_server_value("host_target")
GLOBAL_MENTION   = require_server_value("global_mention")
AUTHOR_URL = require_server_value("author_url")

USE_UNARCHIVE = env_bool("USE_UNARCHIVE", False)
DEFAULT_AUTO_ARCHIVE_MINUTES = int(require_server_value("default_auto_archive_minutes"))
# ────────────────────────────────────────────────────────────────────────────────


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


def commit_state_update(path=STATE_PATH):
    try:
        subprocess.run(["git","config","--global","user.name","GitHub Actions"], check=True)
        subprocess.run(["git","config","--global","user.email","actions@github.com"], check=True)
        subprocess.run(["git","add", path], check=True)
        # commit only if there are staged changes
        staged = subprocess.run(["git","diff","--staged","--quiet"])
        if staged.returncode != 0:
            subprocess.run(["git","commit","-m", f"Auto-update: {os.path.basename(path)}"], check=True)
            subprocess.run(["git","push","origin","main"], check=True)
        else:
            print(f"ℹ️ No changes detected in {path}, skipping commit.")
    except Exception as e:
        print(f"❌ Git commit/push for {path} failed: {e}")


# ─── DISCORD SENDER ────────────────────────────────────────────────────────────
def send_bot_message(bot_token: str, channel_or_thread_id: str, message_payload: dict):
    """
    POST rendered TOML payload via bot token to the given channel/thread ID.
    Threads are also channels in Discord API, so same endpoint works.
    """
    url = f"https://discord.com/api/v10/channels/{channel_or_thread_id}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type":  "application/json"
    }

    payload = to_discord_api_payload(message_payload)

    def _send():
        return requests.post(url, headers=headers, json=payload, timeout=20)

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

def safe_send_bot(bot_token: str, channel_or_thread_id: str, message_payload: dict) -> bool:
    try:
        send_bot_message(bot_token, channel_or_thread_id, message_payload)
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


def build_completion_context(novel, chap_field, chap_link, duration: str = "") -> dict:
    return {
        "global_mention": GLOBAL_MENTION,
        "translator": novel.get("translator", ""),
        "author_url": AUTHOR_URL,
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
      - host == "Mistmint Haven"
      - last_chapter is defined
      - at least one feed present (free or paid)
    """
    novels = []
    for host, host_data in (HOSTING_SITE_DATA or {}).items():
        if host != HOST_NAME_TARGET:
            continue

        # default translator for this host
        host_translator = host_data.get("translator", "")

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
                "chapter_count":    details.get("chapter_count", ""),
                "last_chapter":     last,
                "start_date":       details.get("start_date", ""),
                "free_feed":        free,
                "paid_feed":        paid,
                "short_code":       details.get("short_code", ""),  # used for thread env
                # per-novel override, else host-level default
                "translator":       details.get("translator", host_translator),
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

        # Search for the last_chapter marker in feed entries
        for entry in feed.entries:
            # (optional) guard by novel title if using shared feed
            entry_title = (entry.get("title") or "").strip()
            if entry_title and entry_title != novel_id:
                continue
        
            base = entry.get("chapter") or entry.get("chapter", "") or ""
            ext  = entry.get("chaptername") or ""
        
            # 1) use combined string only for matching
            chap_match = f"{base} {ext}".strip()
        
            if last_chap not in chap_match:
                continue
        
            # 2) use a clean title for display (prefer base)
            chap_display = base or chap_match

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
                    break
            
                duration = get_duration(novel.get("start_date", ""), chap_date)
                msg = build_only_free_completion(novel, chap_display, entry.link, duration)
                print(f"→ Built message of {len(msg.get('content', ''))} characters")
            
                if safe_send_bot(bot_token, thread_id, msg):
                    print(f"✔️ Sent only-free completion announcement for {novel_id} → thread {thread_id}")
                    state.setdefault(novel_id, {})["only_free_completion"] = {
                        "chapter": chap_display,
                        "sent_at": datetime.now().isoformat()
                    }
                    save_state(state)
                    commit_state_update(STATE_PATH)
                else:
                    print(f"→ Not marking {novel_id} as only_free_completion (send failed)")
                break
            
            # PAID completion
            elif feed_type == "paid":
                if state.get(novel_id, {}).get("paid_completion"):
                    print(f"→ skipping {novel_id} (paid_completion) — already notified")
                    break
            
                duration = get_duration(novel.get("start_date", ""), chap_date)
                msg = build_paid_completion(novel, chap_display, entry.link, duration)
                print(f"→ Built message of {len(msg.get('content', ''))} characters")
            
                if safe_send_bot(bot_token, thread_id, msg):
                    print(f"✔️ Sent paid-completion announcement for {novel_id} → thread {thread_id}")
                    state.setdefault(novel_id, {})["paid_completion"] = {
                        "chapter": chap_display,
                        "sent_at": datetime.now().isoformat()
                    }
                    save_state(state)
                    commit_state_update(STATE_PATH)
                else:
                    print(f"→ Not marking {novel_id} as paid_completion (send failed)")
                break
            
            # STANDARD FREE completion (series that also had a paid feed)
            elif feed_type == "free":
                if state.get(novel_id, {}).get("free_completion"):
                    print(f"→ skipping {novel_id} (free_completion) — already notified")
                    break
            
                msg = build_free_completion(novel, chap_display, entry.link)
                print(f"→ Built message of {len(msg.get('content', ''))} characters")
            
                if safe_send_bot(bot_token, thread_id, msg):
                    print(f"✔️ Sent free-completion announcement for {novel_id} → thread {thread_id}")
                    state.setdefault(novel_id, {})["free_completion"] = {
                        "chapter": chap_display,
                        "sent_at": datetime.now().isoformat()
                    }
                    save_state(state)
                    commit_state_update(STATE_PATH)
                else:
                    print(f"→ Not marking {novel_id} as free_completion (send failed)")
                break


if __name__ == "__main__":
    main()
