#!/usr/bin/env python3
"""Build the Ticker release zip and update both manifests (repeatable release step).

Run from anywhere:  python scripts/build_release_zip.py

What it does (the manual "step 3" of the release chain, done the same way every time):
  1. Reads the version from plugin.json.
  2. Writes zips/ticker/ticker-<version>.zip containing EXACTLY 8 flat files:
       ca_city_names.json, eas_tone.wav, eas_tone_ca.wav, logo.png, plugin.json, plugin.py,
       channels.json, channel_aliases.json   (the last two copied FLAT from lib/ -- production
       expects them next to plugin.py). Files are the repo's own bytes, unmodified.
  3. Copies it to zips/ticker/ticker-latest.zip.
  4. Updates manifest.json (root) and zips/ticker/manifest.json with the new version,
     md5, sha256, size and timestamps (em dashes stay unescaped). If plugin.json has a
     min_dispatcharr_version it is recorded on the version entry and on "latest".

It does NOT commit, push, tag or publish anything.
"""
import hashlib
import json
import shutil
import sys
import zipfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ZIPS = ROOT / "zips" / "ticker"
BASE = "https://raw.githubusercontent.com/jstevenscl/ticker/master/zips/ticker"

ROOT_FILES = ["ca_city_names.json", "eas_tone.wav", "eas_tone_ca.wav", "logo.png", "plugin.json", "plugin.py"]
LIB_FILES = ["channels.json", "channel_aliases.json"]


def dump(obj):
    return json.dumps(obj, indent=2, ensure_ascii=False)


def main():
    plugin = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    version = plugin["version"]
    min_app = plugin.get("min_dispatcharr_version", "")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    ZIPS.mkdir(parents=True, exist_ok=True)
    zip_path = ZIPS / f"ticker-{version}.zip"
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
        for name in ROOT_FILES:
            z.write(ROOT / name, name)
        for name in LIB_FILES:
            z.write(ROOT / "lib" / name, name)
    shutil.copyfile(zip_path, ZIPS / "ticker-latest.zip")

    data = zip_path.read_bytes()
    md5, sha256, size = hashlib.md5(data).hexdigest(), hashlib.sha256(data).hexdigest(), len(data)

    # zips/ticker/manifest.json
    mpath = ZIPS / "manifest.json"
    m = json.loads(mpath.read_text(encoding="utf-8"))
    entry = {
        "version": version,
        "url": f"{BASE}/ticker-{version}.zip",
        "checksum_md5": md5,
        "checksum_sha256": sha256,
        "size": size,
        "last_updated": now,
        "build_timestamp": now,
    }
    latest = dict(entry, latest_url=f"{BASE}/ticker-latest.zip")
    if min_app:
        entry["min_dispatcharr_version"] = min_app
        latest["min_dispatcharr_version"] = min_app
    inner = m["manifest"]
    inner["last_updated"] = now
    inner["latest"] = latest
    inner["versions"] = [entry] + [v for v in inner.get("versions", []) if v.get("version") != version]
    m["generated_at"] = now
    mpath.write_text(dump(m), encoding="utf-8")

    # root manifest.json
    rpath = ROOT / "manifest.json"
    r = json.loads(rpath.read_text(encoding="utf-8"))
    p = r["manifest"]["plugins"][0]
    p.update(last_updated=now, latest_version=version, latest_md5=md5, latest_sha256=sha256, latest_size=size)
    if min_app:
        p["min_dispatcharr_version"] = min_app
    r["generated_at"] = now
    rpath.write_text(dump(r), encoding="utf-8")

    print(f"built {zip_path.name}: {size} bytes")
    print(f"md5    {md5}")
    print(f"sha256 {sha256}")
    with zipfile.ZipFile(zip_path) as z:
        print("files:", ", ".join(z.namelist()))
    return 0


if __name__ == "__main__":
    sys.exit(main())
