"""The one module in this repo that changes the filesystem.

core.py decides what the names should be; this module decides nothing. It takes
a finished Plan, refuses it outright if anything about it is wrong, writes an
undo manifest, and only then starts moving files.

Four properties are deliberate:

* **Validate everything, then act.** A set of renames is explicitly not atomic
  (README), so the only defence against a half-renamed season is to check the
  whole set up front and report per-file afterwards.
* **The manifest is written before the first move**, and a manifest that cannot
  be written refuses the whole run. A move with no record of how to reverse it
  is the one outcome worth aborting for.
* **Order is computed, not assumed.** core.order_moves sequences chains so no
  rename lands on a file that has not moved yet.
* **The manifest is an ordered op log, undone in reverse.** That is what lets a
  show-folder rename — which changes the parent of every path already recorded
  — be part of the same run: undo puts the folder back first, and the recorded
  file paths are valid again.

Targets Python 3.8 (the deployment VM).
"""
from __future__ import annotations

import binascii
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

import core
from config import OutsideRoots, resolve_within_roots

MANIFEST_VERSION = 1

OP_MKDIR = "mkdir"       # a season folder this run created
OP_MOVE = "move"         # a file rename
OP_RMDIR = "rmdir"       # the emptied source season folder
OP_MOVE_DIR = "move_dir"  # the opt-in show folder rename


class ManifestError(Exception):
    """A manifest that cannot be found, read, or trusted."""


@dataclass
class Operation:
    """One filesystem change, planned before it happens and stamped afterwards.

    `applied` is what actually took place, not what was intended — undo walks
    only the ops that succeeded, so a run that failed halfway reverses exactly
    the part that landed.
    """
    op: str
    src: Optional[str] = None
    dst: Optional[str] = None
    applied: bool = False
    error: Optional[str] = None

    def to_json(self) -> Dict[str, Any]:
        return {"op": self.op, "from": self.src, "to": self.dst,
                "applied": self.applied, "error": self.error}

    @classmethod
    def from_json(cls, d: Dict[str, Any]) -> "Operation":
        if not isinstance(d, dict) or not d.get("op"):
            raise ManifestError("manifest contains an entry with no operation")
        return cls(op=str(d["op"]), src=d.get("from"), dst=d.get("to"),
                   applied=bool(d.get("applied")), error=d.get("error"))


@dataclass
class Result:
    """What a run did, per operation. Serialised straight to the browser."""
    ops: List[Operation] = field(default_factory=list)
    # Non-empty means nothing was touched at all: pre-flight said no.
    refused: List[str] = field(default_factory=list)
    manifest: Optional[str] = None
    season_dir: Optional[str] = None
    show_dir: Optional[str] = None

    @property
    def ok(self) -> bool:
        return not self.refused and not any(o.error for o in self.ops)

    @property
    def applied_count(self) -> int:
        return sum(1 for o in self.ops if o.applied)

    def to_json(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "refused": self.refused,
            "manifest": self.manifest,
            "season_dir": self.season_dir,
            "show_dir": self.show_dir,
            "applied": self.applied_count,
            "ops": [o.to_json() for o in self.ops],
            "errors": [o.error for o in self.ops if o.error],
        }


# ── Pre-flight ─────────────────────────────────────────────────────────────

def _nearest_existing(path: Path) -> Path:
    """The deepest ancestor of `path` that exists — where a not-yet-created
    destination inherits its device and its writability from."""
    p = path
    while not p.exists() and p.parent != p:
        p = p.parent
    return p


def _writable(path: Path) -> bool:
    """Whether a directory can actually be written to — tested by writing.

    `os.access` is not usable here. Both libraries are NFS exports, and on NFS
    access(2) is answered by the server's own permission evaluation rather than
    from the mode bits the client can see. This DSM export reports W_OK false
    for a directory that is mode 777, owned by the very uid asking, and which a
    plain open()-for-write succeeds on — so trusting it refused a perfectly
    valid plan on the whole KIKU library. Found by the first scratch-copy run
    (Phase 4); it is the reason that run exists.

    So: create a file and remove it. That is the only answer that means
    anything, and it is what the rename is about to need anyway. The probe is
    a dot-file with a random suffix so a concurrent request cannot collide with
    it, and it is unlinked immediately.
    """
    probe = path / ".plex-renamer-write-probe-{}".format(
        binascii.hexlify(os.urandom(6)).decode("ascii"))
    try:
        with probe.open("w"):
            pass
    except OSError:
        return False
    try:
        probe.unlink()
    except OSError:
        # Written but not removable is still writable, which is what was asked.
        pass
    return True


