#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
new_extra_checker.py (mistmint-discord)

Detects Extras / Side Stories in paid feeds and posts ONE announcement
into each novel's per-thread channel (no global fallback).

Routing:
  - For each novel, resolve its thread via env:
      <SHORTCODE>_THREAD_ID   (e.g. TDLBKGC_THREAD_ID=1433327...)
  - short_code taken from HOSTING_SITE_DATA[host].novels[title]['short_code']
    else derived from title: uppercase + non-alnum -> underscore.

Behavior:
  - Only processes novels under host == "Mistmint Haven".
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

# try to import your mapping package from rss-feed repo
try:
    from novel_mappings import HOSTING_SITE_DATA, get_nsfw_novels
except Exception as e:
    print(f"⚠️ novel_mappings not available ({e}); using empty maps.")
    HOSTING_SITE_DATA = {}
    def get_nsfw_novels():
        return set()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
STATE_PATH      = "state.json"
HOST_TARGET     = "Mistmint Haven"
BOT_TOKEN_ENV   = "DISCORD_BOT_TOKEN"
# ────────────────────────────────────────────────────────────────────────────────


# ─── DISCORD SEND (per-thread) ─────────────────────────────────────────────────

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


def send_bot_message(bot_token: str, thread_id: str, content: str):
    """POST to thread; auto-join on Missing Access and retry; simple 429 backoff."""
    url = f"https://discord.com/api/v10/channels/{thread_id}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type":  "application/json"
    }
    payload = {
        "content": content or "",
        "allowed_mentions": {"parse": []},   # no pings for Mistmint
        "flags": 4                            # suppress embeds for clean text
    }

    # 1) first attempt
    r = requests.post(url, headers=headers, json=payload, timeout=20)

    # 2) handle Missing Access / Missing Permissions by joining then retrying once
    if r.status_code == 403:
        code = None
        try:
            code = r.json().get("code")
        except Exception:
            pass
        if code in (50001, 50013) or "Missing Access" in (r.text or ""):
            if ensure_bot_in_thread(bot_token, thread_id):
                r = requests.post(url, headers=headers, json=payload, timeout=20)
            else:
                print(f"⚠️ Could not join thread {thread_id}; skipping retry")

    # 3) simple rate-limit backoff
    if r.status_code == 429:
        try:
            wait = float(r.json().get("retry_after", 1.0))
        except Exception:
            wait = 1.0
        time.sleep(min(wait, 5.0))
        r = requests.post(url, headers=headers, json=payload, timeout=20)

    if not r.ok:
        print(f"⚠️ Discord error {r.status_code}: {r.text}")
    r.raise_for_status()
    return r


def safe_send_bot(bot_token: str, thread_id: str, content: str) -> bool:
    try:
        send_bot_message(bot_token, thread_id, content)
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
def nsfw_detected(feed_entries, novel_title):
    """Log-only NSFW detector (no pinging in Mistmint)."""
    for entry in feed_entries:
        if (novel_title or "").lower() in (entry.get("title") or "").lower() and \
           "nsfw" in (entry.get("category", "") or "").lower():
            print(f"⚠️ NSFW detected in entry: {entry.get('title')}")
            return True
    return False

def find_released_extras(paid_feed, raw_kw):
    """Find max index released for a given keyword group (extra / side story)."""
    if not raw_kw:
        return set()
    # capture a trailing number after the keyword
    pattern = re.compile(rf"(?i)\b{raw_kw}s?\b.*?(\d+)")
    seen = set()
    for e in paid_feed.entries:
        for field in ("chaptername", "nameextend", "volume"):
            val = e.get(field, "") or ""
            m = pattern.search(val)
            if m:
                seen.add(int(m.group(1)))
    return seen

def sanitize_shortcode_from_title(title: str) -> str:
    """Fallback SHORTCODE from title."""
    return re.sub(r"[^A-Z0-9]+", "_", (title or "").upper()).strip("_")

def resolve_thread_id(novel_title: str, details: dict) -> str | None:
    sc = (details.get("short_code") or "").strip()
    if not sc:
        sc = sanitize_shortcode_from_title(novel_title)
    env_key = f"{sc.upper()}_THREAD_ID"
    val = os.getenv(env_key, "").strip()
    if not val:
        print(f"❌ Missing env {env_key} for '{novel_title}'")
        return None
    return val


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

    # If paid feed already contains the configured last_chapter, skip extras flow
    last_chap = novel.get("last_chapter", "")
    for e in paid_feed.entries:
        chap = f"{(e.get('chaptername') or '')}{(e.get('nameextend') or '')}"
        if last_chap and last_chap in chap:
            print(f"→ skipping extras for {novel['novel_id']} — final chapter present in feed")
            return

    # 2) load state & guard against completion (align with Mistmint keys)
    state    = load_state()
    novel_id = novel["novel_id"]
    meta     = state.setdefault(novel_id, {})
    if meta.get("paid_completion") or meta.get("free_completion") or meta.get("only_free_completion"):
        print(f"→ skipping extras for {novel_id} — already completed (state.json)")
        return

    # 3) NSFW log (no pings)
    entries = paid_feed.entries
    is_nsfw = (novel["novel_title"] in get_nsfw_novels()) or nsfw_detected(entries, novel["novel_title"])
    print(f"🕵️ is_nsfw={is_nsfw} for {novel['novel_title']}")

    # 4) see what actually dropped
    dropped_extras = find_released_extras(paid_feed, "extra")
    dropped_ss     = find_released_extras(paid_feed, "side story")
    max_ex = max(dropped_extras) if dropped_extras else 0
    max_ss = max(dropped_ss)     if dropped_ss     else 0

    # 5) only announce when something new appears; cap to one lifetime send
    if meta.get("extra_announced"):
        print(f"→ extras already announced for {novel_id}; skipping")
        return

    last    = meta.get("last_extra_announced", 0)
    current = max(max_ex, max_ss)
    if current <= last:
        print(f"→ no new extras/side stories for {novel_id} (last={last}, current={current})")
        return

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
    new_ex = max_ex > last
    new_ss = max_ss > last
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

    if tot_ex and tot_ss:
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

    # assemble (NOTE: removed the line with base_mention | ONGOING_ROLE per your ask)
    msg = (
        f"## :lotus:<a:greensparklingstars:1365569873845157918>NEW {disp_label} JUST DROPPED"
        f"<a:greensparklingstars:1365569873845157918>:lotus:\n"
        f"{remaining}\n"
        f"{cm} in {novel['host']}'s advance access today. "
        f"Thanks for sticking with this one ‘til the end. It means a lot. "
        f"Please show your final love and support by leaving comments on the site~ "
        f"<:turtlelovefamily:1365266991690285156> :heart_hands:"
    )

    bot_token = os.getenv(BOT_TOKEN_ENV, "").strip()
    if not bot_token:
        print("❌ Missing DISCORD_BOT_TOKEN; cannot post")
        return

    if safe_send_bot(bot_token, thread_id, msg):
        # update state
        meta["last_extra_announced"] = current
        meta["extra_announced"]      = True     # never fire again
        save_state(state)


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
                "short_code":    d.get("short_code",""),
            })

    for novel in novels:
        process_extras(novel)
