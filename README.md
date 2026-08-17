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
| Season | season folder, unless it is a 4-digit year, then `01` |

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

**Table**, one row per matching file, sorted by timecode. Each row shows the editable
proposed name and an **Episode** button opening a selectable matrix. Episodes start at
`01` and increment; picking a value for a row **cascades forward** to later rows and
leaves earlier rows unchanged. Changing any top input recomputes every row.

**Season folder:** files are **moved into `Season <nn>`** (created if needed), rather
than renaming the folder in place — so one year folder can later be split across
seasons. An emptied `Season YYYY` is removed; if anything remains it is left alone.

**Show folder rename is opt-in** via checkbox. When ticked the folder becomes
`<Show Name> (<Year>) {id}` from all three inputs. Opt-in specifically so a typo in
Show Name cannot silently rename a directory containing other seasons not visible on
the page.

**Safety:**

- Pre-flight validation of the whole set — duplicate targets, existing destinations,
  root confinement, unwritable paths. Rename stays disabled while any row is invalid.
- The set is explicitly **not atomic**: validate all up front, execute, report
  per-file results.
- Confirmation dialog listing every old→new pair, guarded by a plan hash so the
  directory cannot change between confirming and executing.
- **Undo manifest** (old→new JSON) written before the first move.
- Files not matching the fallback pattern are listed as **skipped, never hidden**.
- Navigation confined to configured roots, resolved with `realpath` so `..` cannot
  escape.

**Out of scope in v1:** sidecar files (.srt/.nfo/artwork), any TMDB/TVDB API calls
(episode matrix is 1–40 with direct typing), authentication, multi-directory batch
runs. Plex rescan after renaming is a manual step.

**Known risk, not yet tested:** Plex treats a renamed file as delete + add, so watched
state and manual metadata edits on those episodes may not survive. Test on a single
file before any bulk run.

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
`POST /api/undo`.

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
3. **Execute.** `execute.py`, undo manifest, confirmation dialog, plan hash.
4. **Deploy and prove.** First real run against a scratch copy of one season, then the
   single-file watched-state test, then real use.

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

Flask is started without debug, so **Jinja does not reload a changed template** —
restart the process after editing `templates/index.html` or you will screenshot the
previous version and go looking for a bug that is not there.

## Status

**Phases 1, 2 and 2.5 complete. Nothing deployed, and no code in this repo can move a
file.**

The page browses the configured roots, derives all four inputs from the path (or from
the files, see above), live re-plans on every edit, groups re-broadcasts via
airings-per-episode, cascades an episode pick forward, handles episodic and mixed
folders, and validates hand-typed names — with the Rename button permanently disabled
until Phase 3 supplies an endpoint behind it.

**Known gap, deliberately not built:** renumbering a *run* of already-episodic files
(an off-by-one across a whole season) takes one pick per file today, because a pick
never cascades through numbered files. The fix, if it is ever wanted, is to shift
subsequent numbered files by the same delta rather than resequencing them, so gaps
survive.

Phase 3 is next: `execute.py`, the undo manifest, the confirmation dialog, and the plan
hash check. That is the first phase that can change a library, so `test_app.py`'s
`test_no_module_can_mutate_the_filesystem` deliberately pins `app.py`, `core.py` and
`config.py` as non-mutating — add `execute.py` as the one exception, do not relax the
rule for the others.
