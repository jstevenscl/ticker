#!/usr/bin/env python3
"""Build a dev zip of Ticker for manual install on a test Dispatcharr (Import Plugin).

Usage: python scripts/build_dev_zip.py <dev-suffix> [out_dir]
  e.g. python scripts/build_dev_zip.py 20261117 -> ticker-<version>-dev.20261117.zip

Mirrors the release layout (8 flat files: lib/channels.json + lib/channel_aliases.json
copied to the zip root next to plugin.py) with the version rewritten to a prerelease so
the dev copy installs alongside the real one. Does NOT touch the repo.
"""
import json, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
FILES = ["ca_city_names.json", "eas_tone.wav", "eas_tone_ca.wav", "logo.png", "plugin.py"]
LIB = ["channels.json", "channel_aliases.json"]

def main():
    suffix = sys.argv[1]
    out_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else ROOT
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    base = manifest["version"].split("-")[0]
    parts = base.split(".")
    parts[-1] = str(int(parts[-1]) + 1).zfill(len(parts[-1]))   # next patch, keep zero-padding (0.5.06 -> 0.5.07)
    version = f"{'.'.join(parts)}-dev.{suffix}"
    manifest["version"] = version
    out = out_dir / f"ticker-{version}.zip"
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as z:
        for f in FILES:
            z.write(ROOT / f, f)
        for f in LIB:
            z.write(ROOT / "lib" / f, f)
        z.writestr("plugin.json", json.dumps(manifest, indent=2, ensure_ascii=False))
    print(out)
    print(version)

if __name__ == "__main__":
    main()
