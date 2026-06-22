#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_comments.py (mistmint-discord)

Reads aggregated comment RSS and posts each new item into the per-novel thread
(on Mistmint Haven only). Routing:
  - Thread IDs are resolved from thread_id_map.json using novel short_code.

SHORTCODE comes from the RSS entry's <short_code>,
else derived from the novel title.

Env:
  DISCORD_BOT_TOKEN
  USE_UNARCHIVE=1        # optional: PATCH unarchive s if archived

State:
  Stores last processed guid in state_rss.json under comments_last_guid
"""

import os
import json
import re
import time
import requests
import feedparser
from dateutil import parser as dateparser

# ─── CONFIG ────────────────────────────────────────────────────────────────────
from config_loader import (
    THREAD_ID_MAP,
    THREAD_MAP_FILE,
    embed_color,
    env_bool,
    require_feed_value,
    require_feeds_value,
    require_file_value,
    require_server_value,
    server_value,
)

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

STATE_FILE = require_file_value("rss_state_path")
RSS_URL    = require_feed_value("comments", "url")
SEEN_KEY   = require_feed_value("comments", "seen_key")
SEEN_CAP   = int(require_feeds_value("seen_cap"))

HOST_TARGET = require_server_value("host_target")

PING_USER_ID = str(
    os.getenv("PING_USER_ID") or server_value("ping_user_id", "")
).strip()

USE_UNARCHIVE = env_bool("USE_UNARCHIVE", False)
DEFAULT_AUTO_ARCHIVE_MINUTES = int(require_server_value("default_auto_archive_minutes"))
# ────────────────────────────────────────────────────────────────────────────────


# ─── STATE ─────────────────────────────────────────────────────────────────────
def load_state():
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        st = {
            "free_last_guid": None,
            "paid_last_guid": None,
            SEEN_KEY: []
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2, ensure_ascii=False)
        return st

    if SEEN_KEY not in st:
        st[SEEN_KEY] = []
        save_state(st)

    return st

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ─── THREAD HELPERS ────────────────────────────────────────────────────────────
def get_entry_short_code(entry) -> str:
    for key in ("short_code", "shortcode", "shortCode", "short"):
        v = entry.get(key)
        if v:
            return str(v).strip().upper()

        v = entry.get(key.lower()) or entry.get(key.upper())
        if v:
            return str(v).strip().upper()

    return ""
  
def sanitize_shortcode_from_title(title: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (title or "").upper()).strip("_")

def resolve_thread_id(novel_title: str, short_code: str = "") -> str | None:
    # Feed short_code first, then title fallback
    sc_raw = (
        (short_code or "").strip()
        or sanitize_shortcode_from_title(novel_title)
    )
    sc_key = sc_raw.upper()
  
    thread_id = (THREAD_ID_MAP.get(sc_key) or THREAD_ID_MAP.get(sc_raw) or "").strip() or None
  
    if not thread_id:
        print(
            f"❌ Missing thread id for shortcode '{sc_key}' (novel='{novel_title}') "
            f"in {THREAD_MAP_FILE}"
        )
        return None

    return thread_id

def unarchive_thread(thread_id: str, *, unlock: bool = True, auto_archive_minutes: int | None = None) -> bool:
    if auto_archive_minutes is None:
        auto_archive_minutes = DEFAULT_AUTO_ARCHIVE_MINUTES
      
    """Unarchive a thread so we can post. Needs MANAGE_THREADS on the bot."""
    url = f"https://discord.com/api/v10/channels/{thread_id}"
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    payload = {"archived": False}
    if unlock:
        payload["locked"] = False
    if auto_archive_minutes:
        payload["auto_archive_duration"] = auto_archive_minutes  # 60, 1440, 4320, 10080
    r = requests.patch(url, headers=headers, json=payload, timeout=15)
    if not r.ok:
        print(f"⚠️ Unarchive failed {r.status_code}: {r.text}")
    return r.ok

def ensure_bot_in_thread(thread_id: str) -> bool:
    """Ensure the bot is a member of the thread (handles 50001/403 cases)."""
    try:
        h = {"Authorization": f"Bot {BOT_TOKEN}"}
        r = requests.get(
            f"https://discord.com/api/v10/channels/{thread_id}/thread-members/@me",
            headers=h, timeout=15
        )
        if r.status_code == 200:
            return True
        j = requests.put(
            f"https://discord.com/api/v10/channels/{thread_id}/thread-members/@me",
            headers=h, timeout=15
        )
        return j.status_code in (200, 204)
    except requests.RequestException:
        return False

def post_message(thread_id: str, content: str, embed: dict | None = None, allowed_mentions: dict | None = None):
    """POST to thread with one-shot recovery for archived/missing access + 429 backoff."""
    url = f"https://discord.com/api/v10/channels/{thread_id}/messages"
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "content": content or "",
        "allowed_mentions": allowed_mentions if allowed_mentions is not None else {"parse": []}
    }
    if embed:
        payload["embeds"] = [embed]

    # Preflight: join thread; optional unarchive
    ensure_bot_in_thread(thread_id)
    if USE_UNARCHIVE:
        unarchive_thread(thread_id, unlock=True, auto_archive_minutes=DEFAULT_AUTO_ARCHIVE_MINUTES)

    def _send():
        return requests.post(url, headers=headers, json=payload, timeout=20)

    r = _send()

    # Archived / missing access → fix once and retry
    if r.status_code in (400, 403):
        try:
            body = r.json()
        except Exception:
            body = {"message": r.text}
        msg  = (body.get("message") or "").lower()
        code = body.get("code")

        fixed = False
        if "archiv" in msg:
            if USE_UNARCHIVE:
                fixed = unarchive_thread(thread_id, unlock=True, auto_archive_minutes=DEFAULT_AUTO_ARCHIVE_MINUTES)
            else:
                print("ℹ️ Thread is archived and USE_UNARCHIVE=0; not patching.")
        if not fixed and (code in (50001, 50013) or "missing access" in msg):
            fixed = ensure_bot_in_thread(thread_id)

        if fixed:
            time.sleep(0.8)
            r = _send()

    # 429 backoff: prefer header, fallback to body
    if r.status_code == 429:
        reset_after = r.headers.get("X-RateLimit-Reset-After") or r.headers.get("x-ratelimit-reset-after")
        try:
            wait = float(reset_after) if reset_after is not None else float(r.json().get("retry_after", 1.0))
        except Exception:
            wait = 1.0
        time.sleep(min(max(wait, 0.0), 5.0))
        r = _send()

    if not r.ok:
        print(f"⚠️ Discord error {r.status_code}: {r.text}")
    r.raise_for_status()
    return r


# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    state   = load_state()
    feed    = feedparser.parse(RSS_URL)
    entries = list(reversed(feed.entries))  # oldest → newest
    to_send = entries

    if not to_send:
        print("🛑 No new comments to send.")
        return

    print(f"🔎 Processing {len(to_send)} new comment(s)…")

    for entry in to_send:
        guid        = entry.get("guid") or entry.get("id")
        host        = (entry.get("host") or "").strip()
        novel_title = (entry.get("title") or "").strip()

        norm = f"{host}::{guid}"
        if norm in state[SEEN_KEY]:
            print(f"↷ Already sent, skipping {norm}")
            continue

        if host != HOST_TARGET:
            print(f"↷ Skipping non-Mistmint host: {host}  ({novel_title})")
            continue

        short_code = get_entry_short_code(entry)
        thread_id = resolve_thread_id(novel_title, short_code)
        if not thread_id:
            continue

        author      = entry.get("author") or entry.get("dc_creator", "") or "anonymous"
        chapter     = (entry.get("chapter") or "").strip()
        comment_txt = (entry.get("description") or "").strip()
        reply_chain = (entry.get("reply_chain") or "").strip()
        host_logo   = (getattr(entry, "hostLogo", None) or getattr(entry, "hostlogo", None) or {}).get("url", "")
        comment_image_obj = entry.get("commentImage") or entry.get("commentimage") or {}
        comment_image = comment_image_obj.get("url", "").strip() if isinstance(comment_image_obj, dict) else ""
        link        = (entry.get("link") or "").strip()
        pubdate_raw = getattr(entry, "published", None)
        timestamp   = dateparser.parse(pubdate_raw).isoformat() if pubdate_raw else None

        # Build a safe <=256 title: ❛❛...❜❜ with ellipsis if needed
        start_marker = "❛❛"
        end_marker   = "❜❜"
        ellipsis     = "..."
        content_max  = 256 - len(start_marker) - len(end_marker) - len(ellipsis)
        safe_comment = (comment_txt[:content_max].rstrip() + ellipsis) if len(comment_txt) > content_max else comment_txt
        full_title   = "" if (comment_image and comment_txt == "Sticker comment") else f"{start_marker}{safe_comment}{end_marker}"

        embed = {
            "author": {
                "name": f"comment by {author} 🕊️ {chapter}",
                "url":  link
            },
            "timestamp": timestamp,
            "color":     embed_color("comments", "F0C7A4"),
            "footer": {
                "text":     host,
                "icon_url": host_logo
            }
        }
        
        if full_title:
            embed["title"] = full_title

        if comment_image:
            embed["image"] = {"url": comment_image}
  
        if reply_chain:
            embed["description"] = reply_chain

        # Build content; only add the " || " if we actually have a mention
        user_mention = f"<@{PING_USER_ID}>" if PING_USER_ID else ""
        content = f"<a:7977heartslike:1368146209981857792> New comment for **{novel_title}** <a:flowersandpetals:1444260426182295623>"
        if user_mention:
            content += f" ||{user_mention}||"

        # Only allow your user mention to ping
        allowed = {"parse": [], "users": [PING_USER_ID]} if PING_USER_ID else {"parse": []}

        try:
            post_message(thread_id, content, embed, allowed_mentions=allowed)
            print(f"✅ Sent comment {guid} → thread {thread_id}")
            
            state[SEEN_KEY].append(norm)
            state[SEEN_KEY] = state[SEEN_KEY][-SEEN_CAP:]
            save_state(state)

        except requests.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            body   = e.response.text if e.response else ""
            print(f"❌ Error {status} for {guid}: {body}")
            # do NOT advance new_last so we retry next run

if __name__ == "__main__":
    main()
