#!/usr/bin/env python3
"""
Build satellite radio sports EPG from public sports schedule pages.

Outputs (repo root, served via GitHub Pages):
  satellite_radio_epg.xml   — XMLTV file for community use as an EPG source
  sports_schedule.json      — structured schedule for plugin use

Run manually:
  python scripts/build_sports_epg.py

Called automatically by .github/workflows/update-sports-epg.yml every 4 hours.

XMLTV channel IDs match satellite radio channel names from channels.json:
  - Team games  → home team name, away team name, + league/national channel
  - Events      → sport's satellite channel name
Users of the raw XMLTV must set tvg-id on their channels to match these names.
The plugin action bypasses tvg-id and matches by channel name directly.
"""

import json
import re
import sys
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).parent.parent
CHANNELS_JSON = ROOT / "lib" / "channels.json"
LOGOS_DIR = ROOT / "lib" / "logos"
OUT_XML = ROOT / "lib" / "satellite_radio_epg.xml"
OUT_JSON = ROOT / "lib" / "sports_schedule.json"

GITHUB_PAGES_BASE = "https://jstevenscl.github.io/ticker/lib/logos"

UA = "Ticker-SportsEPG/1.0 (github.com/jstevenscl/ticker)"

FILL_DAYS = 14        # fill window: how many days ahead to generate EPG
FILL_BLOCK_HOURS = 24  # daily filler blocks between/around game slots

SPORTS = [
    {"slug": "nba",     "label": "NBA",           "url": "https://www.siriusxm.com/sports/nba",       "type": "matchup", "duration_h": 2.5},
    {"slug": "mlb",     "label": "MLB",           "url": "https://www.siriusxm.com/sports/mlb",       "type": "matchup", "duration_h": 3.0},
    {"slug": "nhl",     "label": "NHL",           "url": "https://www.siriusxm.com/sports/nhl",       "type": "matchup", "duration_h": 2.5},
    {"slug": "nfl",     "label": "NFL",           "url": "https://www.siriusxm.com/sports/nfl",       "type": "matchup", "duration_h": 3.0},
    {"slug": "soccer",  "label": "Soccer",        "url": "https://www.siriusxm.com/sports/soccer",    "type": "matchup", "duration_h": 2.0},
    {"slug": "nascar",  "label": "NASCAR",        "url": "https://www.siriusxm.com/sports/nascar",    "type": "event",   "duration_h": 3.5},
    {"slug": "pga",     "label": "PGA Tour",      "url": "https://www.siriusxm.com/sports/pga-tour",  "type": "event",   "duration_h": 5.0},
    {"slug": "indycar", "label": "IndyCar",       "url": "https://www.siriusxm.com/sports/indycar",   "type": "event",   "duration_h": 2.5},
    {"slug": "f1",      "label": "Formula 1",     "url": "https://www.siriusxm.com/sports/formula-1", "type": "event",   "duration_h": 2.0},
]

# Primary satellite channel for each sport (fallback for league/national coverage)
SPORT_PRIMARY_CH = {
    "nba":     "SiriusXM NBA Radio",
    "mlb":     "MLB Network Radio",
    "nhl":     "SiriusXM NHL Network Radio",
    "nfl":     "SiriusXM NFL Radio",
    "soccer":  "SiriusXM FC",
    "nascar":  "SiriusXM NASCAR Radio",
    "pga":     "SiriusXM PGA Tour Radio",
    "indycar": "SiriusXM INDYCAR Nation",
    "f1":      "Formula 1 Racing and Analysis",
}

