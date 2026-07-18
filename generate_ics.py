#!/usr/bin/env python3
"""
Queen Elizabeth Olympic Park / London Stadium traffic-impact calendar generator.

Sources:
  - london-stadium.com                  (upcoming events)
  - queenelizabetholympicpark.co.uk     (upcoming events)
  - The Gazette (thegazette.co.uk)      (Newham Council road/traffic notices,
                                          official UK public record API -
                                          covers both past and upcoming
                                          closures/restrictions)

Because the two venue websites only ever list what's still upcoming, this
script keeps its own rolling history: every event it has ever scraped is
stored in docs/history_archive.json and re-merged on every run. That's what
lets the calendar hold a genuine 6-months-back view even though the source
sites themselves don't publish one. The Newham/Gazette source is different:
it's a real public record, so its historical notices are genuinely backfilled
from day one, not just accumulated going forward.

Run manually:
    pip install -r requirements.txt
    python generate_ics.py

Output:
    docs/qeop-traffic.ics       <- subscribe to this
    docs/history_archive.json   <- internal state, do not edit by hand

NOTE ON ROBUSTNESS
-------------------
The two venue sites are ordinary marketing pages, not APIs, so this parses
their rendered text rather than pinning to specific CSS class names. If a
run finds nothing new, previously archived events are still published -
the calendar never goes blank because of a temporary site change.
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import time
from dataclasses import dataclass, field, asdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUTPUT_DIR = Path(__file__).parent / "docs"
OUTPUT_FILE = OUTPUT_DIR / "qeop-traffic.ics"
ARCHIVE_FILE = OUTPUT_DIR / "history_archive.json"

HISTORY_DAYS = 183  # ~6 months back
HORIZON_DAYS = 183  # ~6 months forward

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "QEOP-TrafficCal/1.1 (personal, non-commercial event calendar)"
    )
}

MONTH_RE_PART = (
    "January|February|March|April|May|June|July|August|"
    "September|October|November|December"
)
FULL_DATE_RE = re.compile(rf"\b(\d{{1,2}})\s+({MONTH_RE_PART})\s+(\d{{4}})\b")
TIME_RE = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:AM|PM))\b", re.IGNORECASE)
DATE_RANGE_RE = re.compile(r"(\d{2})/(\d{2})/(\d{4})\s*-\s*(\d{2})/(\d{2})/(\d{4})")
SINGLE_SLASH_DATE_RE = re.compile(r"\b(\d{2})/(\d{2})/(\d{4})\b")

# Matches phrases like "from Monday 24th February 2025 until Sunday 9th
# March 2025" that appear in Gazette traffic order text.
PROSE_DATE_RANGE_RE = re.compile(
    rf"from\s+\w+\s+(\d{{1,2}})\w{{0,2}}\s+({MONTH_RE_PART})\s+(\d{{4}})\s+"
    rf"(?:until|to)\s+\w+\s+(\d{{1,2}})\w{{0,2}}\s+({MONTH_RE_PART})\s+(\d{{4}})",
    re.IGNORECASE,
)

SKIP_LINE_VALUES = {
    "book now", "more info", "read more", "sign up now", "see all",
    "getting here", "explore the park", "what's on", "plan your visit",
}

SEVERITY_DOT = {"LOW": "\U0001F7E2", "MEDIUM": "\U0001F7E1", "HIGH": "\U0001F534"}


def fetch_text_lines(url: str) -> tuple[list[str], BeautifulSoup]:
    resp = requests.get(url, headers=HEADERS, timeout=30)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "noscript"]):
        tag.decompose()
    raw_lines = soup.get_text("\n").split("\n")
    lines = [l.strip() for l in raw_lines if l.strip()]
    return lines, soup


def build_link_lookup(soup: BeautifulSoup, base_url: str) -> dict[str, str]:
    """Map visible anchor text -> absolute href, for later title lookup."""
    lookup: dict[str, str] = {}
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if not text:
            continue
        href = a["href"]
        if not href.startswith("http"):
            if href.startswith("/"):
                from urllib.parse import urlsplit
                parts = urlsplit(base_url)
                href = f"{parts.scheme}://{parts.netloc}{href}"
            else:
                continue
        lookup[text] = href
    return lookup


@dataclass
class ParkEvent:
    title: str
    start: date
    end: date  # inclusive
    source: str
    location: str = ""
    category: str = ""
    event_time: str = ""  # human readable, e.g. "11:00 AM"
    url: str = ""
    restrictions: list[str] = field(default_factory=list)

    def uid(self) -> str:
        raw = f"{self.source}|{self.title}|{self.start.isoformat()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest() + "@qeop-traffic-cal"

    def to_record(self) -> dict:
        d = asdict(self)
        d["start"] = self.start.isoformat()
        d["end"] = self.end.isoformat()
        d["uid"] = self.uid()
        return d

    @staticmethod
    def from_record(rec: dict) -> "ParkEvent":
        return ParkEvent(
            title=rec["title"],
            start=date.fromisoformat(rec["start"]),
            end=date.fromisoformat(rec["end"]),
            source=rec["source"],
            location=rec.get("location", ""),
            category=rec.get("category", ""),
            event_time=rec.get("event_time", ""),
            url=rec.get("url", ""),
            restrictions=rec.get("restrictions", []),
        )


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------

HIGH_KEYWORDS = [
    "concert", "festival", "west ham", "final", "nfl", "athletics meet",
    "world tour", "attro", "road closure", "marathon", "boxing",
    "world cup", "international", "cup final", "wing fest", "m72",
    "prohibition of traffic", "road closed",
]
MEDIUM_KEYWORDS = [
    "community", "family", "run", "race", "tournament", "match", "fixture",
    "sports", "sport", "circus live", "waiting and loading", "parking",
    "temporary traffic", "diversion",
]


def classify_severity(title: str, category: str, notes: str) -> tuple[str, str]:
    blob = f"{title} {category} {notes}".lower()
    hits_high = sorted({k for k in HIGH_KEYWORDS if k in blob})
    hits_medium = sorted({k for k in MEDIUM_KEYWORDS if k in blob})

    if hits_high:
        return "HIGH", ", ".join(hits_high)
    if hits_medium:
        return "MEDIUM", ", ".join(hits_medium)
    return "LOW", "none matched (default)"


# ---------------------------------------------------------------------------
# London Stadium scraper
# ---------------------------------------------------------------------------

LONDON_STADIUM_SOURCES = [
    ("https://www.london-stadium.com/events/all.html", ""),
    ("https://www.london-stadium.com/events/west-ham.html", "Sport - West Ham United"),
]
LONDON_STADIUM_LOCATION = "London Stadium, Queen Elizabeth Olympic Park, London E20 2ST"


def scrape_london_stadium() -> list[ParkEvent]:
    events: list[ParkEvent] = []
    seen: set[tuple[str, date]] = set()

    for url, forced_category in LONDON_STADIUM_SOURCES:
        try:
            lines, soup = fetch_text_lines(url)
        except requests.RequestException as exc:
            print(f"[warn] could not fetch {url}: {exc}", file=sys.stderr)
            continue

        link_lookup = build_link_lookup(soup, url)

        i = 0
        while i < len(lines):
            m = FULL_DATE_RE.search(lines[i])
            if not m:
                i += 1
                continue

            day, month_name, year = m.groups()
            month = datetime.strptime(month_name[:3], "%b").month
            event_date = date(int(year), month, int(day))

            event_time = ""
            title = ""
            j = i + 1
            window_end = min(i + 6, len(lines))
            while j < window_end:
                line = lines[j]
                low = line.lower()
                if low in SKIP_LINE_VALUES:
                    j += 1
                    continue
                t_match = TIME_RE.search(line)
                if t_match and not title:
                    event_time = t_match.group(1)
                    j += 1
                    continue
                if FULL_DATE_RE.search(line):
                    break
                if not title and len(line) > 3:
                    title = line
                    j += 1
                    continue
                break

            i = j
            if not title:
                continue

            key = (title, event_date)
            if key in seen:
                continue
            seen.add(key)

            href = link_lookup.get(title, "")
            category = forced_category or "Event"

            events.append(
                ParkEvent(
                    title=title,
                    start=event_date,
                    end=event_date,
                    source="London Stadium",
                    location=LONDON_STADIUM_LOCATION,
                    category=category,
                    event_time=event_time,
                    url=href,
                )
            )

    return events


# ---------------------------------------------------------------------------
# Queen Elizabeth Olympic Park scraper
# ---------------------------------------------------------------------------

QEOP_URL = "https://www.queenelizabetholympicpark.co.uk/whats-on"
QEOP_LOCATION = "Queen Elizabeth Olympic Park, London E20"


def scrape_qeop() -> list[ParkEvent]:
    events: list[ParkEvent] = []
    try:
        lines, soup = fetch_text_lines(QEOP_URL)
    except requests.RequestException as exc:
        print(f"[warn] could not fetch {QEOP_URL}: {exc}", file=sys.stderr)
        return events

    link_lookup = build_link_lookup(soup, QEOP_URL)

    for idx, line in enumerate(lines):
        m = DATE_RANGE_RE.search(line)
        single = None
        if not m:
            single = SINGLE_SLASH_DATE_RE.search(line)
        if not m and not single:
            continue

        if m:
            d1, mo1, y1, d2, mo2, y2 = m.groups()
            start = date(int(y1), int(mo1), int(d1))
            end = date(int(y2), int(mo2), int(d2))
        else:
            d1, mo1, y1 = single.groups()
            start = end = date(int(y1), int(mo1), int(d1))

        title = ""
        for back in range(1, 4):
            k = idx - back
            if k < 0:
                break
            cand = lines[k]
            if cand.lower() in SKIP_LINE_VALUES or len(cand) <= 3:
                continue
            if FULL_DATE_RE.search(cand) or DATE_RANGE_RE.search(cand):
                continue
            title = cand
            break

        if not title:
            continue

        href = link_lookup.get(title, "")

        events.append(
            ParkEvent(
                title=title,
                start=start,
                end=end,
                source="Queen Elizabeth Olympic Park",
                location=QEOP_LOCATION,
                category="Event",
                url=href,
            )
        )

    return events


# ---------------------------------------------------------------------------
# Newham Council road/traffic notices via The Gazette's public API
# ---------------------------------------------------------------------------
# The Gazette (thegazette.co.uk) is the UK's official public record. Its
# REST API is documented and free to use without an API key:
# https://github.com/TheGazette/DevDocs/blob/master/notice/notice-feed.md
#
# We search a 1.5 mile radius around Queen Elizabeth Olympic Park across a
# rolling window (today - HISTORY_DAYS to today + HORIZON_DAYS) and keep any
# notice whose text mentions traffic/road-closure terms. This is what
# supplies genuine *historical* road-restriction data, since the venue
# websites don't publish anything about the past.

GAZETTE_API = "https://www.thegazette.co.uk/all-notices/notice/data.json"
QEOP_POSTCODE = "E20 2ST"
GAZETTE_SEARCH_RADIUS_MILES = 1.5
GAZETTE_KEYWORDS = [
    "traffic", "road", "highway", "closure", "closed", "prohibit",
    "diversion", "parking", "waiting", "loading", "footway", "carriageway",
]
GAZETTE_MAX_DETAIL_FETCHES = 25  # cap full-text lookups per run, be a good citizen
GAZETTE_REQUEST_DELAY_SECONDS = 1.0


def _looks_traffic_related(title: str, content: str) -> bool:
    blob = f"{title} {content}".lower()
    return any(k in blob for k in GAZETTE_KEYWORDS)


def _extract_location_snippet(title: str, content: str) -> str:
    """Best-effort short location string from the notice title/content."""
    # Titles are often like "Olympic Park Avenue, Newham, Temporary
    # Prohibition of Traffic" - take the bit before the first comma.
    if "," in title:
        candidate = title.split(",")[0].strip()
        if candidate:
            return f"{candidate}, Newham, London"
    return "Newham, London (see notice for exact location)"


def scrape_newham_gazette_notices() -> list[ParkEvent]:
    events: list[ParkEvent] = []

    start_date = date.today() - timedelta(days=HISTORY_DAYS)
    end_date = date.today() + timedelta(days=HORIZON_DAYS)

    page = 1
    page_size = 50
    max_pages = 6  # safety cap; ~300 notices is more than enough for this radius
    detail_fetches_used = 0

    while page <= max_pages:
        params = {
            "location-postcode-1": QEOP_POSTCODE,
            "location-distance-1": GAZETTE_SEARCH_RADIUS_MILES,
            "start-publish-date": start_date.isoformat(),
            "end-publish-date": end_date.isoformat(),
            "results-page-size": page_size,
            "results-page": page,
            "sort-by": "latest-date",
        }
        try:
            resp = requests.get(GAZETTE_API, params=params, headers=HEADERS, timeout=30)
            resp.raise_for_status()
            data = resp.json()
        except (requests.RequestException, ValueError) as exc:
            print(f"[warn] Gazette API request failed (page {page}): {exc}", file=sys.stderr)
            break

        entries = data.get("entry", [])
        if isinstance(entries, dict):  # single-result responses aren't wrapped in a list
            entries = [entries]
        if not entries:
            break

        for entry in entries:
            title = re.sub(r"\s+", " ", (entry.get("title") or "")).strip()
            content_html = entry.get("content", "") or ""
            content_text = BeautifulSoup(content_html, "html.parser").get_text(" ", strip=True)

            if not _looks_traffic_related(title, content_text):
                continue

            published_str = entry.get("published", "")
            try:
                published_date = datetime.fromisoformat(published_str.replace("Z", "+00:00")).date()
            except ValueError:
                continue

            notice_url = ""
            for link in entry.get("link", []):
                href = link.get("@href", "")
                if href and "id/notice" not in href:
                    notice_url = href
                    break

            start_d, end_d = published_date, published_date
            full_text = content_text

            # Try to get the real closure date range by fetching the full
            # notice text (the search snippet is truncated with "…"). Capped
            # so a single run can't hammer the site.
            if notice_url and detail_fetches_used < GAZETTE_MAX_DETAIL_FETCHES:
                try:
                    time.sleep(GAZETTE_REQUEST_DELAY_SECONDS)
                    detail_resp = requests.get(notice_url, headers=HEADERS, timeout=30)
                    detail_resp.raise_for_status()
                    detail_soup = BeautifulSoup(detail_resp.text, "html.parser")
                    for tag in detail_soup(["script", "style", "nav", "footer"]):
                        tag.decompose()
                    full_text = detail_soup.get_text(" ", strip=True)
                    detail_fetches_used += 1
                except requests.RequestException:
                    pass  # fall back to the truncated snippet, not fatal

            range_m = PROSE_DATE_RANGE_RE.search(full_text)
            if range_m:
                d1, mo1, y1, d2, mo2, y2 = range_m.groups()
                try:
                    start_d = date(int(y1), datetime.strptime(mo1[:3], "%b").month, int(d1))
                    end_d = date(int(y2), datetime.strptime(mo2[:3], "%b").month, int(d2))
                except ValueError:
                    start_d, end_d = published_date, published_date

            events.append(
                ParkEvent(
                    title=title or "Newham traffic/road notice",
                    start=start_d,
                    end=max(end_d, start_d),
                    source="Newham Council (The Gazette)",
                    location=_extract_location_snippet(title, full_text),
                    category="Council traffic notice",
                    url=notice_url,
                    restrictions=[full_text[:600] + ("…" if len(full_text) > 600 else "")],
                )
            )

        total = int(data.get("f:total", 0) or 0)
        if page * page_size >= total:
            break
        page += 1

    return events


# ---------------------------------------------------------------------------
# Restriction notes for venue events (Newham notices already carry their own)
# ---------------------------------------------------------------------------

RESIDENTS_INFO_URL = "https://www.london-stadium.com/residents-information/index.html"

STANDING_RESTRICTION_NOTE = (
    "Standing notice from London Stadium residents' info: on-site noise is "
    "restricted to 08:00-22:00 during build/de-rig for major events; sound "
    "checks run from two days before a concert (09:00-22:00); venue curfew "
    "is 23:00; road closures for major events are managed under an "
    "Anti-Terrorism Traffic Regulation Order (ATTRO). Confirm exact "
    f"closures nearer the date at {RESIDENTS_INFO_URL}"
)


def enrich_with_restrictions(events: list[ParkEvent]) -> None:
    for ev in events:
        if ev.restrictions:
            continue  # Newham notices already carry their own restriction text
        notes = []
        if ev.source == "London Stadium":
            notes.append(STANDING_RESTRICTION_NOTE)
        if "west ham" in ev.category.lower():
            notes.append(
                "Matchday: expect crowding around Stratford station, "
                "Westfield and the Greenway 90 min before/after kick-off."
            )
        ev.restrictions = notes


# ---------------------------------------------------------------------------
# Archive (gives the calendar its rolling 6-month memory)
# ---------------------------------------------------------------------------

def load_archive() -> dict[str, dict]:
    if not ARCHIVE_FILE.exists():
        return {}
    try:
        return json.loads(ARCHIVE_FILE.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        print(f"[warn] could not read archive, starting fresh: {exc}", file=sys.stderr)
        return {}


def save_archive(archive: dict[str, dict]) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    ARCHIVE_FILE.write_text(json.dumps(archive, indent=2, sort_keys=True), encoding="utf-8")


def merge_into_archive(archive: dict[str, dict], fresh_events: list[ParkEvent]) -> None:
    now = datetime.now(timezone.utc).isoformat()
    for ev in fresh_events:
        rec = ev.to_record()
        uid = rec["uid"]
        rec["first_seen"] = archive.get(uid, {}).get("first_seen", now)
        rec["last_seen"] = now
        archive[uid] = rec


def prune_archive(archive: dict[str, dict]) -> dict[str, dict]:
    cutoff_past = date.today() - timedelta(days=HISTORY_DAYS)
    cutoff_future = date.today() + timedelta(days=HORIZON_DAYS)
    kept = {}
    for uid, rec in archive.items():
        try:
            end_d = date.fromisoformat(rec["end"])
            start_d = date.fromisoformat(rec["start"])
        except (KeyError, ValueError):
            continue
        if end_d < cutoff_past or start_d > cutoff_future:
            continue
        kept[uid] = rec
    return kept


# ---------------------------------------------------------------------------
# ICS writer
# ---------------------------------------------------------------------------

def fold_line(line: str) -> str:
    """RFC5545 line folding at 75 octets."""
    if len(line.encode("utf-8")) <= 75:
        return line
    out = []
    while len(line.encode("utf-8")) > 75:
        out.append(line[:74])
        line = " " + line[74:]
    out.append(line)
    return "\r\n".join(out)


def escape_text(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def format_time_field(d: date, human_time: str) -> str:
    date_str = d.strftime("%a %d %b %Y")
    if human_time:
        return f"{date_str}, {human_time}"
    return f"{date_str} (all day / time not specified)"


def build_description(ev: ParkEvent, severity: str, matched_terms: str) -> str:
    restrictions_text = (
        " | ".join(ev.restrictions) if ev.restrictions else "None specified"
    )
    other_info_bits = []
    if ev.category:
        other_info_bits.append(f"Category: {ev.category}")
    other_info = "; ".join(other_info_bits) if other_info_bits else ""

    lines = [
        f"Event Name: {ev.title}",
        f"Event Location: {ev.location or 'Not specified'}",
        f"Event Start Time: {format_time_field(ev.start, ev.event_time)}",
        (
            f"Event End Time: {format_time_field(ev.end, '')}"
            if ev.end != ev.start
            else "Event End Time: Not specified (single-day event)"
        ),
        f"Busyness Factor: {severity.title()}",
        f"Road Restrictions: {restrictions_text}",
        "",
        f"Other Information: {other_info}",
        "",
        "--",
        f"Source of data: {ev.source}",
        f"Website Link: {ev.url or 'Not available'}",
        f"Matched terms: {matched_terms}",
    ]
    return escape_text("\n".join(lines))


def build_ics(events: list[ParkEvent]) -> str:
    now = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//Jack//QEOP Traffic Watch//EN",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:QEOP & London Stadium Traffic Watch",
        "X-WR-CALDESC:High footfall\\, road closure and parking-restriction "
        "risk days around Queen Elizabeth Olympic Park and London Stadium\\, "
        "plus a rolling 6-month history.",
        "X-WR-TIMEZONE:Europe/London",
        "REFRESH-INTERVAL;VALUE=DURATION:P7D",
        "X-PUBLISHED-TTL:P7D",
    ]

    for ev in sorted(events, key=lambda e: e.start):
        dtstart = ev.start.strftime("%Y%m%d")
        dtend_exclusive = (ev.end + timedelta(days=1)).strftime("%Y%m%d")

        severity, matched_terms = classify_severity(
            ev.title, ev.category, " ".join(ev.restrictions)
        )

        dot = SEVERITY_DOT[severity]
        summary = f"{dot} {ev.title}, {ev.location or ev.source}"
        description = build_description(ev, severity, matched_terms)

        lines.append("BEGIN:VEVENT")
        lines.append(fold_line(f"UID:{ev.uid()}"))
        lines.append(f"DTSTAMP:{now}")
        lines.append(f"DTSTART;VALUE=DATE:{dtstart}")
        lines.append(f"DTEND;VALUE=DATE:{dtend_exclusive}")
        lines.append(fold_line(f"SUMMARY:{escape_text(summary)}"))
        lines.append(fold_line(f"DESCRIPTION:{description}"))
        lines.append(f"LOCATION:{escape_text(ev.location or ev.source)}")
        lines.append(f"CATEGORIES:{severity}")
        lines.append("TRANSP:TRANSPARENT")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    fresh_events: list[ParkEvent] = []
    fresh_events.extend(scrape_london_stadium())
    fresh_events.extend(scrape_qeop())
    fresh_events.extend(scrape_newham_gazette_notices())

    if not fresh_events:
        print(
            "[warn] no events scraped from any source this run - relying "
            "entirely on the existing archive, if any.",
            file=sys.stderr,
        )

    # De-duplicate same-title/same-date events seen on more than one venue
    # site, preferring the London Stadium entry (richer notes).
    dedup: dict[tuple[str, date], ParkEvent] = {}
    for ev in fresh_events:
        key = (ev.title.lower().strip(), ev.start)
        if key not in dedup or ev.source == "London Stadium":
            dedup[key] = ev
    fresh_events = list(dedup.values())

    enrich_with_restrictions(fresh_events)

    archive = load_archive()
    merge_into_archive(archive, fresh_events)
    archive = prune_archive(archive)
    save_archive(archive)

    all_events = [ParkEvent.from_record(rec) for rec in archive.values()]

    if not all_events:
        # True first-ever run with nothing scraped and nothing archived:
        # still publish a valid (empty) calendar so the subscription URL
        # works right away.
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        OUTPUT_FILE.write_text(build_ics([]), encoding="utf-8")
        print("Wrote 0 events (no data available yet)")
        return

    ics_text = build_ics(all_events)
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(ics_text, encoding="utf-8")

    print(f"Wrote {len(all_events)} events to {OUTPUT_FILE} "
          f"({len(fresh_events)} freshly scraped this run)")
    for ev in sorted(all_events, key=lambda e: e.start):
        severity, _ = classify_severity(ev.title, ev.category, " ".join(ev.restrictions))
        print(f"  {ev.start} [{severity:6s}] {ev.title} ({ev.source})")


if __name__ == "__main__":
    main()
