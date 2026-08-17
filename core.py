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

# Plex's own name for season 0, and what 29 of the 30 season-0 folders in this
# library are called (the other is 'Season 00'). Without this the word means
# nothing to the tool, DEFAULT_SEASON applies, and opening a Specials folder
# proposes renumbering S00 specials into season 1 *and* moving them out into
# 'Season 01' beside the real episodes.
RE_SPECIALS_DIR = re.compile(r"^Specials\s*$", re.IGNORECASE)
SPECIALS_DIR_NAME = "Specials"

# How the fallback form is written when explaining it to the user. The real
# folders carry a concrete year ('Season 2026'); this is the shape, for prose.
YEAR_FALLBACK_EXAMPLE = "Season YYYY"

# An already-episodic filename: "<head> - S01E04 - <tail>", or a multi-episode
# one, "<head> - S01E01-E02 - <tail>".
#
# The head is non-greedy so the FIRST episode marker wins, and the tail is
# taken as everything after it rather than by splitting on ' - ' — real tails
# contain their own dashes ("[Kioku] 1 Litre of Tears - 01"), and splitting
# would truncate them. A file with no tail at all ("Show - S01E04.mkv") is
# still episodic.
#
# Only the hyphenated '-E02' spelling of a span is recognised. Plex also reads
# 'S01E01E02' and 'S01E01-02', but neither is a form this library actually
# uses: of 9600 files, 58 carry '-E', none carry the bare 'E01E02', and the
# four '-02' ones are all in Star Wars The Clone Wars — the known duplicate-
# media folder. Recognising a spelling means re-rendering it in the canonical
# one, which is a rename of a file nobody asked about, so the other two forms
# stay in the tail and keep round-tripping byte for byte.
RE_EPISODIC = re.compile(
    r"^(?P<head>.+?)"
    r"\s+-\s+S(?P<season>\d{1,4})E(?P<episode>\d{1,4})"
    r"(?P<span>-E(?P<episode_end>\d{1,4}))?"
    r"(?P<rest>.*)$",
    re.IGNORECASE,
)

