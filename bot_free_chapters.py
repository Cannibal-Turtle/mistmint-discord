# -*- coding: utf-8 -*-
from discord.errors import Forbidden, HTTPException
import os, re, json, asyncio
from datetime import datetime, timezone
from dateutil import parser as dateparser
import feedparser

import discord

from message_context import build_feed_context
from message_renderer import render_message, to_discord_py_kwargs
from guid_state import entry_guid_identity, format_seen_guid, raw_guid_from_entry, seen_guid_identities

# ─── CONFIG ────────────────────────────────────────────────────────────────────
from config_loader import (
    THREAD_ID_MAP,
    THREAD_MAP_FILE,
    embed_color,
    env_bool,
    require_feed_value,
    require_feeds_value,
    require_feed_url,
    require_file_value,
    require_server_value,
)

TOKEN = os.environ["DISCORD_BOT_TOKEN"]

STATE_FILE = require_file_value("rss_state_path")
LAUNCH_STATE_FILE = require_file_value("state_path")

FEED_KEY   = require_feed_value("free", "last_guid_key")
RSS_URL    = require_feed_url("free")

HOST_NAME_TARGET = require_server_value("host_target")
AUTHOR_URL       = require_server_value("author_url")
GLOBAL_MENTION   = require_server_value("global_mention")

SEEN_KEY       = require_feed_value("free", "seen_key")
LAST_POST_TIME = require_feed_value("free", "last_post_time_key")
SEEN_CAP       = int(require_feeds_value("seen_cap"))
TIME_BACKSTOP  = bool(require_feeds_value("time_backstop"))

AUTO_ARCHIVE_ALLOWED = set(require_server_value("auto_archive_allowed"))
USE_UNARCHIVE = env_bool("USE_UNARCHIVE", False)
DEFAULT_AUTO_ARCHIVE_MINUTES = int(require_server_value("default_auto_archive_minutes"))
# ───────────────────────────────────────────────────────────────────────────────

def load_state():
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        st = {
            "free_last_guid": None,
            "paid_last_guid": None,
            "comments_seen_guids": [],
            SEEN_KEY: [],
            LAST_POST_TIME: None,
        }
        save_state(st)
        return st

    changed = False

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
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def _norm(s): 
    return (s or "").strip()

def _is_target_host(e):
    host = _norm(e.get("host") or e.get("Host") or e.get("HOST"))
    return host.lower() == HOST_NAME_TARGET.lower()

def find_short_code_for_entry(entry):
    def first(*keys):
        for k in keys:
            v = entry.get(k)
            if v:
                return str(v)
            v = entry.get(k.lower()) or entry.get(k.upper())
            if v:
                return str(v)
        return ""

    sc = (first("short_code", "shortcode", "shortCode", "short") or "").strip()
    if sc:
        return sc.upper()

    title = (first("title") or "").strip()
    guid = (first("guid", "id") or "").strip()
    print(f"⚠️ No short_code found in feed for title='{title}' guid='{guid}'")
    return ""

def _thread_id_for(short_code):
    if not short_code:
        return None

    key = re.sub(r"[^A-Z0-9]+", "_", short_code.upper())
    val = THREAD_ID_MAP.get(key)

    try:
        return int(val) if val else None
    except (TypeError, ValueError):
        return None
    
def setting_bool(env_name: str, server_key: str, default: bool = False) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        try:
            raw = require_server_value(server_key)
        except RuntimeError:
            return default

    if isinstance(raw, bool):
        return raw

    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def first_arc_release_enabled() -> bool:
    return setting_bool("ANNOUNCE_FIRST_ARC_RELEASE", "announce_first_arc_release", False)


def first_chapter_release_enabled() -> bool:
    return setting_bool("ANNOUNCE_FIRST_CHAPTER_RELEASE", "announce_first_chapter_release", True)


