"""
Task: scrape_events
Scrapes upcoming events from https://teaconnect.glueup.com, works out which
Discord channel + role each one would be announced to based on its tags,
and keeps a single status message in #bot-spam up to date (edited in
place each run, not reposted — so daily runs don't spam the channel).

LIVE SINCE 2026-08-03: new events ARE now posted for real to the matching
announcement channel, with live role pings. Set ANNOUNCE_LIVE=false as an
env var to instantly revert to preview-only mode without a code change.

NEW EVENT DETECTION:
State (which events have been seen before, and the #bot-spam status
message ID) is persisted to data/bot_state.json, committed back to the
repo by the workflow after each run (GitHub Actions runs are stateless
otherwise). On each run:
    - Events not in the seen list are NEW: if ANNOUNCE_LIVE and the tag
      maps to a real channel, the announcement is posted there for real
      (with live pings), and #bot-spam notes it was posted.
    - Already-seen events are shown normally in #bot-spam, no action taken.
    - All currently-listed events are added to the seen list either way.

IMPORTANT: before going live, data/bot_state.json needs to already contain
every event that's been manually announced up to this point — otherwise
the first live run would re-announce things a human already posted. See
tasks/seed_state.py / the "Seed Event State" workflow for the one-off
step that handles this.

Requires:
    - DISCORD_BOT_TOKEN env var
    - DISCORD_GUILD_ID env var
    - Bot must have Send Messages / Attach Files / Mention Everyone in
      #bot-spam AND every announcement channel it might post to (reuses
      POST_CHANNEL_NAME from export_members for #bot-spam, override with
      env var EVENTS_POST_CHANNEL_NAME if you want a different channel)
    - playwright + its Chromium browser installed (see workflow file)
    - The workflow must check out with write access and commit
      data/bot_state.json back to the repo after the run (see
      .github/workflows/daily-events.yml)

NOTE ON SITE STRUCTURE:
This was built from a single rendered snapshot of the events page, not a
live inspection of the DOM (this environment can't reach glueup.com
directly). The parsing is written defensively around the *visible text*
pattern of each event card:

    <day> <month>              e.g. "11 Aug"
    <event name>                (linked)
    <date range>                e.g. "Aug 11, 2026 6:00 PM - Aug 11, 2026 8:00 PM"
    <location>                  e.g. "The Olde Mecklenburg Brewery, Charlotte, NC"
                                 (or "Webinar" for online events)
    <tag(s)>                    concatenated with no separator, e.g.
                                 "Asia Pacific DivisionSignature Event"

If the live site's markup doesn't match this, the first run will likely
produce zero or garbled events — check the #bot-spam output and send the
raw page text back for a fix.
"""

import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import discord

from tasks._discord_helper import run_discord_task

EVENTS_URL = "https://teaconnect.glueup.com/organization/7014/events/"
POST_CHANNEL_NAME = os.environ.get("EVENTS_POST_CHANNEL_NAME", "bot-spam")
STATE_PATH = os.path.join("data", "bot_state.json")
ANNOUNCE_LIVE = os.environ.get("ANNOUNCE_LIVE", "true").lower() == "true"
CREATE_DISCORD_EVENTS = os.environ.get("CREATE_DISCORD_EVENTS", "true").lower() == "true"

# Each event's own page states its timezone (per user confirmation), so
# that's what's used — this is only the FALLBACK for when detection
# fails on a given event page (couldn't reach it, wording didn't match
# anything recognized, etc.). Kept as a neutral default rather than
# guessing a region, and always flagged clearly when it's actually used
# so a wrong time never passes as verified.
DEFAULT_EVENT_TZ = timezone.utc
DEFAULT_EVENT_DURATION_HOURS = 3  # used when no end time could be parsed

# Display-name / abbreviation -> IANA timezone, used to interpret
# whatever wording an event's page uses for its timezone. Longest names
# are tried first (via sorted() at lookup time) so e.g. "Eastern Time
# (US & Canada)" isn't short-circuited by a shorter partial match.
TIMEZONE_ALIASES = {
    # US & Canada
    "eastern time (us & canada)": "America/New_York", "eastern time": "America/New_York",
    "edt": "America/New_York", "est": "America/New_York",
    "central time (us & canada)": "America/Chicago", "central time": "America/Chicago",
    "cdt": "America/Chicago",
    "mountain time (us & canada)": "America/Denver", "mountain time": "America/Denver",
    "mdt": "America/Denver", "mst": "America/Denver",
    "pacific time (us & canada)": "America/Los_Angeles", "pacific time": "America/Los_Angeles",
    "pdt": "America/Los_Angeles", "pst": "America/Los_Angeles",
    "arizona": "America/Phoenix", "alaska": "America/Anchorage", "hawaii": "Pacific/Honolulu",
    "toronto": "America/Toronto", "vancouver": "America/Vancouver",
    # South America
    "sao paulo": "America/Sao_Paulo", "brasilia": "America/Sao_Paulo",
    "bogota": "America/Bogota", "costa rica": "America/Costa_Rica",
    "puerto rico": "America/Puerto_Rico",
    # UK & Europe
    "london": "Europe/London", "gmt": "Europe/London", "bst": "Europe/London",
    "dublin": "Europe/Dublin",
    "central european time": "Europe/Berlin", "cet": "Europe/Berlin", "cest": "Europe/Berlin",
    "paris": "Europe/Paris", "berlin": "Europe/Berlin", "madrid": "Europe/Madrid",
    "amsterdam": "Europe/Amsterdam", "rome": "Europe/Rome", "brussels": "Europe/Brussels",
    "warsaw": "Europe/Warsaw", "vienna": "Europe/Vienna", "zurich": "Europe/Zurich",
    "stockholm": "Europe/Stockholm", "copenhagen": "Europe/Copenhagen",
    "lisbon": "Europe/Lisbon", "budapest": "Europe/Budapest",
    "eastern european time": "Europe/Athens", "eet": "Europe/Athens", "athens": "Europe/Athens",
    "istanbul": "Europe/Istanbul", "moscow": "Europe/Moscow",
    # Middle East
    "dubai": "Asia/Dubai", "gulf standard time": "Asia/Dubai", "gst": "Asia/Dubai",
    "riyadh": "Asia/Riyadh", "jerusalem": "Asia/Jerusalem", "tel aviv": "Asia/Jerusalem",
    # APAC
    "singapore": "Asia/Singapore", "sgt": "Asia/Singapore",
    "hong kong": "Asia/Hong_Kong", "hkt": "Asia/Hong_Kong",
    "tokyo": "Asia/Tokyo", "jst": "Asia/Tokyo",
    "seoul": "Asia/Seoul", "kst": "Asia/Seoul",
    "beijing": "Asia/Shanghai", "shanghai": "Asia/Shanghai",
    "taipei": "Asia/Taipei", "manila": "Asia/Manila",
    "bangkok": "Asia/Bangkok", "jakarta": "Asia/Jakarta",
    "kuala lumpur": "Asia/Kuala_Lumpur", "mumbai": "Asia/Kolkata",
    "kolkata": "Asia/Kolkata", "new delhi": "Asia/Kolkata",
    "sydney": "Australia/Sydney", "melbourne": "Australia/Melbourne",
    "brisbane": "Australia/Brisbane", "aest": "Australia/Sydney", "aedt": "Australia/Sydney",
    "perth": "Australia/Perth", "awst": "Australia/Perth",
    "auckland": "Pacific/Auckland", "nzst": "Pacific/Auckland", "nzdt": "Pacific/Auckland",
}