def _show_dir_to_rename(plan: core.Plan) -> Optional[Path]:
    """The show folder as it stands, when the opt-in rename would change it.

    Taken from the season target's parent rather than from a file, so it is
    known even for a plan whose file list is empty. Both forms of
    season_dir_target — the folder as browsed, or a new 'Season nn' beside it —
    sit directly under the show folder.
    """
    if plan.show_dir_target is None or plan.season_dir_target is None:
        return None
    current = plan.season_dir_target.parent
    return None if current == plan.show_dir_target else current


def preflight(plan: core.Plan, roots: Sequence[Path]) -> List[str]:
    """Every reason not to run this plan. Empty means go.

    Deliberately re-checks things core.py already checked. core validated the
    plan the browser was shown; this validates the plan about to be applied,
    against the filesystem as it is right now.
    """
    problems: List[str] = []
    problems.extend(plan.issues)
    for f in plan.files:
        for issue in f.issues:
            problems.append("{}: {}".format(f.source.name, issue))

    show_dir = _show_dir_to_rename(plan)
    if not plan.moves and show_dir is None:
        problems.append("Nothing to rename — no file would change.")
        return problems

    # Root confinement, again. The browser supplies the folder and the typed
    # names, so every path that is about to be written is re-checked here
    # rather than trusted because /api/plan already looked at it.
    candidates: List[Path] = []
    for src, dst in plan.moves:
        candidates.extend((src, dst))
    if plan.season_dir_target is not None:
        candidates.append(plan.season_dir_target)
    if show_dir is not None:
        candidates.extend((show_dir, plan.show_dir_target))
    for path in candidates:
        try:
            resolve_within_roots(path, roots)
        except OutsideRoots as e:
            problems.append(str(e))

    dest = plan.season_dir_target
    sources = set(pf.source for pf in plan.files)
    for src, dst in plan.moves:
        if not src.is_file():
            problems.append("{} is no longer there.".format(src.name))
        if dst.exists() and dst not in sources:
            problems.append("{} already exists.".format(dst.name))

    # Both ends must be writable, and on one filesystem: the two libraries are
    # separate NFS exports, so os.rename between them fails with EXDEV even
    # though they live on the same NAS volume (README).
    if dest is not None and plan.moves:
        anchor = _nearest_existing(dest)
        if not _writable(anchor):
            problems.append("Cannot write to {}.".format(anchor))
        for parent in sorted(set(src.parent for src, _ in plan.moves)):
            if not _writable(parent):
                problems.append("Cannot write to {}.".format(parent))
            elif _device(parent) != _device(anchor):
                problems.append(
                    "{} and {} are on different filesystems; a rename between "
                    "them is not possible.".format(parent, anchor))

    if show_dir is not None:
        if plan.show_dir_target.exists():
            problems.append("A folder named {} already exists.".format(
                plan.show_dir_target.name))
        if not _writable(show_dir.parent):
            problems.append("Cannot write to {}.".format(show_dir.parent))

    return problems


def _device(path: Path) -> Optional[int]:
    try:
        return os.stat(str(path)).st_dev
    except OSError:
        return None


# ── Planning the operations ────────────────────────────────────────────────

