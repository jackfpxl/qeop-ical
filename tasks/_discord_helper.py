"""
Shared helper for tasks that need to connect to Discord.
Not a task itself (hence the leading underscore — main.py skips it).

Usage inside a task:

    from tasks._discord_helper import run_discord_task
    import discord

    async def _do_work(client, guild):
        ...

    def run():
        run_discord_task(_do_work)

If work_fn raises, the error is caught here, posted to #bot-spam (or
whatever ERROR_CHANNEL_NAME is set to) so it's visible without digging
through the Actions log, and then re-raised so the GitHub Actions run
still shows as failed.
"""

import asyncio
import os
import sys
import traceback

import discord

ERROR_CHANNEL_NAME = os.environ.get("ERROR_CHANNEL_NAME", "bot-spam")


def get_bot_token():
    token = os.environ.get("DISCORD_BOT_TOKEN")
    if not token:
        print("ERROR: DISCORD_BOT_TOKEN environment variable is not set.")
        sys.exit(1)
    return token


def get_guild_id():
    guild_id = os.environ.get("DISCORD_GUILD_ID")
    if not guild_id:
        print("ERROR: DISCORD_GUILD_ID environment variable is not set.")
        sys.exit(1)
    return int(guild_id)


async def report_error_to_discord(guild, task_name, exc):
    """Best-effort post of an error to #bot-spam. Never raises itself —
    a failure here should never mask the original error."""
    tb = traceback.format_exc()
    print(f"ERROR in task '{task_name}': {exc}\n{tb}")

    channel = discord.utils.get(guild.text_channels, name=ERROR_CHANNEL_NAME)
    if channel is None:
        print(f"(Could not find #{ERROR_CHANNEL_NAME} to post the error to.)")
        return

    # Keep well under Discord's 2000-char limit once wrapped in a code
    # block; the tail of a traceback is usually the useful part.
    tb_snippet = tb[-1500:]
    message = (
        f"⚠️ **Task '{task_name}' failed:**\n"
        f"```\n{type(exc).__name__}: {exc}\n\n{tb_snippet}\n```"
    )
    try:
        await channel.send(message)
    except Exception as post_err:
        print(f"(Also failed to post the error to #{ERROR_CHANNEL_NAME}: {post_err})")


def run_discord_task(work_fn, intents=None):
    """
    Connects to Discord, waits until ready, looks up the configured guild,
    then calls `await work_fn(client, guild)`, then disconnects cleanly.
    Any exception from work_fn is reported to #bot-spam before being
    re-raised.
    """
    if intents is None:
        intents = discord.Intents.default()
        intents.members = True

    client = discord.Client(intents=intents)
    guild_id = get_guild_id()

    @client.event
    async def on_ready():
        guild = client.get_guild(guild_id)
        if guild is None:
            print(f"ERROR: Bot is not in a server with ID {guild_id}.")
            await client.close()
            return
        try:
            await work_fn(client, guild)
        except Exception as exc:
            await report_error_to_discord(guild, work_fn.__module__, exc)
            raise
        finally:
            await client.close()

    client.run(get_bot_token())
