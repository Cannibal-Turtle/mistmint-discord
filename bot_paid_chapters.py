import os
import re
import json
import asyncio
import urllib.parse
import feedparser
from dateutil import parser as dateparser

import discord
from discord import Embed
from discord.ui import View, Button

from novel_mappings import HOSTING_SITE_DATA

# ─── CONFIG ────────────────────────────────────────────────────────────────
TOKEN              = os.environ["DISCORD_BOT_TOKEN"]
CHANNEL_ID         = int(os.environ["MISTMINT_CHANNEL_ID"])  # align with your secret
STATE_FILE         = "state_rss.json"
FEED_KEY           = "mistmint_paid_last_guid"               # repo-scoped key
RSS_URL            = "https://raw.githubusercontent.com/Cannibal-Turtle/rss-feed/main/paid_chapters_feed.xml"
MISTMINT_HOST_NAME = "Mistmint Haven"
HEADER_LINE        = "𝒫𝓇𝑒𝓂𝒾𝓊𝓂 𝒞𝒽𝒶𝓅𝓉𝑒𝓇 <a:TurtleDance:1365253970435510293>\n"
# ──────────────────────────────────────────────────────────────────────────

def load_state():
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        st = {FEED_KEY: None, "threads": {}}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2, ensure_ascii=False)
        return st
    # migrate old key if needed
    if FEED_KEY not in st and "paid_last_guid" in st:
        st[FEED_KEY] = st.get("paid_last_guid")
    st.setdefault("threads", {})
    return st

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)

def parse_custom_emoji(e: str):
    if not e:
        return None
    s = e.strip()
    m = re.match(r"^<(?P<anim>a?):(?P<name>[A-Za-z0-9_]+):(?P<id>\d+)>$", s)
    if m:
        return discord.PartialEmoji(
            name=m.group("name"),
            id=int(m.group("id")),
            animated=bool(m.group("anim")),
        )
    if "<" not in s and ">" not in s and ":" not in s and len(s) <= 8:
        return s
    return None

def get_coin_button_parts(host: str, novel_title: str, fallback_price: str, fallback_emoji: str = None):
    label_text = ""
    emoji_obj  = None
    try:
        host_block = HOSTING_SITE_DATA.get(host, {})
        details = host_block.get("novels", {}).get(novel_title, {})
        mapped_price = details.get("coin_price")
        if mapped_price is not None:
            label_text = str(mapped_price).strip()
        mapped_emoji_raw = (
            details.get("coin_emoji")
            or host_block.get("coin_emoji")
            or fallback_emoji
            or ""
        )
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
                if num:
                    label_text = num

    if not label_text and not emoji_obj:
        label_text = "Read here"
    return label_text, emoji_obj

def guess_series_slug(link: str) -> str | None:
    try:
        p = urllib.parse.urlparse(link)
        parts = [x for x in p.path.split("/") if x]
        for i, part in enumerate(parts):
            if part.lower() in {"novel", "series", "book", "project", "title", "story"}:
                if i + 1 < len(parts):
                    return parts[i + 1].lower()
        return parts[0].lower() if parts else None
    except Exception:
        return None

async def resolve_thread(bot: discord.Client, base_channel: discord.TextChannel, state: dict, series_slug: str, series_name: str):
    env_key = f"{series_slug.upper().replace('-','_')}_THREAD_ID"
    env_tid = os.environ.get(env_key)
    if env_tid:
        tid = int(env_tid)
        th = bot.get_channel(tid) or await bot.fetch_channel(tid)
        state["threads"][series_slug] = tid
        save_state(state)
        return th
    tid = state.get("threads", {}).get(series_slug)
    if tid:
        try:
            tid = int(tid)
            th = bot.get_channel(tid) or await bot.fetch_channel(tid)
            return th
        except Exception:
            pass
    seed = await base_channel.send(f"Thread for **{series_name}** (premium updates)")
    th = await seed.create_thread(name=f"{series_name} — Premium Chapters")
    state.setdefault("threads", {})[series_slug] = th.id
    save_state(state)
    return th

async def send_new_paid_entries():
    state = load_state()
    last  = state.get(FEED_KEY)
    feed  = feedparser.parse(RSS_URL)

    all_entries = list(reversed(feed.entries))
    entries = [e for e in all_entries if (e.get("host","").strip() == MISTMINT_HOST_NAME)]

    guids = [(e.get("guid") or e.get("id")) for e in entries]
    to_send = entries[guids.index(last)+1:] if last in guids else entries

    if not to_send:
        print("🛑 No new Mistmint premium chapters—skipping Discord login.")
        return

    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready():
        base_channel = bot.get_channel(CHANNEL_ID)
        if not base_channel:
            print(f"❌ Cannot find channel {CHANNEL_ID}")
            await bot.close()
            return

        new_last = last
        for entry in to_send:
            guid        = entry.get("guid") or entry.get("id")
            novel_title = entry.get("novel_title","").strip() or entry.get("series","").strip() or entry.get("novel","").strip()
            host        = entry.get("host","").strip()
            title_text  = entry.get("title","").strip()
            chaptername = entry.get("chaptername","").strip()
            nameextend  = entry.get("nameextend","").strip()
            link        = entry.get("link","").strip()
            translator  = entry.get("translator","").strip()
            thumb_url   = (entry.get("featuredImage") or entry.get("featuredimage") or {}).get("url")
            host_logo   = (entry.get("hostLogo") or entry.get("hostlogo") or {}).get("url")
            pubdate_raw = getattr(entry, "published", None)
            timestamp   = dateparser.parse(pubdate_raw) if pubdate_raw else None
            coin_label_raw = entry.get("coin","").strip()

            series_name = entry.get("series") or entry.get("novel") or novel_title or title_text
            series_slug = (entry.get("seriesSlug") or entry.get("novelSlug") or guess_series_slug(link) or series_name).lower()

            thread = await resolve_thread(bot, base_channel, state, series_slug, series_name)

            content = (
                HEADER_LINE +
                f"<a:1366_sweetpiano_happy:1368136820965249034> **{title_text}** <:pink_lock:1368266294855733291>"
            )

            embed = Embed(
                title=f"<a:moonandstars:1365569468629123184>**{chaptername}**",
                url=link,
                description=nameextend or discord.Embed.Empty,
                timestamp=timestamp,
                color=int("A87676", 16),
            )
            if translator:
                embed.set_author(name=f"{translator}˙ᵕ˙")
            if thumb_url:
                embed.set_thumbnail(url=thumb_url)
            embed.set_footer(text=host, icon_url=host_logo)

            label_text, emoji_obj = get_coin_button_parts(
                host=host,
                novel_title=novel_title or series_name,
                fallback_price=coin_label_raw,
                fallback_emoji=None,
            )
            if not label_text and not emoji_obj:
                label_text = "Read here"

            btn = Button(label=label_text, url=link, emoji=emoji_obj)
            view = View(); view.add_item(btn)

            await thread.send(content=content, embed=embed, view=view)
            print(f"📨 Sent premium → thread {thread.id}: {chaptername} / {guid}")
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
