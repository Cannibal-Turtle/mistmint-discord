# Mistmint Discord — Operator Notes

Mistmint-specific Discord bot scripts for posting novel updates into **per-novel Discord threads**.

This repo is similar to `discord-webhook`, but the target is only:

```text id="or3286"
host == "Mistmint Haven"
```

and announcements are routed to each novel’s own thread using:

```text id="2tnct3"
config/thread_id_map.json
```

---

## What This Repo Does

This repo uses novel metadata from:

```python id="8du2la"
from novel_mappings import HOSTING_SITE_DATA
```

The actual novel data lives in the `rss-feed` repo under split TOML mapping files:

```text id="dk0iyx"
rss-feed/
├─ novel_mappings.py
└─ mappings/
   ├─ output_feeds.toml
   ├─ hosts/
   │  └─ mistmint_haven.toml
   └─ novels/
      ├─ tvitpa.toml
      ├─ tdlbkgc.toml
      ├─ amlwc.toml
      └─ ...
```

Depending on which scripts are included in the workflow, this repo can post:

* Free chapter announcements
* Paid/advance chapter announcements
* Comment announcements
* New series launch announcements
* New arc announcements
* Extras / side story announcements
* Completion announcements

All normal Mistmint posts should go to the novel’s own thread.

---

## Repository Structure

Important files and folders:

```text id="pj6jhx"
mistmint-discord/
├─ config/
│  ├─ embeds.json
│  ├─ feeds.json
│  ├─ files.json
│  ├─ server.json
│  └─ thread_id_map.json
├─ arc_history/
│  ├─ tvitpa_history.json
│  ├─ tdlbkgc_history.json
│  └─ ...
├─ bot_free_chapters.py
├─ bot_paid_chapters.py
├─ bot_comments.py
├─ new_novel_checker.py
├─ new_arc_checker.py
├─ new_extra_checker.py
├─ completed_novel_checker.py
├─ config_loader.py
├─ state.json
└─ README.md
```

---

## Main Difference From `discord-webhook`

`discord-webhook` posts to general/news/free/paid/comment channels.

`mistmint-discord` posts to **per-novel threads**.

Thread routing is based on short code:

```text id="wlyvvk"
short_code → thread_id
```

Example:

```json id="6op41c"
{
  "TDLBKGC": "1438462596381413417",
  "TVITPA": "1444214902322368675",
  "AMLWC": "1517842780003635240"
}
```

---

## One-Time Setup

### 1. Invite the Bot

Invite the bot to the Mistmint server with permissions:

* Send Messages
* Send Messages in Threads
* Read Message History
* Embed Links
* Attach Files, if images are used

---

### 2. Add Repo Secrets

Add these in:

```text id="bw1w9x"
Settings → Secrets and variables → Actions
```

Required:

| Secret              | Purpose           |
| ------------------- | ----------------- |
| `DISCORD_BOT_TOKEN` | Discord bot token |

Optional/legacy, only if an old workflow still uses it:

| Secret                  | Purpose                                 |
| ----------------------- | --------------------------------------- |
| `DISCORD_CHANNEL_ID`    | Legacy channel-based posting            |
| `<SHORTCODE>_THREAD_ID` | Legacy per-novel thread secret fallback |

Recommended current setup is **not** one secret per novel.

Use:

```text id="n6qlx2"
config/thread_id_map.json
```

instead.

---

### 3. Ensure State Files Exist

At minimum:

```json id="lp5lr6"
{}
```

in:

```text id="72s92j"
state.json
```

If your scripts use extra state files, make sure they also contain valid JSON.

Common pitfall:

```text id="eceh0p"
JSONDecodeError
```

usually means a state file is empty or malformed.

Fix it by committing:

```json id="0pil4w"
{}
```

---

## Install the RSS Mapping Package

The workflow should install the latest `rss-feed` package:

```bash id="do9wx8"
pip install --upgrade git+https://github.com/Cannibal-Turtle/rss-feed.git@main
```

This lets Mistmint scripts import:

```python id="cjsorg"
from novel_mappings import HOSTING_SITE_DATA
```

and helpers like:

```python id="n5gr3j"
get_novel_details_by_short_code(short_code)
find_novel_by_short_code(short_code)
short_code_has_free_chapters(short_code)
short_code_has_paid_chapters(short_code)
short_code_has_comments_feed(short_code)
```

---

## Dependencies

Workflow install step should include:

```bash id="pltz4u"
pip install discord.py feedparser python-dateutil aiohttp requests tomli
```

`tomli` is only needed for Python below 3.11, but it is safe to install.

