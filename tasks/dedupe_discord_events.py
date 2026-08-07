"""
Task: dedupe_discord_events
ONE-OFF CLEANUP: finds Discord Scheduled Events that are duplicates of the
same real-world event and cancels all but one.

IMPORTANT: duplicates are grouped by (start time, location) rather than
by name. The bot's naming scheme (region prefix, ⭐ for Signature Events,
[ONLINE] marker) has changed over time as it was refined, and Discord
Event name-matching used as a fallback when the stored ID lookup failed
couldn't recognize an old-format name as "the same event" as the new
format — so it created a second event instead of renaming the first.
Grouping by name would miss exactly this case, since the whole point is
that the two duplicates have DIFFERENT names. Date + location is what's
actually invariant for "the same real event."

For each duplicate group, the event whose description (which is always
set to the event's site URL) matches a currently-listed event gets its
name recomputed fresh and is kept/renamed to the current correct format;
the rest are cancelled. If none match a current listing (e.g. the event
already happened), the most recently created one is kept as a reasonable
default.

Uses "cancel" rather than delete, consistent with how the rest of this
bot handles removed events. Also repairs data/bot_state.json so anything
pointing at a cancelled duplicate gets repointed at the survivor.

Requires:
    - DISCORD_BOT_TOKEN / DISCORD_GUILD_ID env vars
    - Bot must have the "Manage Events" permission
    - playwright + its Chromium browser installed (needs to scrape the
      site to recompute correct names for survivors)
"""

import discord

from tasks._discord_helper import run_discord_task
from tasks.scrape_events import (
    POST_CHANNEL_NAME,
    build_discord_event_name,
    load_state,
    save_state,
    scrape_events,
)


def group_key(ev):
    location = (ev.location or "").strip().lower()
    start = ev.start_time.isoformat() if ev.start_time else None
    return (start, location)


async def _dedupe(client, guild):
    print("Scraping current events (to recompute correct names for survivors)...")
    events = await scrape_events()
    events_by_url = {e["url"]: e for e in events}

    print("Fetching guild's current Discord Scheduled Events...")
    existing = await guild.fetch_scheduled_events()
    # Only consider still-active ones — already-cancelled events from a
    # previous cleanup (or normal cancellation) aren't duplicates to fix.
    existing = [ev for ev in existing if ev.status == discord.EventStatus.scheduled]
    print(f"Guild has {len(existing)} active Scheduled Event(s)")

    groups = {}
    for ev in existing:
        groups.setdefault(group_key(ev), []).append(ev)
    duplicate_groups = {k: v for k, v in groups.items() if len(v) > 1}

    if not duplicate_groups:
        summary = "Dedupe check: no duplicate Scheduled Events found. Nothing to do."
        print(summary)
        channel = discord.utils.get(guild.text_channels, name=POST_CHANNEL_NAME)
        if channel:
            await channel.send(summary)
        return

    state = load_state()
    discord_event_ids = state["discord_event_ids"]
    tracked = state["tracked_events"]

    lines = [f"Dedupe Discord Events — found {len(duplicate_groups)} duplicate group(s)"]
    cancelled_total = 0
    renamed_total = 0

    for (start, location), evs in duplicate_groups.items():
        # Try to find which duplicate's description URL matches a
        # currently-listed event, so we can recompute its correct name
        # and be confident which one to keep.
        matched_url = None
        matched_event_data = None
        for ev in evs:
            url = (ev.description or "").strip()
            if url in events_by_url:
                matched_url = url
                matched_event_data = events_by_url[url]
                break

        evs_sorted = sorted(evs, key=lambda e: e.id, reverse=True)  # newest first
        keep = evs_sorted[0]
        extras = evs_sorted[1:]

        label = matched_event_data["name"] if matched_event_data else (keep.name or "(unnamed)")
        lines.append(f"\n\"{label}\" @ {location or 'unknown location'} — "
                      f"{len(evs)} copies found, keeping id {keep.id}")

        if matched_event_data:
            correct_name = build_discord_event_name(matched_event_data)
            if keep.name != correct_name:
                try:
                    await keep.edit(name=correct_name)
                    renamed_total += 1
                    lines.append(f"  renamed kept event to current format: \"{correct_name}\"")
                except Exception as exc:
                    lines.append(f"  FAILED to rename kept event: {exc}")

        extra_ids = {ev.id for ev in extras}
        if matched_url:
            discord_event_ids[matched_url] = keep.id
            if matched_url in tracked:
                tracked[matched_url]["discord_event_id"] = keep.id
        for url, tracked_id in list(discord_event_ids.items()):
            if tracked_id in extra_ids:
                discord_event_ids[url] = keep.id
        for info in tracked.values():
            if info.get("discord_event_id") in extra_ids:
                info["discord_event_id"] = keep.id

        for ev in extras:
            try:
                await ev.edit(status=discord.EventStatus.cancelled)
                cancelled_total += 1
                lines.append(f"  cancelled duplicate id {ev.id} (\"{ev.name}\")")
                print(f"Cancelled duplicate: '{ev.name}' (id {ev.id})")
            except Exception as exc:
                lines.append(f"  FAILED to cancel id {ev.id}: {exc}")
                print(f"FAILED to cancel duplicate '{ev.name}' (id {ev.id}): {exc}")

    state["discord_event_ids"] = discord_event_ids
    state["tracked_events"] = tracked
    save_state(state)

    lines.insert(1, f"Cancelled {cancelled_total} duplicate(s), renamed {renamed_total} "
                     f"survivor(s) to current format, state repaired.")
    summary = "\n".join(lines)
    print(summary)

    channel = discord.utils.get(guild.text_channels, name=POST_CHANNEL_NAME)
    if channel:
        await channel.send(f"```\n{summary[:1900]}\n```")
    else:
        print(f"WARNING: Could not find #{POST_CHANNEL_NAME} to post the summary.")


def run():
    run_discord_task(_dedupe)