# Supplemental channel names from the official SiriusXM lineup (May 7 PDF).
# Covers satellite team-channel ranges AND their app-streaming equivalents.
# Both ranges exist for the same content — satellite users tune to the lower number,
# app users tune to the higher number. We include both so the EPG matches either source.
#
# Satellite play-by-play ranges (from PDF page 2):
#   MLB:     175–189   NCAA:    190–200
#   NHL:     219–223   NBA:     221–227   (overlap 221–223 is shared between seasons)
#   NFL:     225–234   College: 963–999
#
# App/streaming equivalents (from PDF page 3):
#   NFL:     800–831   F1:      835
#   MLB:     840–869   NBA:     880–909
#   NHL:     920–951   College: 521–522 (limited run)
#
# Overlap note: satellite 221–223 is used by both NHL and NBA (different seasons).
# The schedule page itself tells us which sport, so naming is sport-agnostic here.
_SUPPLEMENT_CH = {
    # ── Satellite team/play-by-play ranges ───────────────────────────────
    # MLB satellite home/away feeds (175–189)
    **{n: f"MLB Play-by-Play {n}" for n in range(175, 190)},
    # NCAA satellite play-by-play (190–200)
    **{n: f"NCAA Play-by-Play {n}" for n in range(190, 201)},
    # NHL satellite home/away feeds (219–223)
    **{n: f"NHL/NBA Play-by-Play {n}" for n in range(219, 224)},
    # NBA satellite home/away feeds (221–227) — overlaps 221–223 with NHL above
    **{n: f"NHL/NBA Play-by-Play {n}" for n in range(221, 228)},
    # NFL satellite home/away feeds (225–234)
    **{n: f"NFL Play-by-Play {n}" for n in range(225, 235)},
    # College satellite overflow (963–999)
    **{n: f"College Sports Play-by-Play {n}" for n in range(963, 1000)},

    # ── App/streaming equivalents ─────────────────────────────────────────
    # NFL streaming home/away feeds (800–831)
    **{n: f"NFL Play-by-Play {n}" for n in range(800, 832)},
    # Formula 1 Racing and Analysis (835) — app only
    835: "Formula 1 Racing and Analysis",
    # MLB streaming home/away feeds (840–869)
    **{n: f"MLB Play-by-Play {n}" for n in range(840, 870)},
    # NBA streaming home/away feeds (880–909)
    **{n: f"NBA Play-by-Play {n}" for n in range(880, 910)},
    # NHL streaming home/away feeds (920–951)
    **{n: f"NHL Play-by-Play {n}" for n in range(920, 952)},
    # College limited-run (501–512) and streaming (521–522)
    **{n: f"College Sports Play-by-Play {n}" for n in range(501, 513)},
    **{n: f"College Sports Play-by-Play {n}" for n in range(521, 523)},
}


def load_channel_map():
    """Return dict of sxm_number (int) -> channel name, merging channels.json + supplement."""
    ch_map = dict(_SUPPLEMENT_CH)  # start with supplement (lower priority)
    try:
        data = json.loads(CHANNELS_JSON.read_text(encoding="utf-8"))
        for v in data.values():
            if v.get("sxm_number") is not None:
                ch_map[v["sxm_number"]] = v["name"]  # channels.json wins on conflict
    except Exception as e:
        print(f"  Warning: could not load channels.json: {e}", file=sys.stderr)
    return ch_map


def channel_name_for_num(num, ch_map, sport_slug):
    """Return a channel name for a satellite radio channel number, always producing something."""
    ch = ch_map.get(num)
    if ch:
        return ch
    # Unknown number — generate a descriptive name so it still appears in the XMLTV
    return f"Satellite Radio Channel {num}"


def fetch_html(url):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode("utf-8", errors="replace")


def clean_text(s):
    """Strip HTML tags, decode entities, collapse whitespace."""
    s = re.sub(r"<[^>]+>", " ", s)
    for ent, rep in [
        ("&amp;", "&"), ("&lt;", "<"), ("&gt;", ">"),
        ("&nbsp;", " "), ("&#160;", " "), ("&#39;", "'"), ("&quot;", '"'),
    ]:
        s = s.replace(ent, rep)
    return re.sub(r"\s+", " ", s).strip()


def _us_eastern_is_dst(d):
    """True if US Eastern Time observes DST on date d (2nd Sun Mar -> 1st Sun Nov).

    Hand-rolled so the script has no tz-database dependency (zoneinfo needs the
    tzdata package on Windows). Day-granularity is fine for schedule dates.
    """
    def nth_sunday(year, month, n):
        first = datetime(year, month, 1)
        first_sun = 1 + (6 - first.weekday()) % 7
        return datetime(year, month, first_sun + 7 * (n - 1))
    start = nth_sunday(d.year, 3, 2)
    end = nth_sunday(d.year, 11, 1)
    return start <= datetime(d.year, d.month, d.day) < end


