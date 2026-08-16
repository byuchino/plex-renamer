"""Pure planning logic for plex-renamer.

Nothing in this module touches the filesystem beyond reading what it is handed.
It turns (a directory listing + the four user inputs) into a set of proposed
moves plus everything wrong with them. execute.py is the only module allowed to
act on the result.

Targets Python 3.8 (the deployment VM), so: typing.List/Dict/Optional rather
than builtin generics, no X | Y unions.
"""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

# Plex's fallback naming when it cannot identify an episode: a timestamp with
# space-separated time parts, e.g. "2026-06-22 23 00 00". Matched anywhere in
# the name so both the pipeline's output form
# ("Show (2026) - 2026-06-22 23 00 00.mp4") and the raw recording form
# ("Show (2026) - 2026-06-22 23 00 00 - Show.ts") are recognised.
RE_TIMECODE = re.compile(r"(?P<tc>\d{4}-\d{2}-\d{2} \d{2} \d{2} \d{2})")

# "Nagatan and Aoto (2026) {tvdb-428763}" -> name / year / source / id.
# Year and id are both optional and independent.
RE_SHOW_DIR = re.compile(
    r"^(?P<name>.+?)"
    r"(?:\s+\((?P<year>\d{4})\))?"
    r"(?:\s+\{(?P<source>tmdb|tvdb)-(?P<ident>[^}]+)\})?"
    r"\s*$"
)

RE_SEASON_DIR = re.compile(r"^Season\s+(?P<num>\d+)\s*$", re.IGNORECASE)

DEFAULT_ID_SOURCE = "tmdb"
DEFAULT_SEASON = 1

# Extensions this tool will consider renaming. Sidecars are out of scope in v1
# (README), and being explicit keeps stray files from being swept in.
VIDEO_SUFFIXES = {".mp4", ".mkv", ".ts", ".m4v", ".avi"}


def pad(n: int) -> str:
    """Zero-fill to at least 2 digits; wider only when genuinely needed, so a
    23-episode season stays E23 and a 105-episode run still renders."""
    return "{:02d}".format(n)


# ── Derivation: path -> sensible defaults ──────────────────────────────────

@dataclass
class Defaults:
    show_name: str
    year: Optional[str]
    ident: Optional[str]
    ident_source: str
    season: int
    season_dir_was_year: bool


def parse_show_dir(name: str) -> Tuple[str, Optional[str], Optional[str], Optional[str]]:
    """'Nagatan and Aoto (2026) {tvdb-428763}' -> (name, year, ident, source)."""
    m = RE_SHOW_DIR.match(name.strip())
    if not m:
        return name.strip(), None, None, None
    return (
        m.group("name").strip(),
        m.group("year"),
        m.group("ident"),
        m.group("source"),
    )


def parse_season_dir(name: str) -> Tuple[Optional[int], bool]:
    """'Season 03' -> (3, False). 'Season 2026' -> (None, True), because a
    4-digit number there is Plex's 'I don't know the season' marker, not a
    season 2026. Anything unrecognised -> (None, False)."""
    m = RE_SEASON_DIR.match(name.strip())
    if not m:
        return None, False
    num = m.group("num")
    if len(num) == 4:
        return None, True
    return int(num), False


def derive_defaults(season_dir: Path) -> Defaults:
    """Everything the form should be pre-filled with, from the path alone."""
    show_dir = season_dir.parent
    show_name, year, ident, source = parse_show_dir(show_dir.name)
    season, was_year = parse_season_dir(season_dir.name)
    return Defaults(
        show_name=show_name,
        year=year,
        ident=ident,
        # Radio follows whatever the folder actually carries; only fall back to
        # the configured default when the folder names no source at all.
        ident_source=source or DEFAULT_ID_SOURCE,
        season=season if season is not None else DEFAULT_SEASON,
        season_dir_was_year=was_year,
    )


def timecode_of(path: Path) -> Optional[str]:
    m = RE_TIMECODE.search(path.name)
    return m.group("tc") if m else None


def collect_files(entries: Sequence[Path]) -> Tuple[List[Path], List[Tuple[Path, str]]]:
    """Split a directory listing into renameable files and explicitly skipped
    ones. Skipped entries are returned with a reason rather than dropped — a
    file silently missing from the page is how you rename half a season and
    not notice."""
    renameable: List[Path] = []
    skipped: List[Tuple[Path, str]] = []
    for p in entries:
        if p.is_dir():
            continue
        if p.suffix.lower() not in VIDEO_SUFFIXES:
            skipped.append((p, "not a video file"))
        elif timecode_of(p) is None:
            skipped.append((p, "no Plex fallback timecode in the name"))
        else:
            renameable.append(p)
    # Sort by timecode, which is what the episode numbering walks. The timecode
    # format sorts correctly as a string (fixed-width, most-significant first).
    renameable.sort(key=lambda p: (timecode_of(p) or "", p.name))
    return renameable, skipped


# ── Planning ───────────────────────────────────────────────────────────────

@dataclass
class PlannedFile:
    source: Path
    episode: int
    target: Path
    issues: List[str] = field(default_factory=list)

    @property
    def target_name(self) -> str:
        return self.target.name

    @property
    def unchanged(self) -> bool:
        return self.source == self.target


