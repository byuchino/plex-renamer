# plex-renamer — working notes

Bulk-renames Plex recordings that landed in the timecode/`Season YYYY` fallback layout,
and fixes up already-episodic files. Flask app on the transcode VM, LAN only, no auth.

**`README.md` is the authoritative spec and phase plan. Read it first.** This file holds
the operational facts that are not derivable from the code.

## Status

Phases 1, 2 and 2.5 are complete. **Phase 3 (execute) is next and is the first code that
can move a real file.** Nothing is deployed as a service yet.

`test_no_module_can_mutate_the_filesystem` in `tests/test_app.py` pins `app.py`,
`core.py` and `config.py` as non-mutating. When Phase 3 lands, add `execute.py` as the
single exception — do not relax the rule for the others.

## Phase 3, concretely

- `execute.py` — the only module allowed to call `os.rename`/`mkdir`.
- Recompute the plan server-side and compare `Plan.fingerprint()` against the one the
  browser confirmed; refuse if the directory changed in between.
- Write the undo manifest (old→new JSON, to `config.undo_dir`) **before** the first move.
- `POST /api/execute` and `POST /api/undo`; confirmation dialog listing every pair.
- Not atomic by design: validate everything up front, execute, report per-file results.
- Order: compute all final paths first, then move files, then container renames.
- Open question still to settle: whether an emptied `Season YYYY` folder gets removed.

Phase 4 is deploy (`/opt/plex-renamer`, `/etc/plex-renamer/config.ini`, systemd unit,
port 8101) and proving it on a scratch copy first. Phase 5 is destination show folders,
confined to the same root *and* same `st_dev`.

## Environment

| | |
|---|---|
| Tests | `.venv/bin/python -m pytest tests -q` (112 passing) |
| Push | `GIT_SSH_COMMAND='ssh -F ~/.ssh/config' git push` — remote uses the **`github-byuchino`** alias; `byuchino` is not this machine's default GitHub account |
| VM | `ssh handbrake-vm` (`brian@192.168.254.206`), **Python 3.8.10** |
| Roots | `/mnt/bama/volume1/TV Shows`, `/mnt/bama/volume1/Videos/KIKU` — both on the home NAS |

**Target Python 3.8**, not the local 3.12: no `X | Y` unions, no builtin generics in
runtime annotations. Syntax-check before deploying:

    cat app.py | ssh handbrake-vm 'python3 -c "import sys; compile(sys.stdin.read(),\"x\",\"exec\")"'

## The running preview

A read-only preview runs on the VM from `~/plex-renamer-preview/` on port 8101. It is
**not** the Phase 4 install: hand-started, no systemd, dies on reboot. To redeploy:

    scp -q app.py core.py config.py handbrake-vm:~/plex-renamer-preview/
    scp -q templates/index.html handbrake-vm:~/plex-renamer-preview/templates/
    ssh handbrake-vm 'PID=$(ss -lptnH "sport = :8101" | grep -o "pid=[0-9]*" | head -1 | cut -d= -f2)
      [ -n "$PID" ] && kill "$PID"; sleep 2
      cd ~/plex-renamer-preview && PLEX_RENAMER_CONFIG=$PWD/config.ini \
        setsid nohup /opt/transcode/venv/bin/python app.py > preview.log 2>&1 < /dev/null &'

## Things that cost real time to learn

- **Flask runs without debug, so Jinja does not reload a changed template.** A screenshot
  after editing `templates/index.html` shows the *previous* page. Restart the process
  before concluding the page is broken.
- **After any change to naming, re-run the real-library sweep.** Point `/api/plan` at ~60
  real season folders and count how many propose changes. The bar is 58 no-ops, 1 folder
  with no renameable files, and 1 genuine fix (`S06e01` → `S06E01`). This is the only
  check that catches "proposes renaming the entire library". Three rules exist because of
  it, all in README: tails preserved byte-for-byte, show name/year defaulting from the
  files, and leaving a correctly-numbered season folder alone.
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
