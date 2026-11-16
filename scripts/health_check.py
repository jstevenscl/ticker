#!/usr/bin/env python3
"""
Ticker data health check.

Checks the PUBLISHED satellite-radio data (what Dispatcharr users actually
download from GitHub Pages) and the Actions workflows that produce it, and exits
non-zero with a human-readable report if anything is degraded.

Why this exists: on 2026-09 SiriusXM redesigned its sports pages, the scraper
started finding 0 events, and every workflow run still said "success" -- so the
guide silently lost its live sports data for ~8 weeks. Job success != data
health, so this checks the data itself.

Run locally:   python scripts/health_check.py
In Actions:    .github/workflows/health-check.yml (opens/updates/closes an issue)

Environment:
  GITHUB_TOKEN / GH_TOKEN   optional; enables the workflow-liveness checks
  GITHUB_REPOSITORY         default jstevenscl/ticker
  HEALTH_REPORT             optional path; markdown report is written there
"""

import json
import os
import sys
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta, timezone

REPO = os.environ.get("GITHUB_REPOSITORY", "jstevenscl/ticker")
BASE = os.environ.get("HEALTH_BASE_URL", "https://jstevenscl.github.io/ticker/lib")
UA = "Ticker-HealthCheck/1.0 (github.com/jstevenscl/ticker)"

# ── Thresholds ────────────────────────────────────────────────────────────────
EPG_MIN_HORIZON_DAYS = 7          # EPG is built with a 14-day window; <7 = builds stalled
EPG_MIN_CHANNEL_RATIO = 0.98      # of lineup channels that must appear in the EPG
EPG_MIN_COVERED_NOW_RATIO = 0.98  # of EPG channels that must have a programme airing right now
SPORTS_MAX_AGE_H = 24             # built every 4h; GitHub drops/delays cron runs, so allow ~5 misses
SHOW_SCHEDULES_MAX_AGE_H = 72     # built daily; 72h = 2 missed runs
SHOW_SCHEDULES_MIN_CHANNELS = 350 # normally ~400 (observed 393-408 since Sep); siriusxm.com scrape, no STL equivalent
STL_URL = "https://stellartunerlog.com/channels.json"   # source of truth for the lineup
STL_MAX_AGE_H = 48                # STL refreshes ~daily; older = STL itself stalled, don't trust it
STL_MIN_CHANNELS = 600            # normally ~745; far below = broken feed, not a lineup change
BUNDLED_MAX_CHANGES = 40          # STL-vs-shipped-zip differences before a release is advised
RELEASE_ZIP_URL = "https://raw.githubusercontent.com/jstevenscl/ticker/master/zips/ticker/ticker-latest.zip"
# workflow file -> (max hours since last run, human name). Cron cadence x ~3.
WORKFLOWS = {
    "update-sports-epg.yml":      (24,      "Update Satellite Radio EPG (every 4h)"),
    "update-show-schedules.yml":  (3 * 24,  "Update Satellite Radio Show Schedules (daily)"),
    "update-channels.yml":        (3 * 24,  "Update Channel Cache (daily)"),
}


