#!/usr/bin/env python3
"""Build a dev zip of Ticker for manual install on a test Dispatcharr (Import Plugin).

Usage: python scripts/build_dev_zip.py <dev-suffix> [out_dir] [--manifest N] [--caps a,b,c] [--async action,action] [--version X.Y.Z]
  e.g. python scripts/build_dev_zip.py 20261117 -> ticker-<version>-dev.20261117.zip
       python scripts/build_dev_zip.py v2a out --manifest 2 --caps outbound_network,filesystem_write
  --manifest / --caps / --async override manifest_version / capabilities / mark actions
  "async": true in the zip's plugin.json only (the repo's plugin.json is never touched) so v2 sandbox trials are one command.

Mirrors the release layout (8 flat files: lib/channels.json + lib/channel_aliases.json
copied to the zip root next to plugin.py) with the version rewritten to a prerelease so
the dev copy installs alongside the real one. Does NOT touch the repo.
"""
import json, re, sys, zipfile
from pathlib import Path

ROOT = Path(__file__).parent.parent
VERSION_RE = re.compile(r"^\d+\.\d+\.(0|0[1-9]|[1-9]\d+)(-[0-9A-Za-z.]+)?$")


def check_version(v, what="version"):
    """Ticker versions are #.#.## (two-digit patch: 0.5.06, 0.6.01); the only exception is a
    patch of zero, written as a single 0 (0.6.0). Reject anything else, e.g. 0.6.1."""
    if not VERSION_RE.match(v):
        sys.exit(f"ERROR: {what} '{v}' is not in Ticker's #.#.## format "
                 f"(two-digit patch such as 0.6.01; a zero patch is a single 0 as in 0.6.0, never 00).")
    return v


def next_patch(base):
    """0.6.0 -> 0.6.01, 0.5.06 -> 0.5.07 (two-digit patch; only a zero patch is a single 0)."""
    major, minor, patch = base.split(".")
    return f"{major}.{minor}.{int(patch) + 1:02d}"

FILES = ["ca_city_names.json", "eas_tone.wav", "eas_tone_ca.wav", "logo.png", "plugin.py"]
LIB = ["channels.json", "channel_aliases.json"]

def main():
    args = sys.argv[1:]
    opts = {}
    for flag in ("--manifest", "--caps", "--async", "--version"):
        if flag in args:
            i = args.index(flag)
            opts[flag] = args[i + 1]
            del args[i:i + 2]
    suffix = args[0]
    out_dir = Path(args[1]) if len(args) > 1 else ROOT
    manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
    base = manifest["version"].split("-")[0]
    version = f"{next_patch(base)}-dev.{suffix}"    # next patch in #.#.## form (0.6.0 -> 0.6.01-dev.x)
    if "--version" in opts:
        version = opts["--version"]          # exact version (e.g. for documentation screenshots)
    check_version(version, "dev build version")
    manifest["version"] = version
    if "--manifest" in opts:
        manifest["manifest_version"] = int(opts["--manifest"])
    if "--async" in opts:
        wanted = set(opts["--async"].split(","))
        for a in manifest["actions"]:
            if a["id"] in wanted:
                a["async"] = True
    if "--caps" in opts:
        manifest["capabilities"] = [c for c in opts["--caps"].split(",") if c]
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
