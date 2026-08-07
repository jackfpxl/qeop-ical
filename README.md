# TEA NextGen Bot

An on-demand task runner, triggered manually from the GitHub Actions tab —
no server to host or maintain. Each capability lives in its own file under
`tasks/`, and you pick which one to run from a dropdown when you trigger
the workflow.

## Structure

```
tea-nextgen-bot/
├── main.py                  # dispatcher — looks up and runs the chosen task
├── requirements.txt
├── tasks/
│   ├── _discord_helper.py       # shared helper for tasks that talk to Discord
│   ├── dedupe_discord_events.py # one-off: cancel duplicate Scheduled Events
│   ├── export_members.py        # export server members to CSV
│   ├── scrape_events.py         # scrape teaconnect.glueup.com, post NEW events live
│   ├── seed_state.py            # one-off: mark current events as already-seen
│   ├── sync_discord_events.py   # backfill: create any missing Discord Scheduled Events
│   └── test_pings_next_event.py # send a live-ping test announcement for the next event
├── data/
│   └── bot_state.json       # persisted seen-events list, status message ID,
│                             # and event URL -> Discord Scheduled Event ID map
│                             # (committed back to the repo by the workflow
│                             # after each run — don't edit by hand)
└── .github/workflows/
    ├── run.yml                   # manual workflow with a "task" dropdown
    ├── daily-events.yml          # scheduled daily run of scrape_events (2pm GMT)
    ├── test-pings.yml            # "Test Pings with next event" — manual only
    ├── seed-state.yml            # "Seed Event State" — run once before going live
    ├── sync-discord-events.yml   # "Sync Discord Events (backfill missing)" — manual
    └── dedupe-discord-events.yml # "Dedupe Discord Events (cancel clones)" — manual
```

**Concurrency note:** every workflow that touches `data/bot_state.json`
shares a GitHub Actions `concurrency` group, so two runs can never execute
at the same time and both independently decide "this doesn't exist yet"
— which is exactly how duplicate Discord Scheduled Events got created
before this was in place. Overlapping triggers now queue instead of
racing.

## Event tag → channel routing (scrape_events)

Defined in `tasks/scrape_events.py`:

| Tag | Channel | Region role |
|---|---|---|
| Eastern North America | #eastern-announcements | Eastern NA |
| Western North America | #western-announcements | Western NA |
| Asia Pacific Division | #apac-announcements | APAC |
| Europe & Middle East Division | #eme-announcements | EME |
| Signature Event (overrides all) | #announcements | @everyone |

**Sub-role matching:** for non-signature events, the event's location text
is also checked against that region's country/state sub-roles (the actual
nested roles in the server, e.g. `Florida`, `UK`, `Australia`). If a match
is found, both roles are included — e.g. an Orlando event tagged Eastern
North America produces `@Eastern NA, @Florida`. The `LOCATION_ROLES` dict
holds the full list per region, and `LOCATION_ALIASES` maps spelled-out
names to abbreviated roles (e.g. "United Kingdom" → `UK`). The generic
`USA` role is deliberately excluded from matching since it doesn't map
cleanly to one region.

**How the daily check behaves:**
- Runs once a day (and on manual trigger), scrapes the current event list.
- Compares against `data/bot_state.json`'s stored snapshot of each event
  (name, date/time, location, tags). Three outcomes per event:
  - **New** — not seen before → posted live, Discord Event created.
  - **Updated** — seen before, but something changed → the *original*
    announcement message is edited in place (with a 🔄 UPDATED banner),
    and the matching Discord Event is updated to match. This does NOT
    re-ping anyone — Discord message edits never trigger new
    notifications, which is the desired behavior here.
  - **Unchanged** — no action, just listed for reference.
- Posts (or, after the first run, *edits*) a single status message in
  #bot-spam listing every upcoming event and what happened to it — so
  daily runs update in place instead of spamming new messages.
- Every event currently listed gets added to the seen list.