def parse_eastern_time(date_str, time_str):
    """Parse 'November 16', '7:00 PM' (US Eastern) -> UTC datetime, or None.

    The page headings carry no year: assume the current year, and roll forward a
    year if that puts the date more than 180 days in the past (Dec -> Jan games).
    """
    now = datetime.now(timezone.utc)
    year = now.year
    try:
        naive = datetime.strptime(f"{date_str} {year} {time_str}", "%B %d %Y %I:%M %p")
    except ValueError:
        return None
    for _ in range(2):
        offset = timedelta(hours=-4) if _us_eastern_is_dst(naive) else timedelta(hours=-5)
        dt_et = naive.replace(tzinfo=timezone(offset))
        if (now - dt_et).days > 180:
            naive = naive.replace(year=naive.year + 1)
            continue
        break
    return dt_et.astimezone(timezone.utc)


# The siriusxm.com sports pages are a Next.js app with CSS-module class names of
# the form "<component>-module-scss-module__<hash>__<name>". The hash changes on
# rebuilds, so every pattern below anchors on the stable "__<name>" suffix only.
# (Pre-2026-09 the pages used "sportschedule_*"/"sportstrip_*"; SiriusXM
# redesigned them and the old parser silently returned 0 events.)
_DATE_HEADING_RE = re.compile(r'<h3[^>]*__dateHeading"[^>]*>(.*?)</h3>', re.I | re.S)
_STRIP_SPLIT_RE = re.compile(r'data-cy="molecules-sports-strip"', re.I)
_ENTITY_SPLIT_RE = re.compile(r'<div[^>]*__entity"[^>]*>', re.I)
_START_TIME_RE = re.compile(r'__startTime"[^>]*>([^<]+)<', re.I)
_NAME_RE = re.compile(r'__name"[^>]*>([^<]+)<', re.I)
_DESC_RE = re.compile(r'__description"[^>]*>([^<]*)<', re.I)
_CHANNEL_RE = re.compile(r'aria-label="[^"]*?\bchannel\s+(\d+)"', re.I)


def _parse_entity(chunk):
    """Return (name, description, [channel numbers]) for one entity block, or None."""
    name_m = _NAME_RE.search(chunk)
    if not name_m:
        return None
    name = clean_text(name_m.group(1))
    if len(name) < 2:
        return None
    desc_m = _DESC_RE.search(chunk)
    desc = clean_text(desc_m.group(1)) if desc_m else ""
    nums = []
    for n in _CHANNEL_RE.findall(chunk):
        if int(n) not in nums:
            nums.append(int(n))
    return name, desc, nums


def parse_schedule_page(html, sport_slug, sport_type, duration_h, ch_map):
    """
    Parse a SiriusXM sports schedule page (post-2026-09 redesign).

    Layout: <h3 ...dateHeading>Monday, November 16</h3> followed by strips; each
    strip has a start time and 1+ entities. Two entities = a matchup (away first,
    home second, joined by an "at" marker); one entity = a single event (race,
    tournament round) with an optional description such as the venue.

    Returns list of event dicts:
      {slug, title, description, start_utc, end_utc, channels: [...]}
    An off-season sport has no schedule section at all and returns [].
    """
    events = []
    headings = list(_DATE_HEADING_RE.finditer(html))
    for i, hm in enumerate(headings):
        date_m = re.search(r'([A-Za-z]+),\s+([A-Za-z]+)\s+(\d{1,2})', clean_text(hm.group(1)))
        if not date_m:
            continue
        date_str = f"{date_m.group(2)} {date_m.group(3)}"
        block_end = headings[i + 1].start() if i + 1 < len(headings) else len(html)
        block = html[hm.end():block_end]

        for strip in _STRIP_SPLIT_RE.split(block)[1:]:
            time_m = _START_TIME_RE.search(strip)
            if not time_m:
                continue
            start_utc = parse_eastern_time(date_str, clean_text(time_m.group(1)))
            if start_utc is None:
                continue
            end_utc = start_utc + timedelta(hours=duration_h)

            entities = [e for e in (_parse_entity(c) for c in _ENTITY_SPLIT_RE.split(strip)[1:]) if e]
            if not entities:
                continue
            if len(entities) >= 2:
                event = _build_matchup(entities[0], entities[1], sport_slug, start_utc, end_utc, ch_map)
            else:
                event = _build_event(entities[0], sport_slug, start_utc, end_utc, ch_map)
            events.append(event)
    return events


