"""
Task: fix_announcement_timezones
ONE-OFF CLEANUP: rewrites already-posted announcement messages to use the
current date/time format (event-local time + abbreviation, e.g. "August
26, 2026 09:00 - 12:00 (PDT)") and strips out the old wording this
replaced — Discord's per-viewer <t:...> timestamp and the "Timezone
guessed from page text..." / "Timezone not detected..." confidence
warnings, which shouldn't have been in the public announcement.

For events still currently listed on the site, the message is fully
regenerated using fresh data (best outcome — correct new format
throughout). For events no longer listed (already happened, or
cancelled), there's no fresh data to reformat with, so this instead
does a targeted removal of just the old warning text/timestamp syntax
from the existing message, leaving everything else as-is.

Requires:
    - DISCORD_BOT_TOKEN / DISCORD_GUILD_ID env vars
    - Bot must have permission to edit its own messages in every
      announcement channel (it already does, from posting them)
    - playwright + its Chromium browser installed
"""

import re

import discord

from tasks._discord_helper import run_discord_task
from tasks.scrape_events import (
    POST_CHANNEL_NAME,
    format_announcement,
    load_state,
    resolve_role_mentions,
    route_event,
    scrape_events,
)

# Patterns matching the old wording, for the best-effort strip on
# messages we can't fully regenerate (event no longer listed).
OLD_TEXT_PATTERNS = [
    r"\n\s*⚠️ _Timezone guessed from page text \(no explicit label found\) — please verify_",
    r"\n\s*⚠️ _Timezone not detected on the event page — showing as UTC, please verify_",
    r"  _\(shown in your local time\)_",
]
OLD_TIMESTAMP_PATTERN = r"<t:\d+:F> – <t:\d+:t>|<t:\d+:F>"


def strip_old_wording(content):
    for pattern in OLD_TEXT_PATTERNS:
        content = re.sub(pattern, "", content)
    return content


async def _fix(client, guild):
    print("Scraping current events (for full reformatting where possible)...")
    events = await scrape_events()
    events_by_url = {e["url"]: e for e in events}

    state = load_state()
    tracked = state["tracked_events"]

    regenerated, stripped, skipped, failed = [], [], [], []

    for url, info in tracked.items():
        channel_id, message_id = info.get("channel_id"), info.get("message_id")
        if not channel_id or not message_id:
            continue

        channel = guild.get_channel(int(channel_id))
        if channel is None:
            skipped.append(f"{info.get('name', url)} (channel not found)")
            continue
        try:
            msg = await channel.fetch_message(int(message_id))
        except discord.NotFound:
            skipped.append(f"{info.get('name', url)} (message not found)")
            continue

        if "<t:" not in msg.content and "please verify" not in msg.content \
                and "shown in your local time" not in msg.content:
            continue  # already clean, nothing to do

        live_event = events_by_url.get(url)
        try:
            if live_event:
                channel_name, roles = route_event(live_event["tags"], live_event["where"])
                mentions = resolve_role_mentions(guild, roles)
                new_content = format_announcement(live_event, mentions)
                await msg.edit(content=new_content)
                regenerated.append(info.get("name", url))
                print(f"REGENERATED: {info.get('name', url)}")
            else:
                new_content = strip_old_wording(msg.content)
                if new_content != msg.content:
                    await msg.edit(content=new_content)
                    stripped.append(info.get("name", url))
                    print(f"STRIPPED old wording (no fresh data available): {info.get('name', url)}")
        except Exception as exc:
            failed.append(f"{info.get('name', url)}: {exc}")
            print(f"FAILED to fix '{info.get('name', url)}': {exc}")

    lines = [
        f"Fix Announcement Timezones — checked {len(tracked)} tracked event(s)",
        f"Fully regenerated (fresh data): {len(regenerated)}",
        f"Old wording stripped (no fresh data, event no longer listed): {len(stripped)}",
        f"Skipped (message/channel gone): {len(skipped)}",
        f"Failed: {len(failed)}",
    ]
    if regenerated:
        lines.append("")
        lines.extend(f"  + {name}" for name in regenerated)
    if stripped:
        lines.append("")
        lines.extend(f"  ~ {name}" for name in stripped)
    if failed:
        lines.append("")
        lines.extend(f"  ! {item}" for item in failed)

    summary = "\n".join(lines)
    print(summary)

    channel = discord.utils.get(guild.text_channels, name=POST_CHANNEL_NAME)
    if channel:
        await channel.send(f"```\n{summary[:1900]}\n```")


def run():
    run_discord_task(_fix)