---

## Config Files

Config lives in:

```text id="q2eouk"
config/
```

---

## `config/files.json`

This stores file paths used by scripts.

Example:

```json id="hb1dye"
{
  "state_path": "state.json",
  "rss_state_path": "state_rss.json",
  "thread_map_file": "config/thread_id_map.json",
  "arc_history_dir": "arc_history"
}
```

Important field:

```json id="4zslvo"
"thread_map_file": "config/thread_id_map.json"
```

This is what lets `config_loader.py` load the per-novel thread map.

---

## `config/thread_id_map.json`

This maps novel short codes to Discord thread IDs.

Example:

```json id="wa93jw"
{
  "TVITPA": "1444214902322368675",
  "TDLBKGC": "1438462596381413417",
  "AMLWC": "1517842780003635240"
}
```

Use the short code from the RSS novel TOML:

```toml id="afyhpm"
short_code = "AMLWC"
```

Then add the same key here:

```json id="z2rbo1"
"AMLWC": "1517842780003635240"
```

---

## How to Get a Thread ID

A Discord thread URL looks like:

```text id="lwo8yo"
https://discord.com/channels/1379303379221614702/1433327716937240626
```

The parts are:

```text id="c31w6p"
1379303379221614702 = server/guild ID
1433327716937240626 = thread ID
```

Use the second number as the thread ID.

Example:

```json id="0k5dyj"
{
  "NEWCODE": "1433327716937240626"
}
```

---

## `config/embeds.json`

Embed colors are configured here.

JSON does not allow `#` comments. Use `_comment` if you want a note.

Example:

```json id="gkc3bf"
{
  "_comment": "Color values can be fixed hex codes or \"novel\". When set to \"novel\", the bot uses theme_color/discord_color from rss-feed/mappings/novels/*.toml.",

  "colors": {
    "free_chapter": "FFF9BF",
    "paid_chapter": "A87676",
    "comments": "F0C7A4",
    "new_novel": "AEC6CF",
    "arc_unlocked": "FFF9BF",
    "arc_locked": "A87676"
  }
}
```

---

## Novel-Specific Embed Colors

Each color can use either a fixed hex code:

```json id="371aop"
"paid_chapter": "A87676"
```

or the novel’s default color from `rss-feed/mappings/novels/*.toml`:

```json id="je5cwu"
"paid_chapter": "novel"
```

When set to `"novel"`, the bot uses:

```toml id="7g70f2"
theme_color = "#c90016"
```

or:

```toml id="6piyit"
discord_color = "#c90016"
```

from the novel’s RSS TOML file.

If no novel color is found, it falls back to the script’s default color.

Example mixed config:

```json id="z7tkdg"
{
  "colors": {
    "free_chapter": "novel",
    "paid_chapter": "novel",
    "comments": "F0C7A4",
    "new_novel": "novel",
    "arc_unlocked": "novel",
    "arc_locked": "novel"
  }
}
```

Supported special values:

```text id="11gieb"
novel
theme
theme_color
discord_color
```

Recommended value:

```json id="8yj2sy"
"paid_chapter": "novel"
```

---

## Important Color Note

If `config/embeds.json` says:

```json id="2ohqa4"
"paid_chapter": "novel"
```

then the script must use:

```python id="aysyel"
resolve_embed_color(...)
```

not:

```python id="3sjzn1"
embed_color(...)
```

`embed_color(...)` expects a real hex color and will crash on `"novel"`.

---

## `config/server.json`

This stores server-level config.

Example values may include:

```json id="d0tmkh"
{
  "guild_id": "1379303379221614702"
}
```

The Mistmint server/guild ID is fixed unless the bot is moved to another server.

---

## `config/feeds.json`

This stores feed-related config if needed.

Most actual feed URLs come from `rss-feed` through `novel_mappings.py`.

---

## Where Data Belongs

### In `rss-feed/mappings/novels/*.toml`

Novel metadata belongs in the RSS repo.

Example:

```toml id="podahh"
host = "Mistmint Haven"

title = "After the Male Leads Went Crazy, They All Turned Into Male Ghosts"
short_code = "AMLWC"

novel_url = "https://mistminthaven.com/novel/after-the-male-leads-went-crazy-they-all-turned-into-male-ghosts/"
featured_image = "https://mistminthaven.com/wp-content/uploads/cover.jpg"

has_free = true
has_paid = true
has_comments = true

is_nsfw = false
is_membership = false

chapter_count = "92 Chapters"
last_chapter = "Chapter 92"
start_date = ""

history_file = "arc_history/amlwc_history.json"

discord_color = "#c90016"
```