def parse_timezone_from_text(text):
    """Look for a timezone in a block of text: an explicit UTC/GMT offset
    first (unambiguous), then a named zone from TIMEZONE_ALIASES. Returns
    a tzinfo object, or None if nothing recognizable was found."""
    if not text:
        return None

    offset_match = re.search(r"\(?(?:GMT|UTC)\s*([+-])\s*(\d{1,2})(?::?(\d{2}))?\)?", text)
    if offset_match:
        sign, hh, mm = offset_match.group(1), int(offset_match.group(2)), int(offset_match.group(3) or 0)
        delta = timedelta(hours=hh, minutes=mm)
        return timezone(-delta if sign == "-" else delta)

    lowered = text.lower()
    for name in sorted(TIMEZONE_ALIASES, key=len, reverse=True):
        if name in lowered:
            try:
                return ZoneInfo(TIMEZONE_ALIASES[name])
            except Exception:
                continue
    return None


def detect_event_timezone(page_text):
    """Try to find the event's stated timezone on its own page.
    Returns (tzinfo_or_None, confidence) where confidence is
    'confirmed' (found next to an explicit 'Time Zone' label — high
    trust), 'low-confidence' (found via a loose full-page text search,
    no label — could coincidentally match something unrelated), or
    'not detected' (nothing found at all)."""
    label_match = re.search(r"time\s*zone[:\s]+([^\n]{1,60})", page_text, re.IGNORECASE)
    if label_match:
        tz = parse_timezone_from_text(label_match.group(1))
        if tz:
            return tz, "confirmed"

    tz = parse_timezone_from_text(page_text)
    if tz:
        return tz, "low-confidence"

    return None, "not detected"


# The full date+time pattern (requires AM/PM on at least the first date) —
# shared between the initial listing-card parse and the detail-page
# fallback below, so both look for dates the same way.
DATE_RANGE_RE = re.compile(
    r"([A-Z][a-z]{2} \d{1,2}, \d{4}.*?(?:AM|PM))(\s*-\s*[A-Z][a-z]{2} \d{1,2}, \d{4}.*?(?:AM|PM))?"
)
# A bare date with no time at all, e.g. "Sep 10, 2026" — used as a guard so
# a line like this is never mistaken for a location (the actual bug that
# produced "Location: Sep 10, 2026").
DATE_ONLY_RE = re.compile(r"^[A-Z][a-z]{2} \d{1,2}, \d{4}\b")


def looks_like_date(text):
    return bool(DATE_ONLY_RE.match((text or "").strip()))


def find_datetime_in_text(text):
    """Look for a full date+time (with AM/PM) anywhere in a block of
    text. Used as a fallback against the event's own detail page when
    the listing card only showed a bare date with no time — the detail
    page usually has the actual start/end time even when the card
    doesn't. Returns the matched string, or None."""
    match = DATE_RANGE_RE.search(text)
    return match.group(0).strip() if match else None


def find_location_in_text(text):
    """Look for a labeled location on the event's detail page (e.g.
    'Location: Warner Bros. World Abu Dhabi'). Returns the matched
    venue text, or None if nothing usable was found — never returns a
    date-shaped match even if one happens to follow a 'Location' label
    elsewhere on the page."""
    match = re.search(r"location[:\s]+([^\n]{1,120})", text, re.IGNORECASE)
    if match:
        candidate = match.group(1).strip()
        if candidate and not looks_like_date(candidate):
            return candidate
    return None

# Known tag vocabulary, longest-first so greedy matching against
# concatenated tag strings doesn't mis-split (e.g. so "Asia Pacific
# Division" isn't confused with a shorter partial match).
KNOWN_TAGS = sorted([
    "Eastern North America",
    "Western North America",
    "Asia Pacific Division",
    "Europe & Middle East Division",
    "Signature Event",
    "SATE NA",
    "Webinar",
], key=len, reverse=True)

# Tag -> (channel name, region role name)
TAG_ROUTING = {
    "Eastern North America": ("eastern-announcements", "Eastern NA"),
    "Western North America": ("western-announcements", "Western NA"),
    "Asia Pacific Division": ("apac-announcements", "APAC"),
    "Europe & Middle East Division": ("eme-announcements", "EME"),
}
SIGNATURE_TAG = "Signature Event"
SIGNATURE_CHANNEL = "announcements"

