#!/usr/bin/env python3
"""
Queen Elizabeth Olympic Park / London Stadium traffic-impact calendar generator.

Scrapes the official London Stadium and Queen Elizabeth Olympic Park "what's
on" pages, works out which events are likely to cause high footfall,
congestion, or parking/road restrictions around the Park, and writes those
out as all-day events into a single .ics file.

Designed to be run on a schedule (see .github/workflows/update.yml) so the
resulting .ics file is a "live" subscription: any calendar app that
subscribes to its raw URL will pick up new/changed events on its own
refresh cycle.

Run manually:
    pip install -r requirements.txt
    python generate_ics.py

Output:
    docs/qeop-traffic.ics

NOTE ON ROBUSTNESS
-------------------
Both source sites are ordinary marketing pages, not APIs, so this parses
their rendered text rather than pinning to specific CSS class names (those
tend to change on redeploys and would silently break a more "precise"
scraper). If a source site is redesigned enough that no events are found,
the script leaves the previously published .ics file untouched rather than
publishing an empty calendar - see `main()`.
"""

from __future__ import annotations

import hashlib
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from bs4 import BeautifulSoup

OUTPUT_DIR = Path(__file__).parent / "docs"
OUTPUT_FILE = OUTPUT_DIR / "qeop-traffic.ics"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0 Safari/537.36 "
        "QEOP-TrafficCal/1.0 (personal, non-commercial event calendar)"
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

SKIP_LINE_VALUES = {
    "book now", "more info", "read more", "sign up now", "see all",
    "getting here", "explore the park", "what's on", "plan your visit",
}


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
    source: str  # "London Stadium" or "Queen Elizabeth Olympic Park"
    category: str = ""
    event_time: str = ""  # human readable, e.g. "11:00 AM"
    url: str = ""
    restrictions: list[str] = field(default_factory=list)
    severity: str = "LOW"
    severity_reason: str = ""

    def uid(self) -> str:
        raw = f"{self.source}|{self.title}|{self.start.isoformat()}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest() + "@qeop-traffic-cal"


# ---------------------------------------------------------------------------
# Severity classification
# ---------------------------------------------------------------------------

HIGH_KEYWORDS = [
    "concert", "festival", "west ham", "final", "nfl", "athletics meet",
    "world tour", "attro", "road closure", "marathon", "boxing",
    "world cup", "international", "cup final", "wing fest", "m72",
]
MEDIUM_KEYWORDS = [
    "community", "family", "run", "race", "tournament", "match", "fixture",
    "sports", "sport", "circus live",
]


def classify_severity(title: str, category: str, notes: str) -> tuple[str, str]:
    blob = f"{title} {category} {notes}".lower()
    hits_high = sorted({k for k in HIGH_KEYWORDS if k in blob})
    hits_medium = sorted({k for k in MEDIUM_KEYWORDS if k in blob})

    if hits_high:
        return "HIGH", f"Matched high-impact terms: {', '.join(hits_high)}"
    if hits_medium:
        return "MEDIUM", f"Matched moderate-impact terms: {', '.join(hits_medium)}"
    return "LOW", "No high/medium traffic-impact keywords matched"


# ---------------------------------------------------------------------------
# London Stadium scraper
# ---------------------------------------------------------------------------

LONDON_STADIUM_SOURCES = [
    ("https://www.london-stadium.com/events/all.html", ""),
    ("https://www.london-stadium.com/events/west-ham.html", "Sport - West Ham United"),
]


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

            # Look ahead a few lines for a time, then the title (the first
            # line that isn't a nav/boilerplate phrase and isn't itself a
            # date/time line), stopping once we hit "Book now"/"More info".
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

        # The title is usually the nearest preceding non-boilerplate line
        # (a heading like "Black to the Future").
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
                category="Event",
                url=href,
            )
        )

    return events


# ---------------------------------------------------------------------------
# Restriction / road-closure notes (standing info from London Stadium)
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
        notes = []
        if ev.source == "London Stadium":
            notes.append(STANDING_RESTRICTION_NOTE)
        if ev.event_time:
            notes.append(f"Scheduled time: {ev.event_time}")
        if "west ham" in ev.category.lower():
            notes.append(
                "Matchday: expect crowding around Stratford station, "
                "Westfield and the Greenway 90 min before/after kick-off."
            )
        ev.restrictions = notes


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
        "risk days around Queen Elizabeth Olympic Park and London Stadium.",
        "X-WR-TIMEZONE:Europe/London",
        "REFRESH-INTERVAL;VALUE=DURATION:P7D",
        "X-PUBLISHED-TTL:P7D",
    ]

    for ev in sorted(events, key=lambda e: e.start):
        dtstart = ev.start.strftime("%Y%m%d")
        dtend_exclusive = (ev.end + timedelta(days=1)).strftime("%Y%m%d")

        severity, reason = classify_severity(
            ev.title, ev.category, " ".join(ev.restrictions)
        )
        ev.severity, ev.severity_reason = severity, reason

        summary = f"[{severity}] {ev.title} ({ev.source})"

        desc_parts = [f"Source: {ev.source}"]
        if ev.category:
            desc_parts.append(f"Category: {ev.category}")
        if ev.event_time:
            desc_parts.append(f"Event time: {ev.event_time}")
        desc_parts.append(f"Severity: {severity} - {reason}")
        if ev.restrictions:
            desc_parts.append("Restrictions / notes:")
            desc_parts.extend(f"- {r}" for r in ev.restrictions)
        if ev.url:
            desc_parts.append(f"More info: {ev.url}")
        description = escape_text("\n".join(desc_parts))

        lines.append("BEGIN:VEVENT")
        lines.append(fold_line(f"UID:{ev.uid()}"))
        lines.append(f"DTSTAMP:{now}")
        lines.append(f"DTSTART;VALUE=DATE:{dtstart}")
        lines.append(f"DTEND;VALUE=DATE:{dtend_exclusive}")
        lines.append(fold_line(f"SUMMARY:{escape_text(summary)}"))
        lines.append(fold_line(f"DESCRIPTION:{description}"))
        lines.append("LOCATION:Queen Elizabeth Olympic Park\\, London E20")
        lines.append(f"CATEGORIES:{severity}")
        lines.append("TRANSP:TRANSPARENT")
        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    all_events: list[ParkEvent] = []
    all_events.extend(scrape_london_stadium())
    all_events.extend(scrape_qeop())

    if not all_events:
        print(
            "[warn] no events scraped from either source this run - "
            "leaving any existing docs/qeop-traffic.ics untouched so the "
            "subscription doesn't go blank because of a temporary site "
            "change or network hiccup.",
            file=sys.stderr,
        )
        if OUTPUT_FILE.exists():
            sys.exit(0)
        else:
            OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
            OUTPUT_FILE.write_text(build_ics([]), encoding="utf-8")
            sys.exit(0)

    dedup: dict[tuple[str, date], ParkEvent] = {}
    for ev in all_events:
        key = (ev.title.lower().strip(), ev.start)
        if key not in dedup or ev.source == "London Stadium":
            dedup[key] = ev
    all_events = list(dedup.values())

    horizon = date.today() + timedelta(days=183)
    all_events = [e for e in all_events if date.today() <= e.start <= horizon]

    enrich_with_restrictions(all_events)

    ics_text = build_ics(all_events)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_FILE.write_text(ics_text, encoding="utf-8")

    print(f"Wrote {len(all_events)} events to {OUTPUT_FILE}")
    for ev in sorted(all_events, key=lambda e: e.start):
        print(f"  {ev.start} [{ev.severity:6s}] {ev.title} ({ev.source})")


if __name__ == "__main__":
    main()
