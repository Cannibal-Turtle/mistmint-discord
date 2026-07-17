from __future__ import annotations

import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

API_BASE = "https://discord.com/api/v10"
MYT = ZoneInfo("Asia/Kuala_Lumpur")
SNOWFLAKE_RE = re.compile(r"^\d{15,22}$")


def parse_channel_ids(raw: str) -> list[str]:
    channel_ids: list[str] = []
    seen: set[str] = set()

    for value in re.split(r"[\s,;]+", raw.strip()):
        value = value.strip()
        if not value:
            continue
        if not SNOWFLAKE_RE.fullmatch(value):
            raise ValueError(
                f"Invalid channel/thread ID: {value!r}. "
                "Use numeric Discord IDs separated by commas, spaces, or new lines."
            )
        if value not in seen:
            seen.add(value)
            channel_ids.append(value)

    return channel_ids


def discord_request(
    token: str,
    method: str,
    path: str,
    *,
    payload: dict[str, Any] | None = None,
    attempts: int = 5,
) -> tuple[int, Any]:
    url = f"{API_BASE}{path}"
    body = json.dumps(payload).encode("utf-8") if payload is not None else None

    for attempt in range(1, attempts + 1):
        request = Request(
            url,
            data=body,
            method=method,
            headers={
                "Authorization": f"Bot {token}",
                "Content-Type": "application/json",
                "User-Agent": "delete-discord-today-workflow/2.0",
            },
        )

        try:
            with urlopen(request, timeout=30) as response:
                status = response.status
                response_body = response.read()
        except HTTPError as exc:
            status = exc.code
            response_body = exc.read()
        except URLError as exc:
            if attempt == attempts:
                return 0, str(exc)
            time.sleep(attempt)
            continue

        parsed: Any = ""
        if response_body:
            try:
                parsed = json.loads(response_body.decode("utf-8"))
            except Exception:
                parsed = response_body.decode("utf-8", errors="replace")

        if status != 429:
            return status, parsed

        retry_after = 1.0
        if isinstance(parsed, dict):
            try:
                retry_after = float(parsed.get("retry_after", retry_after))
            except (TypeError, ValueError):
                pass
        print(f"  Rate limited; retrying in {retry_after:.2f}s")
        time.sleep(max(retry_after, 0.25))

    return 429, {"message": "Rate limit retries exhausted"}


def error_text(body: Any) -> str:
    if isinstance(body, dict):
        message = body.get("message")
        code = body.get("code")
        if message and code is not None:
            return f"{message} (code {code})"
        if message:
            return str(message)
    return str(body or "no response body")


def fetch_today_message_ids(token: str, channel_id: str, cutoff: datetime) -> list[str]:
    message_ids: list[str] = []
    before = ""

    while True:
        query = "?limit=100"
        if before:
            query += f"&before={before}"

        status, body = discord_request(
            token,
            "GET",
            f"/channels/{channel_id}/messages{query}",
        )
        if status != 200:
            raise RuntimeError(f"GET messages returned HTTP {status}: {error_text(body)}")
        if not isinstance(body, list):
            raise RuntimeError("Discord returned an unexpected messages response")
        if not body:
            break

        reached_older_message = False
        for message in body:
            if not isinstance(message, dict):
                continue
            message_id = str(message.get("id") or "")
            timestamp_text = str(message.get("timestamp") or "")
            if not SNOWFLAKE_RE.fullmatch(message_id) or not timestamp_text:
                continue

            try:
                timestamp = datetime.fromisoformat(timestamp_text.replace("Z", "+00:00"))
            except ValueError as exc:
                raise RuntimeError(
                    f"Discord returned an invalid timestamp for message {message_id}: {timestamp_text}"
                ) from exc

            if timestamp < cutoff:
                reached_older_message = True
                break

            message_ids.append(message_id)

        if reached_older_message or len(body) < 100:
            break

        oldest = body[-1]
        before = str(oldest.get("id") or "") if isinstance(oldest, dict) else ""
        if not SNOWFLAKE_RE.fullmatch(before):
            raise RuntimeError("Could not determine the next Discord pagination cursor")

    return message_ids


def delete_message(token: str, channel_id: str, message_id: str) -> None:
    status, body = discord_request(
        token,
        "DELETE",
        f"/channels/{channel_id}/messages/{message_id}",
    )
    if status != 204:
        raise RuntimeError(
            f"DELETE message {message_id} returned HTTP {status}: {error_text(body)}"
        )


def bulk_delete(token: str, channel_id: str, message_ids: list[str]) -> None:
    status, body = discord_request(
        token,
        "POST",
        f"/channels/{channel_id}/messages/bulk-delete",
        payload={"messages": message_ids},
    )
    if status != 204:
        raise RuntimeError(f"Bulk delete returned HTTP {status}: {error_text(body)}")


def delete_ids(token: str, channel_id: str, message_ids: list[str]) -> None:
    for start in range(0, len(message_ids), 100):
        batch = message_ids[start : start + 100]
        if len(batch) == 1:
            delete_message(token, channel_id, batch[0])
            print(f"  Deleted 1 message ({batch[0]})")
        else:
            bulk_delete(token, channel_id, batch)
            print(f"  Deleted batch of {len(batch)} messages")


def main() -> int:
    token = os.environ.get("DISCORD_BOT_TOKEN", "").strip()
    raw_ids = os.environ.get("CHANNEL_IDS", "").strip()
    fallback_id = os.environ.get("FALLBACK_CHANNEL_ID", "").strip()

    if not token:
        print("DISCORD_BOT_TOKEN is missing", file=sys.stderr)
        return 2

    if not raw_ids:
        raw_ids = fallback_id

    try:
        channel_ids = parse_channel_ids(raw_ids)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    if not channel_ids:
        print(
            "No channel/thread IDs supplied, and FALLBACK_CHANNEL_ID is empty.",
            file=sys.stderr,
        )
        return 2

    now_myt = datetime.now(MYT)
    cutoff = now_myt.replace(hour=0, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    print(
        f"Deleting messages dated {now_myt:%Y-%m-%d} in Malaysia time "
        f"from {len(channel_ids)} channel(s)/thread(s)."
    )

    total_deleted = 0
    failures: list[tuple[str, str]] = []

    for index, channel_id in enumerate(channel_ids, start=1):
        print(f"\n[{index}/{len(channel_ids)}] Channel/thread {channel_id}")
        try:
            message_ids = fetch_today_message_ids(token, channel_id, cutoff)
            print(f"  Found {len(message_ids)} message(s) from today")
            if message_ids:
                delete_ids(token, channel_id, message_ids)
                total_deleted += len(message_ids)
        except Exception as exc:
            failures.append((channel_id, str(exc)))
            print(f"  FAILED: {exc}", file=sys.stderr)

    print(f"\nDeleted {total_deleted} message(s) across {len(channel_ids)} channel(s)/thread(s).")
    if failures:
        print(f"Failed in {len(failures)} channel(s)/thread(s):", file=sys.stderr)
        for channel_id, reason in failures:
            print(f"  - {channel_id}: {reason}", file=sys.stderr)
        return 1

    print("Done deleting today's Discord messages (MYT).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
