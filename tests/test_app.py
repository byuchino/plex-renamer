"""Tests for the Flask layer.

Phase 2 is read-only, so alongside the behavioural tests there is an explicit
check that no route and no imported module can move a file — that property is
the whole point of shipping the UI before execute.py, and it should fail loudly
if someone wires up a write later without noticing which phase they are in.
"""
from pathlib import Path

import pytest

import app as app_module
from app import create_app
from config import Config

TIMECODES = [
    "2026-06-22 23 00 00",
    "2026-06-23 17 00 00",
    "2026-06-29 23 00 00",
    "2026-06-30 17 00 00",
]


@pytest.fixture
def library(tmp_path):
    """A real 'Plex gave up' tree: year season folder, timecode filenames."""
    root = (tmp_path / "TV Shows").resolve()
    season = root / "Nagatan and Aoto (2026) {tvdb-428763}" / "Season 2026"
    season.mkdir(parents=True)
    for tc in TIMECODES:
        (season / "Nagatan and Aoto (2026) - {}.mp4".format(tc)).touch()
    (season / "poster.jpg").touch()
    return root, season


@pytest.fixture
def client(library):
    root, _ = library
    return create_app(Config(roots=[root])).test_client()


def get_plan(client, **payload):
    res = client.post("/api/plan", json=payload)
    return res.status_code, res.get_json()


# ── Browsing ───────────────────────────────────────────────────────────────

def test_index_renders(client):
    res = client.get("/")
    assert res.status_code == 200
    assert b"Plex Renamer" in res.data


def test_browse_with_no_path_offers_the_roots(client, library):
    root, _ = library
    data = client.get("/api/browse").get_json()
    assert data["at_top"] is True
    assert [d["path"] for d in data["dirs"]] == [str(root)]


def test_browse_root_lists_shows_and_cannot_go_above_it(client, library):
    root, _ = library
    data = client.get("/api/browse", query_string={"path": str(root)}).get_json()
    assert [d["name"] for d in data["dirs"]] == ["Nagatan and Aoto (2026) {tvdb-428763}"]
    assert data["parent"] is None


def test_browse_season_counts_renameable_and_skipped(client, library):
    _, season = library
    data = client.get("/api/browse", query_string={"path": str(season)}).get_json()
    assert data["renameable"] == len(TIMECODES)
    assert data["skipped"] == 1          # poster.jpg
    assert data["parent"].endswith("{tvdb-428763}")


def test_browse_outside_roots_is_refused(client, tmp_path):
    res = client.get("/api/browse", query_string={"path": str(tmp_path / "elsewhere")})
    assert res.status_code == 400


def test_browse_cannot_escape_with_dot_dot(client, library):
    _, season = library
    res = client.get("/api/browse", query_string={"path": str(season) + "/../../../.."})
    assert res.status_code == 400


# ── Planning: defaults ─────────────────────────────────────────────────────

def test_plan_with_only_a_path_derives_every_default(client, library):
    _, season = library
    code, data = get_plan(client, path=str(season))
    assert code == 200
    assert data["inputs"] == {
        "show_name": "Nagatan and Aoto",
        "year": "2026",
        "season": 1,                       # folder is a year, so defaulted
        "ident": "428763",
        "ident_source": "tvdb",            # follows what the folder carries
        "per_episode": 1,
        "rename_show_dir": False,
    }
    assert data["season_dir_was_year"] is True
    assert data["ok"] is True


def test_plan_rows_are_sorted_by_timecode_and_named_for_the_pipeline(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season))
    assert [f["target_name"] for f in data["files"]] == [
        "Nagatan and Aoto (2026) - S01E0{} - {}.mp4".format(i + 1, tc)
        for i, tc in enumerate(TIMECODES)
    ]
    assert data["skipped"] == [{"name": "poster.jpg", "reason": "not a video file"}]


def test_plan_moves_files_into_a_real_season_folder(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season), season=3)
    assert data["season_dir_target"].endswith("/Season 03")
    assert all("S03E" in f["target_name"] for f in data["files"])


def test_show_dir_rename_is_opt_in(client, library):
    _, season = library
    _, off = get_plan(client, path=str(season))
    assert off["show_dir_target"] is None
    _, on = get_plan(client, path=str(season), rename_show_dir=True)
    assert on["show_dir_target"].endswith("Nagatan and Aoto (2026) {tvdb-428763}")


# ── Planning: the absent/empty distinction ─────────────────────────────────

def test_absent_year_derives_but_empty_year_omits_it(client, library):
    _, season = library
    _, derived = get_plan(client, path=str(season))
    assert derived["files"][0]["target_name"].startswith("Nagatan and Aoto (2026)")

    _, cleared = get_plan(client, path=str(season), year="")
    assert cleared["inputs"]["year"] is None
    assert cleared["files"][0]["target_name"].startswith("Nagatan and Aoto - S01E01")


def test_cleared_id_drops_the_hint_from_the_show_folder(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season), ident="", rename_show_dir=True)
    assert data["show_dir_target"].endswith("Nagatan and Aoto (2026)")