def load_launch_state():
    try:
        with open(LAUNCH_STATE_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _clean_compare(s: str) -> str:
    s = (s or "").replace("\u00A0", " ").strip().lower()
    return re.sub(r"\s+", " ", s)


def _entry_title(entry) -> str:
    return _norm(entry.get("title") or "")


def _entry_chapter(entry) -> str:
    return _norm(entry.get("chapter") or "")


def is_probable_first_free_chapter(entry) -> bool:
    raw_chap = _entry_chapter(entry)
    raw_extend = _norm(entry.get("chaptername") or "")

    fields = [_clean_compare(raw_chap), _clean_compare(raw_extend)]
    text = " ".join(x for x in fields if x)

    if not text:
        return False

    if "prologue" in text:
        return True

    if re.search(r"\bch(?:apter)?\.?\s*0*1\b", text):
        return True

    if re.search(r"\bep(?:isode)?\.?\s*0*1\b", text):
        return True

    if re.search(r"(?:^|\s)1[．\.]\s*0*1\b", text):
        return True

    for field in fields:
        if re.fullmatch(r"0*1", field):
            return True

    return False


def get_launch_free_record(entry):
    title = _entry_title(entry)
    if not title:
        return None

    state = load_launch_state()
    launch_free = state.get(title, {}).get("launch_free")
    return launch_free if isinstance(launch_free, dict) else None


def should_hold_first_free_chapter(entry) -> bool:
    """
    Hold C1 when:
    - launch_free is not recorded yet, OR
    - announce_first_chapter_release is false.

    Do not mark it seen; a later free/both run can post it.
    """
    if not is_probable_first_free_chapter(entry):
        return False

    if not get_launch_free_record(entry):
        print("⏳ Holding first free chapter: launch_free is not recorded yet.")
        return True

    if not first_chapter_release_enabled():
        print("⏳ Holding first free chapter: announce_first_chapter_release is false.")
        return True

    return False


def should_run_first_arc_before_free_chapter(entry) -> bool:
    """
    Only run the first-arc hook for the actual launch chapter recorded by
    new_novel_checker, not for every later free chapter.
    """
    if not first_chapter_release_enabled():
        return False

    if not first_arc_release_enabled():
        return False

    launch_free = get_launch_free_record(entry)
    if not launch_free:
        return False

    recorded_chapter = _clean_compare(str(launch_free.get("chapter") or ""))
    current_chapter = _clean_compare(_entry_chapter(entry))

    if recorded_chapter and current_chapter:
        return recorded_chapter == current_chapter

    return is_probable_first_free_chapter(entry)


def run_first_arc_before_free_chapter(short_code: str, thread_id: int):
    # Lazy import so local py_compile does not require DISCORD_BOT_TOKEN at import time.
    from new_arc_checker import process_arc_by_short_code

    process_arc_by_short_code(short_code, str(thread_id))


async def ensure_unarchived(thread: discord.Thread, *, unlock: bool = True, auto_archive_minutes: int | None = None) -> bool:
    if auto_archive_minutes is None:
        auto_archive_minutes = DEFAULT_AUTO_ARCHIVE_MINUTES
    """
    Make sure the thread is unarchived (and optionally unlocked) before sending.
    Requires the bot to have 'Manage Threads'. Falls back gracefully if the
    guild doesn't allow 7-day auto archive.
    """
    if not isinstance(thread, discord.Thread):
        return True

    # Pick a valid auto-archive duration the guild supports (best-effort)
    duration = min(AUTO_ARCHIVE_ALLOWED, key=lambda v: abs(v - auto_archive_minutes))

    try:
        # First try: unarchive directly
        await thread.edit(
            archived=False,
            locked=(not unlock),
            auto_archive_duration=duration
        )
        return True
    except Forbidden:
        # If we can’t edit (missing perms or not a member), try joining then edit again
        try:
            await thread.join()
        except Exception:
            pass
        try:
            await thread.edit(
                archived=False,
                locked=(not unlock),
                auto_archive_duration=duration
            )
            return True
        except Exception as e:
            print(f"⚠️ Could not unarchive thread {thread.id}: {e}")
            return False
    except HTTPException as e:
        # Some servers don’t allow 10080; retry without changing duration
        if e.status == 400:
            try:
                await thread.edit(archived=False, locked=(not unlock))
                return True
            except Exception as e2:
                print(f"⚠️ Unarchive retry (no duration) failed for {thread.id}: {e2}")
                return False
        print(f"⚠️ HTTPException unarchiving {thread.id}: {e}")
        return False
    except Exception as e:
        print(f"⚠️ Unexpected error unarchiving {thread.id}: {e}")
        return False

async def ensure_thread_ready(thread_or_channel) -> bool:
    """
    If it's a Thread: join it (idempotent). Only attempt unarchive when
    USE_UNARCHIVE=1 (i.e., when the bot has Manage Threads).
    """
    if isinstance(thread_or_channel, discord.Thread):
        try:
            await thread_or_channel.join()  # safe to call repeatedly
        except Exception:
            pass
        if USE_UNARCHIVE:
            return await ensure_unarchived(
                thread_or_channel, unlock=True, auto_archive_minutes=DEFAULT_AUTO_ARCHIVE_MINUTES
            )
        return True
    return True

_guid = lambda e: _norm(e.get("guid") or e.get("id")) or None

def normalize_guid(entry):
    return format_seen_guid(entry, default_host='Mistmint Haven')

def parse_pub_iso(entry):
    pub_raw = getattr(entry, "published", None)
    if not pub_raw:
        return None
    try:
        return dateparser.parse(pub_raw)
    except Exception:
        return None

def _join_mentions(*parts: str) -> str:
    """Join mentions with ' | ' and dedupe while preserving order."""
    seen, out = set(), []
    for p in parts:
        p = _norm(p)
        if not p:
            continue
        if p not in seen:
            seen.add(p)
            out.append(p)
    return " | ".join(out)

async def send_new_entries():
    state = load_state()

    feed     = feedparser.parse(RSS_URL)
    all_ents = list(reversed(feed.entries))            # oldest → newest
    entries  = [e for e in all_ents if _is_target_host(e)]

    seen = seen_guid_identities(state.get(SEEN_KEY, []))
    last_post_time = state.get(LAST_POST_TIME)
    last_post_dt = (
        dateparser.parse(last_post_time)
        if (TIME_BACKSTOP and last_post_time)
        else None
    )

    to_send = []
    for e in entries:
        guid_key = entry_guid_identity(e)
        if not guid_key:
            continue

        if guid_key in seen:
            continue

        if last_post_dt is not None:
            dt = parse_pub_iso(e)
            if dt and dt <= last_post_dt:
                continue

        to_send.append(e)

    if not to_send:
        print(f"🛑 No new {HOST_NAME_TARGET} free chapters—skipping Discord login.")
        return

    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready():
        queue = to_send

        for entry in queue:
            guid       = _guid(entry)
            if not guid:
                continue
        
            guid_key = entry_guid_identity(entry)
            norm = normalize_guid(entry)
            if guid_key in seen:
                print(f"↷ Already sent, skipping {norm}")
                continue
                
            short_code = find_short_code_for_entry(entry)
            if not short_code:
                print(f"⚠️ Skip: no short_code in entry guid={guid}")
                continue

            thread_id = _thread_id_for(short_code)
            if not thread_id:
                print(
                    f"⚠️ Skip: no thread id for shortcode '{short_code}' "
                    f"in {THREAD_MAP_FILE} (guid={guid})"
                )
                continue

            if should_hold_first_free_chapter(entry):
                print(
                    f"⏳ Holding first free chapter for {short_code} / {guid}: "
                    "launch_free is not recorded yet, so Arc 1 should not announce before launch."
                )
                continue

            try:
                dest = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            except (Forbidden, discord.NotFound, HTTPException) as e:
                print(f"⚠️ Cannot access thread {thread_id}: {e}. Skipping {guid}.")
                continue
            
            # Make sure we can actually post (join + unarchive + set auto-archive if allowed)
            ok = await ensure_thread_ready(dest)
            if not ok:
                print(f"❌ Failed to prepare thread {thread_id} (join/unarchive). Skipping {guid}.")
                continue

            if should_run_first_arc_before_free_chapter(entry):
                print(f"🌸 Launch chapter detected for {short_code}. Checking Arc 1 before free chapter.")
                await asyncio.to_thread(run_first_arc_before_free_chapter, short_code, thread_id)

            ctx = build_feed_context(entry)
            
            ctx.update({
                "global_mention": GLOBAL_MENTION,
                "chapter_author_url": AUTHOR_URL,
            })
            
            payload = render_message("free_chapters", ctx)
            kwargs = to_discord_py_kwargs(payload)
            
            await dest.send(**kwargs)
            
            chapter = ctx["chapter"]

            state[SEEN_KEY].append(norm)
            seen.add(guid_key)
            state[SEEN_KEY] = state[SEEN_KEY][-SEEN_CAP:]

            dt = parse_pub_iso(entry) or datetime.now(timezone.utc)
            state[LAST_POST_TIME] = dt.isoformat()
            state[FEED_KEY] = raw_guid_from_entry(entry)

            save_state(state)

            print(f"📨 Sent: {chapter} / {guid} → thread {thread_id}")

        await asyncio.sleep(1)
        await bot.close()

    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(send_new_entries())
