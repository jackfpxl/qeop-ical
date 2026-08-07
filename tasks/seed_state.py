"""
Task: seed_state
ONE-OFF SAFETY STEP for going live.

Scrapes the current event list and marks every one of them as "already
seen" in data/bot_state.json — WITHOUT posting anything to the real
announcement channels, regardless of the ANNOUNCE_LIVE setting.

Why this exists: scrape_events now posts real announcements for events
it hasn't seen before. If state doesn't already contain every event
that's been manually announced up to now, the first live run would
re-announce (and re-ping) things a human already posted weeks ago. Run
this once before turning the schedule on for real, so the slate is clean.

Safe to run more than once — it just unions the current event list into
the seen list, never removes anything.
"""

import discord

from tasks._discord_helper import run_discord_task
from tasks.scrape_events import POST_CHANNEL_NAME, load_state, save_state, scrape_events


async def _seed(client, guild):
    print("Scraping current events to seed 'already seen' state...")
    events = await scrape_events()

    state = load_state()
    before = len(state["seen_event_urls"])
    state["seen_event_urls"] = sorted(
        set(state["seen_event_urls"]) | {e["url"] for e in events}
    )
    added = len(state["seen_event_urls"]) - before
    save_state(state)

    summary = (
        f"Seeded state with {len(events)} current event(s) "
        f"({added} newly marked as seen, {len(events) - added} already were). "
        f"These are treated as already posted manually — nothing was sent "
        f"to the announcement channels. From now on, only genuinely new "
        f"events will trigger a real post."
    )
    print(summary)

    channel = discord.utils.get(guild.text_channels, name=POST_CHANNEL_NAME)
    if channel:
        await channel.send(summary)
    else:
        print(f"WARNING: Could not find #{POST_CHANNEL_NAME} to confirm this in.")


def run():
    run_discord_task(_seed)