### In `mistmint-discord/config/thread_id_map.json`

Thread IDs belong here.

Example:

```json id="3c8y3c"
{
  "AMLWC": "1517842780003635240"
}
```

Do not put thread IDs in RSS mapping unless a script explicitly needs that later.

---

## Adding a New Mistmint Novel

### 1. Add the Novel in `rss-feed`

Create a new TOML file:

```text id="g5d8w0"
rss-feed/mappings/novels/code.toml
```

Example:

```toml id="kha83b"
host = "Mistmint Haven"

title = "Novel Title Here"
short_code = "CODE"

novel_url = "https://mistminthaven.com/novel/..."
featured_image = "https://mistminthaven.com/wp-content/uploads/cover.jpg"

has_free = true
has_paid = true
has_comments = true

is_nsfw = false
is_membership = false

chapter_count = "120 Chapters + 5 Extras + 2 Side Stories"
last_chapter = "Chapter 120"
start_date = "14/02/2025"

history_file = "arc_history/code_history.json"

discord_color = "#c90016"
```

Notes:

* `short_code` should be unique.
* `last_chapter` is used by completion checker.
* `chapter_count` is used in completion/extras messages.
* `start_date` is used for duration text.
* `history_file` is used by arc checker.
* `discord_color` can be used when `embeds.json` uses `"novel"`.

---

### 2. Create or Get the Novel’s Thread

Create the novel thread in the Mistmint server.

Copy the thread ID from the thread URL.

Example URL:

```text id="7bdshp"
https://discord.com/channels/1379303379221614702/1433327716937240626
```

Thread ID:

```text id="kzazlu"
1433327716937240626
```

---

### 3. Add the Thread ID

Edit:

```text id="t5lr6l"
config/thread_id_map.json
```

Add:

```json id="whbmfl"
{
  "CODE": "1433327716937240626"
}
```

Keep the JSON valid.

Example with multiple novels:

```json id="4ff9bs"
{
  "TVITPA": "1444214902322368675",
  "TDLBKGC": "1438462596381413417",
  "AMLWC": "1517842780003635240",
  "CODE": "1433327716937240626"
}
```

---

### 4. Commit Changes

Commit changes in both repos as needed:

```text id="4vuwqr"
rss-feed
mistmint-discord
```

Then run the workflow.

---

## Scripts in This Repo

| Script                       | Purpose                            | Needs Feed                     | Posts To                                   | Required Secret     |
| ---------------------------- | ---------------------------------- | ------------------------------ | ------------------------------------------ | ------------------- |
| `bot_free_chapters.py`       | Free chapter announcements         | `free_feed`                    | Per-novel thread                           | `DISCORD_BOT_TOKEN` |
| `bot_paid_chapters.py`       | Paid/advance chapter announcements | `paid_feed`                    | Per-novel thread                           | `DISCORD_BOT_TOKEN` |
| `bot_comments.py`            | Comment announcements              | `comments_feed`                | Per-novel thread or configured destination | `DISCORD_BOT_TOKEN` |
| `new_novel_checker.py`       | New series launch                  | `free_feed`                    | Per-novel thread                           | `DISCORD_BOT_TOKEN` |
| `new_arc_checker.py`         | New locked/advance arc             | `free_feed` + `paid_feed`      | Per-novel thread                           | `DISCORD_BOT_TOKEN` |
| `new_extra_checker.py`       | Extras / side stories              | `paid_feed`                    | Per-novel thread                           | `DISCORD_BOT_TOKEN` |
| `completed_novel_checker.py` | Paid/free/only-free completion     | `paid_feed` and/or `free_feed` | Per-novel thread                           | `DISCORD_BOT_TOKEN` |

If you kept any legacy channel-based jobs, they may still use `DISCORD_CHANNEL_ID`.

Current Mistmint posting should route through per-novel threads.

---

## Feed Requirements Per Script

### Free Chapter Bot

Requires:

```text id="gs1a4k"
free_feed
```

Posts new free/public chapters.

---

### Paid Chapter Bot

Requires:

```text id="h18ljd"
paid_feed
```

Posts new paid/advance chapters.

---

### Comment Bot

Requires:

```text id="g9sd1c"
comments_feed
```

Posts new comments.

---

### New Series Launch

Requires:

```text id="8bcu36"
free_feed
```

Detects first public drops:

```text id="6oyt5h"
Chapter 1
Ch. 1
Episode 1
Ep. 1
1.1
Prologue
```

