"""Tests for the one module that changes the filesystem.

Everything here runs against a real temp tree rather than a mock, because the
properties being checked are properties of the filesystem: that a refusal
leaves the directory byte-identical, that a manifest exists before the first
move, and that undo puts every file back where it started.
"""
import json
import os
from pathlib import Path

import pytest

import core
import execute

TIMECODES = [
    "2026-06-22 23 00 00",
    "2026-06-23 17 00 00",
    "2026-06-29 23 00 00",
]


@pytest.fixture
def tree(tmp_path):
    """A 'Plex gave up' folder: year season dir, timecode filenames."""
    root = (tmp_path / "TV Shows").resolve()
    season = root / "Nagatan and Aoto (2026) {tvdb-428763}" / "Season 2026"
    season.mkdir(parents=True)
    for tc in TIMECODES:
        (season / "Nagatan and Aoto (2026) - {}.mp4".format(tc)).touch()
    (season / "poster.jpg").touch()
    return root, season


@pytest.fixture
def undo_dir(tmp_path):
    return tmp_path / "undo"


def make_plan(folder, **kw):
    kw.setdefault("show_name", "Nagatan and Aoto")
    kw.setdefault("year", "2026")
    kw.setdefault("season", 1)
    return core.build_plan(season_dir=folder, entries=sorted(folder.iterdir()), **kw)


def snapshot(root):
    return sorted(str(p.relative_to(root)) for p in root.rglob("*"))


# ── The happy path ─────────────────────────────────────────────────────────

def test_a_run_moves_the_files_into_a_real_season_folder(tree, undo_dir):
    root, season = tree
    result = execute.execute_plan(make_plan(season), [root], undo_dir)
    assert result.refused == []
    assert result.ok

    dest = season.parent / "Season 01"
    assert sorted(p.name for p in dest.iterdir()) == [
        "Nagatan and Aoto (2026) - S01E0{} - {}.mp4".format(i + 1, tc)
        for i, tc in enumerate(TIMECODES)
    ]
    assert result.season_dir == str(dest)


def test_a_year_folder_with_anything_left_in_it_is_not_removed(tree, undo_dir):
    """The poster is skipped, not moved, so the folder is still in use. Only a
    folder left completely empty is cleaned up (see the undo test below, which
    exercises the removal)."""
    root, season = tree
    execute.execute_plan(make_plan(season), [root], undo_dir)
    assert season.is_dir()
    assert [p.name for p in season.iterdir()] == ["poster.jpg"]


def test_a_real_season_folder_is_never_removed(tmp_path, undo_dir):
    """Only the year-fallback form is cleaned up. An emptied 'Season 1' stays:
    removing a folder the user did not ask about is the greater surprise."""
    root = (tmp_path / "TV").resolve()
    season = root / "Some Show (2019)" / "Season 1"
    season.mkdir(parents=True)
    (season / "Some Show (2019) - {}.mkv".format(TIMECODES[0])).touch()
    execute.execute_plan(
        make_plan(season, show_name="Some Show", year="2019", season=2),
        [root], undo_dir)
    assert season.is_dir()
    assert list(season.iterdir()) == []


def test_a_correctly_named_season_folder_is_renamed_in_place(tmp_path, undo_dir):
    root = (tmp_path / "TV").resolve()
    season = root / "Some Show (2019)" / "Season 01"
    season.mkdir(parents=True)
    (season / "Some Show (2019) - {}.mkv".format(TIMECODES[0])).touch()
    result = execute.execute_plan(
        make_plan(season, show_name="Some Show", year="2019", season=1),
        [root], undo_dir)
    assert result.ok
    assert [p.name for p in season.iterdir()] == \
        ["Some Show (2019) - S01E01 - {}.mkv".format(TIMECODES[0])]
    # no second folder was created beside it
    assert [p.name for p in season.parent.iterdir()] == ["Season 01"]


def test_the_show_folder_rename_happens_last_and_carries_everything(tree, undo_dir):
    root, season = tree
    plan = make_plan(season, show_name="Renamed Show", rename_show_dir=True,
                     ident="428763", ident_source="tvdb")
    result = execute.execute_plan(plan, [root], undo_dir)
    assert result.ok

    show = root / "Renamed Show (2026) {tvdb-428763}"
    assert show.is_dir()
    assert result.show_dir == str(show)
    # the season path reported back is under the new folder name, so the page
    # can navigate to where the files actually are
    assert result.season_dir == str(show / "Season 01")
    assert (show / "Season 01" / "Renamed Show (2026) - S01E01 - {}.mp4".format(
        TIMECODES[0])).is_file()


