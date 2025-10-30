# bot_free_chapters.py
# Post ONLY Mistmint Haven free-chapter updates into the correct per-series thread.
# - Prefers explicit THREAD_ID secrets (e.g., TDLBKGC_THREAD_ID).
# - Falls back to a cached thread id in state_rss.json.
# - Optionally auto-creates a new thread in MISTMINT_CHANNEL_ID when AUTO_CREATE_THREADS=1.

import os
import json
import asyncio
import urllib.parse
import feedparser
from dateutil import parser as dateparser

import discord
from discord import Embed
from discord.ui import View, Button

from novel_mappings import HOSTING_SITE_DATA

# ─── CONFIG ────────────────────────────────────────────────────────────────────
TOKEN               = os.environ["DISCORD_BOT_TOKEN"]
STATE_FILE          = "state_rss.json"
FEED_KEY            = "mistmint_free_last_guid"
RSS_URL             = "https://raw.githubusercontent.com/Cannibal-Turtle/rss-feed/main/free_chapters_feed.xml"
MISTMINT_HOST_NAME  = "Mistmint Haven"
HEADER_LINE         = "𝐹𝓇𝑒𝑒 𝒞𝒽𝒶𝓅𝓉𝑒𝓇 <a:TurtleDance:1365253970435510293>\n"

# Optional: only needed if you want auto-creation of threads
AUTO_CREATE_THREADS = os.getenv("AUTO_CREATE_THREADS", "0") == "1"
MISTMINT_CHANNEL_ID = int(os.environ.get("MISTMINT_CHANNEL_ID", "0")) if AUTO_CREATE_THREADS else None
# ────────────────────────────────────────────────────────────────────────────────


