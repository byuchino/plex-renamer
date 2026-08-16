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

## Build phases

1. **Core + tests + `cli.py --dry-run`.** No write capability exists in the codebase yet.
2. **Web UI, read-only.** Full page driving `/api/plan`. Can be pointed at the real
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
| Initial roots | `/mnt/bama/volume1/TV Shows`, `/mnt/pippa/volume1/Videos/KIKU` |

**The VM runs Python 3.8.10.** Target 3.8 syntax: no `X | Y` unions, no builtin generic
annotations at runtime without `from __future__ import annotations`. Local development
python is 3.12, so this will not be caught locally — verify against the VM's interpreter.

## Status

Phase 1 in progress. Nothing deployed.
