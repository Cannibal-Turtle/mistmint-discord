#!/usr/bin/env python3
"""
new_novel_checker.py (Mistmint)

Announce a brand new novel when it FIRST becomes available for free/public reading.

Usage:
  python new_novel_checker.py --feed free

Behavior:
- For each novel in HOSTING_SITE_DATA that has a free_feed:
    - Parse the free feed (RSS).
    - Find an entry for that novel whose chapter looks like the first drop:
        "Chapter 1", "Ch 1", "Prologue", or "1.1".
    - If we haven't announced this novel before:
        - Build a launch message (sparkle text).
        - Build an embed (translator, clickable title, cleaned description,
          cover image, footer with host + timestamp).
        - Post both to the novel's thread (per-novel secret).
        - Write launch_free info into state.json so we never post it again.

Env vars required:
  DISCORD_BOT_TOKEN  -> your bot token (not webhook)
  For each Mistmint novel thread, set:  <SHORTCODE>_THREAD_ID
    e.g.  TDLBKGC_THREAD_ID=1433788343954575562

Notes:
- SHORTCODE is taken from HOSTING_SITE_DATA.novels[...]['short_code'] if present.
  If missing, it is derived from the title: uppercase and non-alnum → underscore.
- Thread URL is constructed as:
    https://discord.com/channels/1379303379221614702/<THREAD_ID>
  (1379303379221614702 is the Mistmint server id you provided.)
"""

import argparse
import json
import os
import sys
import re
import html
import feedparser
import requests
from datetime import datetime, timezone
import subprocess

from novel_mappings import (
    HOSTING_SITE_DATA,
    get_nsfw_novels,  # kept for parity; not used after removing ping header
)

# ─── CONFIG ────────────────────────────────────────────────────────────────────

STATE_PATH     = "state.json"
BOT_TOKEN_ENV  = "DISCORD_BOT_TOKEN"

# kept for parity with original (not used after header removal)
GLOBAL_ROLE = "<@&1329502873503006842>"
NSFW_ROLE   = "<@&1343352825811439616>"

# Mistmint server id (to build follow-this-thread URL)
MISTMINT_GUILD_ID = "1379303379221614702"

# ───────────────────────────────────────────────────────────────────────────────

def ensure_bot_in_thread(bot_token: str, thread_id: str) -> bool:
    h = {"Authorization": f"Bot {bot_token}"}
    r = requests.get(
        f"https://discord.com/api/v10/channels/{thread_id}/thread-members/@me",
        headers=h, timeout=15
    )
    if r.status_code == 200:
        return True
    r = requests.put(
        f"https://discord.com/api/v10/channels/{thread_id}/thread-members/@me",
        headers=h, timeout=15
    )
    return r.status_code in (200, 204)

def commit_state_update(path=STATE_PATH):
    """Commit/push state.json so the skip flag survives the next run."""
    try:
        subprocess.run(["git","config","--global","user.name","GitHub Actions"], check=True)
        subprocess.run(["git","config","--global","user.email","actions@github.com"], check=True)
        subprocess.run(["git","add", path], check=True)
        # commit only if there are staged changes
        staged = subprocess.run(["git","diff","--staged","--quiet"])
        if staged.returncode != 0:
            subprocess.run(["git","commit","-m", f"Auto-update: {os.path.basename(path)}"], check=True)
            subprocess.run(["git","push","origin","main"], check=True)
        else:
            print(f"⚠️ No changes detected in {path}, skipping commit.")
    except Exception as e:
        print(f"❌ Git commit/push for {path} failed: {e}")

def load_state(path=STATE_PATH):
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
            if not raw:
                # empty file → treat as empty state
                return {}
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                # malformed JSON → ignore and start fresh in-memory
                print(f"⚠️ {path} contained invalid JSON; using empty state.", file=sys.stderr)
                return {}
    except FileNotFoundError:
        return {}


def save_state(state, path=STATE_PATH):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)


def parsed_time_to_aware(struct_t, fallback_now):
    if not struct_t:
        return fallback_now
    try:
        aware_utc = datetime(
            struct_t.tm_year,
            struct_t.tm_mon,
            struct_t.tm_mday,
            struct_t.tm_hour,
            struct_t.tm_min,
            struct_t.tm_sec,
            tzinfo=timezone.utc,
        )
        return aware_utc.astimezone()
    except Exception:
        return fallback_now


def nice_footer_time(chap_dt: datetime, now_dt: datetime) -> str:
    chap_day = chap_dt.date()
    now_day  = now_dt.date()
    hhmm     = chap_dt.strftime("%H:%M")

    if chap_day == now_day:
        return f"Today at {hhmm}"

    delta_days = (now_day - chap_day).days
    if delta_days == 1:
        return f"Yesterday at {hhmm}"

    return chap_dt.strftime("%Y-%m-%d %H:%M")


def send_bot_message_embed(bot_token: str, channel_or_thread_id: str, content: str, embed: dict):
    url = f"https://discord.com/api/v10/channels/{channel_or_thread_id}/messages"
    headers = {
        "Authorization": f"Bot {bot_token}",
        "Content-Type":  "application/json"
    }
    payload = {
        "content": content,
        "embeds": [embed],
        # keep roles for parity; content no longer has role mentions anyway
        "allowed_mentions": {"parse": ["roles"]},
    }
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    r.raise_for_status()


