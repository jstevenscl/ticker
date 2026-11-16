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
SHOW_SCHEDULES_MIN_CHANNELS = 300 # normally ~400
# workflow file -> (max hours since last run, human name). Cron cadence x ~3.
WORKFLOWS = {
    "update-sports-epg.yml":      (24,      "Update Satellite Radio EPG (every 4h)"),
    "update-show-schedules.yml":  (3 * 24,  "Update Satellite Radio Show Schedules (daily)"),
    "update-channels.yml":        (10 * 24, "Update Channel Cache (weekly)"),
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
    for check in (check_epg, check_sports, check_show_schedules, check_workflows):
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