def test_a_chain_of_renumbered_files_does_not_lose_one(tmp_path, undo_dir):
    """Shifting a run up by one: E01->E02, E02->E03, E03->E04. In listing order
    the first move would overwrite E02 and lose a file."""
    root = (tmp_path / "TV").resolve()
    season = root / "Some Show (2019)" / "Season 01"
    season.mkdir(parents=True)
    name = "Some Show (2019) - S01E0{} - t.mkv"
    for n in (1, 2, 3):
        (season / name.format(n)).touch()
    plan = make_plan(season, show_name="Some Show", year="2019", season=1,
                     anchors={name.format(n): n + 1 for n in (1, 2, 3)})
    result = execute.execute_plan(plan, [root], undo_dir)
    assert result.ok, result.to_json()
    assert sorted(p.name for p in season.iterdir()) == \
        [name.format(n) for n in (2, 3, 4)]


# ── Refusals: nothing is touched at all ────────────────────────────────────

def test_a_plan_with_a_bad_row_is_refused_whole(tree, undo_dir):
    root, season = tree
    before = snapshot(root)
    first = "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[0])
    plan = make_plan(season, name_overrides={first: "../escape.mp4"})
    result = execute.execute_plan(plan, [root], undo_dir)
    assert result.refused
    assert not result.ok
    assert snapshot(root) == before
    assert not undo_dir.exists()          # not even a manifest


def test_a_plan_that_changes_nothing_is_refused(tmp_path, undo_dir):
    root = (tmp_path / "TV").resolve()
    season = root / "Some Show (2019)" / "Season 01"
    season.mkdir(parents=True)
    (season / "Some Show (2019) - S01E01 - t.mkv").touch()
    result = execute.execute_plan(
        make_plan(season, show_name="Some Show", year="2019", season=1),
        [root], undo_dir)
    assert result.refused == ["Nothing to rename — no file would change."]


def test_a_target_outside_the_roots_is_refused(tree, undo_dir, tmp_path):
    """Defence in depth: the plan is re-confined here, not trusted because
    /api/plan looked at it."""
    root, season = tree
    plan = make_plan(season)
    plan.files[0].target = tmp_path / "elsewhere" / "escaped.mp4"
    before = snapshot(root)
    result = execute.execute_plan(plan, [root], undo_dir)
    assert any("not inside any configured root" in r for r in result.refused)
    assert snapshot(root) == before


def test_an_occupied_target_is_refused(tree, undo_dir):
    root, season = tree
    dest = season.parent / "Season 01"
    dest.mkdir()
    (dest / "Nagatan and Aoto (2026) - S01E01 - {}.mp4".format(TIMECODES[0])).touch()
    before = snapshot(root)
    result = execute.execute_plan(make_plan(season), [root], undo_dir)
    assert any("already exists" in r for r in result.refused)
    assert snapshot(root) == before


def test_a_show_folder_rename_onto_an_existing_folder_is_refused(tree, undo_dir):
    root, season = tree
    (root / "Renamed Show (2026)").mkdir()
    plan = make_plan(season, show_name="Renamed Show", ident=None,
                     rename_show_dir=True)
    before = snapshot(root)
    result = execute.execute_plan(plan, [root], undo_dir)
    assert any("already exists" in r for r in result.refused)
    assert snapshot(root) == before


def test_a_missing_source_is_refused_before_anything_moves(tree, undo_dir):
    root, season = tree
    plan = make_plan(season)
    plan.files[1].source.unlink()          # vanished after the plan was built
    before = snapshot(root)
    result = execute.execute_plan(plan, [root], undo_dir)
    assert any("no longer there" in r for r in result.refused)
    assert snapshot(root) == before


def test_an_unwritable_folder_is_refused(tree, undo_dir):
    root, season = tree
    os.chmod(str(season), 0o500)
    try:
        result = execute.execute_plan(make_plan(season), [root], undo_dir)
        assert any("Cannot write to" in r for r in result.refused)
    finally:
        os.chmod(str(season), 0o700)


def test_a_manifest_that_cannot_be_written_refuses_the_run(tree, tmp_path):
    """No undo record, no moves. A run that cannot be reversed is worse than a
    run that did not happen."""
    root, season = tree
    blocked = tmp_path / "blocked"
    blocked.touch()                        # a file where the undo dir should be
    before = snapshot(root)
    result = execute.execute_plan(make_plan(season), [root], blocked / "undo")
    assert any("undo manifest" in r for r in result.refused)
    assert snapshot(root) == before