Skips paywalled-only debuts.

---

### New Arc Alerts

Requires:

```text id="ycsw59"
free_feed
paid_feed
history_file
```

If `history_file = ""`, the novel is skipped for arc tracking.

---

### Extras / Side Stories

Requires:

```text id="2nx7wm"
paid_feed
```

Extras totals may be parsed from `chapter_count` using words like:

```text id="paxgwi"
extras
side story
side stories
```

Example:

```toml id="q90wfc"
chapter_count = "120 Chapters + 5 Extras + 2 Side Stories"
```

---

### Completion

Runs in two modes:

```bash id="2op8qv"
python completed_novel_checker.py --feed paid
python completed_novel_checker.py --feed free
```

Uses:

```text id="odtwhl"
last_chapter
chapter_count
start_date
```

Paid completion and only-free completion can use `start_date`.

If:

```toml id="g1l70j"
start_date = ""
```

then the message skips the “After X of updates...” phrase.

If:

```toml id="gdewrt"
last_chapter = ""
```

completion checking may skip that novel.

---

## Message Style

### General Mistmint Behavior

* Posts go to per-novel threads.
* No global ping line by default.
* No generic channel posting unless a legacy job still does that.
* Thread routing comes from `config/thread_id_map.json`.

---

### `new_novel_checker.py`

The role-react instruction is replaced with a thread-follow instruction:

```text id="lj30x4"
To get notified on new chapters, follow https://discord.com/channels/1379303379221614702/<THREAD_ID> thread
```

The URL is composed from the thread ID.

Embed author name:

```text id="ymqre1"
{translator} <a:Bow:1365575505171976246>
```

---

### `new_extra_checker.py`

The old global header line is removed:

```text id="3b18ms"
base_mention | ONGOING_ROLE
```

Extras announcements post only to the novel thread.

---

### `completed_novel_checker.py`

Handles:

```text id="z7g0pa"
paid_completion
free_completion
only_free_completion
```

Posts only to per-novel threads.

---

## Arc History

Arc history files live in:

```text id="x9dhua"
arc_history/
```

Example:

```text id="v0w78y"
arc_history/amlwc_history.json
```

The matching RSS novel TOML should contain:

```toml id="rclh3d"
history_file = "arc_history/amlwc_history.json"
```

For no arc tracking:

```toml id="4krqmb"
history_file = ""
```

Scripts should treat empty history file as “skip arc tracking.”

---

## State Files

State files prevent duplicate posting.

Common flags:

```text id="i4bcwp"
launch_free
paid_completion
free_completion
only_free_completion
extra_announced
last_extra_announced
```

To re-announce something, delete the relevant flag for that novel, commit, and rerun.

Common pitfall:

```text id="ke2mtn"
JSONDecodeError
```

Fix by committing valid JSON:

```json id="f2bwmd"
{}
```

---

## Manual Runs

Run locally:

```bash id="ed7frf"
# Free chapters
python bot_free_chapters.py

# Paid chapters
python bot_paid_chapters.py

# Comments
python bot_comments.py

# Completion
python completed_novel_checker.py --feed paid
python completed_novel_checker.py --feed free

# Extras
python new_extra_checker.py

# New series launch
python new_novel_checker.py --feed free

# New arcs
python new_arc_checker.py
```

Make sure environment variables and config files are available locally.

---

## Short Code Rule

Preferred source:

```toml id="b50dx4"
short_code = "AMLWC"
```

from the RSS novel TOML.

If `short_code` is missing, some legacy code may auto-derive one from title:

```text id="j5ipxp"
Uppercase
Replace non-alphanumeric with _
Trim _ on both ends
```

But do not rely on that for new novels.

Always set an explicit short code.

---

## Adding a New Mistmint Paid Chapter

This happens on the `rss-feed` side.

Update the novel TOML if needed:

```toml id="pl7jqu"
pub_date_override = { hour = 12, minute = 0, second = 0 }
```

Update manual state if using STATE mode:

```text id="7dfw5p"
manual_scripts/mistmint_state.json
```

Example field:

```json id="ede6sw"
"latest_available_chapter": "Chapter 12"
```

Then run:

```text id="dq6180"
Update Paid Feed
```

workflow in `rss-feed`.

---

## Mistmint Mode Notes

The RSS side may run in:

```text id="eqhbiu"
API mode
STATE mode
```

STATE mode uses manual files like:

```text id="o22i68"
manual_scripts/mistmint_state.json
manual_scripts/paid_history.json
```

If switching modes, clear or update state carefully to avoid duplicate or missing paid chapter entries.

