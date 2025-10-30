#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
new_arc_checker.py (mistmint-discord)

Detects new arcs/worlds and posts a 3-part announcement
(header, Unlocked list if any, Locked list) into the *per-novel thread*.

Routing:
  For each novel, resolve a thread id from environment:
    - If HOSTING_SITE_DATA[host].novels[title]['short_code'] exists, use it.
    - Else derive from title: uppercase + non-alnum -> underscore.
  Then read env:  <SHORTCODE>_THREAD_ID   (e.g. TDLBKGC_THREAD_ID=1433...)

Notes:
  - Only processes novels under host "Mistmint Haven".
  - No role pings; allowed_mentions is empty.
"""

import requests
import feedparser
import os
import json
import re
import sys

# try to load mapping package from your rss-feed repo
try:
    from novel_mappings import HOSTING_SITE_DATA, get_nsfw_novels
except Exception as e:
    print(f"⚠️ novel_mappings not available ({e}); using empty maps.")
    HOSTING_SITE_DATA = {}
    def get_nsfw_novels():
        return set()

# ─── CONFIG ────────────────────────────────────────────────────────────────────
BOT_TOKEN     = os.environ["DISCORD_BOT_TOKEN"]
HOST_TARGET   = "Mistmint Haven"   # only handle Mistmint
NSFW_ROLE_ID  = "<@&1343352825811439616>"  # detected but NOT mentioned
# ────────────────────────────────────────────────────────────────────────────────


# === DISCORD SEND ===

def post_message(thread_id: str, content: str, embeds: list | None = None, suppress_embeds: bool = False):
    """Minimal Discord POST wrapper; threads use the same channel endpoint."""
    url = f"https://discord.com/api/v10/channels/{thread_id}/messages"
    headers = {"Authorization": f"Bot {BOT_TOKEN}", "Content-Type": "application/json"}
    payload = {
        "content": content or "",
        "allowed_mentions": {"parse": []},  # no pings for Mistmint
    }
    if embeds:
        payload["embeds"] = embeds
    if suppress_embeds:
        payload["flags"] = 4
    r = requests.post(url, headers=headers, json=payload, timeout=20)
    if not r.ok:
        print(f"⚠️ Discord error {r.status_code}: {r.text}")
    r.raise_for_status()
    return r


# === FILE IO (history per novel) ===

def load_history(history_file):
    """Load arc history JSON; tolerate blank/invalid content."""
    if os.path.exists(history_file):
        raw = open(history_file, "r", encoding="utf-8").read().strip()
        if not raw:
            print(f"📂 {history_file} empty; init new history")
            return {"unlocked": [], "locked": [], "last_announced": ""}
        try:
            h = json.loads(raw)
        except json.JSONDecodeError:
            print(f"📂 {history_file} invalid JSON; init new history")
            h = {"unlocked": [], "locked": [], "last_announced": ""}
        h.setdefault("unlocked", [])
        h.setdefault("locked", [])
        h.setdefault("last_announced", "")
        print(f"📂 Loaded {history_file}: {len(h['unlocked'])} unlocked, {len(h['locked'])} locked, last={h['last_announced']}")
        return h
    print(f"📂 No {history_file}; init new history")
    return {"unlocked": [], "locked": [], "last_announced": ""}

def save_history(history, history_file):
    print(f"📂 Saving {history_file} (unlocked={len(history['unlocked'])}, locked={len(history['locked'])}, last={history['last_announced']})")
    with open(history_file, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=4, ensure_ascii=False)
    print(f"✅ Saved {history_file}")

def commit_history_update(history_file):
    print(f"📌 Commit {history_file}…")
    os.system("git config --global user.name 'GitHub Actions'")
    os.system("git config --global user.email 'actions@github.com'")
    os.system(f"git add {history_file}")
    changed = os.system("git diff --staged --quiet")
    if changed != 0:
        os.system(f"git commit -m 'Auto-update: {history_file}'")
        print("✅ Committed")
    else:
        print("ℹ️ No changes")
    if os.system("git push origin main") != 0:
        print("❌ Push failed; retry --force")
        os.system("git push origin main --force")


# === UTIL ===

def clean_feed_title(raw_title):
    return (raw_title or "").replace("*", "").strip()

def format_stored_title(title):
    m = re.match(r"(【Arc\s+\d+】)\s*(.*)", title or "")
    return f"**{m.group(1)}**{m.group(2)}" if m else f"**{title}**"

def extract_arc_number(title):
    m = re.search(r"【Arc\s*(\d+)】", title or "")
    return int(m.group(1)) if m else None

def deduplicate(lst):
    seen, out = set(), []
    for x in lst:
        if x not in seen:
            seen.add(x); out.append(x)
    return out

def nsfw_detected(feed_entries, novel_title):
    for e in feed_entries:
        if (novel_title or "").lower() in (e.get("title") or "").lower() and "nsfw" in (e.get("category","") or "").lower():
            print(f"⚠️ NSFW in entry: {e.get('title')}")
            return True
    return False

def extract_arc_title(nameextend):
    clean = (nameextend or "").strip("* ").strip()
    clean = re.sub(r"(?:\s+001|\(1\)|\.\s*1)$", "", clean).strip()
    return clean

def strip_any_number_prefix(s: str) -> str:
    return re.sub(r"^.*?\d+[^\w\s]*\s*", "", s or "")

def next_arc_number(history):
    n = extract_arc_number(history.get("last_announced", ""))
    if n: return n + 1
    nums = []
    for sec in ("unlocked","locked"):
        for t in history[sec]:
            m = extract_arc_number(t)
            if m: nums.append(m)
    return (max(nums) if nums else 0) + 1

DIGIT_EMOJI = {
    '0': '<:7987_zero_emj_png:1368137498496335902>',
    '1': '<:5849_one_emj_png:1368137451801149510>',
    '2': '<:4751_two_emj_png:1368137429369753742>',
    '3': '<:5286_three_emj_png:1368137406523637811>',
    '4': '<:4477_four_emj_png:1368137382813106196>',
    '5': '<:3867_five_emj_png:1368137358800715806>',
    '6': '<:8923_six_emj_png:1368137333886550098>',
    '7': '<:4380_seven_emj_png:1368137314240303165>',
    '8': '<:9891_eight_emj_png:1368137290517581995>',
    '9': '<:1898_nine_emj_png:1368137143196717107>',
}
def number_to_emoji(n: int) -> str:
    return ''.join(DIGIT_EMOJI[d] for d in str(n))


# === THREAD RESOLUTION (shortcode → env) ===

def sanitize_shortcode_from_title(title: str) -> str:
    return re.sub(r"[^A-Z0-9]+", "_", (title or "").upper()).strip("_")

def resolve_thread_id(novel_title: str, details: dict) -> str | None:
    sc = (details.get("short_code") or "").strip()
    if not sc:
        sc = sanitize_shortcode_from_title(novel_title)
    env_key = f"{sc.upper()}_THREAD_ID"
    val = os.getenv(env_key, "").strip()
    if not val:
        print(f"❌ Missing env {env_key} for '{novel_title}'")
        return None
    return val


# === CORE ARC DETECTION ===

def process_arc(novel, thread_id: str):
    print(f"\n=== Processing novel: {novel['novel_title']} → thread {thread_id} ===")

    # 0. Fetch feeds
    free_feed = feedparser.parse(novel["free_feed"])
    paid_feed = feedparser.parse(novel["paid_feed"])
    print(f"🌐 Fetched: {len(free_feed.entries)} free, {len(paid_feed.entries)} paid")

    # 1. NSFW (detected but not pinged)
    is_nsfw = (
        novel["novel_title"] in get_nsfw_novels()
        or nsfw_detected(free_feed.entries + paid_feed.entries, novel["novel_title"])
    )
    print(f"🕵️ NSFW={is_nsfw}")

    # 2. Load history
    history_file = novel.get("history_file")
    if not history_file:
        print(f"⚠️ No history_file for '{novel['novel_title']}', skipping.")
        return
    history = load_history(history_file)

    had_locked_before   = bool(history["locked"])
    had_unlocked_before = bool(history["unlocked"])

    # Helpers to identify “first chapter of a new arc”
    def is_new_marker(raw: str):
        if not raw: return False
        raw = raw.strip()
        return bool(re.search(r"(001|\(1\)|\.\s*1)(\*+)?\s*$", raw))

    def looks_like_arc_start(raw_vol: str, raw_chap: str, raw_extend: str):
        rv, rc, rext = (raw_vol or "").strip(), (raw_chap or "").strip(), (raw_extend or "").strip()
        if is_new_marker(rext) or is_new_marker(rc):
            return True
        if re.match(r"^\**\s*\d+\.\d+\s*\**$", rext):
            if re.match(r"(?i)^(arc|world|plane|story|volume|vol|v)\s*\d+", rv):
                return True
        if re.match(r"(?i)^(arc|world|plane|story|volume|vol|v)\s*\d+", rv):
            if not is_new_marker(rext) and not re.match(r"^\**\s*\d+\.\d+\s*\**$", rext):
                return True
        return False

    def extract_new_bases(feed, current_title):
        bases = []
        for e in feed.entries:
            if (e.get("title") or "").strip() != current_title:
                continue
            raw_vol    = (e.get("volume") or "").replace("\u00A0", " ").strip()
            raw_extend = (e.get("nameextend") or "").replace("\u00A0", " ").strip()
            raw_chap   = (e.get("chaptername") or "").replace("\u00A0", " ").strip()
            if not looks_like_arc_start(raw_vol, raw_chap, raw_extend):
                continue
            if raw_vol:
                base = clean_feed_title(raw_vol)
            elif raw_extend:
                base = extract_arc_title(raw_extend)
            else:
                base = raw_chap
            base = strip_any_number_prefix(base)
            bases.append(base)
        return bases

    free_new = extract_new_bases(free_feed, novel["novel_title"])
    paid_new = extract_new_bases(paid_feed, novel["novel_title"])
    print(f"🔍 New bases: free={len(free_new)}, paid={len(paid_new)}")

    # 3. Update history
    free_created, paid_created = False, False

    # free side
    for base in free_new:
        matched_locked = False
        for full in history["locked"][:]:
            if full.endswith(base):
                matched_locked = True
                history["locked"].remove(full)
                if full not in history["unlocked"]:
                    history["unlocked"].append(full)
                    print(f"🔓 Unlocked arc: {full}")
                break
        if not matched_locked:
            seen_bases = [re.sub(r"^【Arc\s*\d+】", "", t) for t in (history["unlocked"] + history["locked"])]
            if base not in seen_bases:
                n = next_arc_number(history)
                full = f"【Arc {n}】{base}"
                history["unlocked"].append(full)
                free_created = True
                print(f"🌿 Brand-new free arc: {full}")

    # paid side
    seen_bases = [re.sub(r"^【Arc\s*\d+】", "", f) for f in (history["unlocked"] + history["locked"])]
    for base in paid_new:
        if base not in seen_bases:
            n = next_arc_number(history)
            full = f"【Arc {n}】{base}"
            history["locked"].append(full)
            paid_created = True
            print(f"🔐 New locked arc: {full}")

    # dedupe
    history["unlocked"] = deduplicate(history["unlocked"])
    history["locked"]   = deduplicate(history["locked"])

    # 3.5 Bootstrap: if first-ever run created entries, save numbering only
    first_run = (not had_locked_before and not had_unlocked_before)
    if first_run and (free_created or paid_created):
        if history["locked"]:
            history["last_announced"] = history["locked"][-1]
            print(f"🌱 Bootstrap: last_announced = {history['last_announced']}")
        save_history(history, history_file)
        commit_history_update(history_file)
        return

    # Special-case exits
    if free_created and not paid_created and not history["locked"]:
        print("🌱 First arc started FREE; save numbering only.")
        save_history(history, history_file); commit_history_update(history_file); return
    if paid_created and not free_created and not had_locked_before and not had_unlocked_before:
        print("💸 First arc started PAID-only; save numbering only.")
        save_history(history, history_file); commit_history_update(history_file); return

    # if no locked arcs, nothing to hype
    if not history["locked"]:
        print("ℹ️ No locked arcs. Done.")
        return

    new_full = history["locked"][-1]
    if new_full == history.get("last_announced", ""):
        print(f"✅ Already announced: {new_full}")
        return

    # Build strings
    world_number = extract_arc_number(new_full)
    world_emoji  = number_to_emoji(world_number) if world_number is not None else ""

    unlocked_list = history["unlocked"]
    locked_list   = history["locked"]

    unlocked_md = "\n".join(format_stored_title(t) for t in unlocked_list) if unlocked_list else ""
    locked_lines = [format_stored_title(t) for t in locked_list]
    locked_lines = deduplicate(locked_lines)
    if locked_lines:
        locked_lines[-1] = f"<a:9410pinkarrow:1368139217556996117>{locked_lines[-1]}"
    locked_md = "\n".join(locked_lines) if locked_lines else "None"

    # 4. Build messages (no pings, no role-react footer)
    content_header = (
        "## <a:announcement:1365566215975731274> NEW ARC ALERT "
        "<a:pinksparkles:1365566023201198161>"
        "<a:Butterfly:1365572264774471700>"
        "<a:pinksparkles:1365566023201198161>\n"
        f"***<:babypinkarrowleft:1365566594503147550>"
        f"<:world_01:1368202193038999562>"
        f"<:world_02:1368202204468613162> {world_emoji}"
        f"<:babypinkarrowright:1365566635838275595>is Live for*** "
        "<a:pinkloading:1365566815736172637>\n"
        f"### [{novel['novel_title']}]({novel['novel_link']}) "
        "<a:Turtle_Police:1365223650466205738>\n"
        "❀° ┄───────────────────────╮"
    )

    embed_unlocked = None
    if unlocked_md:
        embed_unlocked = {"description": unlocked_md, "color": 0xFFF9BF}

    embed_locked = {"description": f"||{locked_md}||", "color": 0xA87676}

    footer_and_react = (
        "╰───────────────────────┄ °❀\n"
        f"> *Advance access is ready for you on {novel['host']}! "
        "<a:holo_diamond:1365566087277711430>*\n"
        + "<:pinkdiamond_border:1365575603734183936>" * 6
    )

    # 5. Send to thread
    header_ok = False
    try:
        post_message(thread_id, content_header, suppress_embeds=True)
        header_ok = True
        print(f"✅ Header sent: {new_full}")
    except requests.RequestException as e:
        print(f"⚠️ Header send failed: {e}", file=sys.stderr)

    if embed_unlocked:
        try:
            post_message(thread_id, "<a:5693pinkwings:1368138669004820500> `Unlocked 🔓` <a:5046_bounce_pink:1368138460027813888>",
                         embeds=[embed_unlocked])
            print("✅ Unlocked embed sent")
        except requests.RequestException as e:
            print(f"⚠️ Unlocked send failed: {e}", file=sys.stderr)
    else:
        print("ℹ️ No unlocked arcs block.")

    try:
        post_message(thread_id, "<a:5693pinkwings:1368138669004820500> `Locked 🔐` <a:5046_bounce_pink:1368138460027813888>",
                     embeds=[embed_locked])
        print("✅ Locked embed sent")
    except requests.RequestException as e:
        print(f"⚠️ Locked send failed: {e}", file=sys.stderr)

    try:
        post_message(thread_id, footer_and_react, suppress_embeds=True)
        print("✅ Footer sent")
    except requests.RequestException as e:
        print(f"⚠️ Footer send failed: {e}", file=sys.stderr)

    # 6. Record announcement
    if header_ok:
        history["last_announced"] = new_full
        save_history(history, history_file)
        commit_history_update(history_file)
        print(f"📌 Recorded last_announced = {new_full}")
    else:
        print("⚠️ Skipped updating last_announced (header failed).")


# === LOAD & RUN ===
if __name__ == "__main__":
    for host, host_data in (HOSTING_SITE_DATA or {}).items():
        if host != HOST_TARGET:
            continue
        for title, d in host_data.get("novels", {}).items():
            # needs both feeds to do “locked vs unlocked” logic
            if not d.get("free_feed") or not d.get("paid_feed"):
                continue

            thread_id = resolve_thread_id(title, d)
            if not thread_id:
                # env like TDLBKGC_THREAD_ID must be set
                continue

            novel = {
                "novel_title":      title,
                "host":             host,
                "free_feed":        d["free_feed"],
                "paid_feed":        d["paid_feed"],
                "novel_link":       d.get("novel_url", ""),
                "history_file":     d.get("history_file", ""),
            }
            process_arc(novel, thread_id)
