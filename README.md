# QEOP & London Stadium Traffic Watch

An auto-updating calendar feed that flags days around Queen Elizabeth
Olympic Park and London Stadium where you can expect heavier crowds, road
closures, or parking restrictions.

## What it does

Every Monday, an automated job re-checks a set of public sources, works out
which events are likely to cause disruption nearby, and rebuilds a single
`.ics` calendar file. Any calendar app subscribed to that file's web address
picks up the changes on its own — nothing needs to be run manually.

Each entry appears as an all-day event, with:

- A coloured dot showing how disruptive it's likely to be: 🟢 low, 🟡
  medium, 🔴 high
- A structured description covering the event's time, location, road
  restrictions, and where the data came from

The calendar also keeps a rolling six-month view rather than only showing
what's ahead. Events don't disappear from it the moment they're over — they
stay visible for six months afterwards, then age out.

## Where the data comes from

**London Stadium and Queen Elizabeth Olympic Park's own event listings** —
their official "what's on" pages, which cover concerts, sport fixtures,
festivals, and community events at the venues. These sites only ever list
what's still upcoming, so the tool keeps its own running memory of
everything it has ever seen, which is what allows the calendar to build up
a genuine history over time rather than losing events the moment they pass.

**The Gazette** — the UK's official public record for statutory notices.
It publishes Newham Council's road closures, temporary traffic
regulation orders, and similar highway notices for the area around the
Park. Unlike the venue listings, these are permanent public records, so
this part of the calendar's history is complete and accurate from day one
rather than something that builds up gradually.

## How severity is worked out

Each event's title, category, and any associated restriction notice are
checked against two keyword lists — one for higher-impact terms (things
like festivals, finals, major tours, or explicit road-closure language) and
one for moderate ones (community events, fixtures, general traffic
restrictions). Whichever list matches, if any, sets the rating; nothing
that matches is rated low by default. The specific terms that triggered a
rating are always included in that event's description, so it's never a
black box.

## Format example

A high-impact event's title would look like:

```
🔴 Example Concert Night, London Stadium, Queen Elizabeth Olympic Park, London E20
```

And its description:

```
Event Name: Example Concert Night
Event Location: London Stadium, Queen Elizabeth Olympic Park, London E20
Event Start Time: Sat 05 Sep 2026, 19:00
Event End Time: Not specified (single-day event)
Busyness Factor: High
Road Restrictions: Standing notice from the venue: on-site noise restricted
during build/de-rig; sound checks from two days before the event; road
closures managed under a Traffic Regulation Order.

Other Information: Category: Music

--
Source of data: London Stadium
Website Link: https://example.org/events/example-concert-night
Matched terms: concert, road closure
```

A council road-closure notice would follow the same shape, but with
"Newham Council (The Gazette)" as the source and the specific road named as
the location.
