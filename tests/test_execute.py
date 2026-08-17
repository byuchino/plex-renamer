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


# ── The opt-in season folder rename ────────────────────────────────────────

def test_the_season_folder_is_renamed_and_no_file_crosses_a_boundary(tree, undo_dir):
    """The whole point: the same end state as the move path, reached without a
    single cross-directory move. That is what the sync propagates as a rename
    instead of a full re-upload."""
    root, season = tree
    plan = make_plan(season, rename_season_dir=True)
    result = execute.execute_plan(plan, [root], undo_dir)
    assert result.refused == []
    assert result.ok

    dest = season.parent / "Season 01"
    assert not season.exists()
    assert sorted(p.name for p in dest.iterdir()) == [
        "Nagatan and Aoto (2026) - S01E0{} - {}.mp4".format(i + 1, tc)
        for i, tc in enumerate(TIMECODES)
    ] + ["poster.jpg"]
    assert result.season_dir == str(dest)

    # Every file rename stayed inside one directory.
    for op in result.ops:
        if op.op == execute.OP_MOVE:
            assert Path(op.src).parent == Path(op.dst).parent


def test_the_season_rename_reaches_the_same_names_as_the_move_path(tree, undo_dir,
                                                                   tmp_path):
    """Two routes, one destination. If these ever diverge, the checkbox has
    become a naming choice rather than a transfer-cost choice."""
    root, season = tree
    moved = sorted(pf.target.name for pf in make_plan(season).files)
    renamed = sorted(pf.target.name
                     for pf in make_plan(season, rename_season_dir=True).files)
    assert moved == renamed


def test_the_leftover_travels_with_the_renamed_folder(tree, undo_dir):
    """The one visible difference from the move path, and the reason the page
    states it in both states: the poster is not renamed, but it does move."""
    root, season = tree
    execute.execute_plan(make_plan(season, rename_season_dir=True), [root], undo_dir)
    assert (season.parent / "Season 01" / "poster.jpg").is_file()


def test_the_season_rename_is_refused_onto_an_existing_folder(tree, undo_dir):
    """os.rename onto an empty directory succeeds on POSIX, so this has to be a
    refusal rather than something the filesystem stops for us."""
    root, season = tree
    (season.parent / "Season 01").mkdir()
    before = snapshot(root)
    result = execute.execute_plan(
        make_plan(season, rename_season_dir=True), [root], undo_dir)
    assert any("Season 01" in r for r in result.refused)
    assert snapshot(root) == before


def test_undo_puts_the_renamed_season_folder_back(tree, undo_dir):
    root, season = tree
    before = snapshot(root)
    result = execute.execute_plan(
        make_plan(season, rename_season_dir=True), [root], undo_dir)
    assert result.ok
    undone = execute.undo(result.manifest, undo_dir, [root])
    assert undone.ok
    assert snapshot(root) == before


def test_the_season_rename_happens_after_the_files_and_before_the_show(tree, undo_dir):
    """Containers after their contents, inner before outer — the ordering undo
    depends on, since it walks the log backwards."""
    root, season = tree
    plan = make_plan(season, rename_season_dir=True, rename_show_dir=True)
    ops = [op.op for op in execute.plan_operations(plan)]
    assert execute.OP_MKDIR not in ops      # nothing is created
    assert execute.OP_RMDIR not in ops      # and nothing is emptied
    assert ops.index(execute.OP_MOVE) < ops.index(execute.OP_MOVE_SEASON_DIR)
    assert ops.index(execute.OP_MOVE_SEASON_DIR) < ops.index(execute.OP_MOVE_DIR)


def test_undo_reverses_both_folder_renames(tree, undo_dir):
    root, season = tree
    before = snapshot(root)
    result = execute.execute_plan(
        make_plan(season, rename_season_dir=True, rename_show_dir=True),
        [root], undo_dir)
    assert result.ok
    assert execute.undo(result.manifest, undo_dir, [root]).ok
    assert snapshot(root) == before


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


# ── History and retention ──────────────────────────────────────────────────

def test_a_run_records_the_inputs_it_was_made_with(tree, undo_dir):
    """A history entry has to be explicable, not just replayable."""
    root, season = tree
    inputs = {"show_name": "Nagatan and Aoto", "year": "2026", "season": 1,
              "per_episode": 2, "rename_show_dir": False}
    result = execute.execute_plan(make_plan(season, per_episode=2), [root],
                                 undo_dir, inputs=inputs)
    body = json.loads((undo_dir / result.manifest).read_text())
    assert body["inputs"] == inputs


