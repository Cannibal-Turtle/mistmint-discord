# -*- coding: utf-8 -*-
from discord.errors import Forbidden, HTTPException
import os, re, json, asyncio
import feedparser
from datetime import timezone
from dateutil import parser as dateparser

import discord
from discord import Embed, AllowedMentions
from discord.ui import View, Button

from novel_mappings import HOSTING_SITE_DATA  # ← used for fallback short_code

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN      = os.environ["DISCORD_BOT_TOKEN"]
STATE_FILE = "state_rss.json"
FEED_KEY   = "free_last_guid"
RSS_URL    = "https://raw.githubusercontent.com/Cannibal-Turtle/rss-feed/main/free_chapters_feed.xml"
HOST_NAME_TARGET = "Mistmint Haven"
SEEN_KEY = "free_seen_guids"
SEEN_CAP = 500

GLOBAL_MENTION = "||@everyone||"

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

def _norm(s): 
    return (s or "").strip()

def _is_mistmint(e):
    host = _norm(e.get("host") or e.get("Host") or e.get("HOST"))
    return host.lower() == HOST_NAME_TARGET.lower()

NOVEL_SC = {}
for host, h in HOSTING_SITE_DATA.items():
    for title, details in h.get("novels", {}).items():
        sc = (details.get("short_code") or "").strip()
        if sc:
            NOVEL_SC[(host.lower(), title)] = sc.upper()

def find_short_code_for_entry(entry):
    host  = (entry.get("host") or "").strip().lower()
    title = (entry.get("title") or "").strip()
    sc = NOVEL_SC.get((host, title), "")
    if not sc:
        # helpful debug if a title doesn't match your mapping key exactly
        print(f"⚠️ No short_code found for host='{host}' title='{title}'. Check mapping key text.")
    return sc

def _thread_id_for(short_code):
    if not short_code:
        return None

    key = re.sub(r"[^A-Z0-9]+", "_", short_code.upper())
    val = THREAD_ID_MAP.get(key)

    try:
        return int(val) if val else None
    except (TypeError, ValueError):
        return None

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

_guid = lambda e: _norm(e.get("guid") or e.get("id")) or None

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
    entries  = [e for e in all_ents if _is_mistmint(e)]

    to_send = entries

    if not to_send:
        print("🛑 No new Mistmint free chapters—skipping Discord login.")
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
        
            norm = f"{HOST_NAME_TARGET}::{guid}"
            if norm in state[SEEN_KEY]:
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

            # Content
            title = _norm(entry.get("title"))
            content = (
                f"<a:HappyCloud:1365575487333859398> 𝐹𝓇𝑒𝑒 𝒞𝒽𝒶𝓅𝓉𝑒𝓇 <a:TurtleDance:1365253970435510293> {GLOBAL_MENTION}\n"
                f"<a:5037sweetpianoyay:1368138418487427102> **{title}** <:pink_unlock:1368266307824255026>"
            )

            # Embed
            chaptername = _norm(entry.get("chaptername"))
            nameextend  = _norm(entry.get("nameextend"))
            link        = _norm(entry.get("link"))
            translator  = _norm(entry.get("translator"))
            host        = _norm(entry.get("host"))
            thumb_url   = (entry.get("featuredImage") or entry.get("featuredimage") or {}).get("url")
            host_logo   = (entry.get("hostLogo") or entry.get("hostlogo") or {}).get("url")
            pub_raw     = getattr(entry, "published", None)
            ts          = dateparser.parse(pub_raw) if pub_raw else None
            if ts and ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)

            embed = Embed(
                title=f"<a:sun_clouds:1517425608143933470>**{chaptername}**",
                url=link,
                timestamp=ts,
                color=int("FFF9BF", 16),
            )
            
            if nameextend:
                embed.description = nameextend

            embed.set_author(
                name=f"{translator}˙ᵕ˙",
                url="https://www.mistminthaven.com/account-library/d31417df-4167-4105-8905-5f5942bf4f11"
            )
            if thumb_url:
                embed.set_thumbnail(url=thumb_url)
            embed.set_footer(text=host, icon_url=host_logo)

            view = View()
            view.add_item(Button(label="Read here", url=link))
            allowed = AllowedMentions(everyone=True, users=True, roles=True)
            await dest.send(content=content, embed=embed, view=view, allowed_mentions=allowed)

            state[SEEN_KEY].append(norm)
            state[SEEN_KEY] = state[SEEN_KEY][-SEEN_CAP:]
            state["free_last_guid"] = guid  # optional, but useful
            save_state(state)

            print(f"📨 Sent: {chaptername} / {guid} → thread {thread_id}")

        await asyncio.sleep(1)
        await bot.close()

    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(send_new_entries())