def _channels_for(names, nums, sport_slug, ch_map):
    channels = list(names)
    for num in nums:
        ch_name = channel_name_for_num(num, ch_map, sport_slug)
        if ch_name not in channels:
            channels.append(ch_name)
    # Always include the sport's primary league channel
    primary = SPORT_PRIMARY_CH.get(sport_slug)
    if primary and primary not in channels:
        channels.append(primary)
    return channels


def _build_matchup(away, home, sport_slug, start_utc, end_utc, ch_map):
    """Team-vs-team game. Team name IS the channel name in Dispatcharr."""
    away_name, _, away_nums = away
    home_name, _, home_nums = home
    title = f"{away_name} @ {home_name}"
    return {
        "slug": sport_slug,
        "title": title,
        "description": f"{sport_slug.upper()} · {title}",
        "start_utc": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_utc": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channels": _channels_for([home_name, away_name], home_nums + away_nums, sport_slug, ch_map),
    }


def _build_event(entity, sport_slug, start_utc, end_utc, ch_map):
    """Single event (race, tournament round, practice session...)."""
    name, location, nums = entity
    description = f"{SPORT_PRIMARY_CH.get(sport_slug, sport_slug.upper())} · {name}"
    if location:
        description += f" · {location}"
    return {
        "slug": sport_slug,
        "title": name,
        "description": description,
        "start_utc": start_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "end_utc": end_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "channels": _channels_for([], nums, sport_slug, ch_map),
    }


def logo_slug(name):
    """Normalize a channel name to a logo filename slug (mirrors cache_logos.py)."""
    name = re.sub(r"\s*\[.*?\]", "", name)
    name = re.sub(r"\s*\(.*?\)", "", name)
    return re.sub(r"[^a-z0-9]", "", name.lower())


def supplement_logos(ch_logos, ch_names):
    """Add logo URLs for ch_names not already in ch_logos by checking logos/ dir.

    Uses the same slug formula as cache_logos.py so team names like
    'Anaheim Ducks' resolve to logos/anaheimducks.png automatically.
    Returns the number of logos added.
    """
    added = 0
    for name in ch_names:
        if name in ch_logos:
            continue
        slug = logo_slug(name)
        for ext in ("png", "svg", "jpg"):
            if (LOGOS_DIR / f"{slug}.{ext}").exists():
                ch_logos[name] = f"{GITHUB_PAGES_BASE}/{slug}.{ext}"
                added += 1
                break
    return added


def load_channel_info():
    """Return (descriptions, logos) dicts of channel name -> value from channels.json."""
    try:
        data = json.loads(CHANNELS_JSON.read_text(encoding="utf-8"))
        descriptions = {v["name"]: v.get("description", "") for v in data.values() if v.get("name")}
        logos        = {v["name"]: v["logo_url"] for v in data.values() if v.get("name") and v.get("logo_url")}
        return descriptions, logos
    except Exception as e:
        print(f"  Warning: could not load channel info: {e}", file=sys.stderr)
        return {}, {}


def format_game_time_et(dt_utc):
    """Format a UTC datetime as Eastern Time, e.g. 'Sun, May 17 12:15 PM EDT'."""
    _DAYS   = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    _MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun",
               "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    # Real US DST rules (2nd Sun Mar -> 1st Sun Nov). The old `3 <= month <= 11`
    # shortcut labelled all of November as EDT, putting titles an hour off from
    # Nov 1 until the March changeover.
    is_edt = _us_eastern_is_dst(dt_utc + timedelta(hours=-4))
    local  = dt_utc + timedelta(hours=-4 if is_edt else -5)
    tz     = "EDT" if is_edt else "EST"
    hour   = local.hour % 12 or 12
    ampm   = "AM" if local.hour < 12 else "PM"
    return f"{_DAYS[local.weekday()]}, {_MONTHS[local.month - 1]} {local.day} {hour}:{local.minute:02d} {ampm} {tz}"


