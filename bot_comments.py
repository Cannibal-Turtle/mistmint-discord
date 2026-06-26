#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bot_comments.py (for thread servers)

Reads aggregated comment RSS and posts each new item into the per-novel thread
Routing:
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
from datetime import datetime, timezone
from dateutil import parser as dateparser

import requests
import feedparser

from message_context import build_feed_context
from message_renderer import render_message, to_discord_api_payload

# ─── CONFIG ────────────────────────────────────────────────────────────────────
from config_loader import (
    THREAD_ID_MAP,
    THREAD_MAP_FILE,
    env_bool,
    require_feed_value,
    require_feeds_value,
    require_feed_url,
    require_file_value,
    require_server_value,
    server_value,
)

BOT_TOKEN = os.environ["DISCORD_BOT_TOKEN"]

STATE_FILE = require_file_value("rss_state_path")
RSS_URL    = require_feed_url("comments")

FEED_KEY       = require_feed_value("comments", "last_guid_key")
SEEN_KEY       = require_feed_value("comments", "seen_key")
LAST_POST_TIME = require_feed_value("comments", "last_post_time_key")
SEEN_CAP       = int(require_feeds_value("seen_cap"))
TIME_BACKSTOP  = bool(require_feeds_value("time_backstop"))

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
            FEED_KEY: None,
            SEEN_KEY: [],
            LAST_POST_TIME: None,
        }
        save_state(st)
        return st

    changed = False

    if FEED_KEY not in st:
        st[FEED_KEY] = None
        changed = True

    if SEEN_KEY not in st:
        st[SEEN_KEY] = []
        changed = True

    if LAST_POST_TIME not in st:
        st[LAST_POST_TIME] = None
        changed = True

    if changed:
        save_state(st)

    return st


def save_state(state):
    if isinstance(state.get(SEEN_KEY), list) and len(state[SEEN_KEY]) > SEEN_CAP:
        state[SEEN_KEY] = state[SEEN_KEY][-SEEN_CAP:]

    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def normalize_guid(entry):
    host = (entry.get("host") or "").strip()
    guid = (entry.get("guid") or entry.get("id") or "").strip()
    return f"{host}::{guid}" if guid else ""


def parse_pub_iso(entry):
    pub_raw = getattr(entry, "published", None)
    if not pub_raw:
        return None

    try:
        return dateparser.parse(pub_raw)
    except Exception:
        return None


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

def post_payload(thread_id: str, payload: dict):
    """POST rendered template payload to thread with recovery for archived/missing access + 429 backoff."""
    url = f"https://discord.com/api/v10/channels/{thread_id}/messages"
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}

    payload = dict(payload)

    if "allowed_mentions" not in payload:
        payload["allowed_mentions"] = {"parse": []}

    # Preflight: join thread; optional unarchive
    ensure_bot_in_thread(thread_id)

    if USE_UNARCHIVE:
        unarchive_thread(
            thread_id,
            unlock=True,
            auto_archive_minutes=DEFAULT_AUTO_ARCHIVE_MINUTES,
        )

    def _send():
        return requests.post(url, headers=headers, json=payload, timeout=20)

    r = _send()

    # Archived / missing access → fix once and retry
    if r.status_code in (400, 403):
        try:
            body = r.json()
        except Exception:
            body = {"message": r.text}

        msg = (body.get("message") or "").lower()
        code = body.get("code")

        fixed = False

        if "archiv" in msg:
            if USE_UNARCHIVE:
                fixed = unarchive_thread(
                    thread_id,
                    unlock=True,
                    auto_archive_minutes=DEFAULT_AUTO_ARCHIVE_MINUTES,
                )
            else:
                print("ℹ️ Thread is archived and USE_UNARCHIVE=0; not patching.")

        if not fixed and (code in (50001, 50013) or "missing access" in msg):
            fixed = ensure_bot_in_thread(thread_id)

        if fixed:
            time.sleep(0.8)
            r = _send()

    # 429 backoff
    if r.status_code == 429:
        reset_after = (
            r.headers.get("X-RateLimit-Reset-After")
            or r.headers.get("x-ratelimit-reset-after")
        )

        try:
            wait = (
                float(reset_after)
                if reset_after is not None
                else float(r.json().get("retry_after", 1.0))
            )
        except Exception:
            wait = 1.0

        time.sleep(min(max(wait, 0.0), 5.0))
        r = _send()

    if not r.ok:
        print(f"⚠️ Discord error {r.status_code}: {r.text}")

    r.raise_for_status()
    return r

