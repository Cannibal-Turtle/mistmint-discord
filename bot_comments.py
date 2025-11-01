#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
comments_thread_poster.py (mistmint-discord)

Reads aggregated comment RSS and posts each new item into the per-novel thread
(on Mistmint Haven only). Routing is via SHORTCODE env:
  <SHORTCODE>_THREAD_ID   e.g.,  TDLBKGC_THREAD_ID=1433327...

SHORTCODE comes from HOSTING_SITE_DATA[host].novels[title]['short_code'],
else derived from the novel title: uppercase and non-alnum -> underscore.

Env:
  DISCORD_BOT_TOKEN
  USE_UNARCHIVE=1        # optional: PATCH unarchive threads if archived

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

from novel_mappings import HOSTING_SITE_DATA

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ["DISCORD_BOT_TOKEN"]
STATE_FILE    = "state_rss.json"
FEED_KEY      = "comments_last_guid"
RSS_URL       = "https://raw.githubusercontent.com/Cannibal-Turtle/rss-feed/main/aggregated_comments_feed.xml"

HOST_TARGET   = "Mistmint Haven"          # only post Mistmint comments
USE_UNARCHIVE = os.getenv("USE_UNARCHIVE", "0") == "1"

# Hardcoded user to ping (your Discord USER id, not a role)
PING_USER_ID  = "603578473814032414"
# ────────────────────────────────────────────────────────────────────────────────


# ─── STATE ─────────────────────────────────────────────────────────────────────
def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        initial = {
            "free_last_guid": None,
            "paid_last_guid": None,
            "comments_last_guid": None
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(initial, f, indent=2, ensure_ascii=False)
        return initial

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


# ─── THREAD HELPERS ────────────────────────────────────────────────────────────
def sanitize_shortcode_from_title(title: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (title or "").upper()).strip("_")

def resolve_thread_id(novel_title: str) -> str | None:
    host_data = (HOSTING_SITE_DATA or {}).get(HOST_TARGET, {})
    details   = host_data.get("novels", {}).get(novel_title, {}) or {}
    sc = (details.get("short_code") or "").strip() or sanitize_shortcode_from_title(novel_title)
    env_key = f"{sc.upper()}_THREAD_ID"
    val = os.getenv(env_key, "").strip()
    if not val:
        print(f"❌ Missing env {env_key} for '{novel_title}'")
        return None
    return val

def unarchive_thread(thread_id: str, *, unlock: bool = True, auto_archive_minutes: int = 10080) -> bool:
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
        "allowed_mentions": allowed_mentions if allowed_mentions is not None else {"parse": []},
        "flags": 4  # suppress embeds in content
    }
    if embed:
        payload["embeds"] = [embed]

    # Preflight: join thread; optional unarchive
    ensure_bot_in_thread(thread_id)
    if USE_UNARCHIVE:
        unarchive_thread(thread_id, unlock=True, auto_archive_minutes=10080)

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
                fixed = unarchive_thread(thread_id, unlock=True, auto_archive_minutes=10080)
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
    guids   = [(e.get("guid") or e.get("id")) for e in entries]
    last    = state.get(FEED_KEY)
    to_send = entries[guids.index(last)+1:] if last in guids else entries

    if not to_send:
        print("🛑 No new comments to send.")
        return

    new_last = last
    print(f"🔎 Processing {len(to_send)} new comment(s)…")

    for entry in to_send:
        guid        = entry.get("guid") or entry.get("id")
        host        = (entry.get("host") or "").strip()
        novel_title = (entry.get("title") or "").strip()

        # Always advance past non-Mistmint items so we don't loop on them.
        if host != HOST_TARGET:
            print(f"↷ Skipping non-Mistmint host: {host}  ({novel_title})")
            new_last = guid
            continue

        # Resolve thread for this novel (SHORTCODE env). If missing, skip and advance.
        thread_id = resolve_thread_id(novel_title)
        if not thread_id:
            new_last = guid
            continue

        author      = entry.get("author") or entry.get("dc_creator", "") or "anonymous"
        chapter     = (entry.get("chapter") or "").strip()
        comment_txt = (entry.get("description") or "").strip()
        reply_chain = (entry.get("reply_chain") or "").strip()
        host_logo   = (getattr(entry, "hostLogo", None) or getattr(entry, "hostlogo", None) or {}).get("url", "")
        link        = (entry.get("link") or "").strip()
        pubdate_raw = getattr(entry, "published", None)
        timestamp   = dateparser.parse(pubdate_raw).isoformat() if pubdate_raw else None

        # Build a safe <=256 title: ❛❛...❜❜ with ellipsis if needed
        start_marker = "❛❛"
        end_marker   = "❜❜"
        ellipsis     = "..."
        content_max  = 256 - len(start_marker) - len(end_marker) - len(ellipsis)
        safe_comment = (comment_txt[:content_max].rstrip() + ellipsis) if len(comment_txt) > content_max else comment_txt
        full_title   = f"{start_marker}{safe_comment}{end_marker}"

        embed = {
            "author": {
                "name": f"comment by {author} 🕊️ {chapter}",
                "url":  link
            },
            "title":     full_title,
            "timestamp": timestamp,
            "color":     int("F0C7A4", 16),
            "footer": {
                "text":     host,
                "icon_url": host_logo
            }
        }
        if reply_chain:
            embed["description"] = reply_chain

        # Build content; only add the " || " if we actually have a mention
        user_mention = f"<@{PING_USER_ID}>" if PING_USER_ID else ""
        content = f"<a:7977heartslike:1368146209981857792> New comment for **{novel_title}**"
        if user_mention:
            content += f" || {user_mention}"

        # Only allow your user mention to ping
        allowed = {"parse": [], "users": [PING_USER_ID]} if PING_USER_ID else {"parse": []}

        try:
            post_message(thread_id, content, embed, allowed_mentions=allowed)
            print(f"✅ Sent comment {guid} → thread {thread_id}")
            new_last = guid
        except requests.HTTPError as e:
            status = e.response.status_code if e.response else "?"
            body   = e.response.text if e.response else ""
            print(f"❌ Error {status} for {guid}: {body}")
            # do NOT advance new_last so we retry next run

    # Persist last seen guid (even if we skipped non-Mistmint/untargetable items)
    if new_last and new_last != last:
        state[FEED_KEY] = new_last
        save_state(state)
        print(f"💾 Updated {STATE_FILE} → {new_last}")

if __name__ == "__main__":
    main()