def safe_send_bot_embed(bot_token: str, channel_or_thread_id: str, content: str, embed: dict):
    try:
        send_bot_message_embed(bot_token, channel_or_thread_id, content, embed)
        return True
    except requests.HTTPError as e:
        status = e.response.status_code if e.response else "?"
        body   = e.response.text       if e.response else ""
        print(f"⚠️ Bot send failed ({status}):\n{body}", file=sys.stderr)
        return False
    except requests.RequestException as e:
        print(f"⚠️ Bot send error: {e}", file=sys.stderr)
        return False


def is_first_chapter_name(chapter_field: str) -> bool:
    if not chapter_field:
        return False

    text = chapter_field.lower().strip()

    if re.search(r"\bch(?:apter)?\.?\s*0*1\b", text):
        return True

    if re.search(r"\bep(?:isode)?\.?\s*0*1\b", text):
        return True

    if "prologue" in text:
        return True

    if re.search(r"\b1[．\.]\s*0*1\b", text):
        return True

    return False


def clean_feed_description(raw_html: str) -> str:
    if not raw_html:
        return ""

    parts = re.split(r"(?i)<hr[^>]*>", raw_html, maxsplit=1)
    main_part = parts[0]

    no_tags = re.sub(r"(?s)<[^>]+>", "", main_part)

    text = html.unescape(no_tags)

    text = re.sub(r"\s+\n", "\n", text)
    text = re.sub(r"\n\s+", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    text = text.strip()

    if len(text) > 4000:
        text = text[:4000].rstrip() + "…"

    return text


# kept for parity, though the ping header line was removed
def build_ping_roles(novel_title: str, extra_ping_roles_value: str) -> str:
    parts = []
    if GLOBAL_ROLE:
        parts.append(GLOBAL_ROLE.strip())
    if extra_ping_roles_value:
        parts.append(extra_ping_roles_value.strip())
    if novel_title in get_nsfw_novels():
        parts.append(NSFW_ROLE)
    return " ".join(p for p in parts if p)


# ─── Mistmint thread helpers (same principle as your other Mistmint scripts) ───

def sanitize_shortcode_from_title(title: str) -> str:
    up = (title or "").upper()
    return re.sub(r"[^A-Z0-9]+", "_", up).strip("_")


def thread_env_key_for(short_code: str) -> str:
    return f"{short_code}_THREAD_ID"


def resolve_thread_id(novel_title: str, details: dict) -> str | None:
    short_code = (details.get("short_code") or "").strip()
    if not short_code:
        short_code = sanitize_shortcode_from_title(novel_title)
    env_key = thread_env_key_for(short_code.upper())
    val = os.getenv(env_key, "").strip()
    return val or None


def build_thread_url(thread_id: str) -> str:
    return f"https://discord.com/channels/{MISTMINT_GUILD_ID}/{thread_id}"


# ─── Content / Embed builders (minimal edits you asked for) ────────────────────

def build_launch_content(
    title: str,
    novel_url: str,
    chap_name: str,
    chap_link: str,
    host: str,
    follow_thread_url: str,
) -> str:
    """
    Minimal edits:
    - REMOVED the first line that printed the ping_line + bow.
    - REPLACED the CTA with the fixed wording pointing to the computed thread URL.
    """
    chap_display = chap_name.replace("\u00A0", " ").strip()

    return (
        # removed: f"{ping_line} <a:Bow:...>\n"
        "## ꉂ`:fish_cake: ･ﾟ✧ New Series Launch ִֶָ. ..𓂃 ࣪ ִֶָ:wing:་༘࿐<a:1678whalepink:1368136879857205308>\n"
        f"***<a:kikilts_bracket:1365693072138174525>[{title}]({novel_url})<a:lalalts_bracket:1365693058905014313>*** — now officially added to cannibal turtle's lineup! <a:1620cupcakepink:1368136855903801404><a:Stars:1365568624466722816> \n\n"
        f"[{chap_display}]({chap_link}), is out on {host}. "
        "Please give lots of love to our new baby and welcome it to the server "
        "<a:hellokittydance:1365566988826705960>\n"
        "Updates will continue regularly, so hop in early and start reading now <a:2713pandaroll:1368137698212184136> \n"
        f"{'<a:6535_flower_border:1368146360871948321>' * 10}\n"
        f"-# To get notifications for new updates, follow {follow_thread_url}"
    )


def build_launch_embed(
    translator: str,
    title: str,
    novel_url: str,
    desc_text: str,
    cover_url: str,
    host_name: str,
    host_logo_url: str,
    chap_dt_local: datetime
) -> dict:
    iso_timestamp = chap_dt_local.astimezone(timezone.utc).isoformat()
    embed = {
        "author": {
            "name": f"{translator} <a:Bow:1365575505171976246>"
        },
        "title": title,
        "url": novel_url,
        "description": desc_text,
        "image": {"url": cover_url},
        "footer": {"text": host_name, "icon_url": host_logo_url},
        "color": 0xAEC6CF,
        "timestamp": iso_timestamp,
    }
    return embed


def load_novels_from_mapping():
    novels = []
    for host_name, host_data in HOSTING_SITE_DATA.items():
        translator   = host_data.get("translator", "")
        host_logo    = host_data.get("host_logo", "")
        novels_block = host_data.get("novels", {})
        for novel_title, details in novels_block.items():
            free_feed_url = details.get("free_feed")
            if not free_feed_url:
                continue
            novels.append({
                "host":             host_name,
                "translator":       translator,
                "host_logo":        host_logo,
                "novel_title":      novel_title,
                "novel_url":        details.get("novel_url", ""),
                "featured_image":   details.get("featured_image", ""),
                "free_feed":        free_feed_url,
                "custom_emoji":     details.get("custom_emoji", ""),
                "discord_role_url": details.get("discord_role_url", ""),
                "extra_ping_roles": details.get("extra_ping_roles", ""),
                "short_code":       details.get("short_code", ""),
            })
    return novels


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--feed",
        choices=["free"],
        required=True,
        help="We only announce once free/public chapters are available."
    )
    _ = parser.parse_args()

    bot_token = os.getenv(BOT_TOKEN_ENV)
    if not bot_token:
        sys.exit("❌ Missing DISCORD_BOT_TOKEN")

    state  = load_state()
    novels = load_novels_from_mapping()

    now_local = datetime.now(timezone.utc).astimezone()

    for novel in novels:
        if novel["host"] != "Mistmint Haven":
            continue
        novel_title = novel["novel_title"]
        host_name   = novel["host"]

        # only announce for first free
        if state.get(novel_title, {}).get("launch_free"):
            print(f"→ skipping {novel_title} (launch_free) — already launched")
            continue

        # route to per-novel thread via secret <SHORTCODE>_THREAD_ID
        # Show the precise expected env var (short_code aware)
        short_code = (novel.get("short_code") or sanitize_shortcode_from_title(novel_title)).upper()
        env_key    = thread_env_key_for(short_code)
        thread_id  = os.getenv(env_key, "").strip()
        if not thread_id:
            print(f"❌ No thread secret set for {novel_title}. Define {env_key}.")
            continue

        follow_url = build_thread_url(thread_id)

        # ⇩ join or verify membership before posting
        if not ensure_bot_in_thread(bot_token, thread_id):
            print(
                f"❌ Could not join or view thread {thread_id} for {novel_title}. "
                "Check View Channel, Read Message History, Send Messages in Threads, "
                "and whether the thread is private or archived."
            )
            continue

        feed_url = novel.get("free_feed")
        if not feed_url:
            continue

        print(f"Fetching free feed for {novel_title} from {feed_url}")
        resp = requests.get(feed_url, timeout=20)
        feed = feedparser.parse(resp.text)
        print(
            f"Parsed {len(feed.entries)} entries "
            f"(Content-Type: {resp.headers.get('Content-Type')})"
        )

        for entry in feed.entries:
            entry_title = (entry.get("title") or "").strip()
            if entry_title != novel_title:
                continue

            chap_field = (
                entry.get("chaptername")
                or entry.get("chapter")
                or ""
            )
            if not is_first_chapter_name(chap_field):
                continue

            chap_link = entry.link

            raw_desc_html = (
                entry.get("description")
                or entry.get("summary")
                or ""
            )
            desc_text = clean_feed_description(raw_desc_html)

            chap_dt_local = parsed_time_to_aware(
                entry.get("published_parsed") or entry.get("updated_parsed"),
                now_local
            )

            # we keep build_ping_roles around for parity, but we no longer include it in content
            # ping_line = build_ping_roles(novel_title, novel.get("extra_ping_roles",""))

            content_msg = build_launch_content(
                title=novel_title,
                novel_url=novel.get("novel_url", ""),
                chap_name=chap_field,
                chap_link=chap_link,
                host=host_name,
                follow_thread_url=follow_url,
            )

            embed_obj = build_launch_embed(
                translator=novel.get("translator", ""),
                title=novel_title,
                novel_url=novel.get("novel_url", ""),
                desc_text=desc_text,
                cover_url=novel.get("featured_image", ""),
                host_name=host_name,
                host_logo_url=novel.get("host_logo", ""),
                chap_dt_local=chap_dt_local
            )

            print(f"→ Built launch message for {novel_title} ({len(content_msg)} chars + 1 embed)")

            ok = safe_send_bot_embed(
                bot_token=bot_token,
                channel_or_thread_id=thread_id,
                content=content_msg,
                embed=embed_obj
            )

            if ok:
                print(f"✔️ Sent launch announcement for {novel_title} → thread {thread_id}")
                state.setdefault(novel_title, {})["launch_free"] = {
                    "chapter": chap_field,
                    "sent_at": datetime.now().isoformat()
                }
                save_state(state)
                commit_state_update(STATE_PATH)
            else:
                print("→ Send failed; not updating state.json")

            break


if __name__ == "__main__":
    main()