**Cancellation detection:** if a previously-posted, still-upcoming event
disappears from the site entirely, it's treated as cancelled: its
announcement message gets edited to show a ❌ CANCELLED notice (strikethrough
name, kept for reference), and its Discord Event is marked cancelled (not
deleted, so it still shows in the server's Events tab with that status).

**Important distinction handled automatically:** an event disappearing
from the "upcoming" list doesn't always mean cancelled — it might just
mean the event already happened and rolled off normally, which happens
every single day for past events. The bot checks the event's own stored
date: if it's in the past, that's treated as a normal completion (silent,
no action) rather than a cancellation. Only events that were still
*upcoming* per our records but vanished get flagged as cancelled.

**Self-healing for gaps in message tracking:** if an event's details
change but there's no original message to edit — e.g. it was `UNMAPPED`
before, `ANNOUNCE_LIVE` was off when it was first seen, the original post
failed, or it's a legacy event from before this feature existed — the
bot posts a fresh announcement instead of silently doing nothing. Same
for the Discord Event side: if there's no tracked Discord Event (or it's
gone), a new one gets created rather than the update being dropped.

**⚠️ LIVE since 2026-08-03.** New events (not previously seen) are now
posted for real to their matching announcement channel, with live role
pings, per the routing table above. Already-seen events, and events whose
tags don't map to a channel ("UNMAPPED"), are never posted live — unmapped
ones are flagged in #bot-spam as needing manual posting instead. Set the
`ANNOUNCE_LIVE=false` repository/environment variable to instantly revert
to preview-only mode without touching the code.

**Before going live for the first time**, `data/bot_state.json` needs to
already contain every event that was manually announced up to that point
— otherwise the first live run would re-announce (and re-ping) things a
human already posted. That's what `seed_state` / the "Seed Event State"
workflow is for (see below) — run it once, then the daily schedule only
ever announces genuinely new events from that point forward.

## Timezones

Every event's own page states its timezone, so `scrape_events()` visits
each event's page individually (after gathering the main list) to detect
it, rather than assuming a single global timezone for every event —
which was the actual bug behind announcement/Discord Event times showing
wrong for readers outside the event's own timezone (e.g. a Charlotte, NC
event's 6pm Eastern was being stored as if it were 6pm UTC, showing as
7pm in London instead of the correct ~11pm).

**Detection has three confidence levels**, shown in the internal
#bot-spam report (`Tz:` line per event):
- **confirmed** — found next to an explicit "Time Zone:" label
- **low-confidence** — found via a looser full-page text search, no
  label (could coincidentally match something unrelated)
- **not detected** — nothing recognized; falls back to UTC

**Announcements show the event's own local time**, exactly as GlueUp
itself displays it — e.g. `August 26, 2026 09:00 - 12:00 (PDT)` — not
converted to the reader's local time, and without any confidence caveats
cluttering the public message (those stay in the internal #bot-spam
report only). This intentionally replaced an earlier version that used
Discord's auto-converting `<t:...>` timestamp with a "please verify"
note — the current approach matches the source of truth (GlueUp) more
directly. A one-off cleanup task, `fix_announcement_timezones` (Actions
tab → "Fix Announcement Timezones (one-off cleanup)"), reformats any
already-posted announcements from the old style to the new one — fully
regenerated for events still currently listed, or a targeted removal of
just the old wording for events no longer listed (nothing fresh to
reformat with).