def plan_operations(plan: core.Plan) -> List[Operation]:
    """The whole run as an ordered op log.

    Files first, then containers (CLAUDE.md): the show-folder rename goes last
    because it invalidates every path recorded above it, and undo walks the log
    backwards so that resolves itself.
    """
    ops: List[Operation] = []
    dest = plan.season_dir_target
    if dest is not None and not dest.exists():
        ops.append(Operation(op=OP_MKDIR, dst=str(dest)))

    for src, dst in core.order_moves(plan.moves):
        ops.append(Operation(op=OP_MOVE, src=str(src), dst=str(dst)))

    # The source folder, if the files left it entirely. Only the 'Season YYYY'
    # fallback form is cleaned up: a real 'Season 1' emptied by a season change
    # is left in place, since an empty folder is harmless and removing one the
    # user did not ask about is not (README).
    if plan.files:
        source_dir = plan.files[0].source.parent
        _, was_year = core.parse_season_dir(source_dir.name)
        if was_year and dest is not None and dest != source_dir:
            ops.append(Operation(op=OP_RMDIR, dst=str(source_dir)))

    show_dir = _show_dir_to_rename(plan)
    if show_dir is not None:
        ops.append(Operation(op=OP_MOVE_DIR, src=str(show_dir),
                             dst=str(plan.show_dir_target)))
    return ops


def _final_paths(plan: core.Plan, ops: Sequence[Operation]):
    """Where the season and show folder end up, for the page to navigate to."""
    season = plan.season_dir_target
    show = season.parent if season is not None else None
    for op in ops:
        if op.op == OP_MOVE_DIR:
            show = Path(op.dst)
            if season is not None:
                season = show / season.name
    return season, show


# ── Running it ─────────────────────────────────────────────────────────────

def _manifest_name(fingerprint: str, undo_dir: Path,
                   now: Optional[float] = None) -> str:
    """Sortable timestamp plus the plan it came from.

    Suffixed if that name is taken: two runs of the same plan within one second
    is unlikely, but overwriting the record of how to reverse the first one is
    not an acceptable way to find out.
    """
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    base = "{}-{}".format(stamp, fingerprint)
    name = base + ".json"
    n = 2
    while (Path(undo_dir) / name).exists():
        name = "{}-{}.json".format(base, n)
        n += 1
    return name


def _write_manifest(path: Path, body: Dict[str, Any]) -> None:
    tmp = path.with_name(path.name + ".part")
    with tmp.open("w") as fh:
        json.dump(body, fh, indent=2, sort_keys=True)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(str(tmp), str(path))


def _manifest_body(plan: core.Plan, ops: Sequence[Operation],
                   season_dir: Optional[Path]) -> Dict[str, Any]:
    return {
        "version": MANIFEST_VERSION,
        "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "fingerprint": plan.fingerprint(),
        "season_dir": str(season_dir) if season_dir else None,
        "undone": False,
        "operations": [o.to_json() for o in ops],
    }


def execute_plan(plan: core.Plan, roots: Sequence[Path], undo_dir: Path,
                 now: Optional[float] = None) -> Result:
    """Apply a plan. The only entry point that moves a real file.

    Refuses without touching anything if pre-flight finds a problem or the undo
    manifest cannot be written. Past that point it keeps going and reports per
    operation: a failure on one file is not a reason to leave the rest of a
    season half-renamed under two conventions.
    """
    problems = preflight(plan, roots)
    if problems:
        return Result(refused=problems)

    ops = plan_operations(plan)
    # Where the files came from, so undo can send the page back to the folder
    # the run started in rather than to one that no longer exists.
    source_season = (plan.files[0].source.parent if plan.files
                     else plan.season_dir_target)
    manifest_name = None
    try:
        Path(undo_dir).mkdir(parents=True, exist_ok=True)
        manifest_name = _manifest_name(plan.fingerprint(), undo_dir, now)
        manifest_path = Path(undo_dir) / manifest_name
        _write_manifest(manifest_path, _manifest_body(plan, ops, source_season))
    except OSError as e:
        return Result(refused=[
            "Could not write the undo manifest to {} ({}). Nothing was "
            "renamed.".format(Path(undo_dir) / (manifest_name or ""), e)])

    for op in ops:
        _apply(op)

    season, show = _final_paths(plan, ops)
    result = Result(ops=ops, manifest=manifest_name,
                    season_dir=str(season) if season else None,
                    show_dir=str(show) if show else None)
    # Rewrite with what actually happened, so undo reverses the real run. A
    # crash before this leaves the planned log in place, which undo treats as
    # 'try each op and skip whatever is not there'.
    try:
        _write_manifest(manifest_path, _manifest_body(plan, ops, source_season))
    except OSError:
        pass
    return result


