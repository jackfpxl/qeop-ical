"""
Task: test_pings_next_event
Sends a nicely formatted test announcement for the NEXT upcoming event to
#bot-spam — including LIVE role pings (unlike scrape_events, which only
shows role names as plain text for preview purposes).

This exists to sanity-check what a real announcement + its pings will
actually look and feel like, before real auto-posting to the announcement
channels is switched on.

################################################################################
# WARNING: THIS TASK SENDS REAL, LIVE PINGS.                                #
# @role and @everyone mentions in the message WILL notify people who have  #
# that role (or everyone, for a Signature Event) and can see #bot-spam.    #
# It still only posts to #bot-spam — not the real announcement channels —  #
# but the pings themselves are real, not a preview.                        #
################################################################################

Requires:
    - DISCORD_BOT_TOKEN env var
    - DISCORD_GUILD_ID env var
    - Bot must have Send Messages / Mention Everyone permission in
      #bot-spam for pings to actually notify (otherwise they render as
      plain unclickable text with no notification — not an error, just silent)
    - playwright + its Chromium browser installed (see workflow file)
"""

import os

import discord

from tasks._discord_helper import run_discord_task
from tasks.scrape_events import (
    format_announcement,
    resolve_role_mentions,
    route_event,
    scrape_events,
)

POST_CHANNEL_NAME = os.environ.get("EVENTS_POST_CHANNEL_NAME", "bot-spam")


async def _send_test_ping(client, guild):
    print(f"Scraping events to find the next one...")
    events = await scrape_events()

    channel = discord.utils.get(guild.text_channels, name=POST_CHANNEL_NAME)
    if channel is None:
        print(f"WARNING: Could not find #{POST_CHANNEL_NAME}.")
        return

    if not events:
        print("No events found — nothing to test.")
        await channel.send(
            "**Test Pings with next event:** no upcoming events found "
            "to test with."
        )
        return

    # Events are listed in chronological order on the site, so the first
    # one scraped is the next occurring event.
    event = events[0]
    channel_name, role_labels = route_event(event["tags"], event["where"])
    mentions = resolve_role_mentions(guild, role_labels)

    message = (
        format_announcement(event, mentions)
        + f"\n\n-# Test run only — sent to #bot-spam, not the real "
        f"#{channel_name} channel."
    )

    # Explicitly allow the mentions actually in this message to fire —
    # discord.py's default already permits this, but being explicit here
    # makes the intent (real pings, deliberately) unambiguous in the code.
    allowed = discord.AllowedMentions(everyone=True, roles=True, users=False)
    await channel.send(message, allowed_mentions=allowed)

    print(f"Sent test ping for '{event['name']}' to #{POST_CHANNEL_NAME} "
          f"(would route to #{channel_name} for real; roles pinged: {role_labels})")


def run():
    run_discord_task(_send_test_ping)