def build_channel_segments(ch_name, ch_desc, ev_list, window_start, window_end, block_delta):
    """Build the full EPG segment list for one channel over the fill window.

    ev_list: [(event_dict, start_dt, end_dt), ...]  — upcoming events only
    Returns: [(start_dt, end_dt, title, description), ...]

    Block sequence around each game:
      [generic fill] -> [Upcoming: title -- Day, Mon D H:MM AM/PM TZ] -> [LIVE: title] -> [Post-game: title]
    Channels with no events get a single programme spanning the full window.
    """
    sorted_evs = sorted(ev_list, key=lambda x: x[1])

    # No events — chunk into block_delta-sized segments (24h each) rather than one
    # programme spanning the full window. A single multi-day block is outside what
    # EPG clients expect — TiviMate silently fails to render 336h+ programmes while
    # displaying normally-segmented (sports) channels fine; confirmed by direct
    # comparison. Still far short of "hundreds of hourly filler entries" at 24h blocks.
    if not sorted_evs:
        segments = []
        slot = window_start
        while slot < window_end:
            slot_end = min(slot + block_delta, window_end)
            segments.append((slot, slot_end, ch_name, ch_desc or None))
            slot = slot_end
        return segments

    segments   = []
    current    = window_start
    block_s    = block_delta.total_seconds()

    for i, (ev, start_dt, end_dt) in enumerate(sorted_evs):
        if start_dt >= window_end:
            break
        end_dt = min(end_dt, window_end)

        # upcoming_anchor: block boundary 1 full block_delta before game start
        elapsed_s   = (start_dt - window_start).total_seconds()
        n_before    = max(0, int(elapsed_s / block_s) - 1)
        upcoming_anchor = window_start + timedelta(seconds=n_before * block_s)
        if upcoming_anchor < current:
            upcoming_anchor = current

        # Generic fill up to upcoming_anchor
        slot = current
        while slot < upcoming_anchor:
            slot_end = min(slot + block_delta, upcoming_anchor)
            segments.append((slot, slot_end, ch_name, ch_desc or None))
            slot = slot_end
        current = slot

        # Upcoming block(s) from upcoming_anchor to game start
        if current < start_dt:
            time_str = format_game_time_et(start_dt)
            up_title = f"Upcoming: {ev['title']} -- {time_str}"
            up_desc  = f"Upcoming on {ch_name}: {ev['title']} -- {time_str}"
            slot = current
            while slot < start_dt:
                slot_end = min(slot + block_delta, start_dt)
                segments.append((slot, slot_end, up_title, up_desc))
                slot = slot_end
            current = slot

        # LIVE block (exact game times)
        live_desc = ev.get("description") or f"Live coverage on {ch_name}"
        segments.append((start_dt, end_dt, f"LIVE: {ev['title']}", live_desc or None))
        current = end_dt

        # Post-game block — 1 block_delta, capped at next game's upcoming_anchor
        next_ev = sorted_evs[i + 1] if i + 1 < len(sorted_evs) else None
        if next_ev:
            next_elapsed_s  = (next_ev[1] - window_start).total_seconds()
            next_n          = max(0, int(next_elapsed_s / block_s) - 1)
            next_upcoming   = window_start + timedelta(seconds=next_n * block_s)
            post_end = min(current + block_delta, next_upcoming, window_end)
        else:
            post_end = min(current + block_delta, window_end)

        if post_end > current:
            segments.append((current, post_end,
                             f"Post-game: {ev['title']}",
                             f"Post-game coverage following {ev['title']} on {ch_name}"))
            current = post_end

    # Generic fill for remainder of window
    while current < window_end:
        slot_end = min(current + block_delta, window_end)
        segments.append((current, slot_end, ch_name, ch_desc or None))
        current = slot_end

    return segments