---

## Workflows

A typical workflow may run:

```bash id="23t44e"
python bot_free_chapters.py
python bot_paid_chapters.py
python bot_comments.py
python new_novel_checker.py --feed free
python new_arc_checker.py
python new_extra_checker.py
python completed_novel_checker.py --feed paid
python completed_novel_checker.py --feed free
```

The repo may be triggered by:

```text id="mh2g99"
rss-feed workflow dispatch
manual workflow_dispatch
schedule/cron
```

---

## Troubleshooting

### Post Went to the Wrong Place

Check:

```text id="6b8u0g"
config/thread_id_map.json
```

Make sure the short code matches the RSS TOML exactly.

Example:

```toml id="o5e41r"
short_code = "AMLWC"
```

must match:

```json id="u0miig"
"AMLWC": "1517842780003635240"
```

---

### Bot Did Not Post

Check:

* `DISCORD_BOT_TOKEN` exists.
* Bot is invited to the server.
* Bot has Send Messages in Threads.
* Thread ID is correct.
* The thread is not archived/locked, unless your script unarchives.
* The required feed exists for that script.

---

### Embed Color Crashed

If `config/embeds.json` says:

```json id="xxouxb"
"paid_chapter": "novel"
```

then the script must call:

```python id="0rujcn"
resolve_embed_color(...)
```

not:

```python id="1v5ezq"
embed_color(...)
```

`embed_color(...)` expects a hex code and will crash on `"novel"`.

---

### JSON Config Crashed

JSON does not allow comments.

Invalid:

```json id="dpbf8u"
# comment
{
  "colors": {}
}
```

Valid:

```json id="mlfndn"
{
  "_comment": "This is a note.",
  "colors": {}
}
```

---

### Arc Checker Skipped Novel

Check RSS novel TOML:

```toml id="ddnb2x"
history_file = ""
```

Empty means no arc tracking.

For arc tracking, use:

```toml id="au29uw"
history_file = "arc_history/code_history.json"
```

Also make sure:

```toml id="3f6flp"
has_free = true
has_paid = true
```

---

### Completion Checker Skipped Novel

Check:

```toml id="40811y"
last_chapter = ""
```

If empty, completion checking may skip.

Set:

```toml id="05kbbk"
last_chapter = "Chapter 92"
chapter_count = "92 Chapters"
```

---

### New Novel Did Not Announce

Check that the first public drop appears in the free feed and matches one of:

```text id="m6saiz"
Chapter 1
Ch. 1
Episode 1
Ep. 1
1.1
Prologue
```

Paywalled-only debuts are skipped.

---

## Design Guarantees

* This repo targets Mistmint Haven only.
* Posts route to per-novel threads.
* Thread IDs live in `config/thread_id_map.json`.
* Novel metadata lives in `rss-feed/mappings/novels/*.toml`.
* `novel_mappings.py` remains import-compatible.
* Embed colors can use fixed hex values or `"novel"`.
* `"novel"` colors resolve to `theme_color` or `discord_color` from RSS novel TOML.
* `history_file = ""` safely means no arc tracking.
* `start_date = ""` safely means no duration phrase in completion messages.
* State files prevent duplicate announcements.
* No Python script edits are needed per novel.
* New novels only need RSS TOML + thread map entry.

---

## New Novel Checklist

When adding a new Mistmint novel:

1. Add novel TOML in `rss-feed`:

   ```text
   mappings/novels/code.toml
   ```

2. Make sure it has:

   ```toml
   host = "Mistmint Haven"
   short_code = "CODE"
   has_free = true
   has_paid = true
   has_comments = true
   ```

3. Create/get the Discord thread ID.

4. Add the thread ID to:

   ```text
   config/thread_id_map.json
   ```

5. Add arc history only if needed:

   ```toml
   history_file = "arc_history/code_history.json"
   ```

6. Use empty history if not needed:

   ```toml
   history_file = ""
   ```

7. Commit both repos.

8. Run workflow.

---

## Workflow Overview

```text id="l16slu"
rss-feed updates XML feeds
   ↓
workflow triggers mistmint-discord
   ↓
mistmint-discord installs latest rss-feed package
   ↓
scripts import HOSTING_SITE_DATA
   ↓
scripts filter host == "Mistmint Haven"
   ↓
short_code resolves to thread_id
   ↓
announcement posts to the novel thread
   ↓
state/history files update to prevent duplicates
```

---

Now you can add Mistmint novels by updating TOML in `rss-feed` and adding one short-code thread entry in `mistmint-discord`.