def get(url, accept_json=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def parse_iso(s):
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def parse_xmltv(s):
    # "20261116040000 +0000"
    return datetime.strptime(s.split()[0], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)


def age_h(ts, now):
    return (now - ts).total_seconds() / 3600


def check_epg(now, problems, notes):
    try:
        lineup = json.loads(get(f"{BASE}/channels.json").decode("utf-8"))
        lineup_names = {v["name"] for v in lineup.values() if v.get("name")}
        root = ET.fromstring(get(f"{BASE}/satellite_radio_epg.xml"))
    except Exception as e:
        problems.append(f"**EPG unreachable or unparseable** ({BASE}): {e}")
        return

    channel_ids = {c.get("id") for c in root.findall("channel")}
    missing = sorted(lineup_names - channel_ids)
    ratio = 1 - len(missing) / max(len(lineup_names), 1)
    notes.append(f"EPG: {len(channel_ids)} channels, lineup {len(lineup_names)}")
    if ratio < EPG_MIN_CHANNEL_RATIO:
        problems.append(
            f"**EPG is missing {len(missing)} of {len(lineup_names)} lineup channels** "
            f"(e.g. {', '.join(missing[:8])})")

    latest_stop = None
    covered = set()
    n_prog = 0
    for p in root.findall("programme"):
        n_prog += 1
        try:
            start, stop = parse_xmltv(p.get("start")), parse_xmltv(p.get("stop"))
        except Exception:
            continue
        if latest_stop is None or stop > latest_stop:
            latest_stop = stop
        if start <= now < stop:
            covered.add(p.get("channel"))
    notes.append(f"EPG: {n_prog} programmes, runs until {latest_stop:%Y-%m-%d %H:%MZ}" if latest_stop else "EPG: no programmes")

    if latest_stop is None:
        problems.append("**EPG contains no programmes at all**")
        return
    horizon = (latest_stop - now).total_seconds() / 86400
    if horizon < EPG_MIN_HORIZON_DAYS:
        problems.append(
            f"**EPG data is running out**: only {horizon:.1f} days of guide ahead "
            f"(want >= {EPG_MIN_HORIZON_DAYS}); the EPG build has probably stopped publishing")
    cov = len(covered & channel_ids) / max(len(channel_ids), 1)
    if cov < EPG_MIN_COVERED_NOW_RATIO:
        problems.append(
            f"**Only {cov:.0%} of channels have a programme airing right now** "
            f"(want >= {EPG_MIN_COVERED_NOW_RATIO:.0%}) -- guide has gaps")


def check_sports(now, problems, notes):
    try:
        data = json.loads(get(f"{BASE}/sports_schedule.json").decode("utf-8"))
        gen = parse_iso(data["generated_at"])
        events = data.get("events", [])
    except Exception as e:
        problems.append(f"**sports_schedule.json unreachable or unparseable**: {e}")
        return
    upcoming = []
    for ev in events:
        try:
            if parse_iso(ev["end_utc"]) > now:
                upcoming.append(ev)
        except Exception:
            pass
    notes.append(f"Sports: generated {age_h(gen, now):.1f}h ago, {len(events)} events, {len(upcoming)} upcoming")
    if age_h(gen, now) > SPORTS_MAX_AGE_H:
        problems.append(
            f"**Sports schedule is stale**: generated {age_h(gen, now):.0f}h ago "
            f"(limit {SPORTS_MAX_AGE_H}h)")
    if not upcoming:
        problems.append(
            "**No upcoming sports events in the published schedule.** Some league is always "
            "in season, so this means the siriusxm.com scraper is broken "
            "(page markup change -- see `parse_schedule_page` in scripts/build_sports_epg.py)")


def check_show_schedules(now, problems, notes):
    try:
        data = json.loads(get(f"{BASE}/show_schedules.json").decode("utf-8"))
        gen = parse_iso(data["generated_at"])
        chans = {k: v for k, v in data.get("channels", {}).items() if v}
    except Exception as e:
        problems.append(f"**show_schedules.json unreachable or unparseable**: {e}")
        return
    notes.append(f"Show schedules: generated {age_h(gen, now):.1f}h ago, {len(chans)} channels with data")
    if age_h(gen, now) > SHOW_SCHEDULES_MAX_AGE_H:
        problems.append(
            f"**Show schedules are stale**: generated {age_h(gen, now):.0f}h ago "
            f"(limit {SHOW_SCHEDULES_MAX_AGE_H}h)")
    if len(chans) < SHOW_SCHEDULES_MIN_CHANNELS:
        problems.append(
            f"**Show schedules cover only {len(chans)} channels** "
            f"(normally ~400, floor {SHOW_SCHEDULES_MIN_CHANNELS}) -- scraper is likely broken")


def _norm(name):
    return "".join(c for c in name.lower() if c.isalnum())


def lineup_problems(stl, catalog, aliases, epg_ids):
    """Cross-check everything we publish against STL (StellarTunerLog), which
    auto-detects SiriusXM lineup changes and is the source of truth.

    stl      {entity_id: STL channel dict}      live stellartunerlog.com/channels.json
    catalog  {key: our channel dict}            published lib/channels.json
    aliases  {alias_lower: canonical}           published lib/channel_aliases.json
    epg_ids  set of channel ids in the published EPG
    Returns a list of problem strings (empty = consistent).
    """
    problems = []
    by_id = {str(v.get("sxm_entity_id")): v for v in catalog.values()}

    # Catalog: every STL channel present, same name and number.
    missing, renamed, renumbered = [], [], []
    for v in stl.values():
        ours = by_id.get(str(v["id"]))
        if ours is None:
            missing.append(f"{v['name']} (ch {v.get('channel_number')})")
            continue
        if _norm(ours["name"]) != _norm(v["name"]):
            renamed.append(f"{v['name']} -> ours: {ours['name']}")
        if ours.get("sxm_number") != v.get("channel_number"):
            renumbered.append(f"{v['name']}: STL {v.get('channel_number')} vs ours {ours.get('sxm_number')}")
    for label, items in (("missing from our channel catalog", missing),
                         ("named differently in our catalog than in STL", renamed),
                         ("numbered differently in our catalog than in STL", renumbered)):
        if items:
            problems.append(f"**{len(items)} STL channel(s) {label}** -- the channel cache is behind "
                            f"the lineup (e.g. {'; '.join(items[:5])})")

    # EPG: every STL channel (by our canonical name) has a guide channel.
    no_epg = [by_id[str(v["id"])]["name"] for v in stl.values()
              if str(v["id"]) in by_id and by_id[str(v["id"])]["name"] not in epg_ids]
    if no_epg:
        problems.append(f"**{len(no_epg)} STL channel(s) have no EPG entry** (e.g. {', '.join(no_epg[:6])})")

    # Aliases are applied to Dispatcharr names *before* matching against live STL
    # names, so they must never redirect a live STL name to a different channel
    # (2026-11: 'message worship' -> The Message matched ch 78 instead of 79).
    live = {}
    for v in stl.values():
        live.setdefault(_norm(v["name"]), []).append(v)
    bad = []
    for key, target in aliases.items():
        k, t = live.get(_norm(key)), live.get(_norm(target))
        if k and not t:
            bad.append(f"'{key}' is a live STL name but its target '{target}' is not (no match at all)")
        elif k and t and {x["id"] for x in k} != {x["id"] for x in t}:
            bad.append(f"'{key}' (ch {k[0].get('channel_number')}) points at a DIFFERENT live channel "
                       f"'{target}' (ch {t[0].get('channel_number')})")
    if bad:
        problems.append(f"**{len(bad)} alias(es) mis-route live STL channels**: " + "; ".join(bad[:4]))
    return problems


def check_lineup(now, problems, notes):
    try:
        feed = json.loads(get(STL_URL).decode("utf-8"))
        stl = {str(v["id"]): v for v in feed["channels"].values()}
        stl_updated = parse_iso(feed["updated_utc"].split(".")[0].rstrip("Z") + "Z")
        catalog = json.loads(get(f"{BASE}/channels.json").decode("utf-8"))
        aliases = json.loads(get(f"{BASE}/channel_aliases.json").decode("utf-8"))
        epg_ids = {c.get("id") for c in ET.fromstring(get(f"{BASE}/satellite_radio_epg.xml")).findall("channel")}
    except Exception as e:
        problems.append(f"**Could not load STL / published data for the lineup cross-check**: {e}")
        return
    notes.append(f"STL: {len(stl)} channels, updated {age_h(stl_updated, now):.1f}h ago")
    if age_h(stl_updated, now) > STL_MAX_AGE_H:
        problems.append(f"**STL itself looks stalled**: feed last updated {age_h(stl_updated, now):.0f}h ago "
                        f"(limit {STL_MAX_AGE_H}h) -- lineup changes are not being detected")
        return
    if len(stl) < STL_MIN_CHANNELS:
        problems.append(f"**STL feed has only {len(stl)} channels** (normally ~745) -- not trusting it for "
                        f"the cross-check; check stellartunerlog.com")
        return
    problems.extend(lineup_problems(stl, catalog, {k.lower(): v for k, v in aliases.items()}, epg_ids))

    # The plugin zip bundles a fallback catalog (used only if the live fetch fails).
    try:
        import io, zipfile
        z = zipfile.ZipFile(io.BytesIO(get(RELEASE_ZIP_URL)))
        bundled = json.loads(z.read("channels.json").decode("utf-8"))
    except Exception as e:
        notes.append(f"Bundled fallback catalog: could not read release zip ({e})")
        return
    b_ids = {str(v.get("sxm_entity_id")): v for v in bundled.values()}
    added = [v["name"] for v in stl.values() if str(v["id"]) not in b_ids]
    changed = [v["name"] for v in stl.values() if str(v["id"]) in b_ids and
               (_norm(b_ids[str(v["id"])]["name"]) != _norm(v["name"]) or
                b_ids[str(v["id"])].get("sxm_number") != v.get("channel_number"))]
    notes.append(f"Bundled fallback catalog vs STL: {len(added)} new, {len(changed)} renamed/renumbered "
                 f"(live-fetch covers these; fallback only used if STL is unreachable)")
    if len(added) + len(changed) >= BUNDLED_MAX_CHANGES:
        problems.append(f"**Released plugin's bundled channel catalog is {len(added)} new + {len(changed)} "
                        f"changed channels behind STL** -- consider cutting a release to refresh the fallback")


def check_workflows(now, problems, notes):
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        notes.append("Workflow liveness: skipped (no GITHUB_TOKEN)")
        return

    def api(path):
        req = urllib.request.Request(
            f"https://api.github.com/repos/{REPO}/{path}",
            headers={"User-Agent": UA, "Authorization": f"Bearer {token}",
                     "Accept": "application/vnd.github+json"})
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode("utf-8"))

    for wf, (max_h, label) in WORKFLOWS.items():
        try:
            meta = api(f"actions/workflows/{wf}")
            if meta.get("state") != "active":
                problems.append(
                    f"**Workflow `{wf}` is `{meta.get('state')}`** -- GitHub disables scheduled "
                    f"workflows after 60 days of repo inactivity. Re-enable it in the Actions tab.")
                continue
            runs = api(f"actions/workflows/{wf}/runs?per_page=1&status=completed").get("workflow_runs", [])
        except Exception as e:
            problems.append(f"**Could not query workflow `{wf}`**: {e}")
            continue
        if not runs:
            problems.append(f"**Workflow `{wf}` ({label}) has never completed a run**")
            continue
        last = runs[0]
        ts = datetime.strptime(last["created_at"], "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
        notes.append(f"Workflow {wf}: last run {age_h(ts, now):.1f}h ago -> {last['conclusion']}")
        if age_h(ts, now) > max_h:
            problems.append(
                f"**Workflow `{wf}` has not run in {age_h(ts, now):.0f}h** "
                f"(expected every <= {max_h}h) -- {label}")
        if last["conclusion"] not in ("success", "skipped"):
            problems.append(f"**Workflow `{wf}` last run {last['conclusion']}**: {last['html_url']}")


def main():
    now = datetime.now(timezone.utc)
    problems, notes = [], []
    for check in (check_epg, check_sports, check_show_schedules, check_lineup, check_workflows):
        try:
            check(now, problems, notes)
        except Exception as e:  # a broken check must alert, not crash silently
            problems.append(f"**Health check `{check.__name__}` crashed**: {type(e).__name__}: {e}")

    status = "FAILING" if problems else "healthy"
    lines = [f"## Ticker data health: {status}", f"_Checked {now:%Y-%m-%d %H:%M} UTC against `{BASE}`_", ""]
    if problems:
        lines += ["### Problems", *[f"- {p}" for p in problems], ""]
    lines += ["### Details", *[f"- {n}" for n in notes]]
    report = "\n".join(lines)
    print(report)
    path = os.environ.get("HEALTH_REPORT")
    if path:
        with open(path, "w", encoding="utf-8") as f:
            f.write(report + "\n")
    sys.exit(1 if problems else 0)


if __name__ == "__main__":
    main()