# ── The manifest ───────────────────────────────────────────────────────────

def test_the_manifest_is_written_before_the_first_move(tree, undo_dir, monkeypatch):
    """Checked by looking at the disk from inside the first rename."""
    root, season = tree
    seen = {}
    real_rename = os.rename

    def spy(src, dst):
        seen.setdefault("manifests", sorted(p.name for p in undo_dir.iterdir()))
        return real_rename(src, dst)

    monkeypatch.setattr(execute.os, "rename", spy)
    result = execute.execute_plan(make_plan(season), [root], undo_dir)
    assert result.ok
    assert seen["manifests"] == [result.manifest]


def test_the_manifest_records_every_operation_that_happened(tree, undo_dir):
    root, season = tree
    result = execute.execute_plan(make_plan(season), [root], undo_dir)
    body = json.loads((undo_dir / result.manifest).read_text())
    assert body["version"] == execute.MANIFEST_VERSION
    assert body["undone"] is False
    assert body["season_dir"] == str(season)
    ops = body["operations"]
    assert [o["op"] for o in ops] == \
        [execute.OP_MKDIR] + [execute.OP_MOVE] * len(TIMECODES) + [execute.OP_RMDIR]
    assert all(o["applied"] for o in ops[:-1])
    # the rmdir was planned and skipped: poster.jpg is still in the folder
    assert ops[-1]["applied"] is False


def test_the_manifest_name_carries_the_fingerprint(tree, undo_dir):
    root, season = tree
    plan = make_plan(season)
    result = execute.execute_plan(plan, [root], undo_dir, now=0)
    assert result.manifest == "19700101T000000Z-{}.json".format(plan.fingerprint())


# ── Undo ───────────────────────────────────────────────────────────────────

def test_undo_puts_every_file_back(tree, undo_dir):
    root, season = tree
    before = snapshot(root)
    result = execute.execute_plan(make_plan(season), [root], undo_dir)
    assert snapshot(root) != before

    back = execute.undo(result.manifest, undo_dir, [root])
    assert back.refused == []
    assert back.ok
    # the created 'Season 01' is gone again, and every file is under its old name
    assert snapshot(root) == before


def test_undo_reverses_a_show_folder_rename_first(tree, undo_dir):
    """The recorded file paths sit under the old folder name, so the folder has
    to go back before them — which is why the log is undone in reverse."""
    root, season = tree
    before = snapshot(root)
    plan = make_plan(season, show_name="Renamed Show", rename_show_dir=True,
                     ident="428763", ident_source="tvdb")
    result = execute.execute_plan(plan, [root], undo_dir)
    assert (root / "Renamed Show (2026) {tvdb-428763}").is_dir()

    back = execute.undo(result.manifest, undo_dir, [root])
    assert back.ok, back.to_json()
    assert snapshot(root) == before


def test_undo_restores_a_removed_year_folder(tmp_path, undo_dir):
    root = (tmp_path / "TV").resolve()
    season = root / "Show (2026)" / "Season 2026"
    season.mkdir(parents=True)
    (season / "Show (2026) - {}.mp4".format(TIMECODES[0])).touch()
    before = snapshot(root)

    result = execute.execute_plan(
        make_plan(season, show_name="Show", year="2026", season=1), [root], undo_dir)
    assert not season.exists()

    back = execute.undo(result.manifest, undo_dir, [root])
    assert back.ok, back.to_json()
    assert snapshot(root) == before


def test_undo_of_a_swap_comes_apart_again(tmp_path, undo_dir):
    root = (tmp_path / "TV").resolve()
    season = root / "Show (2019)" / "Season 01"
    season.mkdir(parents=True)
    names = ["Show (2019) - S01E0{} - t.mkv".format(n) for n in (1, 2)]
    for n in names:
        (season / n).touch()
    before = snapshot(root)

    plan = make_plan(season, show_name="Show", year="2019", season=1,
                     anchors={names[0]: 2, names[1]: 1})
    result = execute.execute_plan(plan, [root], undo_dir)
    assert result.ok, result.to_json()
    assert snapshot(root) == before          # a swap ends up with the same names

    back = execute.undo(result.manifest, undo_dir, [root])
    assert back.ok, back.to_json()
    assert snapshot(root) == before