def load_state():
    try:
        st = json.load(open(STATE_FILE, encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        st = {FEED_KEY: None, "threads": {}}
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(st, f, indent=2, ensure_ascii=False)
        return st
    # add this migration so it respects your older key
    if FEED_KEY not in st and "free_last_guid" in st:
        st[FEED_KEY] = st.get("free_last_guid")
    st.setdefault("threads", {})
    return st

def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def guess_series_slug(link: str) -> str | None:
    """Best-effort slug from URL path."""
    try:
        p = urllib.parse.urlparse(link)
        parts = [x for x in p.path.split("/") if x]
        for i, part in enumerate(parts):
            if part.lower() in {"novel", "novels", "series", "book", "project", "title", "story"}:
                if i + 1 < len(parts):
                    return parts[i + 1].lower()
        return parts[0].lower() if parts else None
    except Exception:
        return None


def get_mistmint_short_code(novel_title: str, link: str) -> str | None:
    """Read short_code from HOSTING_SITE_DATA for Mistmint Haven."""
    host_block = HOSTING_SITE_DATA.get("Mistmint Haven", {})
    novels = host_block.get("novels", {})
    link_slug = (guess_series_slug(link) or "").lower()
    for title, details in novels.items():
        url = details.get("novel_url", "")
        url_slug = (guess_series_slug(url) or "").lower()
        if title == novel_title or url_slug == link_slug:
            return details.get("short_code")
    return None


async def resolve_thread(
    bot: discord.Client,
    state: dict,
    series_slug: str | None,
    series_name: str,
    short_code: str | None,
):
    """
    Priority:
      1) <SHORT_CODE>_THREAD_ID (e.g., TDLBKGC_THREAD_ID)
      2) <SERIES_SLUG>_THREAD_ID  (slug uppercased, '-' → '_')
      3) state["threads"][key]
      4) If AUTO_CREATE_THREADS=1 and MISTMINT_CHANNEL_ID is set: create & cache
      5) Else raise so you can add the THREAD_ID secret explicitly
    """
    key_in_state = (series_slug or short_code or series_name.lower())

    # 1/2) Environment secrets
    env_keys = []
    if short_code:
        env_keys.append(f"{short_code.upper()}_THREAD_ID")
    if series_slug:
        env_keys.append(f"{series_slug.upper().replace('-','_')}_THREAD_ID")

    for env_key in env_keys:
        env_tid = os.environ.get(env_key)
        if env_tid:
            tid = int(env_tid)
            thread = bot.get_channel(tid) or await bot.fetch_channel(tid)
            # unarchive/unlock if needed
            try:
                if hasattr(thread, "archived") and thread.archived:
                    await thread.edit(archived=False)
                if hasattr(thread, "locked") and thread.locked:
                    await thread.edit(locked=False)
                try:
                    await thread.join()  # no-op if not needed
                except Exception:
                    pass
            except Exception:
                pass
            state.setdefault("threads", {})[key_in_state] = tid
            save_state(state)
            print(f"🧵 Using thread from env {env_key}={tid}")
            return thread

    # 3) Cached in state file
    tid = state.get("threads", {}).get(key_in_state)
    if tid:
        tid = int(tid)
        thread = bot.get_channel(tid) or await bot.fetch_channel(tid)
        try:
            if hasattr(thread, "archived") and thread.archived:
                await thread.edit(archived=False)
            if hasattr(thread, "locked") and thread.locked:
                await thread.edit(locked=False)
            try:
                await thread.join()
            except Exception:
                pass
        except Exception:
            pass
        print(f"🧵 Using thread from state: {tid}")
        return thread

    # 4) Create a thread only if explicitly allowed
    if AUTO_CREATE_THREADS and MISTMINT_CHANNEL_ID:
        base_channel = bot.get_channel(MISTMINT_CHANNEL_ID) or await bot.fetch_channel(MISTMINT_CHANNEL_ID)
        seed = await base_channel.send(f"Thread for **{series_name}** (free updates)")
        thread = await seed.create_thread(name=f"{series_name} — Free Chapters")
        state.setdefault("threads", {})[key_in_state] = thread.id
        save_state(state)
        print(f"🧵 Created new thread {thread.id} for {series_name}")
        return thread

    # 5) Fail loudly so you can add the secret
    hint = ""
    if short_code:
        hint = f"Set {short_code.upper()}_THREAD_ID"
    elif series_slug:
        hint = f"Set {series_slug.upper().replace('-','_')}_THREAD_ID"
    else:
        hint = "Set a *_THREAD_ID secret for this series"
    raise RuntimeError(f"No thread known for '{series_name}'. {hint}, or enable AUTO_CREATE_THREADS=1.")


async def send_new_entries():
    state = load_state()
    last  = state.get(FEED_KEY)

    parsed = feedparser.parse(RSS_URL)
    all_entries = list(reversed(parsed.entries))  # oldest → newest
    # Only Mistmint
    entries = [e for e in all_entries if (e.get("host", "").strip() == MISTMINT_HOST_NAME)]

    guids = [(e.get("guid") or e.get("id")) for e in entries]
    to_send = entries[guids.index(last) + 1 :] if last in guids else entries

    if not to_send:
        print("🛑 No new Mistmint free chapters—skipping Discord login.")
        return

    intents = discord.Intents.default()
    bot = discord.Client(intents=intents)

    @bot.event
    async def on_ready():
        print("🤖 Logged in.")

        new_last = last
        for entry in to_send:
            try:
                guid        = entry.get("guid") or entry.get("id")
                title       = entry.get("title", "").strip()
                chaptername = entry.get("chaptername", "").strip()
                nameextend  = entry.get("nameextend", "").strip()
                link        = entry.get("link", "").strip()
                translator  = entry.get("translator", "").strip()

                # featured image / host logo (handles both camelCase and lowercase)
                thumb_url   = None
                host_logo   = None
                feat = entry.get("featuredImage") or entry.get("featuredimage")
                if isinstance(feat, dict):
                    thumb_url = feat.get("url")
                logo = entry.get("hostLogo") or entry.get("hostlogo")
                if isinstance(logo, dict):
                    host_logo = logo.get("url")

                pubdate_raw = getattr(entry, "published", None)
                timestamp   = dateparser.parse(pubdate_raw) if pubdate_raw else None

                series_name = entry.get("series") or entry.get("novel") or title
                series_slug = (entry.get("seriesSlug") or entry.get("novelSlug") or guess_series_slug(link) or series_name).lower()
                short_code  = get_mistmint_short_code(novel_title=series_name, link=link)

                # Resolve the thread without needing a base channel unless creation is allowed
                thread = await resolve_thread(
                    bot=bot,
                    state=state,
                    series_slug=series_slug,
                    series_name=series_name,
                    short_code=short_code,
                )

                content = (
                    HEADER_LINE +
                    f"<a:5037sweetpianoyay:1368138418487427102> **{title}** <:pink_unlock:1368266307824255026>"
                )

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
                embed.set_footer(text=MISTMINT_HOST_NAME, icon_url=host_logo)

                view = View()
                view.add_item(Button(label="Read here", url=link))

                await thread.send(content=content, embed=embed, view=view)
                print(f"📨 Sent free → thread {thread.id}: {chaptername} / {guid}")

                new_last = guid

            except Exception as e:
                # Log and continue with next entry
                print(f"⚠️ Failed to post entry: {e}")

        if new_last and new_last != state.get(FEED_KEY):
            state[FEED_KEY] = new_last
            save_state(state)
            print(f"💾 Updated {STATE_FILE}[\"{FEED_KEY}\"] → {new_last}")

        await asyncio.sleep(1)
        await bot.close()

    await bot.start(TOKEN)


if __name__ == "__main__":
    asyncio.run(send_new_entries())