def build_comment_title(comment_txt: str, comment_image: str = "") -> str:
    start_marker = "❛❛"
    end_marker = "❜❜"
    ellipsis = "..."

    content_max = 256 - len(start_marker) - len(end_marker) - len(ellipsis)

    if len(comment_txt) > content_max:
        safe_comment = comment_txt[:content_max].rstrip() + ellipsis
    else:
        safe_comment = comment_txt

    if comment_image and comment_txt == "Sticker comment":
        return ""

    return f"{start_marker}{safe_comment}{end_marker}"


def setting_bool(env_name: str, server_key: str, default: bool = False) -> bool:
    raw = os.getenv(env_name)

    if raw is None:
        raw = server_value(server_key, default)

    if isinstance(raw, bool):
        return raw

    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def include_novel_updates_comments() -> bool:
    return setting_bool(
        "INCLUDE_NOVEL_UPDATES_COMMENTS",
        "include_novel_updates_comments",
        False,
    )


def _host_key(host: str) -> str:
    return " ".join(str(host or "").strip().casefold().split())


def is_target_host(host: str) -> bool:
    return _host_key(host) == _host_key(HOST_TARGET)


def is_novel_updates_host(host: str) -> bool:
    key = _host_key(host)
    compact = key.replace(" ", "").replace(".", "")
    return key == "novel updates" or compact in {
        "novelupdates",
        "novelupdatescom",
        "novelupdate",
    }


# ─── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    state   = load_state()
    feed    = feedparser.parse(RSS_URL)
    entries = list(reversed(feed.entries))  # oldest → newest

    seen = set(state.get(SEEN_KEY, []))
    last_post_time = state.get(LAST_POST_TIME)
    last_post_dt = (
        dateparser.parse(last_post_time)
        if (TIME_BACKSTOP and last_post_time)
        else None
    )

    include_nu_comments = include_novel_updates_comments()
    skipped_nu_comments = 0

    to_send = []
    for e in entries:
        host = (e.get("host") or "").strip()
        is_nu = is_novel_updates_host(host)

        norm = normalize_guid(e)
        if not norm:
            continue

        if norm in seen:
            continue

        if is_nu and not include_nu_comments:
            state[SEEN_KEY].append(norm)
            seen.add(norm)
            skipped_nu_comments += 1
            continue

        if not is_target_host(host) and not (include_nu_comments and is_nu):
            continue

        if last_post_dt is not None:
            dt = parse_pub_iso(e)
            if dt and dt <= last_post_dt:
                continue

        to_send.append(e)

    if skipped_nu_comments:
        save_state(state)
        print(
            f"🚫 Skipped {skipped_nu_comments} Novel Updates comment(s) "
            "because include_novel_updates_comments is false."
        )

    if not to_send:
        print("🛑 No new comments to send.")
        return

    print(f"🔎 Processing {len(to_send)} new comment(s)…")

    for entry in to_send:
        guid        = entry.get("guid") or entry.get("id")
        novel_title = (entry.get("title") or "").strip()

        norm = normalize_guid(entry)
        if norm in state[SEEN_KEY]:
            print(f"↷ Already sent, skipping {norm}")
            continue

        short_code = get_entry_short_code(entry)
        thread_id = resolve_thread_id(novel_title, short_code)
        if not thread_id:
            continue

        ctx = build_feed_context(entry)
        
        # Preserve the old fallback behavior.
        if not ctx["author"]:
            ctx["author"] = "anonymous"
        
        comment_txt = ctx["description"]
        comment_image = ctx["comment_image_url"]
        
        color_key = (
            "novel_updates_comments"
            if ctx["host"].strip().lower() == "novel updates"
            else "comments"
        )
        
        user_mention = f"<@{PING_USER_ID}>" if PING_USER_ID else ""
        
        ctx.update({
            "comment_title": build_comment_title(comment_txt, comment_image),
            "comment_color_key": color_key,
            "comment_user_ping_tail": f" ||{user_mention}||" if user_mention else "",
            "ping_user_id": PING_USER_ID,
        })
        
        payload = to_discord_api_payload(render_message("comments", ctx))
        
        try:
            post_payload(thread_id, payload)
            print(f"✅ Sent comment {guid} → thread {thread_id}")
            
            state[SEEN_KEY].append(norm)
            state[FEED_KEY] = guid

            dt = parse_pub_iso(entry) or datetime.now(timezone.utc)
            state[LAST_POST_TIME] = dt.isoformat()

            save_state(state)

        except requests.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            body   = e.response.text if e.response else ""
            print(f"❌ Error {status} for {guid}: {body}")
            # do NOT advance new_last so we retry next run

if __name__ == "__main__":
    main()
