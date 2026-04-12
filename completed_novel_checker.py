#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
completed_novel_checker.py (mistmint-discord)

Posts completion announcements for Mistmint Haven novels into their
per-novel threads (no fallback channel).

Usage:
  python completed_novel_checker.py --feed paid
  python completed_novel_checker.py --feed free

Env secrets (GitHub Actions → Repository secrets):
  - DISCORD_BOT_TOKEN
  - For each novel thread, set:  <SHORTCODE>_THREAD_ID  (e.g. TDLBKGC_THREAD_ID=1433788343954575562)

Notes:
  - Only novels with host == "Mistmint Haven" are considered.
  - SHORTCODE is taken from HOSTING_SITE_DATA.novels[...]['short_code'] if present.
    If missing, it is derived from the title: uppercase and non-alnum → underscore.
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

import feedparser
import time
import subprocess
import requests
from dateutil.relativedelta import relativedelta

# Try to load your mapping package from rss-feed repo
try:
    from novel_mappings import HOSTING_SITE_DATA
except Exception as e:
    print(f"⚠️ novel_mappings not available ({e}); using empty HOSTING_SITE_DATA.")
    HOSTING_SITE_DATA = {}

# ─── CONFIG ────────────────────────────────────────────────────────────────────
STATE_PATH     = "state.json"
BOT_TOKEN_ENV  = "DISCORD_BOT_TOKEN"

HOST_NAME_TARGET = "Mistmint Haven"  # Only post for this host
COMPLETE_ROLE    = "<@&1329502614110474270>"  # kept for future if you ever add it back (not used in messages)
GLOBAL_MENTION = "||@everyone||"

# Only attempt PATCH /channels/{id} when the bot has Manage Threads
USE_UNARCHIVE = os.getenv("USE_UNARCHIVE", "0") == "1"

THREAD_MAP_FILE = "thread_id_map.json"

try:
    with open(THREAD_MAP_FILE, encoding="utf-8") as f:
        THREAD_ID_MAP = json.load(f)
except FileNotFoundError:
    print(f"❌ Missing {THREAD_MAP_FILE}; no threads will be resolved.")
    THREAD_ID_MAP = {}
except json.JSONDecodeError as e:
    print(f"❌ Invalid JSON in {THREAD_MAP_FILE}: {e}")
    THREAD_ID_MAP = {}
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
def send_bot_message(bot_token: str, channel_or_thread_id: str, content: str):
    """
    POST message via bot token to the given channel/thread ID.
    Threads are also channels in Discord API, so same endpoint works.
    """
    url = f"https://discord.com/api/v10/channels/{channel_or_thread_id}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type":  "application/json"
    }
    payload = {
        "content": content,
        # Ping everyone in thread.
        "allowed_mentions": {"parse": ["everyone"]},
        # 4 = SUPPRESS_EMBEDS (keeps this as clean text wall)
        "flags": 4
    }
  
    def _send():
        return requests.post(url, headers=headers, json=payload, timeout=20)

    # Preflight: join thread is always safe; unarchive only when allowed
    ensure_bot_in_thread(bot_token, channel_or_thread_id)
    if USE_UNARCHIVE:
        unarchive_thread(bot_token, channel_or_thread_id, unlock=True, auto_archive_minutes=10080)

    r = _send()

    # Fix archived / missing access once, then retry
    if r.status_code in (400, 403):
        try:
            body = r.json()
        except Exception:
            body = {"message": r.text}
        msg  = (body.get("message") or "").lower()
        code = body.get("code")

        recovered = False
        if "archiv" in msg and USE_UNARCHIVE:
            recovered = unarchive_thread(bot_token, channel_or_thread_id, unlock=True, auto_archive_minutes=10080)
        
        # Always try to join if we still could not recover
        if not recovered and (code in (50001, 50013) or "missing access" in msg):
            recovered = ensure_bot_in_thread(bot_token, channel_or_thread_id)

        if recovered:
            time.sleep(0.8)
            r = _send()

    # Rate limit: respect header, else body, then retry once
    if r.status_code == 429:
        wait = None
        reset_after = r.headers.get("X-RateLimit-Reset-After") or r.headers.get("x-ratelimit-reset-after")
        if reset_after:
            try: wait = float(reset_after)
            except (TypeError, ValueError): pass
        if wait is None:
            try: wait = float(r.json().get("retry_after", 1.0))
            except Exception: wait = 1.0
        time.sleep(max(0.0, min(wait or 1.0, 5.0)))
        r = _send()

    if not r.ok:
        # Let caller’s try/except print useful diagnostics
        r.raise_for_status()
      
