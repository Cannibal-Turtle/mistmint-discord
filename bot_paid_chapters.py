# -*- coding: utf-8 -*-
from discord.errors import Forbidden, HTTPException, NotFound
import os
import re
import json
import asyncio
from datetime import datetime, timezone
import feedparser
from dateutil import parser as dateparser

import discord
from discord import Embed
from discord.ui import View, Button

from novel_mappings import HOSTING_SITE_DATA

# ─── CONFIG (no fallback channel) ──────────────────────────────────────────────
TOKEN      = os.environ["DISCORD_BOT_TOKEN"]
STATE_FILE = "state_rss.json"
FEED_KEY   = "paid_last_guid"
RSS_URL    = "https://raw.githubusercontent.com/Cannibal-Turtle/rss-feed/main/paid_chapters_feed.xml"

HOST_NAME_TARGET = "Mistmint Haven"  # only post items from this host
NSFW_ROLE        = "<@&1402533039497805894>"
MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "10"))

THREAD_ID_MAP_RAW = os.getenv("THREAD_ID_MAP", "{}") or "{}"
try:
    THREAD_ID_MAP = json.loads(THREAD_ID_MAP_RAW)
except json.JSONDecodeError:
    print("⚠️ THREAD_ID_MAP is not valid JSON; using empty map.")
    THREAD_ID_MAP = {}
# ───────────────────────────────────────────────────────────────────────────────

AUTO_ARCHIVE_ALLOWED = {60, 1440, 4320, 10080}

# Turn on later (e.g., set env USE_UNARCHIVE=1) when the bot has Manage Threads
USE_UNARCHIVE = os.getenv("USE_UNARCHIVE", "0") == "1"

async def ensure_unarchived(thread: discord.Thread, *, unlock: bool = True, auto_archive_minutes: int = 10080) -> bool:
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
                thread_or_channel, unlock=True, auto_archive_minutes=10080
            )
        return True
    return True

def find_short_code_for_entry(entry):
    # helper to fetch the first present key, case-insensitive
    def first(*keys):
        for k in keys:
            v = entry.get(k)
            if v:
                return str(v)
            v = entry.get(k.lower()) or entry.get(k.upper())
            if v:
                return str(v)
        return ""

    host  = (first("host") or "").strip()
    title = (first("title") or "").strip()

    # 1) Mapping-first (case-insensitive title match)
    novels  = (HOSTING_SITE_DATA.get(host, {}) or {}).get("novels", {}) or {}
    details = novels.get(title)
    if not details:
        for k, v in novels.items():
            if k.casefold() == title.casefold():
                details = v
                break

    sc = (details or {}).get("short_code")
    if sc:
        return str(sc).strip().upper()

    # 2) Feed-provided short_code
    sc = (first("short_code", "shortcode", "shortCode", "short") or "").strip()
    if sc:
        return sc.upper()

    # 3) Parse from GUID like "tdlbkgc-1"
    gid = (first("guid", "id") or "").strip()
    m = re.match(r"([a-z0-9_]+)-", gid, re.I)
    if m:
        return m.group(1).upper()

    # 4) Give up
    return ""

def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        initial = {"free_last_guid": None, "paid_last_guid": None, "comments_last_guid": None}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(initial, f, indent=2, ensure_ascii=False)
        return initial


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _norm(s): return (s or "").strip()
def _guid(e): return _norm(e.get("guid") or e.get("id")) or None

def _is_mistmint(e):
    host = _norm(e.get("host") or e.get("Host") or e.get("HOST"))
    return host.lower() == HOST_NAME_TARGET.lower()

def _is_nsfw(entry) -> bool:
    cat = (entry.get("category") or entry.get("Category") or "").strip().upper()
    return cat == "NSFW"

def _short_code(e):
    for k in ("short_code", "shortcode", "shortCode", "short"):
        v = e.get(k)
        if v: return _norm(v)
    meta = e.get("meta") or {}
    v = meta.get("short_code") or meta.get("shortcode") or meta.get("shortCode")
    return _norm(v) if v else None

def _thread_id_for(short_code):
    if not short_code:
        return None

    # Normalized key (matches your JSON keys like "TDLBKGC", "TVITPA")
    key = re.sub(r"[^A-Z0-9]+", "_", short_code.upper())

    # 1) Try THREAD_ID_MAP (uppercased key or raw short_code)
    val = (THREAD_ID_MAP.get(key) or THREAD_ID_MAP.get(short_code) or "").strip() or None

    # 2) Fallback to old env style: TDLBKGC_THREAD_ID, TVITPA_THREAD_ID, etc.
    if not val:
        env_key = f"{key}_THREAD_ID"
        val = os.getenv(env_key, "").strip() or None

    try:
        return int(val) if val else None
    except ValueError:
        return None


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


