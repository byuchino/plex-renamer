"""Configuration and path confinement.

Root confinement is the guardrail that keeps an unauthenticated LAN page from
browsing the whole filesystem. It lives here rather than in the Flask layer so
the CLI is subject to exactly the same rule.
"""
from __future__ import annotations

import configparser
import os
from dataclasses import dataclass
from pathlib import Path
from typing import List

CONFIG_PATH = os.environ.get("PLEX_RENAMER_CONFIG", "/etc/plex-renamer/config.ini")


@dataclass
class Config:
    roots: List[Path]
    bind_host: str = "0.0.0.0"
    bind_port: int = 8101
    undo_dir: Path = Path("/var/lib/plex-renamer/undo")


class OutsideRoots(ValueError):
    """Raised for any path that does not resolve inside a configured root."""


def load_config(path: str = None) -> Config:
    path = path or CONFIG_PATH
    cp = configparser.ConfigParser()
    if not cp.read(path):
        raise FileNotFoundError("config file not found: {}".format(path))

    roots = [Path(r.strip()) for r in cp.get("general", "roots").split(",") if r.strip()]
    if not roots:
        raise ValueError("no roots configured — the tool would have nothing to browse")
    return Config(
        roots=[r.resolve() for r in roots],
        bind_host=cp.get("server", "bind_host", fallback="0.0.0.0"),
        bind_port=cp.getint("server", "bind_port", fallback=8101),
        undo_dir=Path(cp.get("general", "undo_dir",
                             fallback="/var/lib/plex-renamer/undo")),
    )


def resolve_within_roots(candidate, roots: List[Path]) -> Path:
    """Resolve a caller-supplied path and confirm it is inside a root.

    Resolution happens before the check, so '..' segments and symlinks are
    collapsed first — checking the string beforehand would be trivially
    defeated by 'root/../../etc'.
    """
    p = Path(candidate).resolve()
    for root in roots:
        try:
            p.relative_to(root)
        except ValueError:
            continue
        return p
    raise OutsideRoots("{} is not inside any configured root".format(p))