def test_a_manifest_can_only_be_undone_once(tree, undo_dir):
    root, season = tree
    result = execute.execute_plan(make_plan(season), [root], undo_dir)
    assert execute.undo(result.manifest, undo_dir, [root]).ok
    again = execute.undo(result.manifest, undo_dir, [root])
    assert any("already been undone" in r for r in again.refused)


def test_undo_refuses_paths_that_left_the_roots(tree, undo_dir, tmp_path):
    """A manifest outlives the config it was written under, so its paths are
    re-confined at undo time rather than trusted."""
    root, season = tree
    result = execute.execute_plan(make_plan(season), [root], undo_dir)
    other = (tmp_path / "Other").resolve()
    other.mkdir()
    back = execute.undo(result.manifest, undo_dir, [other])
    assert any("not inside any configured root" in r for r in back.refused)


def test_undo_reports_a_file_it_cannot_put_back(tree, undo_dir):
    """Something in the way is a per-file failure, not a crash and not a
    silent overwrite."""
    root, season = tree
    result = execute.execute_plan(make_plan(season), [root], undo_dir)
    (season / "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[0])).touch()

    back = execute.undo(result.manifest, undo_dir, [root])
    assert not back.ok
    assert any("already exists" in (o.error or "") for o in back.ops)
    # ...and the rest still went back
    assert (season / "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[1])).is_file()


@pytest.mark.parametrize("name", [
    "", "..", "../../etc/passwd", "sub/dir.json", "nope.json", "notjson",
])
def test_a_manifest_name_is_a_filename_and_nothing_else(name, undo_dir):
    undo_dir.mkdir(parents=True)
    with pytest.raises(execute.ManifestError):
        execute.load_manifest(name, undo_dir)


def test_a_manifest_of_the_wrong_version_is_refused(undo_dir):
    undo_dir.mkdir(parents=True)
    (undo_dir / "x.json").write_text(json.dumps({"version": 99}))
    with pytest.raises(execute.ManifestError):
        execute.load_manifest("x.json", undo_dir)


def test_undoing_a_run_that_did_nothing_is_refused(undo_dir):
    undo_dir.mkdir(parents=True)
    (undo_dir / "x.json").write_text(json.dumps({
        "version": execute.MANIFEST_VERSION,
        "operations": [{"op": "move", "from": "/a", "to": "/b", "applied": False}],
    }))
    result = execute.undo("x.json", undo_dir, [Path("/")])
    assert any("no completed changes" in r for r in result.refused)


# ── Writability is tested by writing, not asked of os.access ───────────────

def test_writable_probes_by_writing_and_leaves_nothing_behind(tmp_path):
    """os.access is unusable on these NFS exports: the DSM server answers
    access(2) itself and reports W_OK false for a directory that is mode 777,
    owned by the asking uid, and demonstrably writable. Trusting it refused a
    valid plan across the whole KIKU library. So the check writes."""
    d = tmp_path / "dir"
    d.mkdir()
    assert execute._writable(d) is True
    assert list(d.iterdir()) == []          # probe cleaned up


def test_writable_is_false_when_it_really_cannot_write(tmp_path):
    d = tmp_path / "ro"
    d.mkdir()
    os.chmod(str(d), 0o500)
    try:
        assert execute._writable(d) is False
    finally:
        os.chmod(str(d), 0o700)


def test_writable_does_not_consult_os_access(tmp_path, monkeypatch):
    """The regression guard. A directory this process can write to must pass
    even when os.access lies about it, which is exactly the NFS case."""
    d = tmp_path / "nfs-like"
    d.mkdir()
    monkeypatch.setattr(execute.os, "access", lambda *a, **k: False)
    assert execute._writable(d) is True


def test_a_run_is_not_refused_when_os_access_lies(tree, undo_dir, monkeypatch):
    """End to end: the plan that the real KIKU library refused must go through."""
    root, season = tree
    monkeypatch.setattr(execute.os, "access", lambda *a, **k: False)
    result = execute.execute_plan(make_plan(season), [root], undo_dir)
    assert result.refused == []
    assert result.ok
    assert (season.parent / "Season 01").is_dir()


def test_no_probe_files_survive_a_run(tree, undo_dir):
    root, season = tree
    execute.execute_plan(make_plan(season), [root], undo_dir)
    strays = [str(p.relative_to(root)) for p in root.rglob(".plex-renamer-write-probe-*")]
    assert strays == []