@dataclass
class Plan:
    files: List[PlannedFile] = field(default_factory=list)
    skipped: List[Tuple[Path, str]] = field(default_factory=list)
    season_dir_target: Optional[Path] = None
    show_dir_target: Optional[Path] = None
    issues: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.issues and not any(f.issues for f in self.files)

    @property
    def moves(self) -> List[Tuple[Path, Path]]:
        return [(f.source, f.target) for f in self.files if not f.unchanged]

    def fingerprint(self) -> str:
        """Identifies exactly this set of moves. execute.py recomputes the plan
        and compares, so a directory that changed between the confirmation
        dialog and the Rename click is refused rather than half-applied."""
        h = hashlib.sha256()
        for src, dst in sorted((str(a), str(b)) for a, b in self.moves):
            h.update(src.encode("utf-8")); h.update(b"\0")
            h.update(dst.encode("utf-8")); h.update(b"\0")
        for d in (self.season_dir_target, self.show_dir_target):
            h.update((str(d) if d else "").encode("utf-8")); h.update(b"\0")
        return h.hexdigest()[:16]


def build_target_name(show_name: str, year: Optional[str], season: int,
                      episode: int, timecode: str, suffix: str) -> str:
    """'Nagatan and Aoto (2026) - S01E02 - 2026-06-23 17 00 00.mp4'.

    The year is part of the convention the transcode pipeline already writes,
    so files renamed here sit consistently alongside ones it produced. It is
    omitted entirely — rather than rendered as '()' — when unknown.
    """
    show = show_name.strip()
    head = "{} ({})".format(show, year) if year else show
    return "{} - S{}E{} - {}{}".format(head, pad(season), pad(episode), timecode, suffix)


def build_show_dir_name(show_name: str, year: Optional[str],
                        ident: Optional[str], ident_source: str) -> str:
    name = "{} ({})".format(show_name.strip(), year) if year else show_name.strip()
    if ident:
        name += " {{{}-{}}}".format(ident_source, ident)
    return name


def assign_episodes(files: Sequence[Path], anchors: Dict[str, int]) -> List[int]:
    """Episode number per file, in order.

    Numbering starts at 1 and increments. An anchor (an explicit pick for one
    file) sets that file's number and everything after it continues from there
    — which is what makes a single correction cascade forward while leaving
    earlier rows alone. The UI drops anchors on later files when a new pick is
    made, so the visible behaviour is 'edit a row, everything below re-derives'.
    """
    out: List[int] = []
    nxt = 1
    for p in files:
        ep = anchors.get(p.name, nxt)
        out.append(ep)
        nxt = ep + 1
    return out


def build_plan(season_dir: Path,
               entries: Sequence[Path],
               show_name: str,
               year: Optional[str],
               season: int,
               ident: Optional[str] = None,
               ident_source: str = DEFAULT_ID_SOURCE,
               anchors: Optional[Dict[str, int]] = None,
               rename_show_dir: bool = False) -> Plan:
    """The whole proposal, as data. Reads nothing from disk except the listing
    it is given, so it is fully testable without a media library."""
    plan = Plan()
    files, plan.skipped = collect_files(entries)

    show_dir = season_dir.parent
    # Files move into a correctly-named season folder rather than the folder
    # being renamed in place, so one 'Season YYYY' can later be split across
    # several real seasons (README).
    plan.season_dir_target = show_dir / "Season {}".format(pad(season))
    if rename_show_dir:
        plan.show_dir_target = show_dir.parent / build_show_dir_name(
            show_name, year, ident, ident_source)

    if not show_name.strip():
        plan.issues.append("Show name is empty.")
    if season < 0:
        plan.issues.append("Season must be zero or greater.")
    if not files:
        plan.issues.append("No files with a Plex fallback timecode in this folder.")

    dest_dir = plan.season_dir_target
    for path, episode in zip(files, assign_episodes(files, anchors or {})):
        tc = timecode_of(path) or ""
        name = build_target_name(show_name, year, season, episode, tc, path.suffix)
        pf = PlannedFile(source=path, episode=episode, target=dest_dir / name)
        if episode < 1:
            pf.issues.append("Episode must be 1 or greater.")
        plan.files.append(pf)

    _check_collisions(plan)
    return plan


def _check_collisions(plan: Plan) -> None:
    """Everything that would make this set of moves wrong, caught before
    anything moves — discovering it halfway through is how you end up with a
    half-renamed season."""
    # Duplicate episode numbers. Note these do NOT collide as filenames: the
    # timecode is part of the name, so two files can both be S01E04 and land
    # side by side quite happily. Plex is what breaks — it sees two files
    # claiming one episode. This is the check that actually fires in practice;
    # the target-path check below is defence for a naming scheme that drops
    # the timecode.
    by_episode: Dict[int, List[PlannedFile]] = {}
    for pf in plan.files:
        by_episode.setdefault(pf.episode, []).append(pf)
    for episode, group in by_episode.items():
        if len(group) > 1:
            for pf in group:
                others = [o.source.name for o in group if o is not pf]
                pf.issues.append(
                    "Episode {} is also used by {}".format(pad(episode), ", ".join(others)))

    seen: Dict[Path, PlannedFile] = {}
    for pf in plan.files:
        other = seen.get(pf.target)
        if other is not None:
            msg = "Same target as {}".format(other.source.name)
            pf.issues.append(msg)
            other.issues.append("Same target as {}".format(pf.source.name))
        else:
            seen[pf.target] = pf

    sources = {pf.source for pf in plan.files}
    for pf in plan.files:
        if pf.unchanged:
            continue
        # A target that exists is only a problem if it isn't one of the files
        # we are ourselves moving out of the way.
        if pf.target.exists() and pf.target not in sources:
            pf.issues.append("A file already exists at the target name.")
