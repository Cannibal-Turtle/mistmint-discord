# Mistmint Discord — Operator Notes

Minimal changes from your `discord-webhook` setup, but **posts to per‑novel threads via env secrets**. Target is **host == "Mistmint Haven"** only.

---

## What this repo does

- Uses mappings from **`rss-feed` → `novel_mappings.HOSTING_SITE_DATA`**.
- Announces (depending on which scripts you include in the workflow):
  - **Extras / Side Stories** (paid feed)
  - **Completion** (paid / free / only‑free)
  - **New Series Launch** (first free/public drop)
- **Destination**: each novel’s **own Discord thread** via `THREAD_ID_MAP (JSON)` or legacy `<SHORTCODE>_THREAD_ID` fallback
- **No generic channel posting** for Mistmint (we don’t use `DISCORD_CHANNEL_ID` here except in any legacy jobs you kept).

---

## One-time setup

1. Invite the bot to Mistmint server with permissions:
   - Send Messages
   - Send Messages in Threads
   - Read Message History

2. Add repo secrets:
   - DISCORD_BOT_TOKEN
   - THREAD_ID_MAP   ← one single JSON secret for all novels (recommended)

   Example value for THREAD_ID_MAP:
   ```json
   {
     "TDLBKGC": "1438462596381413417",
     "TVITPA": "1444214902322368675"
   }
   ```

   (Legacy fallback `<SHORTCODE>_THREAD_ID` secrets still work but are optional.)

4. Ensure `state.json` exists and is valid JSON:
   ```json
   {}
   ```

---

## Adding a new Mistmint novel (checklist)

1. **Update mapping in `rss-feed`**  
   In `novel_mappings.HOSTING_SITE_DATA["Mistmint Haven"]["novels"]`, add an entry like:

   ```python
   "Title Case Name Here": {
       "short_code": "TDLBKGC",              # optional but recommended; else auto-sanitized from title
       "novel_url": "https://…",
       "featured_image": "https://…/cover.jpg",
       "free_feed": "https://…/feed/free.xml",   # needed for launch + free completion
       "paid_feed": "https://…/feed/paid.xml",   # needed for extras + paid completion
       "last_chapter": "Chapter 123",            # substring to match the final chapter in feed
       "chapter_count": "120 chapters + 5 extras + 2 side stories",
       "start_date": "14/02/2025"                # DD/MM/YYYY (for duration calc)
       # Optional (not used in Mistmint messages, kept for compatibility):
       # "discord_role_id": "",
       # "extra_ping_roles": "",
       # "custom_emoji": "",
       # "discord_role_url": ""
   }
   ```

   > **Extras totals** are parsed from the `chapter_count` string using the literal words **“extras”** and **“side story/side stories”**.

2. **Create / get the novel’s thread**
   - Make the thread in Mistmint server and copy its **Thread ID**.  
     Thread URL example:  
     `https://discord.com/channels/1379303379221614702/1433327716937240626`  
     - Server (guild) ID: `1379303379221614702` (fixed for Mistmint)  
     - **Thread ID**: `1433327716937240626`

3. **Add the thread ID to THREAD_ID_MAP (recommended):**
   - Edit the existing THREAD_ID_MAP secret
   - Append a new key:

     "NEWCODE": "123456789012345678"

4. **Commit rss-feed changes.**
5. **Run workflow.**

(Do NOT add new YAML env lines when using THREAD_ID_MAP.)

---

## Scripts in this repo (what they post & what they need)

| Script                        | Purpose                                      | Needs feed             | Posts to            | Secrets required                                  |
|------------------------------|----------------------------------------------|------------------------|---------------------|---------------------------------------------------|
| `new_extra_checker.py`       | **Extras / Side Stories** announcement       | `paid_feed`            | Per‑novel thread    | `DISCORD_BOT_TOKEN`, `<SHORTCODE>_THREAD_ID`      |
| `completed_novel_checker.py` | **Completion** (paid / free / only‑free)     | `paid_feed` and/or `free_feed` | Per‑novel thread | `DISCORD_BOT_TOKEN`, `<SHORTCODE>_THREAD_ID`      |
| `new_novel_checker.py`       | **New Series Launch** (first free chapter)   | `free_feed`            | Per‑novel thread    | `DISCORD_BOT_TOKEN`, `<SHORTCODE>_THREAD_ID`      |

