"""
Task: export_members
Exports every member of the configured Discord server to a CSV file
in the output/ folder, with as much detail as the Discord API exposes,
plus a message-activity summary (count + last message date) pulled from
every text channel the bot can read.

Requires:
    - DISCORD_BOT_TOKEN env var
    - DISCORD_GUILD_ID env var
    - Bot must have "Server Members Intent" enabled in the Dev Portal
    - Bot must have "Read Message History" permission in the server
      (needed for the message-count/last-active columns; does NOT
      require Message Content Intent since we only read metadata,
      not message text)
"""

import csv
import os

import discord

from tasks._discord_helper import run_discord_task

OUTPUT_FILE = os.path.join("output", "members_export.csv")

# Name of the channel to post the CSV into (without the #).
# Override at runtime by setting the POST_CHANNEL_NAME env var.
POST_CHANNEL_NAME = os.environ.get("POST_CHANNEL_NAME", "bot-spam")

# Safety cap on how many messages to scan per channel when building the
# activity summary. None = no limit (scans entire channel history).
# Override with the MESSAGE_SCAN_LIMIT env var if a full-history scan is
# too slow on a busy/old server.
_scan_limit_env = os.environ.get("MESSAGE_SCAN_LIMIT")
MESSAGE_SCAN_LIMIT = int(_scan_limit_env) if _scan_limit_env else None


async def _build_activity_summary(guild):
    """
    Scans every text channel the bot can read message history in and
    tallies, per member: total message count and their most recent
    message timestamp. Returns {member_id: {"count": int, "last": datetime}}.
    """
    activity = {}

    for channel in guild.text_channels:
        perms = channel.permissions_for(guild.me)
        if not (perms.view_channel and perms.read_message_history):
            print(f"Skipping #{channel.name} (no read history permission)")
            continue

        print(f"Scanning #{channel.name}...")
        try:
            async for message in channel.history(limit=MESSAGE_SCAN_LIMIT):
                author_id = message.author.id
                entry = activity.setdefault(author_id, {"count": 0, "last": None})
                entry["count"] += 1
                if entry["last"] is None or message.created_at > entry["last"]:
                    entry["last"] = message.created_at
        except discord.Forbidden:
            print(f"Skipping #{channel.name} (forbidden)")
            continue

    return activity


async def _export(client, guild):
    print(f"Fetching members for: {guild.name} ({guild.member_count} members)...")

    activity = await _build_activity_summary(guild)

    rows = []
    async for member in guild.fetch_members(limit=None):
        roles = [role.name for role in member.roles if role.name != "@everyone"]
        member_activity = activity.get(member.id, {"count": 0, "last": None})
        rows.append({
            "id": member.id,
            "username": member.name,
            "discriminator": member.discriminator,
            "display_name": member.display_name,
            "nickname": member.nick or "",
            "bot": member.bot,
            "joined_at": member.joined_at.isoformat() if member.joined_at else "",
            "account_created_at": member.created_at.isoformat(),
            "roles": ", ".join(roles),
            "top_role": member.top_role.name if member.top_role else "",
            "avatar_url": str(member.display_avatar.url) if member.display_avatar else "",
            "premium_since": member.premium_since.isoformat() if member.premium_since else "",
            "message_count": member_activity["count"],
            "last_message_at": member_activity["last"].isoformat() if member_activity["last"] else "",
        })

    if not rows:
        print("No members found — check bot permissions and intents.")
        return

    fieldnames = list(rows[0].keys())
    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    print(f"Exported {len(rows)} members to {OUTPUT_FILE}")

    channel = discord.utils.get(guild.text_channels, name=POST_CHANNEL_NAME)
    if channel is None:
        print(f"WARNING: Could not find a #{POST_CHANNEL_NAME} channel in "
              f"'{guild.name}'. CSV was created but not posted. Check the "
              f"channel name and that the bot can see it.")
        return

    perms = channel.permissions_for(guild.me)
    if not (perms.send_messages and perms.attach_files):
        print(f"WARNING: Bot lacks Send Messages / Attach Files permission "
              f"in #{POST_CHANNEL_NAME}. CSV was created but not posted.")
        return

    await channel.send(
        content=f"Member export complete — {len(rows)} members.",
        file=discord.File(OUTPUT_FILE),
    )
    print(f"Posted CSV to #{POST_CHANNEL_NAME}")


def run():
    run_discord_task(_export)
