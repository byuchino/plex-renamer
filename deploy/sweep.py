#!/usr/bin/env python3
"""Plan every season folder under every configured root and print the result.

This is the check that catches "proposes renaming the entire library". The
absolute counts are informative; the real test is running it before and after a
naming change and diffing the two outputs byte for byte.

Run it on the VM (the NAS roots are not mounted anywhere else):

    PYTHONPATH=. /opt/plex-renamer/venv/bin/python deploy/sweep.py \
        "/mnt/bama/volume1/TV Shows" "/mnt/bama/volume1/Videos/KIKU" > /tmp/sweep.txt

Baseline as of 2026-08-17: 498 no-op, 56 proposing changes (1106 files), 49 with
no renameable files.

It drives /api/plan through the Flask test client, so it exercises the same
derivation the page does — including the defaults, which is where a naming
change actually lands. Nothing is written: /api/plan cannot move a file.
"""
from __future__ import annotations

import sys
from pathlib import Path

from app import create_app
from config import Config


def season_dirs(root: Path):
    for show in sorted(p for p in root.iterdir() if p.is_dir()):
        for season in sorted(p for p in show.iterdir() if p.is_dir()):
            yield season


def main(argv):
    roots = [Path(a).resolve() for a in argv[1:]]
    if not roots:
        print("usage: sweep.py ROOT [ROOT...]", file=sys.stderr)
        return 2

    client = create_app(Config(roots=roots, undo_dir=Path("/tmp/sweep-undo"))).test_client()
    totals = {"noop": 0, "changes": 0, "nofiles": 0, "error": 0, "moves": 0}

    for root in roots:
        for season in season_dirs(root):
            data = client.post("/api/plan", json={"path": str(season)}).get_json()
            if data.get("error"):
                totals["error"] += 1
                print("ERROR   {}\n          {}".format(season, data["error"]))
                continue
            if not data["files"]:
                totals["nofiles"] += 1
                print("NOFILES {}".format(season))
                continue
            moves = [f for f in data["files"] if not f["unchanged"]]
            if not moves:
                totals["noop"] += 1
                print("NOOP    {}".format(season))
                continue
            totals["changes"] += 1
            totals["moves"] += len(moves)
            print("CHANGES {}  ({} of {})".format(season, len(moves), len(data["files"])))
            for f in moves:
                print("          {}\n       -> {}".format(f["source_name"], f["target_name"]))

    print("\n{noop} no-op, {changes} proposing changes ({moves} files), "
          "{nofiles} with no renameable files, {error} errors".format(**totals))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