# Country/state sub-roles nested under each region, taken from the actual
# server role list. "USA" is deliberately excluded — it's a generic role
# that doesn't cleanly belong to one region (both Eastern NA and Western NA
# have USA states under them), so pinging it alongside a specific state
# would be redundant/ambiguous. Adjust here if that's wrong.
LOCATION_ROLES = {
    "Europe & Middle East Division": [
        "UK", "France", "Netherlands", "Spain", "Germany", "Norway",
        "Austria", "Poland", "Italy", "Ireland", "Switzerland", "Belgium",
        "Denmark", "Luxembourg", "Portugal", "Sweden", "Hungary", "UAE",
        "Israel", "Bulgaria", "Romania", "Saudi Arabia", "Ukraine",
        "Türkiye",
    ],
    "Asia Pacific Division": [
        "Australia", "Japan", "China", "New Zealand", "South Korea",
        "Hong Kong", "India", "Malaysia", "Thailand", "Macau",
        "Philippines", "Singapore", "Indonesia", "Taiwan",
    ],
    "Western North America": [
        "Alaska", "Arizona", "California", "Colorado", "Hawaii", "Idaho",
        "Kansas", "Montana", "Nebraska", "Nevada", "New Mexico",
        "North Dakota", "Oklahoma", "Oregon", "South Dakota", "Texas",
        "Utah", "Washington", "Wyoming",
    ],
    "Eastern North America": [
        "Canada", "Brazil", "Georgia", "Alabama", "Arkansas", "Connecticut",
        "Delaware", "DC", "Florida", "Illinois", "Indiana", "Iowa",
        "Kentucky", "Louisiana", "Maine", "Maryland", "Massachusetts",
        "Michigan", "Minnesota", "Mississippi", "Missouri",
        "New Hampshire", "Ohio", "Pennsylvania", "Rhode Island",
        "South Carolina", "Tennessee", "Vermont", "Virginia",
        "West Virginia", "Wisconsin", "New Jersey", "New York",
        "North Carolina", "Colombia", "Costa Rica", "Puerto Rico",
    ],
}

# Some roles are abbreviated (UK, UAE) but event location text tends to
# spell things out in full ("United Kingdom", "England"). Map those
# alternate phrasings to the actual role name here.
LOCATION_ALIASES = {
    "UK": ["United Kingdom", "England", "Scotland", "Wales", "Northern Ireland"],
    "UAE": ["United Arab Emirates"],
    "South Korea": ["Korea"],
    "Hong Kong": ["Hong Kong SAR"],
}


def find_sub_roles(region_tag, location_text):
    """Search an event's location text for a matching country/state role
    under the given region. Longest names (incl. aliases) are checked first
    so a more specific match (e.g. 'New York') wins over a shorter one."""
    candidates = LOCATION_ROLES.get(region_tag, [])

    # Build (role_name, phrase_to_search_for) pairs: the role's own name,
    # plus any aliases that apply to it.
    search_pairs = []
    for name in candidates:
        search_pairs.append((name, name))
        for alias in LOCATION_ALIASES.get(name, []):
            search_pairs.append((name, alias))

    search_pairs.sort(key=lambda pair: len(pair[1]), reverse=True)

    matches = []
    for role_name, phrase in search_pairs:
        if role_name in matches:
            continue
        pattern = r"\b" + re.escape(phrase) + r"\b"
        if re.search(pattern, location_text, re.IGNORECASE):
            matches.append(role_name)
    return matches[:2]  # cap at 2 in case of odd overlapping matches


def route_event(tags, where):
    """Given an event's tags and location text, return (channel, [role labels])."""
    if SIGNATURE_TAG in tags:
        return SIGNATURE_CHANNEL, ["@everyone"]
    for tag in tags:
        if tag in TAG_ROUTING:
            channel, region_role = TAG_ROUTING[tag]
            sub_roles = find_sub_roles(tag, where)
            roles = [f"@{region_role}"] + [f"@{r}" for r in sub_roles]
            return channel, roles
    return "UNMAPPED", ["no matching tag — needs manual routing"]


def resolve_role_mentions(guild, role_labels):
    """Convert role label strings (e.g. '@Eastern NA', '@everyone') into
    live Discord role mentions where a matching role exists. Falls back to
    the plain label text if no matching role is found (e.g. the
    'UNMAPPED' case), so it never crashes on an odd label."""
    mentions = []
    for label in role_labels:
        name = label.lstrip("@")
        if name == "everyone":
            mentions.append("@everyone")
            continue
        role = discord.utils.get(guild.roles, name=name)
        mentions.append(role.mention if role else label)
    return mentions


def get_region_prefix(tags):
    """Return the short region label to prefix a Discord Event name with,
    e.g. 'EME', 'APAC', 'Eastern NA', 'Western NA'. Falls back to the
    first remaining tag if no recognized region tag is present (e.g. an
    event tagged only 'SATE NA' with no region tag alongside it), or None
    if there's nothing usable. 'Webinar' and 'Signature Event' are
    excluded from the fallback since they're handled separately (as an
    [ONLINE] marker and a ⭐ marker respectively), not as regions."""
    for tag in tags:
        if tag in TAG_ROUTING:
            return TAG_ROUTING[tag][1]
    fallback_candidates = [
        t for t in tags if t not in ("Webinar", SIGNATURE_TAG, "No tags found")
    ]
    if fallback_candidates:
        return fallback_candidates[0]
    return None


def build_discord_event_name(event):
    """The event's Discord Scheduled Event name:
    - ⭐ prefix for Signature Events, instead of a '[Signature Event]'
      bracket (which would otherwise show up via the region fallback for
      events with no other identifying tag)
    - [Region] bracket, e.g. '[EME]', '[APAC]'
    - [ONLINE] marker for webinars
    in that order, e.g. '⭐ [APAC] SATE APAC 2026' or
    '[Western NA] [ONLINE] TEA Masters Spotlight'. Truncated to Discord's
    100-char hard limit on scheduled event names, prefix included."""
    tags = event["tags"]
    parts = []
    if SIGNATURE_TAG in tags:
        parts.append("⭐")
    region = get_region_prefix(tags)
    if region:
        parts.append(f"[{region}]")
    if "Webinar" in tags:
        parts.append("[ONLINE]")

    prefix = " ".join(parts)
    name = f"{prefix} {event['name']}" if prefix else event["name"]
    return name[:100]


