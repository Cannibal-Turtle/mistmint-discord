# -*- coding: utf-8 -*-
from discord.errors import Forbidden, HTTPException, NotFound
import os
import re
import json
import asyncio
from datetime import timezone
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
# ───────────────────────────────────────────────────────────────────────────────

AUTO_ARCHIVE_ALLOWED = {60, 1440, 4320, 10080}

async def ensure_unarchived(thread: discord.Thread, *, unlock: bool = True, auto_archive_minutes: int = 10080) -> bool:
    """Unarchive + optionally unlock; tolerate servers that disallow 10080."""
    if not isinstance(thread, discord.Thread):
        return True
    duration = min(AUTO_ARCHIVE_ALLOWED, key=lambda v: abs(v - auto_archive_minutes))
    try:
        await thread.edit(archived=False, locked=(not unlock), auto_archive_duration=duration)
        return True
    except Forbidden:
        try:
            await thread.join()
        except Exception:
            pass
        try:
            await thread.edit(archived=False, locked=(not unlock), auto_archive_duration=duration)
            return True
        except Exception as e:
            print(f"⚠️ Could not unarchive thread {thread.id}: {e}")
            return False
    except HTTPException as e:
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
    """If Thread: join then unarchive. Return True if safe to send."""
    if isinstance(thread_or_channel, discord.Thread):
        try:
            await thread_or_channel.join()
        except Exception:
            pass
        return await ensure_unarchived(thread_or_channel, unlock=True, auto_archive_minutes=10080)
    return True

def find_short_code_for_entry(entry):
    sc = (entry.get('short_code') or entry.get('shortcode') or '').strip()
    if sc:
        return sc.upper()
    gid = (entry.get('guid') or entry.get('id') or '')
    m = re.match(r'([a-z0-9_]+)-', str(gid), re.I)
    if m:
        return m.group(1).upper()
    host  = (entry.get('host') or '').strip()
    title = (entry.get('title') or '').strip()
    host_block = HOSTING_SITE_DATA.get(host, {})
    for novel_title, details in host_block.get('novels', {}).items():
        if novel_title == title:
            sc = (details.get('short_code') or '').strip()
            if sc:
                return sc.upper()
    return ''

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

def _thread_id_for(short_code):
    if not short_code: return None
    env_key = re.sub(r"[^A-Z0-9]+", "_", short_code.upper()) + "_THREAD_ID"
    val = os.getenv(env_key)
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
    all_ents = list(reversed(feed.entries))              # oldest → newest
    entries  = [e for e in all_ents if _is_mistmint(e)]  # Mistmint-only

    guids   = [_guid(e) for e in entries]
    to_send = entries[guids.index(last)+1:] if last in guids else entries

    if not to_send:
        print("🛑 No new Mistmint paid chapters—skipping Discord login.")
        return

    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready():
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
                print(f"⚠️ Skip: no {short_code.upper()}_THREAD_ID secret set for guid={guid}")
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

            # ── Build content (no role/global mentions)
            title_text  = _norm(entry.get("title"))
            content = (
                "<a:Crown:1365575414550106154> 𝒫𝓇𝑒𝓂𝒾𝓊𝓂 𝒞𝒽𝒶𝓅𝓉𝑒𝓇 <a:TurtleDance:1365253970435510293>\n"
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
            pub_raw     = entry.get("published")
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
            embed.set_author(name=f"{translator}˙ᵕ˙")
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

            # ── Send with one graceful retry if needed
            try:
                await dest.send(
                    content=content,
                    embed=embed,
                    view=view,
                    allowed_mentions=discord.AllowedMentions.none()
                )
            except HTTPException as e:
                if isinstance(dest, discord.Thread) and e.status in (400, 403):
                    if await ensure_thread_ready(dest):
                        await dest.send(
                            content=content,
                            embed=embed,
                            view=view,
                            allowed_mentions=discord.AllowedMentions.none()
                        )
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
