#!/usr/bin/env python3
"""Print what a rename would do. Writes nothing, ever.

This exists so the planning logic can be pointed at the real library and
inspected before any code capable of moving a file is written (Phase 1 of the
README's build plan).

    ./cli.py "/mnt/bama/volume1/TV Shows/Nagatan and Aoto (2026) {tvdb-428763}/Season 2026" \
        --root "/mnt/bama/volume1/TV Shows" --season 1
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path
from typing import Dict, List

import core
from config import OutsideRoots, load_config, resolve_within_roots


def parse_episode_anchors(values: List[str]) -> Dict[str, int]:
    anchors: Dict[str, int] = {}
    for v in values or []:
        if "=" not in v:
            raise SystemExit("--episode expects FILENAME=N, got: {}".format(v))
        name, _, num = v.rpartition("=")
        try:
            anchors[name] = int(num)
        except ValueError:
            raise SystemExit("--episode expects an integer, got: {}".format(num))
    return anchors


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Dry-run a Plex rename. Writes nothing.")
    p.add_argument("directory", help="season folder to inspect")
    p.add_argument("--config", help="config.ini path (default: $PLEX_RENAMER_CONFIG or /etc/plex-renamer/config.ini)")
    p.add_argument("--root", action="append",
                   help="allowed root, repeatable; bypasses the config file when given")
    p.add_argument("--show", help="override the derived show name")
    p.add_argument("--year", help="override the derived year ('' to omit it)")
    p.add_argument("--season", type=int, help="override the derived season")
    p.add_argument("--id", dest="ident", help="override the derived TMDB/TVDB id")
    p.add_argument("--id-source", choices=("tmdb", "tvdb"), help="override the id source")
    p.add_argument("--episode", action="append", metavar="FILENAME=N",
                   help="anchor one file to an episode number; later files follow on")
    p.add_argument("--per-episode", type=int, default=1, metavar="N",
                   help="how many consecutive recordings share one episode number "
                        "(2 for a series that is re-broadcast the next day)")
    p.add_argument("--episodes-per-file", type=int, default=1, metavar="N",
                   help="how many consecutive episodes one recording holds "
                        "(2 renders S01E01-E02); composes with --per-episode")
    p.add_argument("--rename-show-dir", action="store_true",
                   help="also propose renaming the show folder from show/year/id")
    return p


def main(argv=None) -> int:
    args = build_parser().parse_args(argv)

    if args.root:
        roots = [Path(r).resolve() for r in args.root]
    else:
        try:
            roots = load_config(args.config).roots
        except FileNotFoundError as e:
            print("error: {}\nPass --root to run without a config file.".format(e),
                  file=sys.stderr)
            return 2

    try:
        season_dir = resolve_within_roots(args.directory, roots)
    except OutsideRoots as e:
        print("error: {}".format(e), file=sys.stderr)
        return 2
    if not season_dir.is_dir():
        print("error: not a directory: {}".format(season_dir), file=sys.stderr)
        return 2

    defaults = core.derive_defaults(season_dir)
    show_name = args.show if args.show is not None else defaults.show_name
    year = args.year if args.year is not None else defaults.year
    year = year or None
    season = args.season if args.season is not None else defaults.season
    ident = args.ident if args.ident is not None else defaults.ident
    ident_source = args.id_source or defaults.ident_source

    plan = core.build_plan(
        season_dir=season_dir,
        entries=sorted(season_dir.iterdir()),
        show_name=show_name,
        year=year,
        season=season,
        ident=ident,
        ident_source=ident_source,
        anchors=parse_episode_anchors(args.episode),
        per_episode=args.per_episode,
        episodes_per_file=args.episodes_per_file,
        rename_show_dir=args.rename_show_dir,
    )

    print("DRY RUN — nothing is written.\n")
    print("Folder:      {}".format(season_dir))
    print("Show name:   {}".format(show_name))
    print("Year:        {}".format(year or "(none)"))
    print("ID:          {}".format(
        "{}-{}".format(ident_source, ident) if ident else "(none)"))
    print("Season:      {}{}".format(
        core.pad(season),
        "  (folder is a year — defaulted)" if defaults.season_dir_was_year else ""))
    print("Files go to: {}".format(plan.season_dir_target))
    if plan.show_dir_target:
        print("Show folder: {}".format(plan.show_dir_target))
    print()

    if plan.files:
        width = max(len(f.source.name) for f in plan.files)
        for f in plan.files:
            print("  {:<{w}}  ->  {}".format(f.source.name, f.target.name, w=width))
            for issue in f.issues:
                print("  {:<{w}}      !! {}".format("", issue, w=width))
        print()

    for path, why in plan.skipped:
        print("  skipped: {}  ({})".format(path.name, why))
    if plan.skipped:
        print()

    for note in plan.notes:
        print("  note: {}".format(note))
    if plan.notes:
        print()

    for issue in plan.issues:
        print("  !! {}".format(issue))

    print("{} file(s) to rename, {} skipped, {}".format(
        len(plan.moves), len(plan.skipped),
        "plan is valid" if plan.ok else "PLAN HAS PROBLEMS"))
    print("fingerprint: {}".format(plan.fingerprint()))
    return 0 if plan.ok else 1


if __name__ == "__main__":
    sys.exit(main())