> If you kept any legacy channel‑based jobs (e.g., old arc checker), those may still use `DISCORD_CHANNEL_ID` / webhook. Mistmint posts route to threads instead.

---

## Message style (already baked in)

- **No global ping line** in Mistmint posts.
- **`new_novel_checker.py`**:
  - Replaces the role‑react instruction with:  
    `To get notified on new chapters, follow https://discord.com/channels/1379303379221614702/<THREAD_ID> thread`
    (URL composed from the thread id secret).
  - Embed author name is: `"{translator} <a:Bow:1365575505171976246>"`.
- **`new_extra_checker.py`**:
  - Removed the `base_mention | ONGOING_ROLE` header line.
- **`completed_novel_checker.py`**:
  - Handles `paid_completion`, `free_completion`, and `only_free_completion`.
  - Posts only to per‑novel threads.

---

## `state.json` (how to reset)

- Per‑novel flags are stored here to prevent double‑posting:
  - `launch_free`
  - `paid_completion`, `free_completion`, `only_free_completion`
  - `extra_announced`, `last_extra_announced`
- **To re‑announce**, delete the relevant flag for that novel, commit, rerun.

**Common pitfall:** `JSONDecodeError` → your `state.json` is empty or malformed. Fix by committing `{}`.

---

## Manual runs (local)

```bash
# Completion
python completed_novel_checker.py --feed paid
python completed_novel_checker.py --feed free

# Extras
python new_extra_checker.py

# New series launch
python new_novel_checker.py --feed free
```

---

## Shortcode rule (for secrets)

- If `short_code` is **not** set in the mapping, we auto‑derive:
  - Uppercase
  - Replace non‑alphanumeric with `_`
  - Trim `_` on both ends
- THREAD_ID resolution order:
  - THREAD_ID_MAP["SHORTCODE"]
  - <SHORTCODE>_THREAD_ID (legacy fallback)
  - ERROR + skip novel
    
---

## Legacy mode (optional)

- If you prefer the old per-novel secrets:
  - You may keep <SHORTCODE>_THREAD_ID secrets
  - You must also wire each one into YAML
  - Scripts will automatically fallback to them if THREAD_ID_MAP has no match

> This mode requires manual YAML updates and is not recommended.

---

## Add a new Mistmint novel (per-novel thread mapping)

When you add a Mistmint Haven novel, do **two** things:

1) **Create the secret**
   - Go to: **Settings → Secrets and variables → Actions → New repository secret**
   - Name: `<SHORTCODE>_THREAD_ID`  
     Example: `TDLBKGC_THREAD_ID`
   - Value: the Discord **thread ID** (numbers only), e.g. `1433327716937240626`

2) **Wire the secret into the workflow env**
   - When using THREAD_ID_MAP you only wire ONE secret in YAML:

env:
  DISCORD_BOT_TOKEN: ${{ secrets.DISCORD_BOT_TOKEN }}
  THREAD_ID_MAP: ${{ secrets.THREAD_ID_MAP }}

     ```yaml
    env:
      DISCORD_BOT_TOKEN: ${{ secrets.DISCORD_BOT_TOKEN }}
      THREAD_ID_MAP: ${{ secrets.THREAD_ID_MAP }}
     ```
---

## Add a new Mistmint paid chapter (rss-feed repo side)

- Update `"pub_date_override"` from `HOSTING_SITE_DATA` in [`novel_mappings.py`](https://github.com/Cannibal-Turtle/rss-feed/blob/main/novel_mappings.py).
- Update `"latest_available_chapter"` in [`mistmint_state.json`](https://github.com/Cannibal-Turtle/rss-feed/blob/main/mistmint_state.json).
- Run `Update Paid Feed` workflow.

**Notes**
- You do **not** edit the Python scripts per novel. Scripts auto-resolve the correct thread from `<SHORTCODE>_THREAD_ID`.
- `<SHORTCODE>` comes from `HOSTING_SITE_DATA` (`short_code`), or falls back to a sanitized UPPERCASE title (non-alnum → `_`).
- If you later rename a short code, update:  
  a) the secret name in **Actions secrets**, and  
  b) the matching `env:` line in the workflow.

