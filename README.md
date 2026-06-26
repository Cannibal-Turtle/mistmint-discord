# Mistmint Discord — Operator Notes

Mistmint-specific Discord bot scripts for posting novel updates into **per-novel Discord threads**.

This repo is similar to `discord-webhook`, but it filters announcements to:

```text
host == "Mistmint Haven"
```

and routes normal announcements to each novel’s own Discord thread using:

```text
config/thread_id_map.json
```

---

## What This Repo Does

This repo uses novel metadata from the `rss-feed` package:

```python
from novel_mappings import HOSTING_SITE_DATA
```

The actual novel data lives in `rss-feed` under split TOML mapping files:

```text
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

Depending on which workflow/scripts run, this repo can post:

- free chapter announcements
- paid/advance chapter announcements
- comment announcements
- new series launch announcements
- new arc announcements
- extras / side story announcements
- completion announcements

All normal Mistmint posts should go to the novel’s own thread.

---

## Repository Structure

Important files and folders:

```text
mistmint-discord/
├─ .github/workflows/
│  ├─ chapters_discord.yml
│  ├─ comments_discord.yml
│  ├─ rss_to_discord.yml
│  ├─ delete-discord-message.yml
│  └─ delete_discord_today.yml
├─ config/
│  ├─ embeds.json
│  ├─ feeds.json
│  ├─ files.json
│  ├─ server.json
│  └─ thread_id_map.json
├─ arc_history/
│  ├─ amlwc_history.json
│  ├─ hiaflg_history.json
│  ├─ tdlbkgc_history.json
│  └─ tvitpa_history.json
├─ message_templates/
│  ├─ comments.toml
│  ├─ completed_novels.toml
│  ├─ free_chapters.toml
│  ├─ new_arcs.toml
│  ├─ new_extras.toml
│  ├─ new_novels.toml
│  └─ paid_chapters.toml
├─ requirements/
│  ├─ chapters.txt
│  ├─ comments.txt
│  └─ rss_dispatch.txt
├─ bot_free_chapters.py
├─ bot_paid_chapters.py
├─ bot_comments.py
├─ new_novel_checker.py
├─ new_arc_checker.py
├─ new_extra_checker.py
├─ completed_novel_checker.py
├─ config_loader.py
├─ message_context.py
├─ message_renderer.py
├─ state.json
├─ state_rss.json
└─ README.md
```

---

## Main Difference From `discord-webhook`

`discord-webhook` posts to general/news/free/paid/comment channels.

`mistmint-discord` posts to **per-novel threads**.

Thread routing is based on short code:

```text
short_code → thread_id
```

Example:

```json
{
  "TDLBKGC": "1438462596381413417",
  "TVITPA": "1444214902322368675",
  "AMLWC": "1517851370055794868"
}
```

---

## One-Time Setup

### 1. Invite the Bot

Invite the bot to the Mistmint server with permissions:

- Send Messages
- Send Messages in Threads
- Read Message History
- Embed Links
- Attach Files, if images are used

---

### 2. Add Repo Secrets

Add these in:

```text
Settings → Secrets and variables → Actions
```

Required:

| Secret | Purpose |
| --- | --- |
| `DISCORD_BOT_TOKEN` | Discord bot token |

Optional/legacy, only if an old workflow still uses it:

| Secret | Purpose |
| --- | --- |
| `DISCORD_CHANNEL_ID` | Legacy channel-based posting |
| `<SHORTCODE>_THREAD_ID` | Legacy per-novel thread secret fallback |

Recommended current setup is **not** one secret per novel.

Use:

```text
config/thread_id_map.json
```

instead.

---

### 3. Ensure State Files Exist

At minimum, commit valid JSON in:

```text
state.json
state_rss.json
arc_history/*.json
```

For empty state files, use:

```json
{}
```

A common failure is:

```text
JSONDecodeError
```

That usually means a state file is empty or malformed.

---

## Install the RSS Mapping Package

The workflow should install the latest `rss-feed` package:

```bash
pip install --upgrade git+https://github.com/Cannibal-Turtle/rss-feed.git@main
```

This lets Mistmint scripts import:

```python
from novel_mappings import HOSTING_SITE_DATA
```

and helpers such as:

```python
get_novel_details_by_short_code(short_code)
find_novel_by_short_code(short_code)
short_code_has_free_chapters(short_code)
short_code_has_paid_chapters(short_code)
short_code_has_comments_feed(short_code)
```

---

## Dependencies

Use the requirement files in `requirements/` from the workflows.

Typical install commands:

```bash
pip install -r requirements/chapters.txt
pip install -r requirements/comments.txt
pip install -r requirements/rss_dispatch.txt
pip install --upgrade git+https://github.com/Cannibal-Turtle/rss-feed.git@main
```

Common direct dependencies include:

```bash
pip install discord.py feedparser python-dateutil aiohttp requests tomli
```

---

## Config Files

### `config/files.json`

Central paths:

```json
{
  "state_path": "state.json",
  "rss_state_path": "state_rss.json",
  "thread_map_file": "config/thread_id_map.json",
  "arc_history_dir": "arc_history"
}
```

---

### `config/server.json`

Mistmint server-level settings:

```json
{
  "host_target": "Mistmint Haven",
  "author_url": "https://www.mistminthaven.com/account/@CannibalTurtle-5082",
  "global_mention": "||@everyone||",
  "ping_user_id": "603578473814032414",
  "auto_archive_allowed": [60, 1440, 4320, 10080],
  "default_auto_archive_minutes": 10080,
  "announce_first_arc_release": false
}
```

| Field | Purpose |
| --- | --- |
| `host_target` | Host filter. Usually `Mistmint Haven` |
| `author_url` | Author/profile URL used in embeds |
| `global_mention` | Global mention text used where appropriate |
| `ping_user_id` | Optional user ID for admin/error pings |
| `auto_archive_allowed` | Allowed Discord thread archive durations |
| `default_auto_archive_minutes` | Default auto-archive value for thread handling |
| `announce_first_arc_release` | If `true`, allows the first detected arc to post a first-arc launch announcement after the new novel launch has been recorded |

---

### `config/thread_id_map.json`

Maps novel short codes to Discord thread IDs:

```json
{
  "TVITPA": "1444214902322368675",
  "TDLBKGC": "1438462596381413417",
  "ATVHE": "1462019944823656608",
  "WSMSC": "1469896904761544845",
  "HIAFLG": "1471742754261438620",
  "EC": "1488217762231877743",
  "AMLWC": "1517851370055794868"
}
```

Short codes should match the novel TOML in `rss-feed` exactly after uppercasing.

---

## How to Get a Thread ID

In Discord desktop/web:

1. Enable Developer Mode.
2. Right-click the thread.
3. Click **Copy Thread ID**.
4. Add it to `config/thread_id_map.json`.

Thread IDs are not the same as role IDs.

---

### `config/feeds.json`

Defines the RSS source URLs and dedupe keys:

```json
{
  "free": {
    "url": "https://raw.githubusercontent.com/Cannibal-Turtle/rss-feed/main/free_chapters_feed.xml",
    "last_guid_key": "free_last_guid",
    "seen_key": "free_seen_guids"
  },
  "paid": {
    "url": "https://raw.githubusercontent.com/Cannibal-Turtle/rss-feed/main/paid_chapters_feed.xml",
    "last_guid_key": "paid_last_guid",
    "seen_key": "paid_seen_guids"
  },
  "comments": {
    "url": "https://raw.githubusercontent.com/Cannibal-Turtle/rss-feed/main/aggregated_comments_feed.xml",
    "seen_key": "comments_seen_guids"
  },
  "seen_cap": 500
}
```

---

### `config/embeds.json`

Embed color settings:

```json
{
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

Colors can be hex strings or, where supported by Python, `"novel"`.

If a color is set to `"novel"`, it resolves from the novel TOML in `rss-feed`, usually `theme_color` or `discord_color`.

---

## Where Data Belongs

### In `rss-feed/mappings/novels/*.toml`

Novel metadata:

```toml
host = "Mistmint Haven"
title = "Novel Title"
short_code = "CODE"
novel_url = "https://..."
featured_image = "https://..."

has_free = true
has_paid = true
has_comments = true

is_nsfw = false
is_membership = false

chapter_count = "93 Chapters"
last_chapter = "Chapter 93"
start_date = ""
history_file = ""
discord_color = "#c90016"
```

### In `mistmint-discord/config/thread_id_map.json`

Only Mistmint thread routing:

```json
{
  "CODE": "123456789012345678"
}
```

Do not put novel metadata here.

---

## Scripts in This Repo

| Script | Purpose |
| --- | --- |
| `bot_free_chapters.py` | Posts free Mistmint chapters to the novel thread |
| `bot_paid_chapters.py` | Posts paid/advance Mistmint chapters to the novel thread |
| `bot_comments.py` | Posts comments to the novel thread |
| `new_novel_checker.py` | Announces a new Mistmint series launch |
| `new_arc_checker.py` | Announces new arcs |
| `new_extra_checker.py` | Announces extras/side stories |
| `completed_novel_checker.py` | Announces paid/free/only-free completion |

Shared helpers:

```text
config_loader.py
message_context.py
message_renderer.py
```

---

## Feed Requirements Per Script

### Free Chapter Bot

Needs:

```text
title
link
chapter
chaptername
host
short_code
pub_date/guid
```

### Paid Chapter Bot

Needs:

```text
title
link
chapter
chaptername
host
short_code
category/pub_date/guid
```

### Comment Bot

Needs:

```text
title
link
author/comment info
host
short_code
pub_date/guid
```

### New Series Launch

Detects first public drops such as:

```text
Chapter 1
Ch. 1
Episode 1
Ep. 1
1.1
Prologue
```

### New Arc Alerts

Uses paid feed + `history_file` from novel TOML.

### Extras / Side Stories

Detects extra/side-story chapter labels.

### Completion

Uses novel TOML fields such as:

```toml
chapter_count = "93 Chapters"
last_chapter = "Chapter 93"
start_date = ""
```

---

## Message Style

Templates live in:

```text
message_templates/
```

Current templates:

```text
comments.toml
completed_novels.toml
free_chapters.toml
new_arcs.toml
new_extras.toml
new_novels.toml
paid_chapters.toml
```

General behavior:

- normal chapter/comment posts go to the novel thread
- Mistmint-only scripts filter `host == "Mistmint Haven"`
- templates control the message wording and embed style
- config files control thread routing and colors
- Python controls parsing, dedupe, filtering, and posting logic

---

## Arc History

Arc history lives in:

```text
arc_history/
```

Use a history file only for novels that need arc tracking.

In `rss-feed/mappings/novels/code.toml`:

```toml
history_file = "arc_history/code_history.json"
```

Then create the file in this repo:

```text
arc_history/code_history.json
```

with:

```json
{}
```

If a novel does not use arc tracking:

```toml
history_file = ""
```

The checker safely skips it.

### First Arc Launch Announcement Switch

By default, the arc checker treats the first detected arc as a bootstrap setup step. This prevents old or existing Arc 1 data from being announced accidentally when arc tracking is first added.

The switch lives in `config/server.json`:

```json
"announce_first_arc_release": false
```

Default behavior:

```json
"announce_first_arc_release": false
```

The first detected arc is saved into arc history, but no first arc announcement is posted.

Enabled behavior:

```json
"announce_first_arc_release": true
```

The first detected arc can post a first-arc launch announcement.

This works for:

* free-only first arc
* paid-only first arc
* first run where unlocked and locked arc sections both have content

The checker only renders sections that have content:

* `has_unlocked = true` shows the Unlocked section
* `has_locked = true` shows the Locked section

So a free-only first arc will not show an empty Locked embed, and a paid-only first arc will not show an empty Unlocked embed.

First arc announcements are also delayed until the new novel launch announcement has been recorded in state. This prevents the first arc announcement from posting before the new novel launch message.

---

## State Files

State files prevent duplicate announcements:

```text
state.json
state_rss.json
arc_history/*.json
```

Make sure they contain valid JSON.

For empty state:

```json
{}
```

Common issue:

```text
JSONDecodeError
```

Fix by replacing the malformed file with valid JSON and committing it.

---

## Manual Runs

From the repo root, common manual script runs are:

```bash
python bot_free_chapters.py
python bot_paid_chapters.py
python bot_comments.py
python completed_novel_checker.py
python new_extra_checker.py
python new_novel_checker.py
python new_arc_checker.py
```

Make sure the required environment variables and dependencies are available before running locally.

---

## Short Code Rule

Short codes are the stable bridge between repos.

The same short code should be used in:

```text
rss-feed/mappings/novels/*.toml
mistmint-discord/config/thread_id_map.json
arc_history/code_history.json, if applicable
```

Use uppercase short codes:

```toml
short_code = "AMLWC"
```

---

## Adding a New Mistmint Novel

### 1. Add the Novel in `rss-feed`

Create:

```text
rss-feed/mappings/novels/code.toml
```

Minimum fields:

```toml
host = "Mistmint Haven"
title = "Novel Title"
short_code = "CODE"
novel_url = "https://..."
featured_image = "https://..."

has_free = true
has_paid = true
has_comments = true

is_nsfw = false
is_membership = false
```

Optional fields:

```toml
chapter_count = "93 Chapters"
last_chapter = "Chapter 93"
start_date = ""
history_file = ""
discord_color = "#c90016"
```

### 2. Create or Get the Novel Thread

Create the thread in Discord or open the existing novel thread.

Copy the thread ID using Developer Mode.

### 3. Add the Thread ID

Edit:

```text
config/thread_id_map.json
```

Add:

```json
{
  "CODE": "123456789012345678"
}
```

### 4. Add Arc History if Needed

If using arc tracking:

```toml
history_file = "arc_history/code_history.json"
```

and create:

```json
{}
```

in:

```text
arc_history/code_history.json
```

### 5. Commit Changes

Commit both repos if you changed both:

```text
rss-feed
mistmint-discord
```

---

## Adding a New Mistmint Paid Chapter

Usually no Discord repo edit is needed.

The flow is:

```text
Mistmint paid chapter appears
   ↓
rss-feed updates paid_chapters_feed.xml
   ↓
mistmint-discord reads paid feed
   ↓
short_code resolves to thread_id
   ↓
bot posts in the novel thread
```

Check these if it does not post:

1. The novel has `has_paid = true` in `rss-feed`.
2. The paid feed contains the item.
3. The short code exists in `config/thread_id_map.json`.
4. `DISCORD_BOT_TOKEN` is valid.
5. The bot can send messages in the thread.

---

## Mistmint Mode Notes

- This repo should only post Mistmint items.
- Host filtering uses `host_target` from `config/server.json`.
- Thread routing uses short code.
- No Python edit should be needed per novel.
- New novels need RSS TOML + thread map entry.

---

## Workflows

### `chapters_discord.yml`

Runs free and paid chapter posting.

### `comments_discord.yml`

Runs comment posting.

### `rss_to_discord.yml`

Runs checker-style announcements such as extras and completion.

### `delete-discord-message.yml`

Manual utility to delete specific Discord messages by ID.

### `delete_discord_today.yml`

Manual utility to delete today’s messages in a channel/thread.

---

## Troubleshooting

### Post Went to the Wrong Place

Check:

```text
config/thread_id_map.json
short_code in RSS/novel TOML
host_target in config/server.json
```

Also confirm the ID is a thread ID, not a role/channel/message ID.

### Bot Did Not Post

Check:

1. The feed contains a new item.
2. The item host is `Mistmint Haven`.
3. The item has a short code.
4. The short code exists in `thread_id_map.json`.
5. State did not already mark it as seen.
6. The bot has permission to send in the thread.

### Embed Color Crashed

Check:

```text
config/embeds.json
```

Valid colors:

```json
"FFF9BF"
"#FFF9BF"
"novel"
```

Invalid colors:

```json
"yellow"
"FFF"
"not-a-color"
```

If using `"novel"`, make sure the novel TOML has a valid color such as:

```toml
discord_color = "#c90016"
```

### JSON Config Crashed

JSON does not allow comments or trailing commas.

Bad:

```json
{
  "state_path": "state.json", // comment
}
```

Good:

```json
{
  "state_path": "state.json"
}
```

### Arc Checker Skipped Novel

Check:

```toml
history_file = "arc_history/code_history.json"
```

If it is empty, the skip is intentional.

### Completion Checker Skipped Novel

Check:

```toml
chapter_count = "93 Chapters"
last_chapter = "Chapter 93"
```

If `start_date = ""`, the duration phrase is omitted but completion can still work.

### New Novel Did Not Announce

Check that the feed item looks like a first chapter:

```text
Chapter 1
Ch. 1
Episode 1
Ep. 1
1.1
Prologue
```

Also check that the novel host is Mistmint and the short code resolves.

---

## Design Guarantees

- This repo is Mistmint-only.
- `novel_mappings.py` remains import-compatible.
- Novel metadata lives in `rss-feed`.
- Thread routing lives in `config/thread_id_map.json`.
- Embed colors live in `config/embeds.json`.
- Server-level behavior lives in `config/server.json`.
- `history_file = ""` safely means no arc tracking.
- `start_date = ""` safely means no duration phrase in completion messages.
- State files prevent duplicate announcements.
- No Python script edits are needed per novel.
- New novels only need RSS TOML + thread map entry.

---

## New Novel Checklist

When adding a new Mistmint novel:

1. Add novel TOML in `rss-feed/mappings/novels/`.
2. Make sure it has:

   ```toml
   host = "Mistmint Haven"
   short_code = "CODE"
   has_free = true
   has_paid = true
   has_comments = true
   ```

3. Create/get the Discord thread ID.
4. Add the thread ID to `config/thread_id_map.json`.
5. Add arc history only if needed:

   ```toml
   history_file = "arc_history/code_history.json"
   ```

6. Use empty history if not needed:

   ```toml
   history_file = ""
   ```

7. Commit both repos.
8. Run the workflow.

---

## Workflow Overview

```text
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
