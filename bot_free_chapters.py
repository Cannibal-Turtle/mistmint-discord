# -*- coding: utf-8 -*-
import os
import re
import json
import asyncio
import feedparser
from dateutil import parser as dateparser

import discord
from discord import Embed
from discord.ui import View, Button

# ─── CONFIG (no fallback channel) ──────────────────────────────────────────────
TOKEN      = os.environ["DISCORD_BOT_TOKEN"]
STATE_FILE = "state_rss.json"
FEED_KEY   = "free_last_guid"
RSS_URL    = "https://raw.githubusercontent.com/Cannibal-Turtle/rss-feed/main/free_chapters_feed.xml"

HOST_NAME_TARGET = "Mistmint Haven"   # only post items from this host
# ───────────────────────────────────────────────────────────────────────────────

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

def _short_code(e):
    for k in ("short_code", "shortcode", "shortCode", "short"):
        v = e.get(k)
        if v: return _norm(v)
    meta = e.get("meta") or {}
    v = meta.get("short_code") or meta.get("shortcode") or meta.get("shortCode")
    return _norm(v) if v else None

def _thread_id_for(short_code):
    if not short_code: return None
    env_key = re.sub(r"[^A-Z0-9]+", "_", short_code.upper()) + "_THREAD_ID"
    val = os.getenv(env_key)
    try:
        return int(val) if val else None
    except ValueError:
        return None

async def send_new_entries():
    state = load_state()
    last  = state.get(FEED_KEY)

    feed     = feedparser.parse(RSS_URL)
    all_ents = list(reversed(feed.entries))          # oldest → newest
    entries  = [e for e in all_ents if _is_mistmint(e)]

    guids = [_guid(e) for e in entries]
    to_send = entries[guids.index(last)+1:] if last in guids else entries

    if not to_send:
        print("🛑 No new Mistmint free chapters—skipping Discord login.")
        return

    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready():
        # recompute slice during session
        _guids = [_guid(e) for e in entries]
        _last  = state.get(FEED_KEY)
        queue  = entries[_guids.index(_last)+1:] if _last in _guids else entries

        new_last = _last
        for entry in queue:
            guid       = _guid(entry)
            short_code = _short_code(entry)
            if not short_code:
                print(f"⚠️ Skip: no short_code in entry guid={guid}")
                continue

            thread_id = _thread_id_for(short_code)
            if not thread_id:
                print(f"⚠️ Skip: no {short_code.upper()}_THREAD_ID secret set for guid={guid}")
                continue

            dest = bot.get_channel(thread_id)
            if dest is None:
                try:
                    dest = await bot.fetch_channel(thread_id)
                except Exception as e:
                    print(f"❌ Skip: cannot resolve thread {thread_id} for guid={guid} ({e})")
                    continue

            # content (no role/global mentions)
            title = _norm(entry.get("title"))
            content = (
                "<a:HappyCloud:1365575487333859398> 𝐹𝓇𝑒𝑒 𝒞𝒽𝒶𝓅𝓉𝑒𝓇 <a:TurtleDance:1365253970435510293>\n"
                f"<a:5037sweetpianoyay:1368138418487427102> **{title}** <:pink_unlock:1368266307824255026>"
            )

            # embed
            chaptername = _norm(entry.get("chaptername"))
            nameextend  = _norm(entry.get("nameextend"))
            link        = _norm(entry.get("link"))
            translator  = _norm(entry.get("translator"))
            thumb_url   = (entry.get("featuredImage") or entry.get("featuredimage") or {}).get("url")
            host        = _norm(entry.get("host"))
            host_logo   = (entry.get("hostLogo") or entry.get("hostlogo") or {}).get("url")
            pub_raw     = getattr(entry, "published", None)
            timestamp   = dateparser.parse(pub_raw) if pub_raw else None

            embed = Embed(
                title=f"<a:moonandstars:1365569468629123184>**{chaptername}**",
                url=link,
                description=nameextend or discord.Embed.Empty,
                timestamp=timestamp,
                color=int("FFF9BF", 16),
            )
            embed.set_author(name=f"{translator}˙ᵕ˙")
            if thumb_url:
                embed.set_thumbnail(url=thumb_url)
            embed.set_footer(text=host, icon_url=host_logo)

            view = View()
            view.add_item(Button(label="Read here", url=link))
            await dest.send(content=content, embed=embed, view=view)

            print(f"📨 Sent: {chaptername} / {guid} → thread {thread_id}")
            new_last = guid

        if new_last and new_last != state.get(FEED_KEY):
            state[FEED_KEY] = new_last
            save_state(state)
            print(f"💾 Updated {STATE_FILE} → {new_last}")

        await asyncio.sleep(1)
        await bot.close()

    await bot.start(TOKEN)

if __name__ == "__main__":
    asyncio.run(send_new_entries())