def _apply(op: Operation) -> None:
    try:
        if op.op == OP_MKDIR:
            Path(op.dst).mkdir(parents=True, exist_ok=True)
        elif op.op in (OP_MOVE, OP_MOVE_DIR):
            src, dst = Path(op.src), Path(op.dst)
            if not src.exists():
                op.error = "{} is no longer there".format(src.name)
                return
            # Never os.replace: a destination that appeared since pre-flight
            # must fail the one file, not silently overwrite it.
            if dst.exists():
                op.error = "{} already exists".format(dst.name)
                return
            os.rename(str(src), str(dst))
        elif op.op == OP_RMDIR:
            path = Path(op.dst)
            if not path.exists():
                return
            if any(path.iterdir()):
                # Something else lives here — a sidecar, a skipped file, or a
                # recording that failed to move. Left alone by design.
                return
            path.rmdir()
        else:
            op.error = "unknown operation {}".format(op.op)
            return
        op.applied = True
    except OSError as e:
        op.error = str(e)


# ── Undo ───────────────────────────────────────────────────────────────────

def load_manifest(name: str, undo_dir: Path) -> Dict[str, Any]:
    """Read a manifest by bare filename.

    The name arrives from a browser, so it is a filename and nothing else: a
    path segment here would read JSON from anywhere on the disk and then be
    handed to os.rename.
    """
    if not name or "/" in name or "\\" in name or "\0" in name or name in (".", ".."):
        raise ManifestError("not a manifest name: {!r}".format(name))
    if not name.endswith(".json"):
        raise ManifestError("not a manifest name: {!r}".format(name))
    path = Path(undo_dir) / name
    if Path(path.name) != Path(name) or not path.is_file():
        raise ManifestError("no such undo manifest: {}".format(name))
    try:
        with path.open() as fh:
            body = json.load(fh)
    except (OSError, ValueError) as e:
        raise ManifestError("cannot read {}: {}".format(name, e))
    if not isinstance(body, dict) or body.get("version") != MANIFEST_VERSION:
        raise ManifestError("{} is not a version {} manifest".format(
            name, MANIFEST_VERSION))
    return body


def undo(name: str, undo_dir: Path, roots: Sequence[Path]) -> Result:
    """Reverse a run.

    The forward order guarantees no move lands on an occupied name; applying
    the inverses in reverse order therefore walks back through states that
    genuinely existed, which is why chains and parked cycles come apart
    correctly without any special handling here.
    """
    body = load_manifest(name, undo_dir)
    if body.get("undone"):
        return Result(refused=["{} has already been undone.".format(name)],
                      manifest=name)

    ops = [Operation.from_json(d) for d in (body.get("operations") or [])]
    applied = [o for o in ops if o.applied]
    if not applied:
        return Result(refused=["{} recorded no completed changes.".format(name)],
                      manifest=name)

    # A manifest is a file full of paths that will be handed to os.rename, and
    # it outlives the config it was written under. Re-confine every one of them.
    for op in applied:
        for raw in (op.src, op.dst):
            if raw is None:
                continue
            try:
                resolve_within_roots(raw, roots)
            except OutsideRoots as e:
                return Result(refused=[str(e)], manifest=name)

    reversed_ops: List[Operation] = []
    for op in reversed(applied):
        if op.op in (OP_MOVE, OP_MOVE_DIR):
            back = Operation(op=op.op, src=op.dst, dst=op.src)
        elif op.op == OP_MKDIR:
            back = Operation(op=OP_RMDIR, dst=op.dst)
        elif op.op == OP_RMDIR:
            back = Operation(op=OP_MKDIR, dst=op.dst)
        else:
            back = Operation(op=op.op, src=op.src, dst=op.dst,
                             error="cannot reverse {}".format(op.op))
            reversed_ops.append(back)
            continue
        _apply(back)
        reversed_ops.append(back)

    result = Result(ops=reversed_ops, manifest=name)
    season = body.get("season_dir")
    result.season_dir = season
    result.show_dir = str(Path(season).parent) if season else None
    if result.ok:
        body["undone"] = True
        body["undone_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            _write_manifest(Path(undo_dir) / name, body)
        except OSError:
            pass
    return result