# ── Planning: episodes ─────────────────────────────────────────────────────

def test_per_episode_pairs_each_episode_with_its_rebroadcast(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season), per_episode=2)
    assert [f["episode"] for f in data["files"]] == [1, 1, 2, 2]
    # Two files on one episode is normal here, and must never block the plan.
    assert data["ok"] is True
    assert data["notes"] == ["Episode 01 has 2 files", "Episode 02 has 2 files"]


def test_anchor_cascades_forward_only(client, library):
    _, season = library
    third = "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[2])
    _, data = get_plan(client, path=str(season), anchors={third: 7})
    assert [f["episode"] for f in data["files"]] == [1, 2, 7, 8]


def test_a_non_numeric_anchor_is_rejected(client, library):
    _, season = library
    code, data = get_plan(client, path=str(season), anchors={"x.mp4": "abc"})
    assert code == 400
    assert "anchor" in data["error"]


# ── Planning: hand-typed names ─────────────────────────────────────────────

def test_a_typed_name_replaces_just_that_row(client, library):
    _, season = library
    first = "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[0])
    _, data = get_plan(client, path=str(season),
                       name_overrides={first: "Something Else.mp4"})
    assert data["files"][0]["target_name"] == "Something Else.mp4"
    assert data["files"][1]["target_name"].endswith("S01E02 - {}.mp4".format(TIMECODES[1]))
    assert data["ok"] is True


def test_a_typed_name_cannot_contain_a_path_separator(client, library):
    _, season = library
    first = "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[0])
    _, data = get_plan(client, path=str(season),
                       name_overrides={first: "../../escape.mp4"})
    assert data["ok"] is False
    assert "path separator" in " ".join(data["files"][0]["issues"])
    # and the row falls back to the derived name rather than showing the escape
    assert data["files"][0]["target_name"].startswith("Nagatan and Aoto")


def test_a_typed_name_cannot_change_the_container(client, library):
    _, season = library
    first = "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[0])
    _, data = get_plan(client, path=str(season),
                       name_overrides={first: "Renamed.mkv"})
    assert data["ok"] is False
    assert "Extension must stay .mp4." in data["files"][0]["issues"]


def test_two_rows_typed_to_the_same_name_collide(client, library):
    _, season = library
    a = "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[0])
    b = "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[1])
    _, data = get_plan(client, path=str(season),
                       name_overrides={a: "Same.mp4", b: "Same.mp4"})
    assert data["ok"] is False
    assert any("Same target as" in i for i in data["files"][0]["issues"])


# ── Planning: bad input ────────────────────────────────────────────────────

def test_plan_outside_roots_is_refused(client, tmp_path):
    code, _ = get_plan(client, path=str(tmp_path / "elsewhere"))
    assert code == 400


def test_plan_needs_a_path(client):
    code, _ = get_plan(client)
    assert code == 400


def test_plan_rejects_a_non_numeric_season(client, library):
    _, season = library
    code, data = get_plan(client, path=str(season), season="autumn")
    assert code == 400
    assert "whole numbers" in data["error"]


def test_plan_rejects_a_bad_id_source(client, library):
    _, season = library
    code, _ = get_plan(client, path=str(season), ident_source="imdb")
    assert code == 400


def test_plan_rejects_a_non_object_body(client):
    assert client.post("/api/plan", json=["not", "an", "object"]).status_code == 400


def test_empty_folder_is_an_issue_not_a_crash(client, library):
    root, _ = library
    empty = root / "Nagatan and Aoto (2026) {tvdb-428763}" / "Season 03"
    empty.mkdir()
    code, data = get_plan(client, path=str(empty))
    assert code == 200
    assert data["ok"] is False
    assert data["files"] == []
    assert "No renameable video files" in " ".join(data["issues"])


# ── Phase 2 is read-only ───────────────────────────────────────────────────

def test_no_execute_route_exists_yet(client):
    for route in ("/api/execute", "/api/undo"):
        assert client.post(route, json={}).status_code == 404


def test_planning_leaves_the_directory_untouched(client, library):
    _, season = library
    before = sorted(p.name for p in season.iterdir())
    get_plan(client, path=str(season), season=3, per_episode=2,
             rename_show_dir=True, name_overrides={"x": "y.mp4"})
    assert sorted(p.name for p in season.iterdir()) == before
    # The season folder the plan names must not have been created either.
    assert not (season.parent / "Season 03").exists()


@pytest.mark.parametrize("module", ["app.py", "core.py", "config.py"])
def test_no_module_can_mutate_the_filesystem(module):
    """The claim in the README is that nothing in the repo can move a file.
    Phase 3 adds execute.py and this list stays as it is."""
    source = (Path(__file__).resolve().parent.parent / module).read_text()
    for forbidden in ("os.rename", "os.replace", "shutil.move", ".mkdir(",
                      ".unlink(", ".rmdir(", ".write_text(", "open("):
        assert forbidden not in source, "{} contains {}".format(module, forbidden)


