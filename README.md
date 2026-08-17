# plex-renamer

A small web tool for bulk-renaming Plex recordings that Plex could not match to an
episode, and which therefore landed in its timecode fallback layout:

```
Show Name (2026) {tvdb-428763}/
  Season 2026/                                  <- 4-digit year = Plex gave up
    Show Name (2026) - 2026-06-22 23 00 00.mp4  <- timecode instead of S01E01
```

You point it at a season folder, tell it the season number, adjust episode numbers,
and it renames the files into the standard Plex layout.

## Why these files exist

They come out of the [plex-auto-transcode](https://github.com/byuchino/plex-auto-transcode)
pipeline, which transcodes Plex DVR recordings. When a recording is a **re-broadcast**,
its air date matches no TVDB episode, and Plex's own EPG supplies no episode title —
so there is genuinely nothing to correlate against automatically. Confirmed against
live TVDB data: for one series, every recording postdated the last original air date
by months. Human judgement is the only way to map these, hence this tool.

## Why this is a separate project from plex-auto-transcode

Considered and deliberately rejected as a same-repo addition:

- **Different naming convention.** The pipeline writes
  `Show (Year) - S01E04 - Episode Title.mp4` via `build_output_path()`. This tool
  writes `Show (Year) - S01E04 - <original timecode>.mp4`. Not the same function,
  and not a function that should grow a second mode.
- **Different metadata source.** Defaults to TMDB ids; the pipeline is TVDB-only.
- **Different inputs.** Browses arbitrary directories rather than reading
  `[library.*]` config, and never touches the pipeline's `jobs.db`.
- **Blast radius.** A library-curation bug must not be able to break unattended
  transcoding.

Shared code would amount to one regex for `{tvdb-nnnnn}`.

**Accepted consequence:** renaming a file makes `output_path` stale on the transcode
dashboard's historical `done` row. That is cosmetic staleness in a historical record,
deliberately preferred over giving a general-purpose renamer write access to `jobs.db`.

## Spec (v1)

**Target filename:** `<Show Name> (<Year>) - S<nn>E<yy> - <timecode>.<ext>`
— timecode verbatim from the original name, extension preserved from the original.

**Four inputs**, all defaulted from the browsed path:

| Input | Default source |
|---|---|
| Show Name | show folder, stripped of `(Year)` and `{id}` |
| Year | show folder |
| ID + TMDB/TVDB radio | `{tmdb-…}` / `{tvdb-…}` in the show folder; blank + TMDB when absent |
| Season | season folder — `Specials` is season 0; a 4-digit year means Plex gave up, so `01` |

**Airings per episode.** Most Hawaii series are broadcast twice — the original and
a re-broadcast the next day — and Plex records both, so a 16-file folder is routinely
8 episodes rather than 16. A per-episode control sets how many consecutive recordings
share one number (`2` gives 1,1,2,2,3,3…). **Several files on one episode is normal and
never blocks a rename**; they cannot clash on disk because the timecode stays in the
filename. It is reported as a neutral note so the grouping stays visible.

Real folders are not always uniform: one series pairs every week (16 files, 8 episodes,
zero corrections needed), another airs weekly singles for a while before pairing starts.
An anchor always begins a fresh group, so a change of rhythm costs one pick rather than
a correction on every row after it.

**Already-episodic and mixed folders.** A file carrying an `S00E00` marker is
recognised too, and its episode number comes from its own name. Everything after the
marker — usually a release string (`720p.BluRay.x264.ShAaNiG`), not an episode title —
is preserved **byte for byte**, including double spaces, trailing spaces and dangling
separators. They look like defects, but tidying them would propose renaming hundreds of
files nobody asked about. Verified against 60 real season folders: 58 propose no change
at all, and the only one that does is a genuine fix (`S06e01` → `S06E01`).

For the same reason the show name and year default **from the files** when a folder's
episodic contents all agree on a head that differs from the folder name — a real
`Battlestar Galactica (2003)` folder is full of `Battlestar Galactica - S04E01 - …`, and
defaulting to the folder there would open the page proposing to rename all 84 files.
The folder is the fallback, used when the files disagree with each other.

In a **mixed** folder both kinds appear in one table, ordered numbered-files-first by
episode and then undated recordings by timecode. Numbered files are *implicit anchors*:
they hold their own number, and the undated run continues after them — four new
recordings beside an existing E01–E03 become E04+, not a second E01. `per_episode`
groups only the undated run. An explicit pick on a numbered file moves that one file and
does not cascade through later numbered ones, since those carry their own numbers and
renumbering a whole run off one pick would destroy deliberate gaps.

A file whose parsed season disagrees with the season being written is **noted, never
silently renumbered** — it is as likely to be a misfiled recording as a wrong season box.

**Season folder:** if the current folder already denotes the requested season it is left
exactly as named. Both `Season 1` and `Season 01` are common in a real library (116 and
55 of them here), and always targeting the padded form would move every file out of a
perfectly good `Season 1` into a second folder, splitting the season in two.

`Specials` counts as season 0 — Plex's own name for it, and what 29 of the 30 season-0
folders here are called (the other is `Season 00`, which is equally valid and equally left
alone). Without that, the word means nothing to the tool, the season defaults to 1, and
opening a Specials folder proposes renumbering `S00E01` into `S01E01` **and** moving the
files out into `Season 01` beside the real episodes — a valid, executable plan, in 27 of
these folders. A season-0 folder that has to be *created* is named `Specials` to match.

**Table**, one row per matching file, sorted by timecode. Each row shows the editable
proposed name and an **Episode** button opening a selectable matrix. Episodes start at
`01` and increment; picking a value for a row **cascades forward** to later rows and
leaves earlier rows unchanged. Changing any top input recomputes every row.

**Season folder:** files are **moved into `Season <nn>`** (created if needed), rather
than renaming the folder in place — so one year folder can later be split across
seasons. An emptied `Season YYYY` is removed; if anything remains — a poster, a skipped
file — it is left alone. Only the year-fallback form is cleaned up: an emptied
`Season 1` stays, because removing a folder the user did not ask about is the greater
surprise.

**Show folder rename is opt-in** via checkbox. When ticked the folder becomes
`<Show Name> (<Year>) {id}` from all three inputs. Opt-in specifically so a typo in
Show Name cannot silently rename a directory containing other seasons not visible on
the page.

**Safety:**

- Pre-flight validation of the whole set — duplicate targets, existing destinations,
  root confinement, unwritable paths. Rename stays disabled while any row is invalid.
  Writability is tested by **writing** a probe dot-file, not by asking `os.access`: both
  libraries are NFS exports, and this DSM server answers `access(2)` itself, reporting
  `W_OK` false for directories it then happily accepts writes to.
- The set is explicitly **not atomic**: validate all up front, execute, report
  per-file results.
- Confirmation dialog listing every old→new pair, guarded by a plan hash so the
  directory cannot change between confirming and executing.
- **Undo manifest** written before the first move.
- **History**, in the page and always visible, because a run has to be reachable after
  you have navigated away from the folder it touched — otherwise closing the result
  dialog is the last chance to undo it. It is not a second record: it is the manifests
  read back, so it cannot disagree with what undo will actually do. Each entry expands to
  every operation and its outcome, and carries the inputs the run was made with.
- **A refused run leaves no manifest** and so no history entry, deliberately — "a
  manifest exists" has to keep meaning "something happened". Refusals go to the log, at
  WARNING with their reasons, which is the only place "why will it not rename this" can
  be answered. `/api/plan` is logged at DEBUG only: it fires on every keystroke.
- **Retention** via `keep_runs` (default 200, `0` for unlimited). Pruning is strictly
  oldest-first and ignores whether a run was undone — once a run is 200 runs old,
  offering to reverse it is worse than forgetting it.
- Files not matching the fallback pattern are listed as **skipped, never hidden**.
- Navigation confined to configured roots, resolved with `realpath` so `..` cannot
  escape.

**Out of scope in v1:** sidecar files (.srt/.nfo/artwork), any TMDB/TVDB API calls
(episode matrix is 1–40 with direct typing), authentication, multi-directory batch
runs. Plex rescan after renaming is a manual step.

**Watched state on rename: accepted as lost, not tested.** Plex treats a renamed file as
delete + add, so watched state and manual metadata edits on those episodes may not
survive. Deliberately not investigated, because of what the tool is for: the primary case
is **freshly transcoded recordings in the timecode fallback layout** — files that have
never been watched, and carry no hand-made metadata, because nobody can play them under a
name Plex could not match in the first place. Spending the effort to preserve state that
does not exist yet would be paying for the wrong case. Renaming an old, already-watched
episode is possible and will probably cost its watched flag; that is a known price, not a
surprise.

## Design

Two rules the code is organised around:

1. **All path computation is pure and testable; exactly one module mutates the
   filesystem.** `core.py` turns a directory listing plus the four inputs into
   source→target pairs and validation results, as data. `execute.py` is the only place
   `os.rename`/`mkdir` appears.
2. **The server computes names, not the browser.** The obvious way to get live-updating
   fields is to build filenames in JavaScript — which yields two implementations of the
   naming convention that will eventually disagree. Instead the page debounces and calls
   `POST /api/plan`. One implementation, in Python, tested.

```
core.py        pure: derive defaults, build plan, validate. No I/O.
execute.py     the only module that mutates the filesystem
config.py      roots, loading, path confinement
app.py         Flask routes
cli.py         dry-run from the shell: print the plan, touch nothing
templates/index.html
tests/         pytest, against temp trees
deploy/        systemd unit, config.ini.example
```

Endpoints: `GET /api/browse`, `POST /api/plan` (no writes), `POST /api/execute`,
`POST /api/undo`, `GET /api/runs`, `GET /api/runs/<manifest>`.

`POST /api/execute` takes the same body as `/api/plan` plus the `fingerprint` of the
plan the user confirmed. It rebuilds the plan here, from a fresh listing, and compares:
a folder that changed between the dialog and the click is refused whole rather than
half-applied against names nobody agreed to. `POST /api/undo` takes a manifest filename
and nothing else — a path segment there would read JSON from anywhere on disk and then
hand it to `os.rename`.

**The undo manifest is an ordered op log**, not a flat old→new map, and it is undone in
reverse. That is what lets the opt-in show-folder rename — which changes the parent of
every path already recorded — belong to the same run: undo puts the folder back first,
and the recorded file paths are valid again. Each op carries what *actually* happened,
so a run that failed halfway reverses exactly the part that landed.

**Rename order is computed, not assumed.** Renumbering a run produces chains (E01→E02
while E02→E03) and, rarely, a cycle (two files swapping names). `core.order_moves`
sequences them so no rename ever lands on a file that has not moved yet, parking one
file under a temporary name to break a cycle. Applied in listing order, the first move
of a chain would silently destroy a file.

`POST /api/plan` takes the folder plus any inputs the user has changed. An **absent**
key means "derive it from the path"; a key present but **empty** means "the user
cleared it, leave it out" — which is how a show with no year and a show whose year has
not been typed yet stay distinguishable. The response echoes the inputs it resolved, so
the form populates itself from the same derivation the plan used rather than a second
one in JavaScript.

`/#path=<encoded>` opens straight into a folder, which makes a season bookmarkable and
the page reachable in one step from a headless browser.

## Build phases

1. ✅ **Core + tests + `cli.py --dry-run`.** No write capability exists in the codebase yet.
2. ✅ **Web UI, read-only.** Full page driving `/api/plan`. Can be pointed at the real
   library and is incapable of changing it.
3. ✅ **Execute.** `execute.py`, undo manifest, confirmation dialog, plan hash.
4. ✅ **Deploy and prove.** systemd unit on the VM, own venv, proven on scratch copies of
   real seasons in both roots. The watched-state test was dropped by decision, see above.

## Environment

| | |
|---|---|
| Local repo | `/home/brian/plex-renamer` |
| GitHub | `byuchino/plex-renamer` via remote `git@github-byuchino:byuchino/plex-renamer.git` |
| | the `github-byuchino` SSH alias is required — `byuchino` is **not** this machine's default GitHub account |
| Deploy target | `envy-ubuntu20`, SSH alias `handbrake-vm`, `brian@192.168.254.206` |
| Install path | `/opt/plex-renamer/` + own venv |
| Service | `plex-renamer.service`, port **8101** (8099 = transcode dashboard, 8100 = worker API) |
| Config | `/etc/plex-renamer/config.ini` — allowed roots |
| Initial roots | `/mnt/bama/volume1/TV Shows`, `/mnt/bama/volume1/Videos/KIKU` |

**Both roots are on the home nas.** The condo nas has its own copy of the KIKU library,
but the two boxes sync it between themselves, so renaming the local copy is enough. That
keeps this tool off the WireGuard tunnel entirely — unlike the transcode pipeline, it has
no remote-mount failure mode.

They are still **separate NFS exports** (`TV Shows` and `Videos` are exported
independently, `st_dev` 40 and 41), so `os.rename` between the two roots fails with
`EXDEV` even though both live on one NAS volume. Moves within a root are fine, which is
all the Phase 5 merge case needs — but the same-device check is required, not optional.

**The VM runs Python 3.8.10.** Target 3.8 syntax: no `X | Y` unions, no builtin generic
annotations at runtime without `from __future__ import annotations`. Local development
python is 3.12, so this will not be caught locally — verify against the VM's interpreter.

## Running it locally

```sh
.venv/bin/python -m pytest tests -q
PLEX_RENAMER_CONFIG=/path/to/config.ini .venv/bin/python app.py
```

On the VM it runs as `plex-renamer.service` from `/opt/plex-renamer` with its own venv,
reading `/etc/plex-renamer/config.ini`. Logs go to journald
(`journalctl -u plex-renamer -f`); `deploy/plex-renamer.service` is the unit as installed.
The unit needs `SupplementaryGroups=users` to write the KIKU tree, and deliberately stops
short of `ProtectSystem=strict` — that would require every configured root as a
`ReadWritePaths` entry and would break the moment a root is added to the config.

Flask is started without debug, so **Jinja does not reload a changed template** —
restart the process after editing `templates/index.html` or you will screenshot the
previous version and go looking for a bug that is not there.

## Status

**Phases 1 through 4 complete. Installed and running as a service; it can move files.**

The page browses the configured roots, derives all four inputs from the path (or from
the files, see above), live re-plans on every edit, groups re-broadcasts via
airings-per-episode, cascades an episode pick forward, handles episodic and mixed
folders, validates hand-typed names, and executes a confirmed plan behind a dialog that
lists every pair — writing an undo manifest first, with one-click undo of the run just
made.

`test_app.py`'s `test_no_module_can_mutate_the_filesystem` pins `app.py`, `core.py` and
`config.py` as containing no mutation at all. `execute.py` is the single exception and is
deliberately **not** on that list. Do not relax the rule for the others.

**Known gap, deliberately not built:** renumbering a *run* of already-episodic files
(an off-by-one across a whole season) takes one pick per file today, because a pick
never cascades through numbered files. The fix, if it is ever wanted, is to shift
subsequent numbered files by the same delta rather than resequencing them, so gaps
survive. Execution handles the resulting chains correctly either way.

**Settled since:** `Specials` is now recognised as season 0 (above), which took the
library from 81 folders proposing changes to 56 — 25 `Specials` folders went quiet, no
folder started proposing anything, and the two that still do are instances of the case
below rather than of this one. Remaining, one level down: a folder whose episodic files disagree with each other falls back to the
folder name for show and year (as designed). The one show where that bites,
`Star Wars The Clone Wars`, turns out to hold **two complete copies of every season** — a
bare `.avi` rip and a titled 1080p `.mkv` — so no show-name/year choice leaves both sets
alone, and adding the missing `(2008)` to the folder raises the churn from 125 renames to
141 rather than settling it. That one is a duplicate-media decision, not a naming bug.
Neither is new behaviour, and neither is caught by the 60-folder sweep.

Phase 5 is next: destination show folders, for merging the duplicate show folders that
exist in the library — confined to the same root *and* the same `st_dev`, since the two
roots are separate NFS exports and `os.rename` between them fails with `EXDEV`.