def unarchive_thread(bot_token: str, thread_id: str, *, unlock: bool = True, auto_archive_minutes: int = 10080) -> bool:
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

def safe_send_bot(bot_token: str, channel_or_thread_id: str, content: str) -> bool:
    try:
        # one more gentle preflight (cheap idempotent calls)
        ensure_bot_in_thread(bot_token, channel_or_thread_id)
        if USE_UNARCHIVE:
            unarchive_thread(bot_token, channel_or_thread_id, unlock=True, auto_archive_minutes=10080)
        
        send_bot_message(bot_token, channel_or_thread_id, content)
        return True
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


def thread_env_key_for(short_code: str) -> str:
    return f"{short_code}_THREAD_ID"


def resolve_thread_id(novel_title: str, details: dict) -> str | None:
    short_code = (details.get("short_code") or "").strip() or sanitize_shortcode_from_title(novel_title)
    key = re.sub(r"[^A-Z0-9]+", "_", short_code.upper())

    val = THREAD_ID_MAP.get(key)
    return str(val) if val else None


# ─── MESSAGE BUILDERS (mentions/footer removed for Mistmint) ───────────────────
def build_paid_completion(novel, chap_field, chap_link, duration: str):
    translator  = novel.get("translator", "")
    title       = novel.get("novel_title", "")
    link        = novel.get("novel_link", "")
    host        = novel.get("host", "")
    count       = novel.get("chapter_count", "the entire series")
    DIV         = "<:purple_divider1:1365652778957144165>"
    divider_line = DIV * 10

    chap_text = (chap_field or "").replace("\u00A0", " ")

    if duration:
        mid_line = (
            f"After {duration} of updates, {title} is now fully translated with "
            f"{count}! Thank you for coming on this journey and for your continued "
            f"support <:turtle_plead:1365223487274352670> You can now visit {host} "
            f"to binge all advance releases~*<a:Heart:1365575427724283944>"
            f"<a:Paws:1365676154865979453>\n"
        )
    else:
        mid_line = (
            f"{title} is now fully translated with {count}! Thank you for coming "
            f"on this journey and for your continued support "
            f"<:turtle_plead:1365223487274352670> You can now visit {host} "
            f"to binge all advance releases~*<a:Heart:1365575427724283944>"
            f"<a:Paws:1365676154865979453>\n"
        )

    return (
        f"{GLOBAL_MENTION}\n"
        "## ꧁ᐟᐟ ◌ೄ⟢  Completion Announcement  :blueberries: ˚. ᵎᵎ˖ˎˊ-\n"
        f"{divider_line}\n"
        f"***<a:kikilts_bracket:1365693072138174525>[{title}]({link})"
        f"<a:lalalts_bracket:1365693058905014313> — officially completed!*** "
        f"<a:cowiggle:1368136766791483472><a:whitesparkles:1365569806966853664>\n\n"
        f"*The last chapter, [{chap_text}]({chap_link}), has now been released. "
        f"<a:turtle_hyper:1365223449827737630>\n"
        f"{mid_line}"
        f"{'<:FF_Divider_Pink:1365575626194681936>' * 5}\n"
        f"-# Check out other translated projects at [{translator}'s library](https://www.mistminthaven.com/account-library/d31417df-4167-4105-8905-5f5942bf4f11) "
        f"and follow your favourite series’ Discord threads for instant updates <a:moonoutline:1365569437792731198>"
    )