def format_announcement(event, mentions, note=None):
    """The live announcement message format, shared with
    test_pings_next_event so previews and real posts always look
    identical. `note`, if given, is shown as a small banner above the
    announcement — used for the 'UPDATED' marker when editing a message
    for an event whose details changed.

    Date/time is shown in the EVENT'S OWN local timezone (as stated on
    its GlueUp page), e.g. "August 26, 2026 09:00 - 12:00 (PDT)" — not
    converted to the reader's local time. This matches how GlueUp itself
    displays it, and avoids a per-viewer auto-converted time potentially
    reading as more "confirmed" than it is. Falls back to the raw
    scraped text if the date/time couldn't be parsed.

    The location line is omitted entirely if no location was found on
    the site, rather than showing a "Location not found" placeholder."""
    banner = f"{note}\n\n" if note else ""
    when_line = _format_when_line(event)
    location_line = f"📍 Location: {event['where']}\n" if event.get("where") else ""
    return (
        f"{banner}**EVENT ANNOUNCEMENT:**\n\n"
        f"**{event['name']}**\n\n"
        f"{location_line}"
        f"🗓️ Date & Time: {when_line}\n\n"
        f"🔗 URL: {event['url']}\n\n"
        f"{' '.join(mentions)}"
    )


def _format_when_line(event):
    """Plain event-local date/time string, e.g.
    'August 26, 2026 09:00 - 12:00 (PDT)', using whichever timezone was
    detected for this event (falls back to UTC, clearly labeled as such
    via the abbreviation itself, if none could be detected). No
    confidence caveats here — those stay in the internal #bot-spam
    report (see _format_entry) rather than the public announcement."""
    start_dt, end_dt = parse_event_datetime(event["when"], event.get("timezone"))
    if start_dt is None:
        return f"{event['when']}  ⚠️ _(couldn't parse date/time — verify manually)_"

    tz_abbr = start_dt.tzname() or "UTC"
    date_part = start_dt.strftime("%B %-d, %Y")
    start_time = start_dt.strftime("%H:%M")
    end_time = end_dt.strftime("%H:%M")

    if end_dt.date() != start_dt.date():
        end_date_part = end_dt.strftime("%B %-d, %Y")
        return f"{date_part} {start_time} - {end_date_part} {end_time} ({tz_abbr})"
    return f"{date_part} {start_time} - {end_time} ({tz_abbr})"


def format_cancellation(event):
    """Message content an announcement gets edited to when its event is
    detected as cancelled (was previously posted, still shown as
    upcoming in our records, but has disappeared from the site)."""
    location_line = f"📍 Was at: {event['where']}\n" if event.get("where") else ""
    return (
        f"❌ **THIS EVENT HAS BEEN CANCELLED**\n\n"
        f"~~**{event['name']}**~~\n\n"
        f"{location_line}"
        f"🗓️ Was scheduled: {event['when']}\n\n"
        f"🔗 {event['url']}"
    )


def parse_event_datetime(when_text, tz=None):
    """Parse the scraped 'when' string (e.g. 'Aug 11, 2026 6:00 PM - Aug
    11, 2026 8:00 PM') into (start_dt, end_dt) timezone-aware datetimes,
    interpreted in the given tzinfo (the event's own detected timezone —
    see detect_event_timezone). Falls back to DEFAULT_EVENT_TZ (UTC) if
    tz is None. Returns (None, None) if the text can't be parsed at all.
    If only a start time is found, end_dt defaults to
    start + DEFAULT_EVENT_DURATION_HOURS (Discord requires an end time
    for external scheduled events)."""
    if not when_text or when_text == "Date not found":
        return None, None

    tzinfo_obj = tz or DEFAULT_EVENT_TZ
    fmt = "%b %d, %Y %I:%M %p"
    parts = [p.strip() for p in when_text.split(" - ")]

    try:
        start = datetime.strptime(parts[0], fmt).replace(tzinfo=tzinfo_obj)
    except ValueError:
        return None, None

    end = None
    if len(parts) > 1:
        try:
            end = datetime.strptime(parts[1], fmt).replace(tzinfo=tzinfo_obj)
        except ValueError:
            end = None

    if end is None or end <= start:
        end = start + timedelta(hours=DEFAULT_EVENT_DURATION_HOURS)

    return start, end


async def create_discord_event(guild, event, start_dt, end_dt):
    """Create a native Discord Guild Scheduled Event (EXTERNAL type, since
    these are all real-world venues, not voice/stage channels). Requires
    the bot to have the 'Manage Events' permission. Raises on failure —
    callers should catch and handle/report it themselves.

    Discord's API requires a non-empty location for EXTERNAL events even
    though the announcement/report display now omits the line entirely
    when nothing was found — 'Location TBA' is used here purely to
    satisfy that API requirement, not shown anywhere else."""
    return await guild.create_scheduled_event(
        name=build_discord_event_name(event),  # e.g. "[EME] TEA Mixer @ Venue"
        description=f"{event['url']}"[:1000],
        start_time=start_dt,
        end_time=end_dt,
        entity_type=discord.EntityType.external,
        privacy_level=discord.PrivacyLevel.guild_only,
        location=(event.get("where") or "Location TBA")[:100],
    )


async def find_discord_event(guild, discord_event_id):
    """Look up a Scheduled Event by ID among the guild's current events.
    Returns None if it's gone (deleted, or Discord auto-removed it after
    it completed) rather than raising."""
    if not discord_event_id:
        return None
    existing = await guild.fetch_scheduled_events()
    return discord.utils.get(existing, id=int(discord_event_id))


async def update_discord_event(guild, discord_event_id, event):
    """Update an existing Scheduled Event's name/location/times to match
    the event's current (changed) details. Skips updating start/end time
    if the new 'when' text can't be parsed, rather than risk clobbering a
    valid schedule with garbage. Returns True if something was actually
    updated, False if the event couldn't be found, raises on API errors."""
    scheduled = await find_discord_event(guild, discord_event_id)
    if scheduled is None:
        return False

    kwargs = {
        "name": build_discord_event_name(event),
        "location": (event.get("where") or "Location TBA")[:100],
        "description": f"{event['url']}"[:1000],
    }
    start_dt, end_dt = parse_event_datetime(event["when"], event.get("timezone"))
    if start_dt is not None:
        kwargs["start_time"] = start_dt
        kwargs["end_time"] = end_dt

    await scheduled.edit(**kwargs)
    return True


