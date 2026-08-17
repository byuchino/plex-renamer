"""Flask layer.

Routing and request parsing only. This module computes no names — that is
core.py — and moves nothing itself: every filesystem change goes through
execute.py, which is the single module in the repo allowed to make one.
`POST /api/plan` stays read-only; `POST /api/execute` and `POST /api/undo` act.

The page deliberately does not build filenames in JavaScript. Every keystroke
in the form comes back here and re-plans, so the naming convention has exactly
one implementation, in core.py, under test. See README "Design".

Targets Python 3.8 (the deployment VM).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from flask import Flask, jsonify, render_template, request

import core
import execute
from config import Config, OutsideRoots, load_config, resolve_within_roots

# Directories that are never useful to browse into and only add noise.
# '@eaDir' and '#recycle' are both Synology's, and both show up in these
# libraries alongside the real folders.
HIDDEN_DIR_PREFIXES = (".", "@", "#")

log = logging.getLogger(__name__)

# /api/plan is called on every keystroke, so it is logged at DEBUG and off by
# default — an INFO line per keystroke would bury the lines that matter.
# Everything that changes the library, or refuses to, is logged by execute.py.

# How many past runs the history list returns by default.
RUNS_LIMIT = 50


def _error(message: str, code: int = 400):
    return jsonify({"error": message}), code


def _dir_entries(path: Path) -> List[Path]:
    """One listing, sorted. Everything the request needs is derived from this.

    Deliberately not recursive: a root can hold a hundred show folders, and
    these live on NFS mounts — the Hawaii one across a WireGuard tunnel — so
    descending to count files per child would turn one listing into a hundred
    round trips on every browse.
    """
    try:
        return sorted(path.iterdir(), key=lambda p: p.name.lower())
    except PermissionError:
        return []


def _resolve(cfg: Config, raw: Optional[str]) -> Path:
    """Caller-supplied path -> a real path known to be inside a root."""
    if not raw:
        raise OutsideRoots("no path given")
    return resolve_within_roots(raw, cfg.roots)


# Counting renameable files means one extra listing per child directory. That
# is fine for a show folder holding a handful of seasons and wrong for a root
# holding 202 shows, especially with one library across a WireGuard tunnel.
CHILD_COUNT_LIMIT = 40


def _crumbs(cfg: Config, path: Path) -> List[Dict[str, str]]:
    """The path split into clickable ancestors, from its root downward.

    Navigation is confined to roots, so the trail stops at whichever root
    contains the path rather than walking up to '/'.
    """
    for root in cfg.roots:
        try:
            rel = path.relative_to(root)
        except ValueError:
            continue
        out = [{"name": root.name or str(root), "path": str(root)}]
        cur = root
        for part in rel.parts:
            cur = cur / part
            out.append({"name": part, "path": str(cur)})
        return out
    return []


def _parent_within_roots(cfg: Config, path: Path) -> Optional[str]:
    """The 'up' link, or None when already at a root — browsing must not be
    able to walk out the top of a configured root."""
    if any(path == root for root in cfg.roots):
        return None
    try:
        return str(resolve_within_roots(path.parent, cfg.roots))
    except OutsideRoots:
        return None


def _str_or_none(value: Any) -> Optional[str]:
    """Treat an explicit empty string as 'omit this', not as a value.

    The distinction matters for year and id: absent means "derive it from the
    folder", empty means "the user cleared the box and wants it left out".
    """
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class BadRequest(Exception):
    """A request body that cannot be turned into a plan."""

    def __init__(self, message: str, code: int = 400):
        super().__init__(message)
        self.message = message
        self.code = code


def _plan_from_payload(cfg: Config,
                       payload: Dict[str, Any]) -> Tuple[Path, "core.Defaults",
                                                          Dict[str, Any], "core.Plan"]:
    """Request body -> (season dir, derived defaults, resolved inputs, plan).

    /api/execute rebuilds its plan through this same function, from a fresh
    directory listing, rather than trusting the one the browser is showing —
    that recomputation is what the fingerprint comparison is checking against.
    """
    try:
        season_dir = _resolve(cfg, payload.get("path"))
    except OutsideRoots as e:
        raise BadRequest(str(e))
    if not season_dir.is_dir():
        raise BadRequest("not a directory: {}".format(season_dir))

    entries = _dir_entries(season_dir)
    defaults = core.derive_defaults(season_dir, entries)

    # Absent key -> use the value derived from the path. Present key -> use it,
    # including when it is empty (see _str_or_none).
    show_name = payload["show_name"] if "show_name" in payload else defaults.show_name
    show_name = (show_name or "").strip()
    year = _str_or_none(payload["year"]) if "year" in payload else defaults.year
    ident = _str_or_none(payload["ident"]) if "ident" in payload else defaults.ident
    ident_source = payload.get("ident_source") or defaults.ident_source
    if ident_source not in ("tmdb", "tvdb"):
        raise BadRequest("ident_source must be tmdb or tvdb")

    try:
        season = int(payload["season"]) if "season" in payload else defaults.season
        per_episode = int(payload.get("per_episode", 1))
        episodes_per_file = int(payload.get("episodes_per_file", 1))
    except (TypeError, ValueError):
        raise BadRequest(
            "season, per_episode and episodes_per_file must be whole numbers")

    anchors: Dict[str, int] = {}
    for name, value in (payload.get("anchors") or {}).items():
        try:
            anchors[str(name)] = int(value)
        except (TypeError, ValueError):
            raise BadRequest("episode anchor for {} is not a number".format(name))

    overrides: Dict[str, str] = {
        str(k): str(v) for k, v in (payload.get("name_overrides") or {}).items()
    }

    # Whether the cheap path is even on offer. The disk fact lives here rather
    # than in core so build_plan stays free of filesystem reads; core owns the
    # rules and the wording. Asking for it when it does not apply is ignored
    # rather than refused — a page left open while the folder changed underneath
    # should quietly fall back to the move path, not fail to plan at all.
    season_rename_ok, season_rename_why = core.season_rename_state(
        season_dir.name, season,
        (season_dir.parent / core.season_dir_name(season)).exists())
    rename_season_dir = bool(payload.get("rename_season_dir")) and season_rename_ok

    plan = core.build_plan(
        season_dir=season_dir,
        entries=entries,
        show_name=show_name,
        year=year,
        season=season,
        ident=ident,
        ident_source=ident_source,
        anchors=anchors,
        per_episode=per_episode,
        episodes_per_file=episodes_per_file,
        rename_show_dir=bool(payload.get("rename_show_dir")),
        rename_season_dir=rename_season_dir,
        name_overrides=overrides,
    )
    # Everything in the folder that the plan does not move. Taken from the
    # listing rather than from plan.skipped because collect_files drops
    # subdirectories silently — and a subdirectory both keeps the rmdir from
    # succeeding and rides along on a folder rename, so leaving it out would
    # make the page promise a cleanup that will not happen.
    leaving = set(pf.source for pf in plan.files)
    leftovers = sorted(p.name for p in entries if p not in leaving)
    moves_out = plan.season_dir_target != season_dir

    # Presentation state, not plan state: kept out of core.Plan so the plan
    # stays exactly the set of changes and nothing about how to describe them.
    folder_rename = {
        "available": season_rename_ok,
        "reason": season_rename_why,
        "requested": rename_season_dir,
        "from": season_dir.name,
        "to": core.season_dir_name(season),
        # Named so the description can say which files ride along instead of
        # saying "some files" — that is the sentence the decision turns on.
        "leftovers": leftovers,
        "moves_out": moves_out,
        # Mirrors plan_operations' rmdir condition exactly: year-fallback form,
        # files actually leaving, and nothing left behind to hold it open.
        "source_removed": bool(defaults.season_dir_was_year and moves_out
                               and plan.files and not leftovers),
    }

    inputs = {
        "show_name": show_name,
        "year": year,
        "season": season,
        "ident": ident,
        "ident_source": ident_source,
        "per_episode": per_episode,
        "episodes_per_file": episodes_per_file,
        "rename_show_dir": bool(payload.get("rename_show_dir")),
        # The gated value, not the requested one, so the manifest records what
        # the run actually did.
        "rename_season_dir": rename_season_dir,
    }
    return season_dir, defaults, inputs, plan, folder_rename


def _plan_response(cfg: Config, payload: Dict[str, Any]):
    try:
        season_dir, defaults, inputs, plan, folder_rename = _plan_from_payload(
            cfg, payload)
    except BadRequest as e:
        return _error(e.message, e.code)

    return jsonify({
        "path": str(season_dir),
        # Echoed back so the form can populate itself on first load from the
        # same derivation the plan used, rather than a second one in JS.
        "inputs": inputs,
        "season_dir_was_year": defaults.season_dir_was_year,
        "season_dir_target": str(plan.season_dir_target) if plan.season_dir_target else None,
        "season_dir_rename_to": (str(plan.season_dir_rename_to)
                                 if plan.season_dir_rename_to else None),
        "folder_rename": folder_rename,
        "show_dir_target": str(plan.show_dir_target) if plan.show_dir_target else None,
        # What the show folder *would* become, computed whether or not the box
        # is ticked — the checkbox is unreadable if the name it offers only
        # appears after you have already agreed to it.
        "show_dir_current": season_dir.parent.name,
        # Whether ticking the box would actually move the folder. A show folder
        # already carrying the right name makes the tick a no-op, and the Rename
        # button must not light up for a run with nothing in it.
        "show_dir_changes": bool(plan.show_dir_target is not None
                                 and plan.show_dir_target != season_dir.parent),
        "show_dir_preview": core.build_show_dir_name(
            inputs["show_name"], inputs["year"], inputs["ident"],
            inputs["ident_source"]),
        "files": [
            {
                "source": str(f.source),
                "source_name": f.source.name,
                "kind": f.kind,
                "episode": f.episode,
                "episode_end": f.episode_end,
                "target_name": f.target_name,
                "unchanged": f.unchanged,
                "issues": f.issues,
            }
            for f in plan.files
        ],
        "skipped": [{"name": p.name, "reason": why} for p, why in plan.skipped],
        "issues": plan.issues,
        "notes": plan.notes,
        "ok": plan.ok,
        "move_count": len(plan.moves),
        "file_count": len(plan.files),
        "fingerprint": plan.fingerprint(),
    })


def create_app(config: Optional[Config] = None) -> Flask:
    cfg = config or load_config()
    app = Flask(__name__)

    @app.route("/")
    def index():
        return render_template("index.html")

    @app.route("/api/browse")
    def browse():
        raw = request.args.get("path")
        if not raw:
            # No path: offer the configured roots, which is where every
            # navigation has to start.
            return jsonify({
                "path": None,
                "parent": None,
                "at_top": True,
                "crumbs": [],
                "dirs": [{"name": str(r), "path": str(r)} for r in cfg.roots],
                "renameable": 0,
                "skipped": 0,
            })
        try:
            path = _resolve(cfg, raw)
        except OutsideRoots as e:
            return _error(str(e))
        if not path.is_dir():
            return _error("not a directory: {}".format(path))

        entries = _dir_entries(path)
        renameable, skipped = core.collect_files(entries)
        children = [p for p in entries
                    if p.is_dir() and not p.name.startswith(HIDDEN_DIR_PREFIXES)]
        dirs = [{"name": p.name, "path": str(p)} for p in children]
        if len(children) <= CHILD_COUNT_LIMIT:
            # Small enough to be worth telling the user which subfolder holds
            # something renameable, so season folders can be picked at a glance.
            for entry, child in zip(dirs, children):
                found, _ = core.collect_files(_dir_entries(child))
                entry["renameable"] = len(found)
        return jsonify({
            "path": str(path),
            "parent": _parent_within_roots(cfg, path),
            "at_top": False,
            "crumbs": _crumbs(cfg, path),
            "dirs": dirs,
            # Lets the browser show which folder is worth opening without a
            # second request per directory.
            "renameable": len(renameable),
            "skipped": len(skipped),
        })

    @app.route("/api/plan", methods=["POST"])
    def plan():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("expected a JSON object")
        return _plan_response(cfg, payload)

    @app.route("/api/execute", methods=["POST"])
    def execute_route():
        """Apply the plan the browser confirmed — if it is still that plan.

        The body is the same one /api/plan takes, plus the fingerprint of what
        the user actually agreed to. The plan is rebuilt here from a fresh
        listing and its fingerprint compared, so a folder that changed between
        the confirmation dialog and this request is refused whole rather than
        half-applied against a stale set of names.
        """
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("expected a JSON object")
        confirmed = payload.get("fingerprint")
        if not confirmed:
            return _error("missing plan fingerprint")
        try:
            season_dir, _defaults, inputs, plan, _fr = _plan_from_payload(
                cfg, payload)
        except BadRequest as e:
            return _error(e.message, e.code)
        if plan.fingerprint() != confirmed:
            # Logged here rather than in execute.py, which never sees this: the
            # plan was rebuilt and no longer matches what the user agreed to.
            log.warning("execute REFUSED for %s: fingerprint %s was confirmed, "
                        "plan is now %s", season_dir, confirmed,
                        plan.fingerprint())
            return jsonify({
                "error": "This folder changed since the plan was confirmed. "
                         "Nothing was renamed — review the new plan and try again.",
                "fingerprint": plan.fingerprint(),
            }), 409

        # inputs go into the manifest so a run in the history can be explained
        # later, not just replayed as a list of paths.
        result = execute.execute_plan(plan, cfg.roots, cfg.undo_dir,
                                      inputs=inputs, keep_runs=cfg.keep_runs)
        # 409 is 'refused, nothing touched'. A run that started and hit a
        # per-file error is a 200 carrying its own report: the caller needs the
        # detail, not a status code.
        return jsonify(result.to_json()), (409 if result.refused else 200)

    @app.route("/api/undo", methods=["POST"])
    def undo_route():
        payload = request.get_json(silent=True)
        if not isinstance(payload, dict):
            return _error("expected a JSON object")
        try:
            result = execute.undo(str(payload.get("manifest") or ""),
                                  cfg.undo_dir, cfg.roots)
        except execute.ManifestError as e:
            return _error(str(e), 404)
        return jsonify(result.to_json()), (409 if result.refused else 200)

    @app.route("/api/runs")
    def runs():
        """Every run still on disk, newest first.

        Deliberately not scoped to the folder being browsed: the point of the
        history is to reach a run you have navigated away from, which is the
        only way to undo one after closing the result dialog.
        """
        try:
            limit = int(request.args.get("limit", RUNS_LIMIT))
        except (TypeError, ValueError):
            return _error("limit must be a whole number")
        runs = execute.list_runs(cfg.undo_dir, max(1, min(limit, 500)))
        return jsonify({
            "runs": [r.to_json() for r in runs],
            "keep_runs": cfg.keep_runs,
        })

    @app.route("/api/runs/<name>")
    def run_detail(name):
        """One run in full, including every operation and its outcome.

        The list stays light and the detail is fetched on expand; a hundred runs
        of sixty files each is not a payload worth sending to draw one row.
        """
        try:
            body = execute.load_manifest(name, cfg.undo_dir)
        except execute.ManifestError as e:
            return _error(str(e), 404)
        summary = execute.summarise_manifest(name, body)
        out = summary.to_json()
        out["operations"] = body.get("operations") or []
        return jsonify(out)

    return app


def main() -> None:
    cfg = load_config()
    # Logging goes to stderr and nowhere else. Under Phase 4's systemd unit
    # journald owns persistence and rotation; owning a log file here would mean
    # owning rotation too, and would put file writing into this module, which
    # test_no_module_can_mutate_the_filesystem exists to prevent.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    create_app(cfg).run(host=cfg.bind_host, port=cfg.bind_port, threaded=True)


if __name__ == "__main__":
    main()
