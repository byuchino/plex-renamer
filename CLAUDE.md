# plex-renamer — working notes

Bulk-renames Plex recordings that landed in the timecode/`Season YYYY` fallback layout,
and fixes up already-episodic files. Flask app on the transcode VM, LAN only, no auth.

**`README.md` is the authoritative spec and phase plan. Read it first.** This file holds
the operational facts that are not derivable from the code.

## Status

Phases 1 through 4 are complete: **installed as `plex-renamer.service` on the VM and able
to move real files.** 233 tests.

Multi-episode support (`episodes_per_file`, see below) is deployed. `main` is pushed and
`/opt/plex-renamer` matches `HEAD` byte for byte (checked by md5).

**Resume here — nothing is half-finished; these are choices, not chores:**

1. **Phase 5, the next build:** destination show folders, so the duplicate show folders in
   the library can be merged. Pick an existing destination *show* folder by browsing;
   confine to the same root **and** the same `st_dev` (the roots are separate NFS exports,
   so cross-root is `EXDEV`); hard-refuse cross-device rather than silently copying
   multi-GB files. Season subfolder still derived as it is today.
2. **A decision only the user can make:** `Star Wars The Clone Wars` holds two complete
   copies of every season (see below). 125 renames whichever naming is chosen. It is a
   "which copy do you keep" call, not something this tool can settle.
3. **A follow-up, if it is ever wanted:** per-row episode spans. `episodes_per_file` is
   folder-wide, so a season where only the opener is a double episode still needs that
   row hand-edited. The fix is to make an anchor `(episode, span)` instead of an `int`
   and add a width selector to the episode matrix.
4. **Nothing else is outstanding.** Watched state is settled (see below, do not re-open).
   No known bugs. The real-library sweep sits at 56 of 599 folders proposing changes, and
   the ones that do are either genuine fixes or the Clone Wars duplicate above.

`test_no_module_can_mutate_the_filesystem` in `tests/test_app.py` pins `app.py`,
`core.py` and `config.py` as non-mutating. `execute.py` is the single exception and is
deliberately absent from that list — do not add the others to it.

## How execution is built

- `execute.py` is the only module that calls `os.rename`/`mkdir`/`rmdir`.
- `/api/execute` takes the `/api/plan` body plus the confirmed `fingerprint`, rebuilds
  the plan from a fresh listing, and refuses on mismatch. `_plan_from_payload` in
  `app.py` is the single derivation both routes share — keep it that way or the two
  endpoints will disagree about what the plan is.
- The manifest is an **ordered op log** (`mkdir`/`move`/`rmdir`/`move_dir`), undone in
  reverse, each op stamped with what actually happened. Reverse order is what makes the
  show-folder rename safe to include, since it invalidates every path above it.
- `core.order_moves` sequences chains and breaks cycles with a parking name. Without it,
  a renumbered run applied in listing order destroys a file.
- A manifest that cannot be written refuses the whole run. No undo record, no moves.
- Settled: an emptied `Season YYYY` **is** removed, but only that form — an emptied
  `Season 1` stays. Anything left in the folder (a poster, a skipped file) keeps it.

## Multi-episode files (`episodes_per_file`, added 2026-08-17)

One recording holding two consecutive episodes, written `S01E01-E02` — Plex reads the
span off the filename. See README for the full rules. The facts worth not rediscovering:

- **Only the hyphenated `-E02` spelling is parsed.** Of 9600 files in the library, 58
  carry `-E`, **none** carry the bare `S01E01E02`, and the four `S01E01-02` are all in
  `Star Wars The Clone Wars` (the duplicate-media folder). Recognising a spelling means
  re-rendering it canonically, i.e. renaming files nobody asked about — so the other two
  forms stay in the byte-preserved tail and keep round-tripping. Don't "improve" this.
- **`RE_EPISODIC` had to learn spans regardless of the new input.** Before, `S01E01-E02`
  parsed as episode 1 with `-E02` swallowed into the tail. It round-tripped byte-identical
  *by accident*, but the implicit-anchor cascade read it as E01, so every row below a
  double episode was off by one. That was a live bug, not a new feature.
- **A malformed span (`S01E05-E02`) is handed back to the tail**, not corrected.
- **The two controls compose and both shapes are real.** `Shinsengumi With You I Bloom`
  is a double episode re-broadcast the next day: 2 airings/episode × 2 episodes/file.
- **The sweep came back byte-identical against `HEAD`** over all 599 folders. It is inert
  on the existing library because every folder holding a span holds *only* episodic files,
  which are their own anchors — the span only changes what a following timecode run gets.