**This is honestly untested against the live site** (this environment
can't reach it to check exact wording) — the parser is built defensively
around common phrasings (`TIMEZONE_ALIASES` in `scrape_events.py`), but
the first real run is a validation step. If timezones come back "not
detected" for events that should be confirmable, the report/logs show
what was attempted — send that back for a parser fix.

`sync_discord_events` also corrects the schedule (not just the name) on
already-existing Discord Events if the freshly-detected timezone produces
a different time than what's currently stored — covers events created
before per-event timezone detection existed.

## Discord Scheduled Events

Whenever a new event is posted live (see above), the bot also creates a
native **Discord Scheduled Event** (the ones in the server's Events tab)
for it — same name, location, and URL in the description. This requires
the bot to have the **"Manage Events"** permission.

**⚠️ Timezone caveat:** the site doesn't indicate which timezone each
event's displayed time is in, and this can't be inspected directly from
here. Scraped times are currently treated as **UTC** by default
(`DEFAULT_EVENT_TZ` in `tasks/scrape_events.py`). If Scheduled Events show
up off by a few hours once live, this is the first thing to fix — swap in
a fixed offset that matches what the site actually uses, or extend the
parser to read a timezone if the site provides one somewhere per event.

If an event's date/time can't be parsed at all (a small number won't be —
e.g. one seen during testing was formatted as "Oct 9, 2026 - Oct 9, 2026
10:00 PM" with no start time), the announcement still posts normally but
the Discord Event is skipped, noted as such in the #bot-spam report, and
would need to be created manually.

Set `CREATE_DISCORD_EVENTS=false` to turn this off without touching code.

## Dedupe Discord Events (dedupe_discord_events)

A one-off cleanup workflow (Actions tab → "Dedupe Discord Events (cancel
clones)" → Run workflow) for fixing duplicate Scheduled Events that
already exist. Groups events by **date + location** (not name — two
duplicates of the same real event can easily have different names if the
bot's naming format changed between when each was created, which is
exactly how this happened in practice). For each duplicate group, it
tries to match a duplicate's stored URL (kept in its description) against
a currently-listed event, keeps/renames that one to the current correct
name format, and cancels the rest. If no match is found (event already
happened), it falls back to keeping the most recently created copy.
Also repairs `data/bot_state.json` so anything pointing at a cancelled
duplicate gets repointed at the survivor. Posts a summary to #bot-spam.
Safe to run any time — it's a no-op if there are no duplicates.

## Sync Discord Events (sync_discord_events)

A manually-triggered backfill/audit workflow (Actions tab → "Sync Discord
Events (backfill missing)" → Run workflow) that checks every currently
listed event against the guild's actual Scheduled Events and creates any
that are missing — e.g. events announced before this feature existed, a
creation that failed silently, or one a human deleted afterward. Posts a
summary to #bot-spam. Safe to run any time; it never removes or duplicates
existing Scheduled Events, only fills gaps.

## Test Pings with next event (test_pings_next_event)

A separate, manually-triggered workflow (Actions tab → "Test Pings with
next event" → Run workflow) that sends a nicely formatted announcement
preview for the **next occurring event** to #bot-spam:

```
EVENT ANNOUNCEMENT:

Event Name

📍 Location: ...
🗓️ Date & Time: ...

🔗 URL: ...

@Eastern NA @North Carolina
```

**⚠️ Unlike scrape_events, this one sends REAL live pings.** The roles
mentioned actually notify anyone who has that role (or everyone, for a
Signature Event) and can see #bot-spam — it's not just text. It still only
posts to #bot-spam, not the real announcement channels, but the pings
themselves are live. Use it to check pings look/feel right before real
auto-posting is enabled.

## Seed Event State (seed_state)

A one-off safety workflow (Actions tab → "Seed Event State (run once
before going live)" → Run workflow) that scrapes the current event list
and marks every one of them as already-seen in `data/bot_state.json` —
without posting anything live, regardless of `ANNOUNCE_LIVE`. Run this
once before the daily schedule's first live run, so events that were
already announced manually don't get re-announced. Safe to run again
later too; it only ever adds to the seen list, never removes.

## One-time setup

1. Push this repo to GitHub as a **private** repository.
2. Add repository secrets (Settings → Secrets and variables → Actions):
   - `DISCORD_BOT_TOKEN` — your bot's token
   - `DISCORD_GUILD_ID` — the server ID the bot operates on
3. Make sure your bot has "Server Members Intent" enabled in the
   [Discord Developer Portal](https://discord.com/developers/applications)
   (only needed for tasks that read the member list).

## Running a task

Go to the **Actions** tab → **TEA NextGen Bot** workflow → **Run workflow**
→ pick a task from the dropdown → **Run workflow**.

When it finishes, download the result from the **Artifacts** section at
the bottom of the run page.

## Adding a new task

1. Create `tasks/your_task_name.py` with a `run()` function that does the
   work. Write any output files into the `output/` folder — whatever's in
   there gets uploaded as the artifact automatically.
2. If it needs Discord, use the shared helper:
   ```python
   from tasks._discord_helper import run_discord_task

   async def _do_work(client, guild):
       ...

   def run():
       run_discord_task(_do_work)
   ```
   If it doesn't need Discord (e.g. scraping a website for event data),
   just write plain Python — no Discord imports needed at all.
3. Add the task name to the `options:` list in
   `.github/workflows/run.yml`.
4. Add any new dependencies to `requirements.txt`.

`requests` and `beautifulsoup4` are already included for future
website-scraping tasks.
