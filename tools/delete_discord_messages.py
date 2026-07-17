from __future__ import annotations

import json
import os
import re
import sys
import time
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API_BASE = "https://discord.com/api/v10"
ROOT = Path(__file__).resolve().parents[1]
SNOWFLAKE_RE = re.compile(r"^\d{15,22}$")
MESSAGE_LINK_RE = re.compile(
    r"https?://(?:www\.)?(?:discord(?:app)?\.com)/channels/(?:@me|\d+)/(\d+)/(\d+)",
    re.IGNORECASE,
)


def ordered_add(items: list[str], seen: set[str], value: Any) -> None:
    text = str(value or "").strip()
    if SNOWFLAKE_RE.fullmatch(text) and text not in seen:
        seen.add(text)
        items.append(text)


def load_json_file(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except Exception as exc:
        print(f"Warning: could not read {path}: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def load_json_url(url: str) -> dict[str, Any]:
    try:
        req = Request(url, headers={"User-Agent": "discord-message-delete-workflow/1.0"})
        with urlopen(req, timeout=20) as response:
            data = json.load(response)
    except Exception as exc:
        print(f"Warning: could not load {url}: {exc}")
        return {}
    return data if isinstance(data, dict) else {}


def parse_message_input(raw: str) -> tuple[list[str], dict[str, str]]:
    messages: list[str] = []
    seen: set[str] = set()
    direct_channels: dict[str, str] = {}

    def consume_link(match: re.Match[str]) -> str:
        channel_id, message_id = match.groups()
        ordered_add(messages, seen, message_id)
        direct_channels[message_id] = channel_id
        return " "

    remainder = MESSAGE_LINK_RE.sub(consume_link, raw)
    for message_id in re.findall(r"\d{15,22}", remainder):
        ordered_add(messages, seen, message_id)

    return messages, direct_channels


def collect_channel_fields(
    data: Any,
    channels: list[str],
    channel_seen: set[str],
    guilds: list[str],
    guild_seen: set[str],
    parent_key: str = "",
) -> None:
    if isinstance(data, dict):
        for key, value in data.items():
            key_text = str(key).strip().casefold()
            if isinstance(value, (dict, list)):
                collect_channel_fields(
                    value,
                    channels,
                    channel_seen,
                    guilds,
                    guild_seen,
                    key_text,
                )
                continue

            text = str(value or "").strip()
            if not SNOWFLAKE_RE.fullmatch(text):
                continue

            if "guild" in key_text:
                ordered_add(guilds, guild_seen, text)
                continue

            excluded = ("role", "user", "member", "message", "emoji", "ping")
            channelish = (
                "channel",
                "thread",
                "archive",
                "comments",
                "announcement",
                "chapter",
                "mod",
                "forum",
            )
            if any(word in key_text for word in excluded):
                continue
            if any(word in key_text for word in channelish):
                ordered_add(channels, channel_seen, text)

    elif isinstance(data, list):
        for value in data:
            collect_channel_fields(
                value,
                channels,
                channel_seen,
                guilds,
                guild_seen,
                parent_key,
            )


def collect_all_values(data: Any, channels: list[str], seen: set[str]) -> None:
    if isinstance(data, dict):
        for value in data.values():
            collect_all_values(value, channels, seen)
    elif isinstance(data, list):
        for value in data:
            collect_all_values(value, channels, seen)
    else:
        ordered_add(channels, seen, data)


def discover_configuration() -> tuple[list[str], list[str]]:
    channels: list[str] = []
    channel_seen: set[str] = set()
    guilds: list[str] = []
    guild_seen: set[str] = set()

    # Downstream Discord repositories: local server and thread map.
    local_server = load_json_file(ROOT / "config" / "server.json")
    collect_channel_fields(local_server, channels, channel_seen, guilds, guild_seen)

    local_thread_map = load_json_file(ROOT / "config" / "thread_id_map.json")
    collect_all_values(local_thread_map, channels, channel_seen)

    # rss-feed: channel IDs already registered for novel-card updates.
    local_targets = load_json_file(ROOT / "novel_status_targets.json")
    collect_channel_fields(local_targets, channels, channel_seen, guilds, guild_seen)

    # rss-feed: load both downstream Discord integrations from their raw configs.
    integrations = load_json_file(ROOT / "config" / "integrations.json")
    for section in integrations.values():
        if not isinstance(section, dict):
            continue
        raw_base = str(section.get("raw_base") or "").strip().rstrip("/")
        paths = section.get("paths")
        if not raw_base or not isinstance(paths, dict):
            continue

        server_path = str(paths.get("server_json") or "").strip().lstrip("/")
        if server_path:
            server = load_json_url(f"{raw_base}/{server_path}")
            collect_channel_fields(server, channels, channel_seen, guilds, guild_seen)

        for key, path_value in paths.items():
            if "thread" not in str(key).casefold():
                continue
            path = str(path_value or "").strip().lstrip("/")
            if path:
                thread_map = load_json_url(f"{raw_base}/{path}")
                collect_all_values(thread_map, channels, channel_seen)

    return guilds, channels


def discord_request(
    token: str,
    method: str,
    path: str,
    *,
    attempts: int = 4,
) -> tuple[int, Any]:
    url = path if path.startswith("http") else f"{API_BASE}{path}"

    for attempt in range(1, attempts + 1):
        req = Request(
            url,
            method=method,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "discord-message-delete-workflow/1.0",
            },
        )
        try:
            with urlopen(req, timeout=30) as response:
                body = response.read()
                status = response.status
        except HTTPError as exc:
            status = exc.code
            body = exc.read()
        except URLError as exc:
            if attempt == attempts:
                return 0, str(exc)
            time.sleep(attempt)
            continue

        parsed: Any = ""
        if body:
            try:
                parsed = json.loads(body.decode("utf-8"))
            except Exception:
                parsed = body.decode("utf-8", errors="replace")

        if status != 429:
            return status, parsed

        retry_after = 1.0
        if isinstance(parsed, dict):
            try:
                retry_after = float(parsed.get("retry_after") or retry_after)
            except (TypeError, ValueError):
                pass
        print(f"Rate limited; retrying after {retry_after:.2f}s")
        time.sleep(max(0.1, min(retry_after, 30.0)))

    return 429, {"message": "rate limit retries exhausted"}


def discover_live_channels(
    token: str,
    guilds: Iterable[str],
    channels: list[str],
    seen: set[str],
) -> None:
    message_channel_types = {0, 5, 10, 11, 12}

    for guild_id in guilds:
        status, payload = discord_request(token, "GET", f"/guilds/{guild_id}/channels")
        if status == 200 and isinstance(payload, list):
            for channel in payload:
                if not isinstance(channel, dict):
                    continue
                if channel.get("type") in message_channel_types:
                    ordered_add(channels, seen, channel.get("id"))
        else:
            print(f"Warning: could not enumerate channels in guild {guild_id} (HTTP {status})")

        status, payload = discord_request(token, "GET", f"/guilds/{guild_id}/threads/active")
        if status == 200 and isinstance(payload, dict):
            for thread in payload.get("threads", []):
                if isinstance(thread, dict):
                    ordered_add(channels, seen, thread.get("id"))
        else:
            print(f"Warning: could not enumerate active threads in guild {guild_id} (HTTP {status})")


def locate_message_with_search(token: str, guilds: Iterable[str], message_id: str) -> str | None:
    numeric_id = int(message_id)
    query = urlencode(
        {
            "min_id": str(numeric_id - 1),
            "max_id": str(numeric_id + 1),
            "limit": "25",
        }
    )

    for guild_id in guilds:
        status, payload = discord_request(
            token,
            "GET",
            f"/guilds/{guild_id}/messages/search?{query}",
        )
        if status != 200 or not isinstance(payload, dict):
            continue

        for group in payload.get("messages", []):
            if not isinstance(group, list):
                continue
            for message in group:
                if not isinstance(message, dict):
                    continue
                if str(message.get("id")) == message_id:
                    channel_id = str(message.get("channel_id") or "").strip()
                    if SNOWFLAKE_RE.fullmatch(channel_id):
                        return channel_id

    return None


def delete_from_channel(token: str, channel_id: str, message_id: str) -> tuple[bool, int, Any]:
    status, payload = discord_request(
        token,
        "DELETE",
        f"/channels/{channel_id}/messages/{message_id}",
    )
    return status == 204, status, payload


def main() -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    raw_ids = os.environ.get("MESSAGE_IDS", "")

    if not token:
        print("DISCORD_BOT_TOKEN is missing.")
        return 2

    message_ids, direct_channels = parse_message_input(raw_ids)
    if not message_ids:
        print("No valid Discord message IDs were provided.")
        return 2

    guilds, channels = discover_configuration()
    channel_seen = set(channels)
    discover_live_channels(token, guilds, channels, channel_seen)

    print(f"Messages requested: {len(message_ids)}")
    print(f"Guilds available for exact search: {len(guilds)}")
    print(f"Candidate channels/threads: {len(channels)}")

    deleted = 0
    failed: list[str] = []
    last_successful_channel: str | None = None

    for message_id in message_ids:
        print(f"\nLooking for message {message_id}...")

        direct_channel = direct_channels.get(message_id)
        if direct_channel:
            ok, status, payload = delete_from_channel(token, direct_channel, message_id)
            if ok:
                print(f"Deleted {message_id} from channel/thread {direct_channel}")
                deleted += 1
                last_successful_channel = direct_channel
                continue
            print(f"Could not delete linked message {message_id}: HTTP {status} {payload}")
            failed.append(message_id)
            continue

        search_channel = locate_message_with_search(token, guilds, message_id)
        if search_channel:
            ok, status, payload = delete_from_channel(token, search_channel, message_id)
            if ok:
                print(f"Deleted {message_id} from channel/thread {search_channel}")
                deleted += 1
                last_successful_channel = search_channel
                continue
            print(f"Exact search found channel {search_channel}, but delete returned HTTP {status}: {payload}")

        candidates = list(channels)
        if last_successful_channel and last_successful_channel in channel_seen:
            candidates.remove(last_successful_channel)
            candidates.insert(0, last_successful_channel)

        found = False
        forbidden_channels = 0
        for channel_id in candidates:
            ok, status, payload = delete_from_channel(token, channel_id, message_id)
            if ok:
                print(f"Deleted {message_id} from channel/thread {channel_id}")
                deleted += 1
                last_successful_channel = channel_id
                found = True
                break
            if status == 403:
                forbidden_channels += 1
            elif status not in {400, 404}:
                print(f"Warning: HTTP {status} while checking {channel_id}: {payload}")

        if not found:
            suffix = f" ({forbidden_channels} inaccessible candidates)" if forbidden_channels else ""
            print(f"Message {message_id} was not found in any configured accessible channel/thread{suffix}.")
            failed.append(message_id)

    print(f"\nDeleted: {deleted}")
    print(f"Not deleted: {len(failed)}")
    if failed:
        print("Unresolved message IDs: " + ", ".join(failed))
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
