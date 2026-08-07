"""
Task: sync_discord_events
Reconciliation/backfill task for Discord's native Scheduled Events feature.

Checks every currently-listed event (from the live site) against the
guild's actual current Scheduled Events, and creates one for any that's
missing — e.g. because it was scraped before the Discord Event feature
existed, creation failed silently on a previous run, or someone manually
deleted the Discord Event afterward.

This does NOT post announcement messages to the region channels — it only
touches Discord's native Scheduled Events feature. Run it any time you
want to double check everything's in sync (e.g. after enabling this
feature for the first time, so past events that were announced before it
existed get a Scheduled Event created retroactively).

Requires:
    - DISCORD_BOT_TOKEN / DISCORD_GUILD_ID env vars
    - Bot must have the "Manage Events" permission
    - playwright + its Chromium browser installed
"""

import discord

from tasks._discord_helper import run_discord_task
from tasks.scrape_events import (
    POST_CHANNEL_NAME,
    build_discord_event_name,
    create_discord_event,
    load_state,
    parse_event_datetime,
    save_state,
    scrape_events,
    update_discord_event,
)


async def _sync(client, guild):
    print("Scraping current events...")
    events = await scrape_events()
    print(f"Found {len(events)} events on the site")

    print("Fetching guild's current Discord Scheduled Events...")
    existing_events = await guild.fetch_scheduled_events()
    existing_ids = {str(ev.id) for ev in existing_events}
    print(f"Guild currently has {len(existing_events)} Scheduled Event(s)")

    state = load_state()
    discord_event_ids = state["discord_event_ids"]

    already_ok = []
    renamed = []
    retimed = []
    created = []
    skipped_no_date = []
    failed = []

    for e in events:
        desired_name = build_discord_event_name(e)
        stored_id = discord_event_ids.get(e["url"])

        # Match by stored ID first (most reliable), then fall back to
        # matching by name — checking both the old unprefixed form (for
        # events created before this feature existed) and the current
        # prefixed form.
        matching = None
        if stored_id:
            matching = discord.utils.get(existing_events, id=int(stored_id)) \
                if str(stored_id) in existing_ids else None
        if matching is None:
            matching = discord.utils.get(existing_events, name=e["name"]) \
                or discord.utils.get(existing_events, name=desired_name)

        if matching:
            already_ok.append(e["name"])
            if not stored_id:
                discord_event_ids[e["url"]] = matching.id
            if matching.name != desired_name:
                try:
                    await matching.edit(name=desired_name)
                    renamed.append(f"{matching.name} -> {desired_name}")
                    print(f"RENAMED: '{matching.name}' -> '{desired_name}'")
                except Exception as rename_exc:
                    failed.append(f"{e['name']} (rename): {rename_exc}")
                    print(f"RENAME FAILED for '{matching.name}': {rename_exc}")

            # Also correct the scheduled time using the freshly-detected
            # timezone — catches events created before per-event timezone
            # detection existed, which may have the wrong UTC instant
            # even though the name/location are otherwise fine.
            start_dt, end_dt = parse_event_datetime(e["when"], e.get("timezone"))
            if start_dt is not None and (matching.start_time != start_dt or matching.end_time != end_dt):
                try:
                    await update_discord_event(guild, matching.id, e)
                    retimed.append(e["name"])
                    print(f"RETIMED: '{e['name']}' -> {start_dt.isoformat()} - {end_dt.isoformat()}")
                except Exception as retime_exc:
                    failed.append(f"{e['name']} (retime): {retime_exc}")
                    print(f"RETIME FAILED for '{e['name']}': {retime_exc}")
            continue

        start_dt, end_dt = parse_event_datetime(e["when"], e.get("timezone"))
        if start_dt is None:
            skipped_no_date.append(e["name"])
            print(f"SKIPPED (unparseable date): {e['name']} — when='{e['when']}'")
            continue

        try:
            scheduled = await create_discord_event(guild, e, start_dt, end_dt)
            discord_event_ids[e["url"]] = scheduled.id
            created.append(desired_name)
            print(f"CREATED: {desired_name} ({start_dt.isoformat()} - {end_dt.isoformat()})")
        except Exception as exc:
            failed.append(f"{e['name']}: {exc}")
            print(f"FAILED to create: {e['name']}: {exc}")

    state["discord_event_ids"] = discord_event_ids
    save_state(state)

    lines = [
        f"Discord Event Sync — {len(events)} event(s) checked against "
        f"{len(existing_events)} existing Scheduled Event(s)",
        "",
        f"Already had a matching Scheduled Event: {len(already_ok)}",
        f"Renamed to add region prefix: {len(renamed)}",
        f"Retimed (wrong timezone/schedule corrected): {len(retimed)}",
        f"Newly created: {len(created)}",
    ]
    if renamed:
        lines.extend(f"  ~ {item}" for item in renamed)
    if retimed:
        lines.extend(f"  @ {name}" for name in retimed)
    if created:
        lines.extend(f"  + {name}" for name in created)
    if skipped_no_date:
        lines.append(f"Skipped (couldn't parse date/time): {len(skipped_no_date)}")
        lines.extend(f"  ? {name}" for name in skipped_no_date)
    if failed:
        lines.append(f"Failed to create: {len(failed)}")
        lines.extend(f"  ! {item}" for item in failed)

    summary = "\n".join(lines)
    print(summary)

    channel = discord.utils.get(guild.text_channels, name=POST_CHANNEL_NAME)
    if channel:
        await channel.send(f"```\n{summary[:1900]}\n```")
    else:
        print(f"WARNING: Could not find #{POST_CHANNEL_NAME} to post the summary.")


def run():
    run_discord_task(_sync)
