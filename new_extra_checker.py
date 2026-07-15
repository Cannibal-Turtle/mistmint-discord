#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
new_extra_checker.py (for thread servers)

Detects Extras / Side Stories in paid feeds and posts ONE announcement
into each novel's per-thread channel (no global fallback).

Routing:
  - Thread IDs are resolved from thread_id_map.json using novel short_code.
  - short_code taken from HOSTING_SITE_DATA[host].novels[title]['short_code']
    else derived from title: uppercase + non-alnum -> underscore.

Behavior:
  - Only processes novels under target host.
  - Skips if series already completed (paid/free/only_free completion keys in state.json).
  - Sends at most once per novel (meta.extra_announced = True).

Env:
  - DISCORD_BOT_TOKEN
"""

import os
import json
import re
import sys
import requests
import feedparser
import time
from message_renderer import render_message, to_discord_api_payload
from git_state_commit import commit_state_update
from novel_mappings import HOSTING_SITE_DATA

# ─── CONFIG ────────────────────────────────────────────────────────────────────
from config_loader import (
    THREAD_ID_MAP,
    THREAD_MAP_FILE,
    env_bool,
    require_file_value,
    require_server_value,
)

STATE_PATH   = require_file_value("state_path")
HOST_TARGET  = require_server_value("host_target")
BOT_TOKEN_ENV  = "DISCORD_BOT_TOKEN"

USE_UNARCHIVE = env_bool("USE_UNARCHIVE", False)
DEFAULT_AUTO_ARCHIVE_MINUTES = int(require_server_value("default_auto_archive_minutes"))
# ────────────────────────────────────────────────────────────────────────────────


# ─── DISCORD SEND (per-thread) ─────────────────────────────────────────────────

def unarchive_thread(bot_token: str, thread_id: str, *, unlock: bool = True, auto_archive_minutes: int | None = None) -> bool:
    if auto_archive_minutes is None:
        auto_archive_minutes = DEFAULT_AUTO_ARCHIVE_MINUTES
      
    """Unarchive a thread so we can post. Needs MANAGE_THREADS."""
    url = f"https://discord.com/api/v10/channels/{thread_id}"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}
    payload = {"archived": False}
    if unlock:
        payload["locked"] = False            # if it was locked on archive
    if auto_archive_minutes:
        payload["auto_archive_duration"] = auto_archive_minutes  # 60, 1440, 4320, 10080
    r = requests.patch(url, headers=headers, json=payload, timeout=15)
    if not r.ok:
        print(f"⚠️ Unarchive failed {r.status_code}: {r.text}")
    return r.ok
  
def ensure_bot_in_thread(bot_token: str, thread_id: str) -> bool:
    """Ensure the bot is a member of the thread (handles 50001/403 cases)."""
    try:
        h = {"Authorization": f"Bot {bot_token}"}
        # already a member?
        r = requests.get(
            f"https://discord.com/api/v10/channels/{thread_id}/thread-members/@me",
            headers=h, timeout=15
        )
        if r.status_code == 200:
            return True
        # try join
        j = requests.put(
            f"https://discord.com/api/v10/channels/{thread_id}/thread-members/@me",
            headers=h, timeout=15
        )
        return j.status_code in (200, 204)
    except requests.RequestException:
        return False


def send_bot_payload(bot_token: str, thread_id: str, message_payload: dict):
    """POST rendered TOML payload to thread; auto-join on Missing Access and retry; simple 429 backoff."""
    url = f"https://discord.com/api/v10/channels/{thread_id}/messages"
    headers = {"Authorization": f"Bot {bot_token}", "Content-Type": "application/json"}

    payload = to_discord_api_payload(message_payload)

    if "allowed_mentions" not in payload:
        payload["allowed_mentions"] = {"parse": []}

    # Preflight: join (idempotent)
    ensure_bot_in_thread(bot_token, thread_id)

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
                fixed = unarchive_thread(
                    bot_token,
                    thread_id,
                    unlock=True,
                    auto_archive_minutes=DEFAULT_AUTO_ARCHIVE_MINUTES,
                )
            else:
                print("ℹ️ Thread is archived and USE_UNARCHIVE=0; not patching. Unlock it or grant Manage Threads.")

        if not fixed and (code in (50001, 50013) or "missing access" in msg):
            fixed = ensure_bot_in_thread(bot_token, thread_id)

        if fixed:
            time.sleep(0.8)
            r = _send()

    # Rate limit backoff
    if r.status_code == 429:
        wait = None
        reset_after = r.headers.get("X-RateLimit-Reset-After") or r.headers.get("x-ratelimit-reset-after")

        if reset_after:
            try:
                wait = float(reset_after)
            except (TypeError, ValueError):
                wait = None

        if wait is None:
            try:
                wait = float(r.json().get("retry_after", 1.0))
            except Exception:
                wait = 1.0

        time.sleep(min(max(wait, 0.0), 5.0))
        r = _send()

    if not r.ok:
        print(f"⚠️ Discord error {r.status_code}: {r.text}")
        r.raise_for_status()

    return r

def safe_send_bot_payload(bot_token: str, thread_id: str, message_payload: dict) -> bool:
    try:
        send_bot_payload(bot_token, thread_id, message_payload)
        print(f"✅ Posted to thread {thread_id}")
        return True

    except requests.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        body   = e.response.text if e.response else ""
        print(f"⚠️ Failed to send to {thread_id} ({status}): {body}", file=sys.stderr)
        return False

    except requests.RequestException as e:
        print(f"⚠️ Failed to send to {thread_id}: {e}", file=sys.stderr)
        return False

# ─── STATE ─────────────────────────────────────────────────────────────────────
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


# ─── HELPERS ───────────────────────────────────────────────────────────────────
def find_released_extra_entries(paid_feed, raw_kw):
    """Return [(number, entry)] for a given keyword group (extra / side story)."""
    if not raw_kw:
        return []

    # capture a trailing number after the keyword
    if raw_kw.lower() == "side story":
        keyword = r"side\s+stor(?:y|ies)"
    else:
        keyword = rf"{re.escape(raw_kw)}s?"

    # The bonus label must begin the field (or follow a leading "Chapter N"
    # prefix). This avoids treating ordinary titles such as
    # "Put Extra Seasoning on It" as bonus content.
    label_prefix = r"^\s*(?:chapter\s+\d+\s*(?:[-:|–—]\s*)?)?"
    numbered_pattern = re.compile(
        rf"(?i){label_prefix}{keyword}\s*(?:[-:|–—]\s*)?(\d+)\b"
    )
    keyword_pattern = re.compile(
        rf"(?i){label_prefix}{keyword}(?=\s*(?:$|[-:|–—]|\d+\b))"
    )
    found = []

    for e in paid_feed.entries:
        values = [e.get(field, "") or "" for field in ("chapter", "chaptername", "volume")]

        # Prefer an explicit bonus number, e.g. "Extra 2" or "Side Story 3".
        explicit_number = None
        for val in values:
            m = numbered_pattern.search(val)
            if m:
                explicit_number = int(m.group(1))
                break

        if explicit_number is not None:
            found.append((explicit_number, e))
            continue

        # Some Mistmint chapters use an ordinary chapter number plus an
        # unnumbered label, e.g.:
        #   <chapter>Chapter 131</chapter>
        #   <chaptername>Extra | The Tale of Tiny Wan Wan</chaptername>
        # Treat the first such bonus item as bonus #1 so the announcement gate
        # can open. This checker sends only one extras announcement per novel.
        if any(keyword_pattern.search(val) for val in values):
            found.append((1, e))

    return found


def find_released_extras(paid_feed, raw_kw):
    """Find max index released for a given keyword group (extra / side story)."""
    return {number for number, _entry in find_released_extra_entries(paid_feed, raw_kw)}


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

# ─── CORE ──────────────────────────────────────────────────────────────────────
def process_extras(novel: dict):
    """
    novel dict fields:
      novel_id, novel_title, paid_feed, chapter_count, last_chapter,
      host, novel_link, short_code
    """
    thread_id = resolve_thread_id(novel["novel_title"], novel)
    if not thread_id:
        return

    # 1) parse the paid feed up-front
    paid_feed = feedparser.parse(novel["paid_feed"])

    # 🔒 TITLE GUARD — keep only entries that belong to THIS novel
    novel_title = novel["novel_title"].strip()
    filtered = []
    for e in paid_feed.entries:
        entry_title = (e.get("title") or "").strip()
        if entry_title and entry_title == novel_title:
            filtered.append(e)

    paid_feed.entries = filtered
    print(f"🔐 Title-guarded extras feed for {novel_title}: {len(filtered)} entries kept")

    # Do not skip just because the main final chapter is still visible in RSS.
    # Extras often appear while that final-chapter entry is still inside the feed window.

    # 2) load state & guard against completion
    state    = load_state()
    novel_id = novel["novel_id"]
    meta     = state.setdefault(novel_id, {})
    if meta.get("paid_completion") or meta.get("free_completion") or meta.get("only_free_completion"):
        print(f"→ skipping extras for {novel_id} — already completed (state.json)")
        return

    # 3) see what actually dropped
    extra_entries = find_released_extra_entries(paid_feed, "extra")
    ss_entries    = find_released_extra_entries(paid_feed, "side story")
    dropped_extras = {number for number, _entry in extra_entries}
    dropped_ss     = {number for number, _entry in ss_entries}
    max_ex = max(dropped_extras) if dropped_extras else 0
    max_ss = max(dropped_ss)     if dropped_ss     else 0

    # 4) only announce when something new appears; cap to one lifetime send
    if meta.get("extra_announced"):
        print(f"→ extras already announced for {novel_id}; skipping")
        return

    last    = meta.get("last_extra_announced", 0)
    current = max(max_ex, max_ss)
    if current <= last:
        print(f"→ no new extras/side stories for {novel_id} (last={last}, current={current})")
        return

    # At this point the first extra/side-story has appeared in RSS.
    # Announce it now; bot_paid_chapters.py will hold the actual first
    # bonus chapter until this state flag exists.
    new_ex = max_ex > last
    new_ss = max_ss > last

    # totals from mapping's chapter_count string
    m_ex   = re.search(r"(\d+)\s*extras?", novel.get("chapter_count", ""), re.IGNORECASE)
    m_ss   = re.search(r"(\d+)\s*(?:side story|side stories)", novel.get("chapter_count", ""), re.IGNORECASE)
    tot_ex = int(m_ex.group(1)) if m_ex else 0
    tot_ss = int(m_ss.group(1)) if m_ss else 0

    # label
    parts = []
    if tot_ex: parts.append("EXTRA" if tot_ex == 1 else "EXTRAS")
    if tot_ss: parts.append("SIDE STORY" if tot_ss == 1 else "SIDE STORIES")
    disp_label = " + ".join(parts) if parts else "BONUS CONTENT"

    # decide “dropped” wording
    if new_ex and not new_ss:
        if max_ex == 1:
            cm = "The first of those extras just dropped"
        elif max_ex < tot_ex or tot_ex == 0:
            cm = "New extras just dropped"
        else:
            cm = "All extras just dropped"
    elif new_ss and not new_ex:
        if max_ss == 1:
            cm = "The first of those side stories just dropped"
        elif max_ss < tot_ss or tot_ss == 0:
            cm = "New side stories just dropped"
        else:
            cm = "All side stories just dropped"
    else:  # both new_ex and new_ss
        if (tot_ex and max_ex == tot_ex) and (tot_ss and max_ss == tot_ss):
            cm = "All extras and side stories just dropped"
        else:
            cm = "New extras and side stories just dropped"

    # “remaining” line
    base = (
        f"<:babypinkarrowleft:1365566594503147550>***[{novel['novel_title']}]"
        f"({novel['novel_link']})***<:babypinkarrowright:1365566635838275595>"
    )
    extra_label = "extra" if tot_ex == 1 else "extras"
    ss_label    = "side story" if tot_ss == 1 else "side stories"

    # No final chapter is mapped yet, so do not claim the series is near its end.
    has_last_chapter = bool((novel.get("last_chapter") or "").strip())
    if not has_last_chapter:
        remaining = ""
    elif tot_ex and tot_ss:
        remaining = (
            f"{base} is almost at the very end — just "
            f"{tot_ex} {extra_label} and {tot_ss} {ss_label} left before we wrap up this journey for good  "
            f"<:turtle_cowboy2:1365266375274266695>"
        )
    elif tot_ex:
        remaining = (
            f"{base} is almost at the very end — just "
            f"{tot_ex} {extra_label} left before we wrap up this journey for good  "
            f"<:turtle_cowboy2:1365266375274266695>"
        )
    elif tot_ss:
        remaining = (
            f"{base} is almost at the very end — just "
            f"{tot_ss} {ss_label} left before we wrap up this journey for good  "
            f"<:turtle_cowboy2:1365266375274266695>"
        )
    else:
        remaining = (
            f"{base} is at the very end — no extras or side stories left!  "
            f"<:turtle_cowboy2:1365266375274266695>"
        )

    # render TOML message
    ctx = {
        "display_label": disp_label,
        "remaining": remaining,
        "drop_message": cm,
        "host": novel["host"],
        "short_code": novel.get("short_code", ""),
    }

    message_payload = render_message("new_extras", ctx)

    # ensure we can post before sending
    bot_token = os.getenv(BOT_TOKEN_ENV, "").strip()
    if not bot_token:
        print("❌ Missing DISCORD_BOT_TOKEN; cannot post")
        return

    ensure_bot_in_thread(bot_token, thread_id)
    if USE_UNARCHIVE:
        unarchive_thread(
            bot_token,
            thread_id,
            unlock=True,
            auto_archive_minutes=DEFAULT_AUTO_ARCHIVE_MINUTES,
        )

    if safe_send_bot_payload(bot_token, thread_id, message_payload):
        meta["last_extra_announced"] = current
        meta["extra_announced"]      = True

        save_state(state)
        commit_state_update(STATE_PATH)


# ─── ENTRYPOINT ────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    novels = []
    for host, host_data in (HOSTING_SITE_DATA or {}).items():
        if host != HOST_TARGET:
            continue
        for title, d in host_data.get("novels", {}).items():
            if not d.get("paid_feed"):
                continue
            novels.append({
                "novel_id":      title,
                "novel_title":   title,
                "paid_feed":     d["paid_feed"],
                "chapter_count": d.get("chapter_count",""),
                "last_chapter":  d.get("last_chapter",""),
                "host":          host,
                "novel_link":    d.get("novel_url",""),
                "short_code":    (d.get("short_code", "") or "").strip().upper(),
            })

    for novel in reversed(novels):
        process_extras(novel)