def load_show_schedules():
    """
    Load lib/show_schedules.json (produced by scrape_show_schedules.py,
    run separately on a daily cadence — see that script and
    update-show-schedules.yml). Returns {channel_name: [(start_dt, end_dt,
    show_name), ...]}, sorted. Missing/malformed file -> {} (silently) so
    a scrape failure never breaks the sports EPG build; every channel
    just falls back to today's existing generic-fill behavior, same as
    if this feature didn't exist.
    """
    path = ROOT / "lib" / "show_schedules.json"
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"  Warning: could not load show_schedules.json: {e}", file=sys.stderr)
        return {}

    out = {}
    try:
        channels_data = data.get("channels", {})
        for ch_name, slots in channels_data.items():
            parsed = []
            for s in slots:
                try:
                    sd = datetime.strptime(s["start_utc"], "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                    ed = datetime.strptime(s["end_utc"],   "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc)
                except Exception:
                    continue
                if ed > sd:
                    parsed.append((sd, ed, s.get("show_name") or ch_name))
            parsed.sort()
            if parsed:
                out[ch_name] = parsed
    except Exception as e:
        print(f"  Warning: show_schedules.json has an unexpected structure: {e}", file=sys.stderr)
        return {}
    return out


def build_show_segments(ch_name, ch_desc, show_slots, window_start, window_end, block_delta):
    """
    Build EPG segments for a channel using real scraped show-schedule
    data — real show names/times wherever SiriusXM's own schedule page
    covers, generic block_delta-chunked fill for any gap between shows
    and for the remainder of the window beyond however far the scraped
    data reaches (typically ~5-7 days of the full FILL_DAYS window).

    Only called for channels with NO sports events (see main()) — a
    channel with both would need events vs. show-slot collision
    handling this v1 doesn't attempt; those channels keep using
    build_channel_segments() unchanged, same as before this feature
    existed.

    show_slots: [(start_dt, end_dt, show_name), ...] sorted
    Returns: [(start_dt, end_dt, title, description), ...]
    """
    usable = []
    for s, e, name in show_slots:
        if e <= window_start or s >= window_end:
            continue
        usable.append((max(s, window_start), min(e, window_end), name))

    if not usable:
        segments = []
        slot = window_start
        while slot < window_end:
            slot_end = min(slot + block_delta, window_end)
            segments.append((slot, slot_end, ch_name, ch_desc or None))
            slot = slot_end
        return segments

    segments = []
    current = window_start
    for s, e, name in usable:
        if s < current:
            continue  # overlaps previous slot — keep the earlier one, skip
        if s > current:
            slot = current
            while slot < s:
                slot_end = min(slot + block_delta, s)
                segments.append((slot, slot_end, ch_name, ch_desc or None))
                slot = slot_end
        segments.append((s, e, name, f"{name} on {ch_name}"))
        current = e

    while current < window_end:
        slot_end = min(current + block_delta, window_end)
        segments.append((current, slot_end, ch_name, ch_desc or None))
        current = slot_end

    return segments


def xmltv_dt(dt):
    """Format a datetime object as XMLTV timestamp '20260517200000 +0000'."""
    return dt.strftime("%Y%m%d%H%M%S") + " +0000"


def xmltv_time(dt_str):
    """Convert '2026-05-17T20:00:00Z' to XMLTV format '20260517200000 +0000'."""
    dt = datetime.strptime(dt_str, "%Y-%m-%dT%H:%M:%SZ")
    return dt.strftime("%Y%m%d%H%M%S") + " +0000"


# Characters invalid in XML 1.0: control chars except tab (x09), LF (x0a), CR (x0d)
_INVALID_XML = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f\ud800-\udfff￾￿]')


def xml_esc(s):
    s = _INVALID_XML.sub('', s or '')
    return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")


