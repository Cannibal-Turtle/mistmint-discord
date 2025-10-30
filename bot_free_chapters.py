import os
import re
import json
import asyncio
import urllib.parse
import feedparser
from datetime import datetime
from dateutil import parser as dateparser

import discord
from discord import Embed
from discord.ui import View, Button

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN              = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID         = int(os.environ["MISTMINT_CHANNEL_ID"])          # <- uses your secret name
STATE_FILE         = "state_rss.json"
FEED_KEY           = "mistmint_free_last_guid"                        # keep this repo-scoped
RSS_URL            = "https://raw.githubusercontent.com/Cannibal-Turtle/rss-feed/main/free_chapters_feed.xml"
MISTMINT_HOST_NAME = "Mistmint Haven"                                 # filter target
HEADER_LINE        = "𝐹𝓇𝑒𝑒 𝒞𝒽𝒶𝓅𝓉𝑒𝓇 <a:TurtleDance:1365253970435510293>\n"  # no roles
# ────────────────────────────────────────────────────────────────────────────────

def load_state():
    try:
        return json.load(open(STATE_FILE, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        initial = {
            FEED_KEY: None,
            "threads": {}  # {series_slug: thread_id}
        }
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(initial, f, indent=2, ensure_ascii=False)
        return initial

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def guess_series_slug(link: str) -> str | None:
    """Try to extract a stable series slug from the Mistmint URL."""
    try:
        p = urllib.parse.urlparse(link)
        parts = [x for x in p.path.split("/") if x]
        # common patterns: /novel/<slug>/... or /series/<slug>/...
        for i, part in enumerate(parts):
            if part.lower() in {"novel", "series", "book", "project", "title", "story"}:
                if i + 1 < len(parts):
                    return parts[i + 1].lower()
        # fallback: first segment
        return parts[0].lower() if parts else None
    except Exception:
        return None

async def resolve_thread(bot: discord.Client, base_channel: discord.TextChannel, state: dict, series_slug: str, series_name: str):
    """Return a Thread object for this series. Prefer env override; else state; else create."""
    # 1) Env override (e.g., TDLBKGC_THREAD_ID)
    env_key = f"{series_slug.upper().replace('-','_')}_THREAD_ID"
    env_tid = os.environ.get(env_key)
    if env_tid:
        tid = int(env_tid)
        thread = bot.get_channel(tid) or await bot.fetch_channel(tid)
        state["threads"][series_slug] = tid
        save_state(state)
        return thread

    # 2) State-remembered thread
    tid = state.get("threads", {}).get(series_slug)
    if tid:
        try:
            tid = int(tid)
            thread = bot.get_channel(tid) or await bot.fetch_channel(tid)
            return thread
        except Exception:
            pass  # will re-create below if it went missing

    # 3) Create a new public thread from a seed message
    seed = await base_channel.send(f"Thread for **{series_name}** (free chapter updates)")
    thread = await seed.create_thread(name=f"{series_name} — Free Chapters")
    state.setdefault("threads", {})[series_slug] = thread.id
    save_state(state)
    return thread

async def send_new_entries():
    state   = load_state()
    last    = state.get(FEED_KEY)
    feed    = feedparser.parse(RSS_URL)
    # Mistmint-only
    all_entries = list(reversed(feed.entries))  # oldest → newest
    entries = [e for e in all_entries if (e.get("host","").strip() == MISTMINT_HOST_NAME)]

    # Build GUID list just for Mistmint items
    guids = [(e.get("guid") or e.get("id")) for e in entries]
    if last in guids:
        to_send = entries[guids.index(last)+1:]
    else:
        to_send = entries

    if not to_send:
        print("🛑 No new Mistmint free chapters—skipping Discord login.")
        return

    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready():
        base_channel = bot.get_channel(CHANNEL_ID)
        if base_channel is None:
            print(f"❌ Cannot find channel {CHANNEL_ID}")
            await bot.close()
            return

        new_last = last
        for entry in to_send:
            guid        = entry.get("guid") or entry.get("id")
            link        = entry.get("link","").strip()
            title       = entry.get("title","").strip()                  # site title
            chaptername = entry.get("chaptername","").strip()            # pretty chapter name
            nameextend  = entry.get("nameextend","").strip()
            translator  = entry.get("translator","").strip()
            thumb_url   = (entry.get("featuredImage") or entry.get("featuredimage") or {}).get("url")
            host_logo   = (entry.get("hostLogo") or entry.get("hostlogo") or {}).get("url")
            pubdate_raw = getattr(entry, "published", None)
            timestamp   = dateparser.parse(pubdate_raw) if pubdate_raw else None

            # Series id for threading
            series_name = entry.get("series") or entry.get("novel") or title
            series_slug = (entry.get("seriesSlug") or entry.get("novelSlug") or guess_series_slug(link) or series_name).lower()

            # Resolve or create a per-series thread
            thread = await resolve_thread(bot, base_channel, state, series_slug, series_name)

            # Build content (no roles)
            content = (
                HEADER_LINE +
                f"<a:5037sweetpianoyay:1368138418487427102> **{title}** <:pink_unlock:1368266307824255026>"
            )

            # Build embed
            embed = Embed(
                title=f"<a:moonandstars:1365569468629123184>**{chaptername}**",
                url=link,
                description=nameextend or discord.Embed.Empty,
                timestamp=timestamp,
                color=int("FFF9BF", 16),
            )
            if translator:
                embed.set_author(name=f"{translator}˙ᵕ˙")
            if thumb_url:
                embed.set_thumbnail(url=thumb_url)
            # footer shows Mistmint icon if present
            embed.set_footer(text=MISTMINT_HOST_NAME, icon_url=host_logo)

            # Send with button
            view = View()
            view.add_item(Button(label="Read here", url=link))
            await thread.send(content=content, embed=embed, view=view)
            print(f"📨 Sent to thread {thread.id}: {chaptername} / {guid}")

            new_last = guid

        # Save checkpoint for Mistmint items
        if new_last and new_last != state.get(FEED_KEY):
            state[FEED_KEY] = new_last
            save_state(state)
            print(f"💾 Updated {STATE_FILE} → {new_last}")

        await asyncio.sleep(1)
        await bot.close()

    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(send_new_entries())