def test_list_runs_is_newest_first_and_summarises(tree, undo_dir):
    root, season = tree
    first = execute.execute_plan(make_plan(season), [root], undo_dir, now=0)
    execute.undo(first.manifest, undo_dir, [root])
    second = execute.execute_plan(make_plan(season), [root], undo_dir, now=86400)

    runs = execute.list_runs(undo_dir)
    assert [r.manifest for r in runs] == [second.manifest, first.manifest]
    now, before = runs
    assert now.renames == len(TIMECODES)      # moves only, not the mkdir/rmdir
    assert now.total == len(TIMECODES) + 2    # + mkdir + rmdir
    assert now.errors == 0
    assert (now.undone, now.undoable) == (False, True)
    assert (before.undone, before.undoable) == (True, False)
    assert before.undone_at
    assert now.season_dir == str(season)


def test_list_runs_of_an_empty_or_missing_dir_is_empty(tmp_path):
    assert execute.list_runs(tmp_path / "nope") == []
    (tmp_path / "empty").mkdir()
    assert execute.list_runs(tmp_path / "empty") == []


def test_one_corrupt_manifest_does_not_take_the_history_down(tree, undo_dir):
    """The history is exactly what you would be reading when something is
    wrong, so a bad file is skipped rather than raised."""
    root, season = tree
    good = execute.execute_plan(make_plan(season), [root], undo_dir)
    (undo_dir / "20990101T000000Z-garbage.json").write_text("{not json")
    (undo_dir / "20990102T000000Z-wrongver.json").write_text('{"version": 99}')

    runs = execute.list_runs(undo_dir)
    assert [r.manifest for r in runs] == [good.manifest]


def test_list_runs_honours_a_limit(tree, undo_dir):
    root, season = tree
    for i in range(3):
        r = execute.execute_plan(make_plan(season), [root], undo_dir, now=i * 86400)
        execute.undo(r.manifest, undo_dir, [root])
    assert len(execute.list_runs(undo_dir)) == 3
    assert len(execute.list_runs(undo_dir, limit=2)) == 2


def test_pruning_drops_the_oldest_beyond_the_cap(undo_dir):
    undo_dir.mkdir(parents=True)
    names = ["2026081{}T000000Z-aaa.json".format(i) for i in range(1, 6)]
    for n in names:
        (undo_dir / n).write_text('{"version": 1, "operations": []}')
    (undo_dir / "not-a-manifest.txt").write_text("x")

    removed = execute.prune_runs(undo_dir, keep=2)
    assert removed == names[:3]                      # the three oldest
    assert sorted(p.name for p in undo_dir.iterdir()) == \
        sorted(names[3:] + ["not-a-manifest.txt"])   # non-manifests untouched


def test_pruning_is_disabled_by_zero(undo_dir):
    undo_dir.mkdir(parents=True)
    (undo_dir / "20260811T000000Z-a.json").write_text('{"version": 1}')
    assert execute.prune_runs(undo_dir, keep=0) == []
    assert len(list(undo_dir.iterdir())) == 1


def test_a_run_prunes_old_manifests_but_keeps_its_own(tree, undo_dir):
    root, season = tree
    undo_dir.mkdir(parents=True)
    for i in range(1, 4):
        (undo_dir / "2026080{}T000000Z-old.json".format(i)).write_text(
            '{"version": 1, "operations": []}')
    result = execute.execute_plan(make_plan(season), [root], undo_dir, keep_runs=2)
    left = sorted(p.name for p in undo_dir.iterdir())
    assert result.manifest in left
    assert len(left) == 2


# ── Logging: refusals are only visible here ────────────────────────────────

def test_a_refusal_is_logged_with_its_reasons(tree, undo_dir, caplog):
    """A refused run leaves no manifest by design, so the log is the only place
    'why would it not rename this' can be answered."""
    root, season = tree
    first = "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[0])
    with caplog.at_level("WARNING", logger="execute"):
        execute.execute_plan(make_plan(season, name_overrides={first: "x.mkv"}),
                             [root], undo_dir)
    assert "execute REFUSED" in caplog.text
    assert "Extension must stay" in caplog.text
    assert not undo_dir.exists()


def test_a_successful_run_and_its_undo_are_both_logged(tree, undo_dir, caplog):
    root, season = tree
    with caplog.at_level("INFO", logger="execute"):
        result = execute.execute_plan(make_plan(season), [root], undo_dir)
        execute.undo(result.manifest, undo_dir, [root])
    assert "execute START" in caplog.text
    assert "execute DONE" in caplog.text
    assert "undo START" in caplog.text
    assert "undo DONE" in caplog.text


def test_a_per_file_failure_is_logged_individually(tree, undo_dir, caplog, monkeypatch):
    """'nine of ten applied' is not debuggable on its own."""
    root, season = tree
    real = os.rename
    calls = {"n": 0}

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] == 2:
            raise OSError("simulated NFS hiccup")
        return real(src, dst)

    monkeypatch.setattr(execute.os, "rename", flaky)
    with caplog.at_level("WARNING", logger="execute"):
        result = execute.execute_plan(make_plan(season), [root], undo_dir)
    assert not result.ok
    assert "simulated NFS hiccup" in caplog.text
    assert "FAILED" in caplog.text