- The UI label is **"Episodes per file"**, deliberately not "episodes per airing": beside
  "Airings per episode" that is the same words reordered, and reading the two backwards
  renames a whole folder the wrong way.

## History and logging

- The manifests **are** the activity history; `/api/runs` reads them back rather than
  keeping a second record that could disagree with what undo will do. `renames` counts
  applied `move` ops only, not the mkdir/rmdir bookkeeping around them.
- `inputs` was added to the manifest body **without bumping `MANIFEST_VERSION`**: it is
  simply absent from earlier manifests, which stay readable and undoable. Bumping would
  have discarded the undo record for runs that already happened.
- **A refusal writes no manifest**, so it is invisible to the history by design. The log
  is the only place to debug one. Logging goes to stderr and nowhere else — journald owns
  persistence once the Phase 4 unit exists, and a log file here would put file writing
  into `app.py`, which the pinned non-mutation test exists to prevent.
- `keep_runs` (config, default 200) prunes oldest-first after a successful run. Pruning
  can never fail a run: the files are already moved by the time it runs.

Phase 4 is deploy (`/opt/plex-renamer`, `/etc/plex-renamer/config.ini`, systemd unit,
port 8101) and proving it on a scratch copy first. Phase 5 is destination show folders,
confined to the same root *and* same `st_dev`.

## Before the first real run

One pre-existing naming misfire is **fixed**; one remains. Neither was caught by the
60-folder sweep, and both were one confirmation away from happening:

- ~~**`Specials` is not season 0.**~~ **Fixed.** `parse_season_dir` returns `(0, False)`
  for `Specials`, and `season_dir_name` calls a newly created season-0 folder `Specials`
  rather than `Season 00`. The folder-left-alone rule then makes those folders no-ops.
  Sweep before/after: **81 → 56** folders proposing changes, all 25 of the difference
  being `Specials`, nothing new started. `Season 00` folders were already handled and are
  still never renamed.
- **A folder whose files disagree with each other falls back to the folder name.**
  By design (README). The one folder where it bites is `Star Wars The Clone Wars`, and the
  cause is **duplicate media, not naming**: every season holds two complete sets — a bare
  `.avi` rip (`… - S01E01.avi`) and a 1080p BluRay `.mkv` (`… (2008) - S01E01 - Ambush …`).
  No show-name/year choice leaves both alone, so whichever one you pick rewrites the other
  set: 125 changes as it stands, **141** if the folder is given `(2008)` — which also turns
  `Season 07` and `Specials` (bare set only, currently clean no-ops) into 16 and 9 changes.
  Nothing collides either way (the bare set has no tail and a different extension), so it
  is churn, not risk. It is the only show in the library with this shape. The real fix is
  deciding which set to keep; the renamer cannot help with that.

## Environment

| | |
|---|---|
| Tests | `.venv/bin/python -m pytest tests -q` (233 passing) |
| Push | `GIT_SSH_COMMAND='ssh -F ~/.ssh/config' git push` — remote uses the **`github-byuchino`** alias; `byuchino` is not this machine's default GitHub account |
| VM | `ssh handbrake-vm` (`brian@192.168.254.206`), **Python 3.8.10** |
| Roots | `/mnt/bama/volume1/TV Shows`, `/mnt/bama/volume1/Videos/KIKU` — both on the home NAS |

**Target Python 3.8**, not the local 3.12: no `X | Y` unions, no builtin generics in
runtime annotations. Syntax-check before deploying:

    cat app.py | ssh handbrake-vm 'python3 -c "import sys; compile(sys.stdin.read(),\"x\",\"exec\")"'

## The install (Phase 4, done 2026-08-17)

| | |
|---|---|
| Unit | `plex-renamer.service`, enabled, `User=brian` + `SupplementaryGroups=users` |
| Code | `/opt/plex-renamer/` with its **own** venv (`venv/bin/python`, flask 3.0.3, py3.8.10) |
| Config | `/etc/plex-renamer/config.ini` (root-owned, 644) |
| State | `/var/lib/plex-renamer/undo/` — manifests: undo records *and* history |
| Logs | journald: `journalctl -u plex-renamer -f` |
| Unit source | `deploy/plex-renamer.service` in this repo |

Redeploy after a change:

    scp -q app.py core.py config.py execute.py cli.py handbrake-vm:/opt/plex-renamer/
    scp -q templates/index.html handbrake-vm:/opt/plex-renamer/templates/
    ssh handbrake-vm 'sudo systemctl restart plex-renamer'

Facts worth not rediscovering:

- **`SupplementaryGroups=users` is load-bearing.** Without gid 100 the service browses the
  KIKU library and refuses every rename in it. See the group note below.
