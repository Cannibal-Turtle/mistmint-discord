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
    feed    = await asyncio.to_thread(feedparser.parse, RSS_URL)
    all_ents = list(reversed(feed.entries))              # oldest → newest
    entries  = [e for e in all_ents if _is_mistmint(e)]  # Mistmint-only

    guids   = [_guid(e) for e in entries]
    to_send = entries[guids.index(last)+1:] if last in guids else entries

    if not to_send:
        print("🛑 No new Mistmint paid chapters—skipping Discord login.")
        return

    intents = discord.Intents.none()
    intents.guilds = True
    bot = discord.Client(intents=intents)
    
    async def hard_exit_after(seconds: int):
        await asyncio.sleep(seconds)
        print(f"⏱️ Hard exit after {seconds}s to avoid CI hang")
        try:
            await bot.close()
        except Exception:
            pass
    
    asyncio.create_task(hard_exit_after(600))  # 10 minutes

    @bot.event
    async def on_ready():
        await asyncio.sleep(2)
        _guids = [_guid(e) for e in entries]
        _last  = state.get(FEED_KEY)
        queue  = entries[_guids.index(_last)+1:] if _last in _guids else entries

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

            # Make sure we can actually post (join + unarchive + set auto-archive if allowed)
            ok = await ensure_thread_ready(dest)
            if not ok:
                print(f"❌ Failed to prepare thread {thread_id} (join/unarchive). Skipping {guid}.")
                continue

            # ── Build content (append NSFW role if category == NSFW)
            title_text = _norm(entry.get("title"))
            nsfw_tail  = NSFW_ROLE if _is_nsfw(entry) else ""
            content = (
                f"<a:Crown:1365575414550106154> 𝒫𝓇𝑒𝓂𝒾𝓊𝓂 𝒞𝒽𝒶𝓅𝓉𝑒𝓇 <a:TurtleDance:1365253970435510293>\n"
                f"<a:1366_sweetpiano_happy:1368136820965249034> **{title_text}** <:pink_lock:1368266294855733291>"
            )

            # ── Embed
            novel_title = _norm(entry.get("title"))
            chaptername = _norm(entry.get("chaptername"))
            nameextend  = _norm(entry.get("nameextend"))
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
                title=f"<a:moonandstars:1365569468629123184>**{chaptername}**",
                url=link,
                description=nameextend or discord.Embed.Empty,
                timestamp=timestamp,
                color=int("A87676", 16),  # dusty rose
            )
            embed.set_author(
                name=f"{translator}˙ᵕ˙",
                url="https://www.mistminthaven.com/account-library/d31417df-4167-4105-8905-5f5942bf4f11"
            )
            if thumb_url:
                embed.set_thumbnail(url=thumb_url)
            embed.set_footer(text=host, icon_url=host_logo)

            # ── Button (coin label/emoji if available)
            coin_label_raw = _norm(entry.get("coin"))
            label_text, emoji_obj = get_coin_button_parts(
                host=host,
                novel_title=novel_title,
                fallback_price=coin_label_raw,
                fallback_emoji=None,
            )
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

            print(f"📨 Sent paid: {chaptername} / {guid} → thread {thread_id}")
            new_last = guid

        if new_last and new_last != state.get(FEED_KEY):
            state[FEED_KEY] = new_last
            save_state(state)
            print(f"💾 Updated {STATE_FILE}[\"{FEED_KEY}\"] → {new_last}")

        await asyncio.sleep(1)
        await bot.close()

    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(send_new_paid_entries())