def build_free_completion(novel, chap_field, chap_link):
    translator  = novel.get("translator", "")
    title       = novel.get("novel_title", "")
    link        = novel.get("novel_link", "")
    host        = novel.get("host", "")
    count       = novel.get("chapter_count", "the entire series")
    DIV         = "<:purple_divider1:1365652778957144165>"
    divider_line = DIV * 10

    chap_text = (chap_field or "").replace("\u00A0", " ")

    return (
        f"{GLOBAL_MENTION}\n"
        "## 𐔌  Announcing: Complete Series Unlocked ,, :cherries: — 𝝑𝝔  ꒱\n"
        f"{divider_line}\n"
        f"***<a:kikilts_bracket:1365693072138174525>[{title}]({link})"
        f"<a:lalalts_bracket:1365693058905014313>— complete access granted!*** "
        f"<a:cowiggle:1368136766791483472><a:whitesparkles:1365569806966853664>\n\n"
        f"*All {count} has been unlocked and ready for you to binge—completely free!\n"
        f"Thank you all for your amazing support "
        f"<:green_turtle_heart:1365264636064305203>\n"
        f"Head over to {host} to dive straight in~*"
        f"<a:Heart:1365575427724283944><a:Paws:1365676154865979453>\n"
        f"{'<:FF_Divider_Pink:1365575626194681936>' * 5}\n"
        f"-# Check out other translated projects at [{translator}'s library](https://www.mistminthaven.com/account-library/d31417df-4167-4105-8905-5f5942bf4f11) "
        f"and follow your favourite series’ Discord threads for instant updates <a:moonoutline:1365569437792731198>"
    )


def build_only_free_completion(novel, chap_field, chap_link, duration: str):
    translator  = novel.get("translator", "")
    title       = novel.get("novel_title", "")
    link        = novel.get("novel_link", "")
    host        = novel.get("host", "")
    count       = novel.get("chapter_count", "the entire series")
    DIV         = "<:purple_divider1:1365652778957144165>"
    divider_line = DIV * 10

    chap_text = (chap_field or "").replace("\u00A0", " ")

    if duration:
        mid_line = (
            f"After {duration} of updates, {title} is now fully translated with "
            f"{count}! Thank you for coming on this journey and for your continued "
            f"support <:luv_turtle:365263712549736448> You can now visit {host} "
            f"to binge on all the releases~*<a:Heart:1365575427724283944>"
            f"<a:Paws:1365676154865979453>\n"
        )
    else:
        mid_line = (
            f"{title} is now fully translated with {count}! Thank you for coming "
            f"on this journey and for your continued support "
            f"<:luv_turtle:365263712549736448> You can now visit {host} "
            f"to binge on all the releases~*<a:Heart:1365575427724283944>"
            f"<a:Paws:1365676154865979453>\n"
        )

    return (
        f"{GLOBAL_MENTION}\n"
        "## ⁺‧ ༻•┈๑☽₊˚ ⌞Completion Announcement⋆ཋྀ ˚₊‧⁺ :kiwi: ∗༉‧₊˚\n"
        f"{divider_line}\n"
        f"***<a:kikilts_bracket:1365693072138174525>[{title}]({link})"
        f"<a:lalalts_bracket:1365693058905014313> — officially completed!*** "
        f"<a:cowiggle:1368136766791483472><a:whitesparkles:1365569806966853664>\n\n"
        f"*The last chapter, [{chap_text}]({chap_link}), has now been released. "
        f"<a:turtle_hyper:1365223449827737630>\n"
        f"{mid_line}"
        f"{'<:FF_Divider_Pink:1365575626194681936>' * 5}\n"
        f"-# Check out other translated projects at [{translator}'s library](https://www.mistminthaven.com/account-library/d31417df-4167-4105-8905-5f5942bf4f11) "
        f"and follow your favourite series’ Discord threads for instant updates <a:moonoutline:1365569437792731198>"
    )


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
                "role_mention":     details.get("discord_role_id", ""),
                "host":             host,
                "novel_link":       details.get("novel_url", ""),
                "chapter_count":    details.get("chapter_count", ""),
                "last_chapter":     last,
                "start_date":       details.get("start_date", ""),
                "free_feed":        free,
                "paid_feed":        paid,
                "discord_role_url": details.get("discord_role_url", ""),
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
        
            base = entry.get("chaptername") or entry.get("chapter", "") or ""
            ext  = entry.get("nameextend") or ""
        
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
                print(f"→ Built message of {len(msg)} characters")
            
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
                print(f"→ Built message of {len(msg)} characters")
            
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
                print(f"→ Built message of {len(msg)} characters")
            
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