def get_coin_button_parts(host: str, novel_title: str, fallback_price: str, fallback_emoji: str = None):
    label_text, emoji_obj = "", None
    try:
        host_block = HOSTING_SITE_DATA.get(host, {})
        novels     = host_block.get("novels", {})
        details    = novels.get(novel_title, {})
        mapped_price = details.get("coin_price")
        if mapped_price is not None:
            label_text = str(mapped_price).strip()
        mapped_emoji_raw = details.get("coin_emoji") or host_block.get("coin_emoji") or fallback_emoji or ""
        emoji_obj = parse_custom_emoji(mapped_emoji_raw)
    except Exception:
        pass

    coin_text = (fallback_price or "").strip()
    if coin_text:
        m = re.match(r"^(?P<emoji><a?:[A-Za-z0-9_]+:\d+>)?\s*(?P<num>\d+)?", coin_text)
        if m:
            if not emoji_obj:
                emoji_obj = parse_custom_emoji((m.group("emoji") or "").strip())
            if not label_text:
                num = (m.group("num") or "").strip()
                if num: label_text = num

    if not label_text and not emoji_obj:
        label_text = "Read here"
    return label_text, emoji_obj
# ───────────────────────────────────────────────────────────────────────────────


async def send_new_paid_entries():
    state   = load_state()
    last    = state.get(FEED_KEY)
    feed    = feedparser.parse(RSS_URL)

    print(f"📡 Feed entries total: {len(feed.entries)}")
    print("📡 First 3 feed GUIDs (raw order):")
    for e in feed.entries[:3]:
        print("   ", _guid(e))
    
    print("📡 Last 3 feed GUIDs (raw order):")
    for e in feed.entries[-3:]:
        print("   ", _guid(e))
    
    all_ents = list(reversed(feed.entries))              # oldest → newest
    entries  = [e for e in all_ents if _is_mistmint(e)]  # Mistmint-only

    print(f"🏷️ Mistmint entries after filter: {len(entries)}")
    print("🏷️ First 3 Mistmint GUIDs:")
    for e in entries[:3]:
        print("   ", _guid(e))
    
    print("🏷️ Last 3 Mistmint GUIDs:")
    for e in entries[-3:]:
        print("   ", _guid(e))

    guids   = [_guid(e) for e in entries]

    print(f"🧠 State paid_last_guid: {last}")
    print(f"🧠 Is paid_last_guid in feed? {last in guids}")
    
    if last in guids:
        idx = guids.index(last)
        print(f"🧠 paid_last_guid index in entries: {idx}")
        print(f"🧠 GUID before index: {guids[idx-1] if idx > 0 else 'NONE'}")
        print(f"🧠 GUID after index: {guids[idx+1] if idx+1 < len(guids) else 'NONE'}")
    
    to_send = entries[guids.index(last)+1:] if last in guids else entries

    print(f"📦 to_send length: {len(to_send)}")
    if to_send:
        print("📦 First to_send GUID:", _guid(to_send[0]))
        print("📦 Last to_send GUID:", _guid(to_send[-1]))

    if not to_send:
        print("🛑 No new Mistmint paid chapters—skipping Discord login.")
        return

    import logging
    import urllib.request
    import ssl

    # enable discord.py logging to stdout (helps show gateway errors)
    logging.basicConfig(level=logging.INFO)

    # quick connectivity + token sanity check BEFORE starting discord.py
    def check_discord_token_and_network(token: str):
        try:
            req = urllib.request.Request(
                "https://discord.com/api/v10/gateway/bot",
                headers={"Authorization": f"Bot {token}"},
                method="GET"
            )
            # use a short timeout so we fail fast in GH Actions (2s)
            with urllib.request.urlopen(req, timeout=5, context=ssl.create_default_context()) as resp:
                return resp.status, resp.read(2000)[:2000]
        except Exception as e:
            return None, str(e)

    ok_status, body_or_err = check_discord_token_and_network(TOKEN)
    print(f"🔎 Discord token/gateway check -> status: {ok_status}")
    if ok_status is None:
        print(f"🔎 Gateway check error: {body_or_err}")
    else:
        print("🔎 Gateway check response preview:", (body_or_err[:200] if isinstance(body_or_err, (bytes, bytearray)) else body_or_err))

    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready():
        print("🟢 Discord on_ready() fired")
        # keep the original on_ready body below unchanged
        _guids = [_guid(e) for e in entries]
        _last  = state.get(FEED_KEY)
        queue_all = entries[_guids.index(_last)+1:] if _last in _guids else entries
        queue = queue_all[:MAX_POSTS_PER_RUN]

        new_last = _last
        for entry in queue:
            guid         = _guid(entry)
            short_code   = find_short_code_for_entry(entry)
            if not short_code:
                print(f"⚠️ Skip: no short_code in entry guid={guid}")
                continue

            thread_id = _thread_id_for(short_code)
            if not thread_id:
                print(
                    f"⚠️ Skip: no thread id for shortcode '{short_code}' "
                    f"in THREAD_ID_MAP or env {short_code.upper()}_THREAD_ID (guid={guid})"
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

            ok = await ensure_thread_ready(dest)
            if not ok:
                print(f"❌ Failed to prepare thread {thread_id} (join/unarchive). Skipping {guid}.")
                continue

            # (rest of your original send loop — unchanged)
            ...
            # After the loop finishes:
        await asyncio.sleep(1)
        await bot.close()

    # start the bot but fail fast if startup hangs (timeout)
    try:
        # 120 seconds is generous; action step has 5 minutes so this will stop earlier
        await asyncio.wait_for(bot.start(TOKEN), timeout=120)
    except asyncio.TimeoutError:
        print("⏱️ bot.start() timed out after 120s. Likely gateway/connectivity issue.")
    except Exception as e:
        print(f"❌ bot.start() raised exception: {e}")


if __name__ == "__main__":
    asyncio.run(send_new_paid_entries())