def build_xmltv(all_events, ch_segments, ch_logos=None):
    """Build XMLTV covering all satellite radio channels.

    all_events  — list of game event dicts (for the sports_schedule.json channels)
    ch_segments — dict of ch_name -> [(start_dt, end_dt, title, desc), ...]
                  covering ALL channels (sports + fill-only)
    ch_logos    — dict of ch_name -> logo URL (from channels.json)
    """
    ch_logos = ch_logos or {}

    lines = ['<?xml version="1.0" encoding="UTF-8"?>']
    lines.append('<!DOCTYPE tv SYSTEM "xmltv.dtd">')
    lines.append(
        '<tv source-info-name="Ticker Satellite Radio EPG" '
        'source-info-url="https://jstevenscl.github.io/ticker/lib/satellite_radio_epg.xml" '
        'generator-info-name="Ticker">'
    )

    for ch in sorted(ch_segments):
        ch_e = xml_esc(ch)
        lines.append(f'  <channel id="{ch_e}">')
        lines.append(f'    <display-name>{ch_e}</display-name>')
        if ch in ch_logos:
            lines.append(f'    <icon src="{xml_esc(ch_logos[ch])}" />')
        lines.append(f'  </channel>')

    for ch in sorted(ch_segments):
        ch_e = xml_esc(ch)
        for seg_start, seg_end, title, desc in ch_segments[ch]:
            lines.append(
                f'  <programme start="{xmltv_dt(seg_start)}" '
                f'stop="{xmltv_dt(seg_end)}" channel="{ch_e}">'
            )
            lines.append(f'    <title lang="en">{xml_esc(title)}</title>')
            if desc:
                lines.append(f'    <desc lang="en">{xml_esc(desc)}</desc>')
            lines.append(f'  </programme>')

    lines.append('</tv>')
    return "\n".join(lines)


