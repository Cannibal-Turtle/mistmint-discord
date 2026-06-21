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

from new_arc_checker import process_arc_by_short_code

# ─── CONFIG (no fallback channel) ──────────────────────────────────────────────
TOKEN      = os.environ["DISCORD_BOT_TOKEN"]
STATE_FILE = "state_rss.json"
FEED_KEY   = "paid_last_guid"
RSS_URL    = "https://raw.githubusercontent.com/Cannibal-Turtle/rss-feed/main/paid_chapters_feed.xml"
SEEN_KEY = "paid_seen_guids"
SEEN_CAP = 500

HOST_NAME_TARGET = "Mistmint Haven"  # only post items from this host

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
            SEEN_KEY: []
        }
        save_state(st)
        return st

    if SEEN_KEY not in st:
        st[SEEN_KEY] = []
        save_state(st)

    return st


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def _norm(s): return (s or "").strip()
def _guid(e): return _norm(e.get("guid") or e.get("id")) or None

def _is_mistmint(e):
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


def get_coin_button_parts(fallback_price: str):
    label_text, emoji_obj = "", None

    coin_text = (fallback_price or "").strip()
    if coin_text:
        m = re.match(r"^(?P<emoji><a?:[A-Za-z0-9_]+:\d+>)?\s*(?P<num>\d+)?", coin_text)
        if m:
            emoji_obj = parse_custom_emoji((m.group("emoji") or "").strip())
            num = (m.group("num") or "").strip()
            if num:
                label_text = num

    if not label_text and not emoji_obj:
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
    entries  = [e for e in all_ents if _is_mistmint(e)]  # Mistmint-only

    to_send = entries

    if not to_send:
        print("🛑 No new Mistmint paid chapters—skipping Discord login.")
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
        
            norm = f"{HOST_NAME_TARGET}::{guid}"
            if norm in state[SEEN_KEY]:
                print(f"↷ Already sent, skipping {norm}")
                continue

            short_code   = find_short_code_for_entry(entry)
            if not short_code:
                print(f"⚠️ Skip: no short_code in entry guid={guid}")
                continue

            # ── ARC CHECK BEFORE SENDING PAID CHAPTER ──
            if is_arc_start_entry(entry):
                thread_id_for_arc = _thread_id_for(short_code)
            
                if thread_id_for_arc:
                    print(f"🌸 Arc start detected for {_norm(entry.get('title'))}")
                    process_arc_by_short_code(short_code, thread_id_for_arc)
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

            # ── Build content
            title_text = _norm(entry.get("title"))
            content = (
                f"<a:Crown:1365575414550106154> 𝒫𝓇𝑒𝓂𝒾𝓊𝓂 𝒞𝒽𝒶𝓅𝓉𝑒𝓇 <a:TurtleDance:1365253970435510293>\n"
                f"<a:1366_sweetpiano_happy:1368136820965249034> **{title_text}** <:pink_lock:1368266294855733291>"
            )

            # ── Embed
            novel_title = _norm(entry.get("title"))
            chapter = _norm(entry.get("chapter"))
            chaptername  = _norm(entry.get("chaptername"))
            link        = _norm(entry.get("link"))
            translator  = _norm(entry.get("translator"))
            host        = _norm(entry.get("host"))
            thumb_url   = (entry.get("featuredImage") or entry.get("featuredimage") or {}).get("url")
            host_logo   = (entry.get("hostLogo") or entry.get("hostlogo") or {}).get("url")
            pub_raw     = getattr(entry, "published", None)
            timestamp = dateparser.parse(pub_raw) if pub_raw else None
            if timestamp and timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)

            embed = Embed(
                title=f"<a:moonandstars:1365569468629123184>**{chapter}**",
                url=link,
                timestamp=timestamp,
                color=int("A87676", 16),
            )
            
            if chaptername:
                embed.description = chaptername

            embed.set_author(
                name=f"{translator}˙ᵕ˙",
                url="https://www.mistminthaven.com/account/@CannibalTurtle-5082"
            )
            if thumb_url:
                embed.set_thumbnail(url=thumb_url)
            embed.set_footer(text=host, icon_url=host_logo)

            # ── Button (coin label/emoji if available)
            coin_label_raw = _norm(entry.get("coin"))
            label_text, emoji_obj = get_coin_button_parts(coin_label_raw)
            btn = Button(label=label_text or "Read here", url=link, emoji=emoji_obj)
            view = View()
            view.add_item(btn)

            # Send with one retry if we hit archived/membership bounce
            try:
                await dest.send(content=content, embed=embed, view=view)
            except HTTPException as e:
                if isinstance(dest, discord.Thread) and e.status in (400, 403):
                    if await ensure_thread_ready(dest):
                        await dest.send(content=content, embed=embed, view=view)
                    else:
                        print(f"⚠️ Send retry failed for {thread_id}: {e}")
                        continue
                else:
                    print(f"⚠️ Send failed for {thread_id}: {e}")
                    continue

            print(f"📨 Sent paid: {chapter} / {guid} → thread {thread_id}")
            sent += 1
            
            state[SEEN_KEY].append(norm)
            state[SEEN_KEY] = state[SEEN_KEY][-SEEN_CAP:]
            state["paid_last_guid"] = guid  # optional, debug only
            save_state(state)

        await asyncio.sleep(1)
        await bot.close()

    print("🔌 Connecting to Discord gateway…")
    try:
        await asyncio.wait_for(bot.start(TOKEN), timeout=30)
    except asyncio.TimeoutError:
        print("❌ Gateway connect timed out — exiting cleanly")
        return


if __name__ == "__main__":
    asyncio.run(send_new_paid_entries())