def test_show_folder_preview_is_offered_before_the_box_is_ticked(client, library):
    """The checkbox has to say what it would do while still unticked."""
    _, season = library
    _, data = get_plan(client, path=str(season))
    assert data["show_dir_target"] is None                       # opt-in, not proposed
    assert data["show_dir_current"] == "Nagatan and Aoto (2026) {tvdb-428763}"
    assert data["show_dir_preview"] == "Nagatan and Aoto (2026) {tvdb-428763}"


def test_show_folder_preview_tracks_the_inputs(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season), show_name="Renamed Show")
    assert data["show_dir_preview"] == "Renamed Show (2026) {tvdb-428763}"
    assert data["show_dir_current"] == "Nagatan and Aoto (2026) {tvdb-428763}"


# ── Mixed and episodic folders (Phase 2.5) ─────────────────────────────────

@pytest.fixture
def mixed_library(tmp_path):
    """Three named episodes plus two new fallback recordings in one folder."""
    root = (tmp_path / "TV Shows").resolve()
    season = root / "Some Show (2019)" / "Season 01"
    season.mkdir(parents=True)
    for n in (1, 2, 3):
        (season / "Some Show (2019) - S01E0{} - 1080p.WEB.mkv".format(n)).touch()
    for tc in ("2026-06-22 23 00 00", "2026-06-23 17 00 00"):
        (season / "Some Show (2019) - {}.mkv".format(tc)).touch()
    return root, season


@pytest.fixture
def mixed_client(mixed_library):
    root, _ = mixed_library
    return create_app(Config(roots=[root])).test_client()


def test_mixed_folder_lists_both_kinds_with_numbers_continuing(mixed_client, mixed_library):
    _, season = mixed_library
    code, data = get_plan(mixed_client, path=str(season))
    assert code == 200
    assert [f["kind"] for f in data["files"]] == \
        ["episodic", "episodic", "episodic", "timecode", "timecode"]
    # the new recordings continue after the episodes that already exist
    assert [f["episode"] for f in data["files"]] == [1, 2, 3, 4, 5]


def test_mixed_folder_only_changes_the_new_recordings(mixed_client, mixed_library):
    _, season = mixed_library
    _, data = get_plan(mixed_client, path=str(season))
    changed = [f for f in data["files"] if not f["unchanged"]]
    assert data["move_count"] == 2
    assert data["file_count"] == 5
    assert [f["kind"] for f in changed] == ["timecode", "timecode"]
    assert changed[0]["target_name"] == "Some Show (2019) - S01E04 - 2026-06-22 23 00 00.mkv"


def test_an_episodic_folder_opens_as_a_no_op(mixed_client, mixed_library):
    """Nothing proposed, and the season folder is left exactly as named."""
    root, season = mixed_library
    for p in season.iterdir():
        if "2026-" in p.name:
            p.unlink()
    _, data = get_plan(mixed_client, path=str(season))
    assert data["move_count"] == 0
    assert data["ok"] is True
    assert data["season_dir_target"] == str(season)


# ── Folder browser (Phase 2.5 UI) ──────────────────────────────────────────

def test_browse_returns_clickable_crumbs_stopping_at_the_root(client, library):
    root, season = library
    data = client.get("/api/browse", query_string={"path": str(season)}).get_json()
    assert [c["name"] for c in data["crumbs"]] == [
        "TV Shows", "Nagatan and Aoto (2026) {tvdb-428763}", "Season 2026"]
    assert data["crumbs"][0]["path"] == str(root)


def test_small_listings_report_renameable_counts_per_folder(client, library):
    """Counting is per child directory, so it is the season folders under a
    show that carry a useful number — a show folder itself holds no files."""
    _, season = library
    data = client.get("/api/browse",
                      query_string={"path": str(season.parent)}).get_json()
    assert data["dirs"] == [
        {"name": "Season 2026", "path": str(season), "renameable": len(TIMECODES)}]


def test_large_listings_skip_the_per_folder_count(client, tmp_path):
    """One listing per child is fine for a few seasons and wrong for a root
    holding hundreds of shows across a tunnel."""
    root = (tmp_path / "Big").resolve()
    for i in range(app_module.CHILD_COUNT_LIMIT + 1):
        (root / "Show {:03d}".format(i)).mkdir(parents=True)
    c = create_app(Config(roots=[root])).test_client()
    data = c.get("/api/browse", query_string={"path": str(root)}).get_json()
    assert len(data["dirs"]) == app_module.CHILD_COUNT_LIMIT + 1
    assert "renameable" not in data["dirs"][0]


@pytest.mark.parametrize("noise", ["@eaDir", "#recycle", ".hidden"])
def test_synology_and_hidden_folders_are_not_listed(tmp_path, noise):
    root = (tmp_path / "Lib").resolve()
    (root / noise).mkdir(parents=True)
    (root / "Real Show").mkdir()
    c = create_app(Config(roots=[root])).test_client()
    data = c.get("/api/browse", query_string={"path": str(root)}).get_json()
    assert [d["name"] for d in data["dirs"]] == ["Real Show"]
