"""Flask layer.

Read-only. This module has no way to change a media library: it never imports
execute.py (which does not exist yet), and the only filesystem calls it makes
are directory listings. `POST /api/plan` computes proposed names and hands them
back as data — /api/execute and /api/undo arrive in Phase 3.

The page deliberately does not build filenames in JavaScript. Every keystroke
in the form comes back here and re-plans, so the naming convention has exactly
one implementation, in core.py, under test. See README "Design".

Targets Python 3.8 (the deployment VM).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from flask import Flask, jsonify, render_template, request

import core
from config import Config, OutsideRoots, load_config, resolve_within_roots

# Directories that are never useful to browse into and only add noise.
HIDDEN_DIR_PREFIXES = (".", "@")


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


def _plan_response(cfg: Config, payload: Dict[str, Any]):
    try:
        season_dir = _resolve(cfg, payload.get("path"))
    except OutsideRoots as e:
        return _error(str(e))
    if not season_dir.is_dir():
        return _error("not a directory: {}".format(season_dir))

    defaults = core.derive_defaults(season_dir)

    # Absent key -> use the value derived from the path. Present key -> use it,
    # including when it is empty (see _str_or_none).
    show_name = payload["show_name"] if "show_name" in payload else defaults.show_name
    show_name = (show_name or "").strip()
    year = _str_or_none(payload["year"]) if "year" in payload else defaults.year
    ident = _str_or_none(payload["ident"]) if "ident" in payload else defaults.ident
    ident_source = payload.get("ident_source") or defaults.ident_source
    if ident_source not in ("tmdb", "tvdb"):
        return _error("ident_source must be tmdb or tvdb")

    try:
        season = int(payload["season"]) if "season" in payload else defaults.season
        per_episode = int(payload.get("per_episode", 1))
    except (TypeError, ValueError):
        return _error("season and per_episode must be whole numbers")

    anchors: Dict[str, int] = {}
    for name, value in (payload.get("anchors") or {}).items():
        try:
            anchors[str(name)] = int(value)
        except (TypeError, ValueError):
            return _error("episode anchor for {} is not a number".format(name))

    overrides: Dict[str, str] = {
        str(k): str(v) for k, v in (payload.get("name_overrides") or {}).items()
    }

    plan = core.build_plan(
        season_dir=season_dir,
        entries=_dir_entries(season_dir),
        show_name=show_name,
        year=year,
        season=season,
        ident=ident,
        ident_source=ident_source,
        anchors=anchors,
        per_episode=per_episode,
        rename_show_dir=bool(payload.get("rename_show_dir")),
        name_overrides=overrides,
    )

    return jsonify({
        "path": str(season_dir),
        # Echoed back so the form can populate itself on first load from the
        # same derivation the plan used, rather than a second one in JS.
        "inputs": {
            "show_name": show_name,
            "year": year,
            "season": season,
            "ident": ident,
            "ident_source": ident_source,
            "per_episode": per_episode,
            "rename_show_dir": bool(payload.get("rename_show_dir")),
        },
        "season_dir_was_year": defaults.season_dir_was_year,
        "season_dir_target": str(plan.season_dir_target) if plan.season_dir_target else None,
        "show_dir_target": str(plan.show_dir_target) if plan.show_dir_target else None,
        "files": [
            {
                "source": str(f.source),
                "source_name": f.source.name,
                "timecode": core.timecode_of(f.source),
                "episode": f.episode,
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
        return jsonify({
            "path": str(path),
            "parent": _parent_within_roots(cfg, path),
            "at_top": False,
            "dirs": [
                {"name": p.name, "path": str(p)}
                for p in entries
                if p.is_dir() and not p.name.startswith(HIDDEN_DIR_PREFIXES)
            ],
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

    return app


def main() -> None:
    cfg = load_config()
    create_app(cfg).run(host=cfg.bind_host, port=cfg.bind_port, threaded=True)


if __name__ == "__main__":
    main()
