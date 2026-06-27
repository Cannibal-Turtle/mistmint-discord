# -*- coding: utf-8 -*-
from discord.errors import Forbidden, HTTPException, NotFound
import os
import re
import json
import asyncio
import feedparser
from datetime import datetime, timezone
from dateutil import parser as dateparser

import discord

from new_arc_checker import process_arc_by_short_code
from message_context import build_feed_context
from message_renderer import render_message, to_discord_py_kwargs
from guid_state import entry_guid_identity, format_seen_guid, raw_guid_from_entry, seen_guid_identities

try:
    from novel_mappings import get_translator_url
except Exception:
    def get_translator_url(host, novel_title=""):
        return ""

try:
    from novel_mappings import get_coin_emoji
except Exception:
    def get_coin_emoji(host):
        return ""

# ─── CONFIG (no fallback channel) ──────────────────────────────────────────────
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

TOKEN = os.environ["DISCORD_BOT_TOKEN"]

STATE_FILE = require_file_value("rss_state_path")
FEED_KEY   = require_feed_value("paid", "last_guid_key")
RSS_URL    = require_feed_url("paid")

SEEN_KEY       = require_feed_value("paid", "seen_key")
LAST_POST_TIME = require_feed_value("paid", "last_post_time_key")
SEEN_CAP       = int(require_feeds_value("seen_cap"))
TIME_BACKSTOP  = bool(require_feeds_value("time_backstop"))

HOST_NAME_TARGET = require_server_value("host_target")
TRANSLATOR_URL = str(server_value("translator_url", "") or "").strip()

AUTO_ARCHIVE_ALLOWED = set(require_server_value("auto_archive_allowed"))
USE_UNARCHIVE = env_bool("USE_UNARCHIVE", False)
DEFAULT_AUTO_ARCHIVE_MINUTES = int(require_server_value("default_auto_archive_minutes"))
# ───────────────────────────────────────────────────────────────────────────────


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

    gid = (first("guid", "id") or "").strip()
    m = re.match(r"([a-z0-9_]+)-", gid, re.I)
    if m:
        return m.group(1).upper()

    title = (first("title") or "").strip()
    print(f"⚠️ No short_code found in feed for title='{title}' guid='{gid}'")
    return ""
    
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


def _norm(s): return (s or "").strip()
def _guid(e): return _norm(e.get("guid") or e.get("id")) or None

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
    

def _is_target_host(e):
    host = _norm(e.get("host") or e.get("Host") or e.get("HOST"))
    return host.lower() == HOST_NAME_TARGET.lower()

def _thread_id_for(short_code):
    if not short_code:
        return None

    key = re.sub(r"[^A-Z0-9]+", "_", short_code.upper())
    val = THREAD_ID_MAP.get(key)

    try:
        return int(val) if val else None
    except (TypeError, ValueError):
        return None

def setting_bool(env_name: str, server_key: str, default: bool = True) -> bool:
    raw = os.getenv(env_name)
    if raw is None:
        try:
            raw = require_server_value(server_key)
        except RuntimeError:
            return default

    if isinstance(raw, bool):
        return raw

    return str(raw).strip().lower() in {"1", "true", "yes", "y", "on"}


def first_chapter_release_enabled() -> bool:
    return setting_bool(
        "ANNOUNCE_FIRST_CHAPTER_RELEASE",
        "announce_first_chapter_release",
        True,
    )


def _clean_compare(s: str) -> str:
    s = (s or "").replace("\u00A0", " ").strip().lower()
    return re.sub(r"\s+", " ", s)


def is_probable_first_paid_chapter(entry) -> bool:
    raw_chap = entry.get("chapter") or ""
    raw_extend = entry.get("chaptername") or ""

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


def should_hold_first_paid_chapter(entry) -> bool:
    if first_chapter_release_enabled():
        return False

    return is_probable_first_paid_chapter(entry)

# ── Paid coin button helpers ───────────────────────────────────────────────────
def parse_custom_emoji(e: str):
    if not e: return None
    s = e.strip()
    m = re.match(r"^<(?P<anim>a?):(?P<name>[A-Za-z0-9_]+):(?P<id>\d+)>$", s)
    if m:
        return discord.PartialEmoji(
            name=m.group("name"),
            id=int(m.group("id")),
            animated=bool(m.group("anim"))
        )
    if "<" not in s and ">" not in s and ":" not in s and len(s) <= 8:
        return s
    return None


def get_coin_button_parts(fallback_price: str, host: str = ""):
    label_text = ""
    emoji_obj = None

    coin_text = (fallback_price or "").strip()
    if not coin_text:
        return "Read here", None

    # New feed shape: <coin>5</coin>. The emoji is Discord presentation,
    # so get it from rss-feed host mappings instead of storing it in <coin>.
    mapped_emoji = parse_custom_emoji(get_coin_emoji(host))
    if mapped_emoji:
        emoji_obj = mapped_emoji

    # Backward compatible with old feed shape: <coin><:emoji:id> 5</coin>.
    m = re.match(r"^(?P<emoji><a?:[A-Za-z0-9_]+:\d+>)?\s*(?P<num>\d+)?", coin_text)
    if m:
        feed_emoji = parse_custom_emoji((m.group("emoji") or "").strip())
        if feed_emoji:
            emoji_obj = feed_emoji

        num = (m.group("num") or "").strip()
        if num:
            label_text = num

    if not label_text:
        mnum = re.search(r"\d+", coin_text)
        if mnum:
            label_text = mnum.group(0)

    if not label_text:
        label_text = "Read here"

    return label_text, emoji_obj
    
# ───────────────────────────────────────────────────────────────────────────────