- **Hardening stops at `ProtectSystem=full` on purpose.** `strict`, or `PrivateMounts`,
  needs every configured root listed as `ReadWritePaths` — a footgun for a tool whose job
  is writing to those roots, and it breaks silently when a root is added. Verified that
  `full` does *not* block the NFS writes, by executing and undoing a real run through the
  service.
- **`Failed to attach … to compat systemd cgroup` in the journal is environmental**, not
  this unit: `transcode-dashboard` logs it 10 times too (cgroup v1/v2 hybrid on this
  Ubuntu 20.04 host). Ignore it.
- **`~/plex-renamer-preview/` is gone** (deleted 2026-08-17). It was the hand-started
  Phase 2/3 preview, superseded by the unit. If a note elsewhere still refers to it, that
  note is stale — there is one install now, `/opt/plex-renamer` under systemd, and one
  state dir, `/var/lib/plex-renamer/undo`.
- **Own venv, not `/opt/transcode/venv`.** The point is blast radius: a library-curation
  bug must not be able to break unattended transcoding.

## Watched state on rename: settled, will not be tested

Plex treats a rename as delete + add, so watched state and manual metadata edits probably
do not survive. **Accepted as lost (user decision, 2026-08-17)** — do not re-open it or
build anything to preserve it. The reasoning is what matters: the tool's primary case is
**freshly transcoded recordings in the timecode fallback layout**, which have never been
watched and carry no hand-made metadata, because nobody can play a file under a name Plex
could not match. There is no state there to preserve. Renaming an old already-watched
episode will probably cost its watched flag; that is a known price.

## Things that cost real time to learn

- **Flask runs without debug, so Jinja does not reload a changed template.** A screenshot
  after editing `templates/index.html` shows the *previous* page. Restart the process
  before concluding the page is broken.
- **After any change to naming, re-run the real-library sweep.** Point `/api/plan` at
  real season folders and count how many propose changes. The bar on the original ~60
  folder subset is 58 no-ops, 1 folder with no renameable files, and 1 genuine fix
  (`S06e01` → `S06E01`). This is the only check that catches "proposes renaming the
  entire library". Three rules exist because of it, all in README: tails preserved
  byte-for-byte, show name/year defaulting from the files, and leaving a correctly-
  numbered season folder alone.
- **The sweep is stronger run over both roots and diffed against `HEAD`.** The script now
  lives in the repo as `deploy/sweep.py` — copy the repo *and* a `git archive HEAD` export
  to `/tmp` on the VM, run both, `diff`. Needs `PYTHONPATH=.` and
  `/opt/plex-renamer/venv/bin/python`; the NAS roots are not mounted locally. Current
  baseline over all 603 season folders: **498 no-op, 56 proposing changes (1106 files),
  49 with no renameable files.** The absolute numbers are not the check — a byte-identical
  diff against the pre-change code is.
- **`os.access` is unusable on these NFS mounts — never permission-check with it.**
  DSM's NFSv4.1 export answers `access(2)` itself, and reports `W_OK` **false** for a
  directory that is mode 777, owned by the asking uid, and which `open(...,"w")` succeeds
  on. `execute.py`'s pre-flight trusted it and refused every plan on the whole KIKU
  library with "Cannot write to …". `_writable()` now probes by creating and unlinking a
  dot-file, which is the only answer that means anything. Found by the first scratch-copy
  run on 2026-08-17 — this is exactly the class of bug that run exists to catch, and no
  amount of local tmp_path testing would have shown it.
- **Duplicate episode numbers are legitimate.** Most KIKU series are broadcast twice and
  Plex records both. This is a neutral note, never an error — blocking on it would reject
  the correct plan for a whole library.
- **The VM needs group `users` (gid 100) to write to the KIKU tree.** Fixed 2026-08-17
  with `sudo usermod -aG users brian`; the tree is mode 775 owned by a DSM uid the VM does
  not have. A process must be restarted to pick the group up. This lives on the VM, not in
  git — **a rebuilt VM needs it again**. Undo: `sudo gpasswd -d brian users`.
- **Renaming is safe with respect to the NAS sync.** The two NAS boxes sync these
  directories, and a rename propagates as a true rename (verified: inode preserved on the
  receiving side, nothing in the recycle bin), so a bulk rename does not re-transfer the
  files. Sync is two-way. It does **not** propagate POSIX permissions.
- **Do not restart the transcode services** (`transcode-watcher`) casually — it kills any
  in-progress local encode. That project is a separate repo at `/home/brian/handbrake`;
  this tool never touches its `jobs.db`.

## Before touching real files

Prove it on a scratch copy of one season first, then a single-file test to see whether
Plex preserves watched state across a rename (still unknown, and accepted as a risk).
