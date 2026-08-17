# plex-renamer — working notes

Bulk-renames Plex recordings that landed in the timecode/`Season YYYY` fallback layout,
and fixes up already-episodic files. Flask app on the transcode VM, LAN only, no auth.

**`README.md` is the authoritative spec and phase plan. Read it first.** This file holds
the operational facts that are not derivable from the code.

## Status

Phases 1 through 3 are complete: **the code can move real files.** Nothing is deployed as
a service yet, and the running preview on the VM is still Phase 2 code (see below).

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
| Tests | `.venv/bin/python -m pytest tests -q` (205 passing) |
| Push | `GIT_SSH_COMMAND='ssh -F ~/.ssh/config' git push` — remote uses the **`github-byuchino`** alias; `byuchino` is not this machine's default GitHub account |
| VM | `ssh handbrake-vm` (`brian@192.168.254.206`), **Python 3.8.10** |
| Roots | `/mnt/bama/volume1/TV Shows`, `/mnt/bama/volume1/Videos/KIKU` — both on the home NAS |

**Target Python 3.8**, not the local 3.12: no `X | Y` unions, no builtin generics in
runtime annotations. Syntax-check before deploying:

    cat app.py | ssh handbrake-vm 'python3 -c "import sys; compile(sys.stdin.read(),\"x\",\"exec\")"'

## The running preview

A preview runs on the VM from `~/plex-renamer-preview/` on port 8101. Since 2026-08-17 it
carries **Phase 3 code, so its Rename button is live against the real library** — the user
asked for that deliberately, ahead of the scratch-copy proof. It is still **not** the
Phase 4 install: hand-started, no systemd, dies on reboot. `execute.py` must be copied
alongside `app.py` or the import fails. Its `undo_dir` is
`/home/brian/plex-renamer-preview/undo` (created on the first real run), not the
`/var/lib` default — an unwritable `undo_dir` refuses every execute, which is safe but
looks like a bug.

**Restarting it needs `setsid` and separate steps.** `kill` + start in one compound ssh
command gets blocked, and the start command holds the ssh channel open even with `nohup`
— the server is already up and detached, so just stop waiting on that shell and verify
with `ss` from a second connection. To redeploy:

    scp -q app.py core.py config.py execute.py handbrake-vm:~/plex-renamer-preview/
    scp -q templates/index.html handbrake-vm:~/plex-renamer-preview/templates/
    ssh handbrake-vm 'PID=$(ss -lptnH "sport = :8101" | grep -o "pid=[0-9]*" | head -1 | cut -d= -f2)
      [ -n "$PID" ] && kill "$PID"; sleep 2
      cd ~/plex-renamer-preview && PLEX_RENAMER_CONFIG=$PWD/config.ini \
        setsid nohup /opt/transcode/venv/bin/python app.py > preview.log 2>&1 < /dev/null &'

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
- **The sweep is stronger run over both roots and diffed against `HEAD`.** All 599 season
  folders: 473 no-op, 45 with no renameable files, 81 proposing changes. The absolute
  numbers are not the check — a byte-identical diff against the pre-change code is. A
  sweep script (30 lines, drives `/api/plan` through the Flask test client) is quick to
  rewrite; run it on the VM in `/tmp`, since the NAS roots are not mounted locally.
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