def is_arc_start_entry(entry):
    raw_vol    = (entry.get("volume") or "").strip()
    raw_extend = (entry.get("chaptername") or "").strip()
    raw_chap   = (entry.get("chapter") or "").strip()

    def is_new_marker(raw):
        if not raw:
            return False
        raw = raw.strip()
        return bool(re.search(r"(001|\(1\)|\.\s*1)(\*+)?\s*$", raw))

    if is_new_marker(raw_extend) or is_new_marker(raw_chap):
        return True

    if re.match(r"(?i)^(arc|world|plane|story|volume|vol|v)\s*\d+", raw_vol):
        if is_new_marker(raw_extend):
            return True

    return False
    
async def send_new_paid_entries():
    MAX_PER_RUN = int(os.getenv("MAX_PER_RUN", "10"))
    state   = load_state()
    feed    = await asyncio.to_thread(feedparser.parse, RSS_URL)
    all_ents = list(reversed(feed.entries))              # oldest → newest
    entries  = [e for e in all_ents if _is_target_host(e)]  # Target host-only

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
        print(f"🛑 No new {HOST_NAME_TARGET} paid chapters—skipping Discord login.")
        return

    intents = discord.Intents.none()
    intents.guilds = True
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready():
        await asyncio.sleep(2)
        
        sent = 0
        
        queue = to_send

        for entry in queue:
            if sent >= MAX_PER_RUN:
                print(f"🛑 Reached MAX_PER_RUN={MAX_PER_RUN}, stopping")
                break
            guid         = _guid(entry)
            if not guid:
                continue
        
            guid_key = entry_guid_identity(entry)
            norm = normalize_guid(entry)
            if guid_key in seen:
                print(f"↷ Already sent, skipping {norm}")
                continue

            short_code   = find_short_code_for_entry(entry)
            if not short_code:
                print(f"⚠️ Skip: no short_code in entry guid={guid}")
                continue

            # ── ARC CHECK BEFORE SENDING PAID CHAPTER ──
            if should_hold_first_paid_chapter(entry):
                print(
                    f"⏳ Holding first paid chapter for {short_code} / {guid}: "
                    "announce_first_chapter_release is false."
                )
                continue

            if is_arc_start_entry(entry):
                thread_id_for_arc = _thread_id_for(short_code)

                if thread_id_for_arc:
                    print(f"🌸 Arc start detected for {_norm(entry.get('title'))}")
                    await asyncio.to_thread(process_arc_by_short_code, short_code, thread_id_for_arc)
                else:
                    print(f"⚠️ Arc start detected but no thread id for short_code='{short_code}'")

            thread_id = _thread_id_for(short_code)
            if not thread_id:
                print(
                    f"⚠️ Skip: no thread id for shortcode '{short_code}' "
                    f"in {THREAD_MAP_FILE} (guid={guid})"
                )
                continue

            # Resolve the destination channel/thread safely
            try:
                dest = bot.get_channel(thread_id) or await bot.fetch_channel(thread_id)
            except (Forbidden, NotFound) as e:
                print(f"⚠️ Cannot access thread {thread_id}: {e}. Skipping {guid}.")
                continue
            except Exception as e:
                print(f"⚠️ Error fetching thread {thread_id}: {e}. Skipping {guid}.")
                continue

            # Make sure we can actually post (join + unarchive + set auto-archive if allowed)
            ok = await ensure_thread_ready(dest)
            if not ok:
                print(f"❌ Failed to prepare thread {thread_id} (join/unarchive). Skipping {guid}.")
                continue

            ctx = build_feed_context(entry)
            
            label_text, emoji_obj = get_coin_button_parts(
                ctx["coin"],
                ctx.get("host") or HOST_NAME_TARGET,
            )
            
            ctx.update({
                "translator_url": (
                    ctx.get("translator_url", "")
                    or get_translator_url(ctx.get("host") or HOST_NAME_TARGET, ctx.get("title", ""))
                    or TRANSLATOR_URL
                ),
                "button_label": label_text or "Read here",
                "button_emoji": str(emoji_obj or ""),
            })
            
            payload = render_message("paid_chapters", ctx)
            kwargs = to_discord_py_kwargs(payload)
            
            # Send with one retry if we hit archived/membership bounce
            try:
                await dest.send(**kwargs)
            except HTTPException as e:
                if isinstance(dest, discord.Thread) and e.status in (400, 403):
                    if await ensure_thread_ready(dest):
                        await dest.send(**kwargs)
                    else:
                        print(f"⚠️ Send retry failed for {thread_id}: {e}")
                        continue
                else:
                    print(f"⚠️ Send failed for {thread_id}: {e}")
                    continue
            
            chapter = ctx["chapter"]
            print(f"📨 Sent paid: {chapter} / {guid} → thread {thread_id}")
            sent += 1
            
            state[SEEN_KEY].append(norm)
            seen.add(guid_key)
            state[SEEN_KEY] = state[SEEN_KEY][-SEEN_CAP:]

            dt = parse_pub_iso(entry) or datetime.now(timezone.utc)
            state[LAST_POST_TIME] = dt.isoformat()
            state[FEED_KEY] = raw_guid_from_entry(entry)

            save_state(state)

        await asyncio.sleep(1)
        await bot.close()


    print("🔌 Connecting to Discord gateway…")

    try:
        # This runs until on_ready() finishes and calls bot.close().
        # 300s prevents the workflow from hanging forever, but won't kill normal arc work.
        await asyncio.wait_for(bot.start(TOKEN), timeout=300)

    except asyncio.TimeoutError:
        print("❌ Paid bot run timed out after 300s — exiting cleanly")
        if not bot.is_closed():
            await bot.close()


if __name__ == "__main__":
    asyncio.run(send_new_paid_entries())