async def cancel_discord_event(guild, discord_event_id):
    """Mark a Scheduled Event as cancelled (rather than deleting it, so
    it stays visible with a 'Cancelled' label instead of just vanishing).
    Returns True if cancelled, False if it couldn't be found (already
    gone/completed), raises on other API errors."""
    scheduled = await find_discord_event(guild, discord_event_id)
    if scheduled is None:
        return False
    await scheduled.edit(status=discord.EventStatus.cancelled)
    return True


async def edit_tracked_message(guild, channel_id, message_id, new_content):
    """Edit a previously-posted announcement message, given its stored
    channel/message IDs. Returns True if edited, False if the channel or
    message can no longer be found (e.g. deleted), raises on other errors."""
    if not channel_id or not message_id:
        return False
    channel = guild.get_channel(int(channel_id))
    if channel is None:
        return False
    try:
        msg = await channel.fetch_message(int(message_id))
    except discord.NotFound:
        return False
    await msg.edit(content=new_content)
    return True


def split_known_tags(blob):
    """Split a concatenated tag string like 'Asia Pacific DivisionSignature Event'
    into ['Asia Pacific Division', 'Signature Event'] using the known vocabulary."""
    found = []
    remaining = blob
    changed = True
    while changed:
        changed = False
        for tag in KNOWN_TAGS:
            if tag in remaining:
                found.append(tag)
                remaining = remaining.replace(tag, "", 1)
                changed = True
    return found


async def scrape_events():
    """Render the events page with Playwright and parse out event blocks,
    then visit each individual event's page to detect its stated
    timezone (used to correctly interpret its date/time — the listing
    page's times are otherwise ambiguous about which zone they're in).
    Returns a list of dicts: name, url, when, where, tags, timezone
    (a tzinfo object, or None if it couldn't be detected),
    timezone_confidence ('confirmed' / 'low-confidence' / 'not detected').

    Uses Playwright's ASYNC API deliberately — this runs inside discord.py's
    asyncio event loop (via on_ready), and the sync API raises
    'Playwright Sync API inside the asyncio loop' if used here.
    """
    from playwright.async_api import async_playwright

    events = []

    async with async_playwright() as p:
        browser = await p.chromium.launch()
        page = await browser.new_page(user_agent=(
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
        ))
        await page.goto(EVENTS_URL, wait_until="networkidle", timeout=45000)
        await page.wait_for_timeout(2000)  # let any lazy content settle

        # Every event card links to /event/<slug>-<id>/
        links = await page.query_selector_all('a[href*="/event/"]')

        seen_urls = set()
        for link in links:
            href = await link.get_attribute("href")
            name = ((await link.inner_text()) or "").strip()
            if not href or not name or href in seen_urls:
                continue
            seen_urls.add(href)

            # Walk up to a container likely to include the date/location/tags
            # that sit near this link in the layout.
            container = link
            block_text = ""
            for _ in range(5):
                handle = await container.evaluate_handle(
                    "el => el.closest('div') && el.closest('div').parentElement"
                )
                if handle is None:
                    break
                container = handle.as_element()
                if container is None:
                    break
                block_text = (await container.inner_text()) or ""
                if len(block_text) > len(name) + 20:
                    break

            when = ""
            where = ""
            tags = []

            date_match = DATE_RANGE_RE.search(block_text)
            if date_match:
                when = date_match.group(0).strip()

            lines = [l.strip() for l in block_text.split("\n") if l.strip()]
            for line in lines:
                if line == name or (date_match and line in date_match.group(0)):
                    continue
                found_tags = split_known_tags(line)
                if found_tags and len(" ".join(found_tags)) >= len(line) - 3:
                    tags.extend(found_tags)
                elif not where and not date_match_in(line, date_match) and not looks_like_date(line):
                    where = line

            events.append({
                "name": name,
                "url": href if href.startswith("http") else f"https://teaconnect.glueup.com{href}",
                "when": when or "Date not found",
                "where": where,  # left empty if not found — display logic omits the line rather than showing a placeholder
                "tags": tags or ["No tags found"],
            })

        # Visit each event's own page to find its stated timezone, and to
        # recover date/time or location if the listing card didn't have
        # them (e.g. a card that only shows a bare date with no time —
        # the detail page usually has the actual times). Done as a
        # second pass (rather than while gathering the list above) to
        # keep the listing-parse logic simple and isolate failures — one
        # event's page failing to load doesn't affect the others.
        for ev in events:
            tz, confidence = None, "not detected"
            try:
                await page.goto(ev["url"], wait_until="networkidle", timeout=30000)
                page_text = await page.inner_text("body")
                tz, confidence = detect_event_timezone(page_text)

                if ev["when"] == "Date not found":
                    recovered_when = find_datetime_in_text(page_text)
                    if recovered_when:
                        ev["when"] = recovered_when
                        print(f"Recovered date/time for '{ev['name']}' from its "
                              f"detail page: {recovered_when}")

                if not ev["where"] or looks_like_date(ev["where"]):
                    recovered_where = find_location_in_text(page_text)
                    ev["where"] = recovered_where or ""  # blank if genuinely not found, never a guess
                    if recovered_where:
                        print(f"Recovered location for '{ev['name']}' from its "
                              f"detail page: {recovered_where}")
            except Exception as exc:
                print(f"Could not load event page for timezone/date/location "
                      f"detection ('{ev['name']}'): {exc}")
            ev["timezone"] = tz
            ev["timezone_confidence"] = confidence
            if confidence != "confirmed":
                print(f"Timezone for '{ev['name']}': {confidence} "
                      f"({tz if tz else 'falling back to ' + str(DEFAULT_EVENT_TZ)})")

        await browser.close()

    return events


def date_match_in(line, date_match):
    return bool(date_match) and line in date_match.group(0)


STATE_DEFAULTS = {
    "seen_event_urls": [],
    "status_message_ids": [],
    "discord_event_ids": {},
    "tracked_events": {},  # url -> {name, when, where, tags, channel_id,
                            #         message_id, discord_event_id, status}
                            # status: 'scheduled' | 'completed' | 'cancelled'
}