# Strips the separator off a captured rest for display purposes only.
RE_TAIL_SEP = re.compile(r"^\s*-\s*")

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
    season 2026. 'Specials' -> (0, False), Plex's name for season 0. Anything
    unrecognised -> (None, False)."""
    if RE_SPECIALS_DIR.match(name.strip()):
        return 0, False
    m = RE_SEASON_DIR.match(name.strip())
    if not m:
        return None, False
    num = m.group("num")
    if len(num) == 4:
        return None, True
    return int(num), False


def derive_defaults(season_dir: Path,
                    entries: Optional[Sequence[Path]] = None) -> Defaults:
    """Everything the form should be pre-filled with.

    The show name and year come from the folder, EXCEPT when the folder holds
    already-episodic files that all agree on a different head — then the files
    win. Real folders disagree with their contents ('Battlestar Galactica
    (2003)' full of 'Battlestar Galactica - S04E01 - …'), and defaulting to the
    folder there would open the page proposing to rename all 84 files. The
    files are the thing being renamed, so they set the baseline; the folder is
    only a fallback. Typing the year in still applies it to everything.
    """
    show_dir = season_dir.parent
    show_name, year, ident, source = parse_show_dir(show_dir.name)
    season, was_year = parse_season_dir(season_dir.name)

    heads = set()
    for p in entries or []:
        f = classify(p) if not p.is_dir() and p.suffix.lower() in VIDEO_SUFFIXES else None
        if f is not None and f.kind == EPISODIC:
            m = RE_EPISODIC.match(p.stem)
            heads.add(m.group("head").strip())
    if len(heads) == 1:
        file_name, file_year, _, _ = parse_show_dir(heads.pop())
        show_name = file_name
        year = file_year
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


TIMECODE = "timecode"
EPISODIC = "episodic"


@dataclass
class SourceFile:
    """One file on disk, decomposed into the parts a name is rebuilt from.

    `tail_raw` is everything after the episode marker *exactly as written*,
    separator and all: the timecode for a Plex fallback file, and whatever the
    existing name carries for an already episodic one — usually a release
    string ('720p.BluRay.x264.ShAaNiG'), not an episode title.

    It is kept byte-for-byte rather than cleaned up because real names contain
    double spaces, trailing spaces and dangling separators, and "tidying" them
    would propose renaming hundreds of files nobody asked about. `tail` is the
    readable version, for display only.
    """
    path: Path
    kind: str
    tail_raw: str
    season: Optional[int] = None
    episode: Optional[int] = None
    # Last episode in the file, for a multi-episode recording. None means the
    # file holds one episode.
    episode_end: Optional[int] = None

    @property
    def name(self) -> str:
        return self.path.name

    @property
    def span(self) -> int:
        """How many episodes this file holds. Always at least 1."""
        if self.episode is None or self.episode_end is None:
            return 1
        return max(1, self.episode_end - self.episode + 1)

    @property
    def tail(self) -> str:
        return RE_TAIL_SEP.sub("", self.tail_raw).strip()


def classify(path: Path) -> Optional[SourceFile]:
    """Decompose a filename, or None if it is neither form we can rename.

    Episodic is tried first: a file can carry both an episode marker and a
    date-like string in its tail, and the marker is the stronger signal.
    """
    m = RE_EPISODIC.match(path.stem)
    if m:
        episode = int(m.group("episode"))
        rest = m.group("rest") or ""
        end = int(m.group("episode_end")) if m.group("episode_end") else None
        if end is not None and end <= episode:
            # 'S01E05-E02' is not a span, it is a name we do not understand.
            # Hand the text back to the tail so it is reproduced verbatim
            # rather than silently "corrected" into something else.
            rest = m.group("span") + rest
            end = None
        return SourceFile(
            path=path,
            kind=EPISODIC,
            tail_raw=rest,
            season=int(m.group("season")),
            episode=episode,
            episode_end=end,
        )
    tc = timecode_of(path)
    if tc is not None:
        return SourceFile(path=path, kind=TIMECODE, tail_raw=" - " + tc)
    return None


def sort_key(f: SourceFile):
    """One ordering over a mixed folder, because the episode cascade has to
    walk a single well-defined sequence.

    Already-numbered files come first in episode order, then the undated
    recordings in time order — so timecode files continue after the episodes
    that already exist rather than colliding with them from E01.
    """
    if f.kind == EPISODIC:
        return (0, f.season or 0, f.episode or 0, f.name)
    return (1, 0, 0, f.tail + f.name)


def collect_files(entries: Sequence[Path]) -> Tuple[List[SourceFile], List[Tuple[Path, str]]]:
    """Split a directory listing into renameable files and explicitly skipped
    ones. Skipped entries are returned with a reason rather than dropped — a
    file silently missing from the page is how you rename half a season and
    not notice."""
    renameable: List[SourceFile] = []
    skipped: List[Tuple[Path, str]] = []
    for p in entries:
        if p.is_dir():
            continue
        if p.suffix.lower() not in VIDEO_SUFFIXES:
            skipped.append((p, "not a video file"))
            continue
        f = classify(p)
        if f is None:
            skipped.append((p, "no S00E00 marker and no Plex fallback timecode"))
        else:
            renameable.append(f)
    renameable.sort(key=sort_key)
    return renameable, skipped


# ── Planning ───────────────────────────────────────────────────────────────

@dataclass
class PlannedFile:
    source: Path
    episode: int
    target: Path
    kind: str = TIMECODE
    issues: List[str] = field(default_factory=list)
    # Advisory, per row. Deliberately absent from Plan.ok: a warning is shown
    # and the run still goes ahead, which is what separates it from an issue.
    warnings: List[str] = field(default_factory=list)
    # Inclusive last episode. Equal to `episode` for a single-episode file, so
    # everything downstream can treat a row as a range without special-casing.
    episode_end: Optional[int] = None

    def __post_init__(self):
        if self.episode_end is None:
            self.episode_end = self.episode

    @property
    def episodes(self) -> range:
        return range(self.episode, self.episode_end + 1)

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
    # Set only in the opt-in cheap path: the season folder itself is renamed to
    # this, after its files have been renamed in place inside it. When it is
    # set, season_dir_target is the folder as browsed — so every file target is
    # a rename within one directory and nothing crosses a boundary.
    season_dir_rename_to: Optional[Path] = None
    show_dir_target: Optional[Path] = None
    issues: List[str] = field(default_factory=list)
    # Non-blocking observations. Unlike issues, these never prevent a rename.
    notes: List[str] = field(default_factory=list)

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
        # season_dir_rename_to is part of the identity even though it never
        # changes a file target: the two paths to the same end state are
        # different op sets, and confirming one must not execute the other.
        for d in (self.season_dir_target, self.season_dir_rename_to,
                  self.show_dir_target):
            h.update((str(d) if d else "").encode("utf-8")); h.update(b"\0")
        return h.hexdigest()[:16]


def build_target_name(show_name: str, year: Optional[str], season: int,
                      episode: int, tail_raw: str, suffix: str,
                      episode_end: Optional[int] = None) -> str:
    """'Nagatan and Aoto (2026) - S01E02 - 2026-06-23 17 00 00.mp4'.

    The year is part of the convention the transcode pipeline already writes,
    so files renamed here sit consistently alongside ones it produced. It is
    omitted entirely — rather than rendered as '()' — when unknown.

    A file holding two consecutive episodes renders as 'S01E01-E02', which is
    how Plex reads a multi-episode file. A span of one renders plain: never
    'S01E01-E01', which Plex would accept but which would rename every ordinary
    file in the library.

    `tail_raw` carries its own separator and is appended verbatim, making this
    the exact inverse of RE_EPISODIC — which is what lets an already-correct
    episodic file propose itself unchanged rather than being "normalised" into
    a slightly different name.
    """
    show = show_name.strip()
    head = "{} ({})".format(show, year) if year else show
    marker = "S{}E{}".format(pad(season), pad(episode))
    if episode_end is not None and episode_end > episode:
        marker += "-E{}".format(pad(episode_end))
    return "{} - {}{}{}".format(head, marker, tail_raw, suffix)


def season_dir_name(season: int) -> str:
    """What to call a season folder this tool has to create.

    Season 0 becomes 'Specials' rather than 'Season 00' to match the library's
    own convention (29 `Specials` to 1 `Season 00`); Plex reads both. An
    existing folder that already denotes the requested season is never renamed
    either way, so the one real `Season 00` folder here stays as it is.
    """
    if season == 0:
        return SPECIALS_DIR_NAME
    return "Season {}".format(pad(season))


def season_rename_state(season_dir_name_: str,
                        season: int,
                        target_exists: bool) -> Tuple[bool, str]:
    """Whether the season folder may be renamed instead of emptied, and why not.

    Pure by design: `target_exists` arrives as an argument rather than being
    read off the disk here, so build_plan's "reads nothing except the listing it
    is given" property survives and every branch below is testable without a
    media library. app.py supplies the filesystem fact.

    The reason is shown in place of the hidden checkbox. A control that is inert
    on most folders trains the eye to skip it; a sentence saying which condition
    failed is the part that was actually worth showing.
    """
    current, was_year = parse_season_dir(season_dir_name_)
    target = season_dir_name(season)
    if current is not None and current == season:
        return False, ("Files are renamed where they are — no folder move is "
                       "involved, so there is nothing to make cheaper.")
    if not was_year:
        return False, ("Only a '{}' folder is renamed. Renaming '{}' would move "
                       "a folder you did not ask about.".format(
                           YEAR_FALLBACK_EXAMPLE, season_dir_name_))
    if target_exists:
        return False, ("'{}' already exists, so this folder cannot be renamed "
                       "onto it. Files move into it instead.".format(target))
    return True, ""


def build_show_dir_name(show_name: str, year: Optional[str],
                        ident: Optional[str], ident_source: str) -> str:
    name = "{} ({})".format(show_name.strip(), year) if year else show_name.strip()
    if ident:
        name += " {{{}-{}}}".format(ident_source, ident)
    return name


def assign_episodes(files: Sequence[SourceFile], anchors: Dict[str, int],
                    per_episode: int = 1,
                    episodes_per_file: int = 1) -> List[Tuple[int, int]]:
    """The (first, last) episode each file covers, in order.

    Numbering starts at 1 and increments. An anchor (an explicit pick for one
    file) sets that file's number and everything after it continues from there
    — which is what makes a single correction cascade forward while leaving
    earlier rows alone. The UI drops anchors on later files when a new pick is
    made, so the visible behaviour is 'edit a row, everything below re-derives'.

    per_episode is how many consecutive files share one episode number before
    it advances. It exists because most Hawaii series are broadcast twice — the
    original and a re-broadcast the next day — and Plex records both, so a
    16-file folder is routinely 8 episodes rather than 16. Setting it to 2
    produces 1,1,2,2,3,3… and turns eight manual corrections into one control.

    An anchor always starts a fresh group, so an irregular run (a week where
    the re-broadcast was missed, which does happen) is fixed by anchoring at
    the point it breaks rather than by correcting every row after it.

    A file that already carries an S00E00 marker is an *implicit* anchor: its
    number comes from its own name and holds. That is what makes a mixed
    folder work — four new recordings dropped beside an existing E01-E10 get
    E11 onward instead of restarting at E01. per_episode groups only the
    undated run, since numbered files already state their own episode.

    episodes_per_file is the inverse control: how many consecutive episodes one
    recording holds, for a broadcast that airs a double episode as a single
    programme. It widens each row into a span and advances the counter by the
    span, so two double-episode recordings are E01-E02 and E03-E04 rather than
    E01 and E02. The two controls compose — a double episode that is also
    re-broadcast the next day is four files reading E01-E02, E01-E02, E03-E04,
    E03-E04, and both shapes exist in this library.

    A file that already carries its own span keeps it, and the counter advances
    past the whole span. Without that, the file after an existing 'S01E01-E02'
    would be numbered E02: the implicit anchor would be off by one for every
    row below a double episode.
    """
    per_episode = max(1, per_episode)
    episodes_per_file = max(1, episodes_per_file)
    out: List[Tuple[int, int]] = []
    current = 1
    span = episodes_per_file
    used = 0
    for f in files:
        explicit = f.name in anchors
        if explicit:
            current = anchors[f.name]
            span = episodes_per_file
            used = 0
        elif f.kind == EPISODIC and f.episode is not None:
            current = f.episode
            span = f.span
            used = 0
        else:
            span = episodes_per_file
        out.append((current, current + span - 1))
        used += 1
        # A numbered file never shares its number with the next file by way of
        # grouping; only the undated run does that.
        if used >= per_episode or (f.kind == EPISODIC and not explicit):
            current += span
            used = 0
    return out


def check_override(name: str, original_suffix: str,
                   source_tail: str = "") -> Tuple[List[str], List[str]]:
    """What is wrong with a hand-typed filename: (blocking, advisory).

    The page lets a row's proposed name be edited directly, so these strings
    arrive from a browser and are used to build a destination path. A name
    containing a separator would silently write outside the season folder,
    which is the one thing root confinement cannot catch — the path would be
    built from a trusted parent and an untrusted leaf.

    The tail check is advisory, not blocking, and that is deliberate. Every
    derived name keeps the source tail byte-for-byte, and a hand-typed name is
    the ONLY place in the tool where someone retypes those bytes — a mistyped
    timecode in an otherwise well-formed name is accepted by every other check
    here and lands on disk silently. But retitling is legitimate (Plex reads
    'S01E01 - Ambush' and the library already contains that form), so dropping
    the tail cannot be an error. Saying so out loud is the whole fix.

    Advisory findings are suppressed when the name is rejected outright: the
    derived name is used in that case, so an observation about the typed text
    would be describing something that is not going to be applied.
    """
    issues: List[str] = []
    stripped = name.strip()
    if not stripped:
        issues.append("Name is empty.")
        return issues, []
    if "/" in name or "\\" in name or "\0" in name:
        issues.append("Name cannot contain a path separator.")
    if stripped in (".", ".."):
        issues.append("Name is not a filename.")
    if Path(stripped).suffix.lower() != original_suffix.lower():
        # Changing .mp4 to .mkv renames the container without converting it,
        # leaving a file that lies about its own format to Plex.
        issues.append("Extension must stay {}.".format(original_suffix))
    if issues:
        return issues, []

    warnings: List[str] = []
    if source_tail and source_tail not in stripped:
        warnings.append(
            "Does not keep the original '{}' — check for a typo.".format(
                source_tail.strip()))
    return issues, warnings


def build_plan(season_dir: Path,
               entries: Sequence[Path],
               show_name: str,
               year: Optional[str],
               season: int,
               ident: Optional[str] = None,
               ident_source: str = DEFAULT_ID_SOURCE,
               anchors: Optional[Dict[str, int]] = None,
               per_episode: int = 1,
               episodes_per_file: int = 1,
               rename_show_dir: bool = False,
               rename_season_dir: bool = False,
               name_overrides: Optional[Dict[str, str]] = None) -> Plan:
    """The whole proposal, as data. Reads nothing from disk except the listing
    it is given, so it is fully testable without a media library.

    name_overrides maps a source filename to a hand-typed target name, for the
    rows the user edited directly. Overridden rows are validated and collision
    checked exactly like derived ones — the override changes what the name is,
    never whether it is checked.
    """
    plan = Plan()
    files, plan.skipped = collect_files(entries)

    show_dir = season_dir.parent
    # Files move into a correctly-named season folder rather than the folder
    # being renamed in place, so one 'Season YYYY' can later be split across
    # several real seasons (README).
    #
    # But if the folder ALREADY denotes the requested season, keep it exactly
    # as it is. Real libraries use both 'Season 1' and 'Season 01' (both are
    # common here), and always targeting the padded form would propose moving
    # every file out of a perfectly good 'Season 1' into a second folder,
    # splitting the season in two.
    #
    # Unless the cheap path is opted into: then the folder IS renamed, files are
    # renamed in place inside it, and nothing crosses a directory boundary — the
    # sync propagates that as a true rename rather than a full re-upload. Only
    # the year-fallback form qualifies, so the splitting case above still holds
    # for every folder that is already named after a real season.
    current_season, was_year = parse_season_dir(season_dir.name)
    if current_season is not None and current_season == season:
        plan.season_dir_target = season_dir
    elif rename_season_dir and was_year:
        plan.season_dir_target = season_dir
        plan.season_dir_rename_to = show_dir / season_dir_name(season)
    else:
        plan.season_dir_target = show_dir / season_dir_name(season)
    if rename_show_dir:
        plan.show_dir_target = show_dir.parent / build_show_dir_name(
            show_name, year, ident, ident_source)

    if not show_name.strip():
        plan.issues.append("Show name is empty.")
    if season < 0:
        plan.issues.append("Season must be zero or greater.")
    if not files:
        plan.issues.append("No renameable video files in this folder.")

    overrides = name_overrides or {}
    dest_dir = plan.season_dir_target
    mismatched_seasons = set()
    spans = assign_episodes(files, anchors or {}, per_episode, episodes_per_file)
    for f, (episode, episode_end) in zip(files, spans):
        path = f.path
        name = build_target_name(show_name, year, season, episode, f.tail_raw,
                                 path.suffix, episode_end)
        override_issues: List[str] = []
        override_warnings: List[str] = []
        if path.name in overrides:
            typed = overrides[path.name]
            override_issues, override_warnings = check_override(
                typed, path.suffix, f.tail_raw)
            # Keep the derived name when the typed one is unusable, so the row
            # still shows a sane target next to the reason it was rejected.
            if not override_issues:
                name = typed.strip()
        pf = PlannedFile(source=path, episode=episode, target=dest_dir / name,
                         kind=f.kind, episode_end=episode_end)
        pf.issues.extend(override_issues)
        pf.warnings.extend(override_warnings)
        if episode < 1:
            pf.issues.append("Episode must be 1 or greater.")
        plan.files.append(pf)
        if f.kind == EPISODIC and f.season is not None and f.season != season:
            mismatched_seasons.add(f.season)

    # A file that says S02 in a folder being planned as season 1 is reported,
    # never silently renumbered — it is as likely to be a misfiled recording
    # as a wrong season box, and only the user knows which.
    for bad in sorted(mismatched_seasons):
        plan.notes.append(
            "{} file(s) are named S{} but this plan writes S{}".format(
                sum(1 for x in files if x.kind == EPISODIC and x.season == bad),
                pad(bad), pad(season)))

    _check_collisions(plan)
    return plan


# Parking name for a rename cycle. Kept in the same directory as the file it
# stands in for, so the temporary move cannot cross a filesystem boundary.
TMP_SUFFIX = ".plex-renamer-tmp-{}"


def order_moves(moves: Sequence[Tuple[Path, Path]]) -> List[Tuple[Path, Path]]:
    """Sequence a set of renames so no move ever lands on a file that has not
    been moved out of the way yet.

    Renumbering a run of episodes produces chains — E02 becomes E01 while E03
    becomes E02 — and applying those in listing order would overwrite E01 with
    E02 and lose a file. A target that is also somebody's source therefore has
    to wait for that somebody to move first.

    A true cycle (two files swapping names, which needs identical tails and so
    only happens between already-episodic files) cannot be ordered at all, and
    is broken by parking one file under a temporary name and moving it on at
    the end. Both halves are recorded as real moves so the undo manifest can
    retrace them.

    Pure: this decides an order, it does not move anything.
    """
    remaining = list(moves)
    pending = set(src for src, _ in remaining)
    out: List[Tuple[Path, Path]] = []
    parked = 0
    while remaining:
        ready = [m for m in remaining if m[1] not in pending]
        if not ready:
            src, dst = remaining[0]
            tmp = src.with_name(src.name + TMP_SUFFIX.format(parked))
            parked += 1
            out.append((src, tmp))
            # src is now free for whoever was waiting on it, and the second half
            # of the parked move continues to compete for its own target.
            remaining[0] = (tmp, dst)
            pending.discard(src)
            continue
        for m in ready:
            out.append(m)
            remaining.remove(m)
            pending.discard(m[0])
    return out


def _check_collisions(plan: Plan) -> None:
    """Everything that would make this set of moves wrong, caught before
    anything moves — discovering it halfway through is how you end up with a
    half-renamed season."""
    # Several files on one episode is NORMAL here, not an error: most Hawaii
    # series air an episode and re-broadcast it the next day, and Plex records
    # both. The two recordings are the same episode, distinguished only by
    # their timecodes — which stay in the filename, so they never collide on
    # disk. Reported as a neutral note so the grouping is visible while
    # numbering a season, and deliberately not as an issue: blocking here
    # would reject the correct plan for an entire library.
    #
    # A multi-episode file counts against every episode it covers, so a partial
    # overlap ('E01-E02' beside 'E02-E03') is as visible as an exact duplicate.
    # Still a note: the timecodes differ, so nothing collides on disk.
    by_episode: Dict[int, List[PlannedFile]] = {}
    for pf in plan.files:
        for ep in pf.episodes:
            by_episode.setdefault(ep, []).append(pf)
    for episode in sorted(by_episode):
        group = by_episode[episode]
        if len(group) > 1:
            plan.notes.append("Episode {} has {} files".format(pad(episode), len(group)))

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