def main():
    print("Loading channel map, descriptions and logos...")
    ch_map            = load_channel_map()
    ch_descs, ch_logos = load_channel_info()  # name -> description / logo URL
    print(f"  {len(ch_map)} channels with lineup numbers, {len(ch_descs)} with descriptions, {len(ch_logos)} with logos")

    all_events = []
    fetch_failures = 0

    for sport in SPORTS:
        slug  = sport["slug"]
        label = sport["label"]
        print(f"Fetching {label} schedule...")
        try:
            html = fetch_html(sport["url"])
        except Exception as e:
            print(f"  ERROR fetching {sport['url']}: {e}", file=sys.stderr)
            fetch_failures += 1
            continue

        events = parse_schedule_page(html, slug, sport["type"], sport["duration_h"], ch_map)
        print(f"  {len(events)} events found")
        all_events.extend(events)

    if not all_events:
        print("No upcoming events across any sport.", file=sys.stderr)

    # Deduplicate: same title + start_utc + channel set
    seen_keys = set()
    deduped   = []
    for ev in all_events:
        key = (ev["title"], ev["start_utc"], tuple(sorted(ev["channels"])))
        if key not in seen_keys:
            seen_keys.add(key)
            deduped.append(ev)
    all_events = sorted(deduped, key=lambda e: e["start_utc"])

    now     = datetime.now(timezone.utc)
    now_utc = now.strftime("%Y-%m-%dT%H:%M:%SZ")

    # Write sports_schedule.json  (sports events only — plugin uses this)
    schedule = {"generated_at": now_utc, "events": all_events}
    OUT_JSON.write_text(json.dumps(schedule, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Written: {OUT_JSON}  ({len(all_events)} total events)")

    # ── Build XMLTV with ALL satellite radio channels ────────────────────────
    # Invert events: ch_name -> [(ev, start_dt, end_dt)]
    ch_name_to_ev_list = {}
    for ev in all_events:
        try:
            s = datetime.strptime(ev["start_utc"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            e = datetime.strptime(ev["end_utc"],   "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        except Exception:
            continue
        if e <= now:
            continue
        for ch_name in ev.get("channels", []):
            ch_name_to_ev_list.setdefault(ch_name, []).append((ev, s, e))

    window_start = now.replace(minute=0, second=0, microsecond=0)
    window_end   = window_start + timedelta(days=FILL_DAYS)
    block_delta  = timedelta(hours=FILL_BLOCK_HOURS)

    # All channels to include:
    #   1. Every named channel from channels.json (with its description)
    #   2. Any sports-schedule channels not already covered (team names, etc.)
    all_ch_names = set(ch_descs.keys()) | set(ch_name_to_ev_list.keys())

    # Supplement logos: check logos/ dir for team/college channels not in channels.json
    extra_logos = supplement_logos(ch_logos, all_ch_names)
    if extra_logos:
        print(f"  + {extra_logos} additional logos matched in logos/ (total {len(ch_logos)})")

    # Real scraped show schedules (music/talk/news channels — separate,
    # daily-cadence pipeline, see scrape_show_schedules.py). Missing/stale
    # file just means every channel below falls back to generic fill,
    # exactly as if this feature didn't exist.
    show_schedules = load_show_schedules()
    if show_schedules:
        print(f"Loaded show schedules for {len(show_schedules)} channel(s)")

    ch_segments = {}
    show_schedule_ch_count = 0
    for ch_name in all_ch_names:
        ev_list  = ch_name_to_ev_list.get(ch_name, [])
        ch_desc  = ch_descs.get(ch_name, "")
        show_slots = show_schedules.get(ch_name)

        # Channels with actual sports events keep the existing, unchanged
        # Upcoming:/LIVE:/Post-game: behavior — real show data only
        # enriches channels that have none, which is the vast majority
        # of what scrape_show_schedules.py covers anyway.
        #
        # build_show_segments() is new code exercising data scraped from
        # a third-party page that can change shape without notice — an
        # exception here must never be able to crash the whole build
        # (which would also block the sports EPG, since it's the same
        # script/process). Any failure falls back to the same proven
        # generic-fill path this channel would have used before this
        # feature existed, for that one channel only.
        if not ev_list and show_slots:
            try:
                ch_segments[ch_name] = build_show_segments(
                    ch_name, ch_desc, show_slots, window_start, window_end, block_delta,
                )
                show_schedule_ch_count += 1
            except Exception as e:
                print(f"  Warning: build_show_segments failed for {ch_name!r}: {e} "
                      f"-- falling back to generic fill", file=sys.stderr)
                ch_segments[ch_name] = build_channel_segments(
                    ch_name, ch_desc, [], window_start, window_end, block_delta,
                )
        else:
            ch_segments[ch_name] = build_channel_segments(
                ch_name, ch_desc, ev_list, window_start, window_end, block_delta,
            )

    if show_schedule_ch_count:
        print(f"  {show_schedule_ch_count} channel(s) built from real show schedules "
              f"(rest: sports events or generic fill)")

    # Write satellite_radio_epg.xml  (all channels: sports segments + fill for every channel)
    xml_str = build_xmltv(all_events, ch_segments, ch_logos)
    OUT_XML.write_text(xml_str, encoding="utf-8")
    total_programmes = sum(len(v) for v in ch_segments.values())
    print(f"Written: {OUT_XML}  ({len(ch_segments)} channels, {total_programmes} programmes)")

    # Summary by sport
    by_sport = {}
    for ev in all_events:
        by_sport.setdefault(ev["slug"], 0)
        by_sport[ev["slug"]] += 1
    for slug, count in sorted(by_sport.items()):
        print(f"  {slug}: {count} events")

    # ── Health signal ────────────────────────────────────────────────────────
    # Everything above has already been written, so the EPG still publishes
    # (show schedules + generic fill are unaffected). The non-zero exit just
    # lets the workflow raise an alert. Zero events across ALL sports is never
    # legitimate -- some league is always in season -- and is exactly what a
    # siriusxm.com redesign looks like (the 2026-09 redesign went unnoticed for
    # ~8 weeks because this used to exit 0). Single off-season sports are fine.
    if not all_events:
        reason = (f"all {fetch_failures}/{len(SPORTS)} sports pages failed to fetch"
                  if fetch_failures == len(SPORTS)
                  else "0 events parsed from every sports page -- siriusxm.com markup "
                       "has probably changed and parse_schedule_page needs updating")
        print(f"::error title=Sports scrape unhealthy::{reason}")
        print(f"HEALTH FAIL: {reason}", file=sys.stderr)
        sys.exit(3)


if __name__ == "__main__":
    main()