def load_state():
    """Load persisted state: which event URLs we've already seen, the
    message ID(s) of the current #bot-spam status message (so we edit it
    instead of posting a new one each run), a map of event URL -> Discord
    Scheduled Event ID, and full per-event tracking (tracked_events) used
    to detect changes/cancellations and find the original announcement
    message to edit."""
    if not os.path.exists(STATE_PATH):
        return {k: (v.copy() if hasattr(v, "copy") else v) for k, v in STATE_DEFAULTS.items()}
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        for key, default in STATE_DEFAULTS.items():
            data.setdefault(key, default.copy() if hasattr(default, "copy") else default)
        return data
    except (json.JSONDecodeError, OSError) as e:
        print(f"WARNING: Could not read {STATE_PATH} ({e}), starting fresh.")
        return {k: (v.copy() if hasattr(v, "copy") else v) for k, v in STATE_DEFAULTS.items()}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_PATH), exist_ok=True)
    with open(STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
    print(f"Saved state to {STATE_PATH} "
          f"({len(state['seen_event_urls'])} seen events, "
          f"{len(state['status_message_ids'])} status message(s), "
          f"{len(state['discord_event_ids'])} Discord events tracked, "
          f"{len(state['tracked_events'])} events fully tracked)")


def event_snapshot(e):
    """The subset of an event's fields we compare run-to-run to detect
    changes. Order-independent for tags (sorted) so a re-scrape that
    happens to list tags in a different order isn't seen as a change."""
    return {
        "name": e["name"],
        "when": e["when"],
        "where": e["where"],
        "tags": sorted(e["tags"]),
    }


def snapshot_changed(prior, e):
    return event_snapshot(e) != {
        "name": prior.get("name"),
        "when": prior.get("when"),
        "where": prior.get("where"),
        "tags": sorted(prior.get("tags", [])),
    }


def chunk_message(text, limit=1880):
    """Split text into Discord-safe chunks, breaking on blank lines. Limit
    is kept below 2000 to leave room for the ```code block``` fence added
    afterward (8 chars of overhead)."""
    chunks = []
    current = ""
    for block in text.split("\n\n"):
        if len(current) + len(block) + 2 > limit:
            if current:
                chunks.append(current)
            current = block
        else:
            current = f"{current}\n\n{block}" if current else block
    if current:
        chunks.append(current)
    return chunks or [text]


def wrap_code_block(text):
    return f"```\n{text}\n```"


async def sync_status_messages(channel, chunks, previous_ids):
    """Edit existing status message(s) in place where possible, create new
    ones only if there are more chunks than before, and delete any leftover
    messages if there are fewer. Returns the updated list of message IDs."""
    new_ids = []
    for i, chunk in enumerate(chunks):
        content = wrap_code_block(chunk)
        if i < len(previous_ids):
            try:
                msg = await channel.fetch_message(previous_ids[i])
                await msg.edit(content=content)
                new_ids.append(msg.id)
                continue
            except discord.NotFound:
                print(f"Status message {previous_ids[i]} no longer exists, "
                      f"sending a new one.")
        msg = await channel.send(content)
        new_ids.append(msg.id)

    # Clean up any extra old messages beyond what we need this run
    for leftover_id in previous_ids[len(chunks):]:
        try:
            msg = await channel.fetch_message(leftover_id)
            await msg.delete()
        except discord.NotFound:
            pass

    return new_ids


async def _check_events(client, guild):
    print(f"Scraping {EVENTS_URL} ...")
    events = await scrape_events()
    print(f"Found {len(events)} events")

    state = load_state()
    seen_urls = set(state["seen_event_urls"])
    discord_event_ids = state["discord_event_ids"]
    tracked = state["tracked_events"]
    current_urls = {e["url"] for e in events}

    new_lines, updated_lines, unchanged_lines = [], [], []
    posted_count = failed_count = unmapped_count = 0
    discord_event_created = discord_event_failed = 0
    discord_event_updated = discord_event_update_failed = 0
    posted_this_run = []  # [(event, channel_name)] for the standalone alert below

    for e in events:
        url = e["url"]
        channel_name, roles = route_event(e["tags"], e["where"])
        prior = tracked.get(url)
        is_new = url not in seen_urls
        status_line = f"      Route: #{channel_name}  |  Roles: {', '.join(roles)}"

        if prior is None and not is_new:
            # Legacy event: seen before this change-tracking feature existed.
            # We have no snapshot/message ID for it, so just baseline it
            # silently rather than treating it as new or as "changed".
            tracked[url] = {
                **event_snapshot(e), "url": url,
                "channel_id": None, "message_id": None,
                "discord_event_id": discord_event_ids.get(url),
                "status": "scheduled",
            }
            status_line = f"      Route: #{channel_name}  |  Roles: {', '.join(roles)}  (baselined — no prior snapshot)"
            unchanged_lines.append(_format_entry("   ", e, status_line))
            continue

        if prior is None and is_new:
            # Genuinely new event.
            if not ANNOUNCE_LIVE:
                status_line = f"      Would route to: #{channel_name}  |  Roles: {', '.join(roles)}"
                print(f"NEW EVENT (ANNOUNCE_LIVE=false): {e['name']} -> #{channel_name} {roles} — not posted.")
                tracked[url] = {**event_snapshot(e), "url": url, "channel_id": None,
                                 "message_id": None, "discord_event_id": None, "status": "scheduled"}
            elif channel_name == "UNMAPPED":
                unmapped_count += 1
                status_line = "      NEEDS MANUAL POSTING — no tag matched a channel"
                print(f"NEW EVENT, UNMAPPED: {e['name']} — needs manual posting.")
                tracked[url] = {**event_snapshot(e), "url": url, "channel_id": None,
                                 "message_id": None, "discord_event_id": None, "status": "scheduled"}
            else:
                target_channel = discord.utils.get(guild.text_channels, name=channel_name)
                if target_channel is None:
                    failed_count += 1
                    status_line = f"      POST FAILED — could not find #{channel_name}"
                    print(f"NEW EVENT, channel not found: {e['name']} -> #{channel_name}")
                    tracked[url] = {**event_snapshot(e), "url": url, "channel_id": None,
                                     "message_id": None, "discord_event_id": None, "status": "scheduled"}
                else:
                    try:
                        mentions = resolve_role_mentions(guild, roles)
                        allowed = discord.AllowedMentions(everyone=True, roles=True, users=False)
                        sent_msg = await target_channel.send(
                            format_announcement(e, mentions), allowed_mentions=allowed
                        )
                        posted_count += 1
                        posted_this_run.append((e, channel_name))
                        status_line = f"      POSTED to #{channel_name}  |  Pinged: {' '.join(mentions)}"
                        print(f"NEW EVENT posted: {e['name']} -> #{channel_name} {mentions}")

                        discord_event_id = None
                        if CREATE_DISCORD_EVENTS:
                            start_dt, end_dt = parse_event_datetime(e["when"], e.get("timezone"))
                            if start_dt is None:
                                status_line += "\n      Discord Event: skipped (couldn't parse date/time)"
                            else:
                                try:
                                    scheduled = await create_discord_event(guild, e, start_dt, end_dt)
                                    discord_event_id = scheduled.id
                                    discord_event_ids[url] = scheduled.id
                                    discord_event_created += 1
                                    status_line += "\n      Discord Event: created"
                                except Exception as sched_exc:
                                    discord_event_failed += 1
                                    status_line += f"\n      Discord Event: FAILED — {sched_exc}"

                        tracked[url] = {
                            **event_snapshot(e), "url": url,
                            "channel_id": target_channel.id, "message_id": sent_msg.id,
                            "discord_event_id": discord_event_id, "status": "scheduled",
                        }
                    except Exception as post_exc:
                        failed_count += 1
                        status_line = f"      POST FAILED to #{channel_name}: {post_exc}"
                        print(f"NEW EVENT post FAILED: {e['name']} -> #{channel_name}: {post_exc}")
                        tracked[url] = {**event_snapshot(e), "url": url, "channel_id": None,
                                         "message_id": None, "discord_event_id": None, "status": "scheduled"}
            new_lines.append(_format_entry("NEW", e, status_line))
            continue

        # prior is not None: an event we're already tracking with a full snapshot
        if snapshot_changed(prior, e):
            status_line = f"      Route: #{channel_name}  |  Roles: {', '.join(roles)}"
            if ANNOUNCE_LIVE:
                mentions = resolve_role_mentions(guild, roles)
                allowed = discord.AllowedMentions(everyone=True, roles=True, users=False)

                edited = False
                try:
                    edited = await edit_tracked_message(
                        guild, prior.get("channel_id"), prior.get("message_id"),
                        format_announcement(e, mentions, note="🔄 **UPDATED**"),
                    )
                except Exception as edit_exc:
                    status_line += f"\n      Message: edit FAILED — {edit_exc}"

                if edited:
                    status_line += "\n      Message: updated"
                elif channel_name == "UNMAPPED":
                    status_line += "\n      Message: none existed — still UNMAPPED, needs manual posting"
                else:
                    # No original message to edit (never posted, e.g. was
                    # UNMAPPED before, ANNOUNCE_LIVE was off at the time,
                    # the original post failed, or it's a legacy event) —
                    # post it fresh now rather than silently dropping the
                    # update.
                    target_channel = discord.utils.get(guild.text_channels, name=channel_name)
                    if target_channel is None:
                        status_line += f"\n      Message: none existed, and #{channel_name} not found"
                    else:
                        try:
                            sent_msg = await target_channel.send(
                                format_announcement(e, mentions), allowed_mentions=allowed
                            )
                            prior["channel_id"] = target_channel.id
                            prior["message_id"] = sent_msg.id
                            posted_count += 1
                            posted_this_run.append((e, channel_name))
                            status_line += (f"\n      Message: none existed — posted fresh to "
                                             f"#{channel_name}  |  Pinged: {' '.join(mentions)}")
                        except Exception as post_exc:
                            status_line += f"\n      Message: fresh post FAILED — {post_exc}"

                if CREATE_DISCORD_EVENTS:
                    updated_ok = False
                    if prior.get("discord_event_id"):
                        try:
                            updated_ok = await update_discord_event(guild, prior["discord_event_id"], e)
                            if updated_ok:
                                discord_event_updated += 1
                                status_line += "\n      Discord Event: updated"
                            else:
                                status_line += "\n      Discord Event: not found (likely completed) — creating fresh"
                                prior["discord_event_id"] = None
                        except Exception as sched_exc:
                            discord_event_update_failed += 1
                            status_line += f"\n      Discord Event: update FAILED — {sched_exc}"

                    if not prior.get("discord_event_id") and not updated_ok:
                        start_dt, end_dt = parse_event_datetime(e["when"], e.get("timezone"))
                        if start_dt is None:
                            status_line += "\n      Discord Event: skipped (couldn't parse date/time)"
                        else:
                            try:
                                scheduled = await create_discord_event(guild, e, start_dt, end_dt)
                                prior["discord_event_id"] = scheduled.id
                                discord_event_ids[url] = scheduled.id
                                discord_event_created += 1
                                status_line += "\n      Discord Event: none existed — created fresh"
                            except Exception as sched_exc:
                                discord_event_failed += 1
                                status_line += f"\n      Discord Event: creation FAILED — {sched_exc}"
            else:
                status_line += "\n      (ANNOUNCE_LIVE off — change detected but not applied live)"

            tracked[url] = {**prior, **event_snapshot(e)}
            updated_lines.append(_format_entry("UPD", e, status_line))
        else:
            unchanged_lines.append(_format_entry("   ", e, status_line))

    # Cancellation pass: anything we're still tracking as 'scheduled' that
    # has disappeared from the current listing. Distinguish "cancelled"
    # from "just already happened and rolled off the upcoming list" using
    # the event's own stored start time vs now. Note: the stored snapshot
    # (event_snapshot) doesn't persist the detected timezone, so this
    # uses the UTC fallback — a coarse approximation that's only used to
    # decide which side of "now" the date falls on, not to schedule
    # anything, so a few hours of slack around the exact boundary is an
    # acceptable trade-off rather than persisting tzinfo objects to JSON.
    cancelled_lines = []
    now = datetime.now(DEFAULT_EVENT_TZ)
    for url, info in tracked.items():
        if info.get("status") != "scheduled" or url in current_urls:
            continue
        start_dt, _ = parse_event_datetime(info.get("when", ""))
        if start_dt is not None and start_dt < now:
            info["status"] = "completed"
            continue

        status_line = "      Detected as CANCELLED (was upcoming, disappeared from the site)"
        if ANNOUNCE_LIVE:
            fake_event = {"name": info["name"], "where": info["where"], "when": info["when"], "url": url}
            try:
                edited = await edit_tracked_message(
                    guild, info.get("channel_id"), info.get("message_id"),
                    format_cancellation(fake_event),
                )
                status_line += "\n      Message: updated to show cancellation" if edited else \
                    "\n      Message: could not find original message to edit"
            except Exception as edit_exc:
                status_line += f"\n      Message: edit FAILED — {edit_exc}"

            if info.get("discord_event_id"):
                try:
                    did_cancel = await cancel_discord_event(guild, info["discord_event_id"])
                    status_line += "\n      Discord Event: cancelled" if did_cancel else \
                        "\n      Discord Event: not found (may have already completed)"
                except Exception as sched_exc:
                    status_line += f"\n      Discord Event: cancel FAILED — {sched_exc}"
        else:
            status_line += "\n      (ANNOUNCE_LIVE off — not applied live)"

        info["status"] = "cancelled"
        cancelled_lines.append(f"{'[CANCELLED] '}{info['name']}\n{status_line}")

    state["discord_event_ids"] = discord_event_ids
    state["tracked_events"] = tracked

    now_ts = int(time.time())
    header = f"Daily Event Check — checked <t:{now_ts}:f> — {len(events)} upcoming event(s)"
    summary_bits = []
    if new_lines:
        bit = f"{len(new_lines)} new"
        if ANNOUNCE_LIVE:
            bit += f" ({posted_count} posted"
            if unmapped_count:
                bit += f", {unmapped_count} unmapped"
            if failed_count:
                bit += f", {failed_count} failed"
            bit += ")"
        summary_bits.append(bit)
    if updated_lines:
        bit = f"{len(updated_lines)} updated"
        if discord_event_updated or discord_event_update_failed:
            bit += f" ({discord_event_updated} Discord Event(s) updated"
            if discord_event_update_failed:
                bit += f", {discord_event_update_failed} failed"
            bit += ")"
        summary_bits.append(bit)
    if cancelled_lines:
        summary_bits.append(f"{len(cancelled_lines)} cancelled")
    if summary_bits:
        header += "  |  " + "  |  ".join(summary_bits)
    header += "\n"
    if not ANNOUNCE_LIVE:
        header += "(ANNOUNCE_LIVE is off — preview only, nothing posted/edited live.)\n"

    all_sections = new_lines + updated_lines + cancelled_lines + unchanged_lines
    if not events and not cancelled_lines:
        message = (f"{header}\nNo events found — either there genuinely "
                    f"aren't any upcoming, or the scraper needs fixing "
                    f"(site markup may have changed).")
    else:
        message = header + "\n" + "\n\n".join(all_sections)

    # Always leave a copy in output/, both as a fallback and so the
    # artifact-upload step has something to pick up.
    os.makedirs("output", exist_ok=True)
    report_path = os.path.join("output", "events_report.txt")
    with open(report_path, "w", encoding="utf-8") as f:
        f.write(message)
    print(f"Saved report to {report_path}")

    channel = discord.utils.get(guild.text_channels, name=POST_CHANNEL_NAME)
    if channel is None:
        print(f"WARNING: Could not find #{POST_CHANNEL_NAME}. "
              f"State will still be updated, but no message was posted/edited.")
    else:
        chunks = chunk_message(message)
        new_ids = await sync_status_messages(
            channel, chunks, state["status_message_ids"]
        )
        state["status_message_ids"] = new_ids
        print(f"Updated status message(s) in #{POST_CHANNEL_NAME} "
              f"({len(chunks)} message(s))")

        # Standalone alert, separate from the rolling status message above —
        # only sent when something was actually posted, so quiet days stay
        # quiet. Gives a fresh, chronological record in the channel that a
        # new event went out, plus the current full list for context.
        if posted_this_run:
            alert_lines = [f"✅ Posted {len(posted_this_run)} new event(s):"]
            for posted_event, posted_channel in posted_this_run:
                alert_lines.append(f"  • {posted_event['name']} -> #{posted_channel}")
            alert_lines.append("")
            alert_lines.append(f"Current event list ({len(events)} upcoming):")
            for i, ev in enumerate(events, start=1):
                alert_lines.append(f"  {i}. {ev['name']} — {ev['when']}")
            alert_text = "\n".join(alert_lines)
            for chunk in chunk_message(alert_text):
                await channel.send(f"```\n{chunk}\n```")
            print(f"Sent standalone new-event alert for {len(posted_this_run)} event(s)")

    # Mark every currently-listed event as seen, regardless of whether it
    # was new this run, so it won't be flagged again tomorrow.
    state["seen_event_urls"] = sorted(set(state["seen_event_urls"]) | current_urls)
    save_state(state)


def _format_entry(marker, e, status_line):
    tz = e.get("timezone")
    confidence = e.get("timezone_confidence", "not detected")
    if tz is not None:
        tz_display = getattr(tz, "key", None) or str(tz)  # ZoneInfo has .key, fixed offsets don't
        tz_line = f"      Tz:    {tz_display} ({confidence})"
    else:
        tz_line = f"      Tz:    not detected — using UTC, please verify"
    where_line = f"      Where: {e['where']}\n" if e.get("where") else ""
    return (
        f"[{marker}] {e['name']}\n"
        f"      When:  {e['when']}\n"
        f"{tz_line}\n"
        f"{where_line}"
        f"      Tags:  {', '.join(e['tags'])}\n"
        f"{status_line}\n"
        f"      URL:   {e['url']}"
    )


def run():
    run_discord_task(_check_events)
