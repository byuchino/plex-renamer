"""Tests for the Flask layer.

Alongside the behavioural tests there is an explicit check that app.py, core.py
and config.py contain no filesystem mutation at all: execute.py is the single
module allowed to move a file, and that stays true now that it exists. The list
in test_no_module_can_mutate_the_filesystem deliberately does not include it.
"""
import re
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
def client(library, tmp_path):
    root, _ = library
    return create_app(Config(roots=[root],
                             undo_dir=tmp_path / "undo")).test_client()


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
        "episodes_per_file": 1,
        "rename_show_dir": False,
        "rename_season_dir": False,        # opt-in, and off unless asked for
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


def test_episodes_per_file_reaches_the_target_names(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season), episodes_per_file=2)
    assert [(f["episode"], f["episode_end"]) for f in data["files"]] == \
        [(1, 2), (3, 4), (5, 6), (7, 8)]
    assert data["files"][0]["target_name"] == \
        "Nagatan and Aoto (2026) - S01E01-E02 - 2026-06-22 23 00 00.mp4"
    assert data["ok"] is True
    assert data["notes"] == []


def test_episodes_per_file_composes_with_per_episode(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season), per_episode=2, episodes_per_file=2)
    assert [(f["episode"], f["episode_end"]) for f in data["files"]] == \
        [(1, 2), (1, 2), (3, 4), (3, 4)]
    assert data["ok"] is True


def test_episodes_per_file_must_be_a_number(client, library):
    _, season = library
    code, data = get_plan(client, path=str(season), episodes_per_file="two")
    assert code == 400
    assert "whole numbers" in data["error"]


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


# ── Executing ──────────────────────────────────────────────────────────────

def execute_run(client, **payload):
    res = client.post("/api/execute", json=payload)
    return res.status_code, res.get_json()


def test_execute_renames_the_files_and_returns_an_undo_manifest(client, library):
    _, season = library
    _, plan = get_plan(client, path=str(season))
    code, data = execute_run(client, path=str(season), fingerprint=plan["fingerprint"])
    assert code == 200
    assert data["ok"] is True
    assert data["manifest"].endswith(".json")

    dest = season.parent / "Season 01"
    assert data["season_dir"] == str(dest)
    assert sorted(p.name for p in dest.iterdir()) == \
        [f["target_name"] for f in plan["files"]]


def test_execute_needs_the_fingerprint_of_the_plan_that_was_confirmed(client, library):
    _, season = library
    before = sorted(p.name for p in season.iterdir())
    code, data = execute_run(client, path=str(season))
    assert code == 400
    assert "fingerprint" in data["error"]
    assert sorted(p.name for p in season.iterdir()) == before


def test_a_folder_that_changed_since_the_dialog_is_refused(client, library):
    """The guard the fingerprint exists for: a new recording lands between
    confirming and clicking, so the names the user agreed to are no longer the
    names that would be written."""
    _, season = library
    _, plan = get_plan(client, path=str(season))
    (season / "Nagatan and Aoto (2026) - 2026-07-06 23 00 00.mp4").touch()
    before = sorted(p.name for p in season.iterdir())

    code, data = execute_run(client, path=str(season), fingerprint=plan["fingerprint"])
    assert code == 409
    assert "changed since" in data["error"]
    assert data["fingerprint"] != plan["fingerprint"]      # the new one, to re-plan against
    assert sorted(p.name for p in season.iterdir()) == before
    assert not (season.parent / "Season 01").exists()


def test_execute_refuses_a_plan_with_a_bad_row_and_touches_nothing(client, library):
    _, season = library
    first = "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[0])
    overrides = {first: "Renamed.mkv"}          # changes the container
    _, plan = get_plan(client, path=str(season), name_overrides=overrides)
    before = sorted(p.name for p in season.iterdir())

    code, data = execute_run(client, path=str(season), name_overrides=overrides,
                             fingerprint=plan["fingerprint"])
    assert code == 409
    assert data["ok"] is False
    assert any("Extension must stay" in r for r in data["refused"])
    assert sorted(p.name for p in season.iterdir()) == before


def test_execute_carries_the_form_inputs_through(client, library):
    """Same body as /api/plan, so a season number typed in the form is the one
    that gets written."""
    _, season = library
    _, plan = get_plan(client, path=str(season), season=4, per_episode=2)
    code, data = execute_run(client, path=str(season), season=4, per_episode=2,
                             fingerprint=plan["fingerprint"])
    assert code == 200, data
    dest = season.parent / "Season 04"
    assert sorted(p.name for p in dest.iterdir())[:2] == [
        "Nagatan and Aoto (2026) - S04E01 - {}.mp4".format(TIMECODES[0]),
        "Nagatan and Aoto (2026) - S04E01 - {}.mp4".format(TIMECODES[1]),
    ]


def test_execute_writes_multi_episode_names_and_undoes_them(client, library):
    _, season = library
    before = sorted(p.name for p in season.iterdir())
    _, plan = get_plan(client, path=str(season), episodes_per_file=2)
    code, data = execute_run(client, path=str(season), episodes_per_file=2,
                             fingerprint=plan["fingerprint"])
    assert code == 200, data
    dest = season.parent / "Season 01"
    assert sorted(p.name for p in dest.iterdir()) == [
        "Nagatan and Aoto (2026) - S01E01-E02 - {}.mp4".format(TIMECODES[0]),
        "Nagatan and Aoto (2026) - S01E03-E04 - {}.mp4".format(TIMECODES[1]),
        "Nagatan and Aoto (2026) - S01E05-E06 - {}.mp4".format(TIMECODES[2]),
        "Nagatan and Aoto (2026) - S01E07-E08 - {}.mp4".format(TIMECODES[3]),
    ]

    undo = client.post("/api/undo", json={"manifest": data["manifest"]}).get_json()
    assert undo["ok"] is True, undo
    assert sorted(p.name for p in season.iterdir()) == before


def test_replanning_a_multi_episode_folder_proposes_nothing(client, library):
    """The round trip that matters: after a span run, reopening the destination
    must be a no-op rather than renumbering E01-E02 into E01."""
    _, season = library
    _, plan = get_plan(client, path=str(season), episodes_per_file=2)
    execute_run(client, path=str(season), episodes_per_file=2,
                fingerprint=plan["fingerprint"])

    dest = season.parent / "Season 01"
    _, again = get_plan(client, path=str(dest))
    assert again["move_count"] == 0
    assert [(f["episode"], f["episode_end"]) for f in again["files"]] == \
        [(1, 2), (3, 4), (5, 6), (7, 8)]


def test_execute_outside_the_roots_is_refused(client, tmp_path):
    code, _ = execute_run(client, path=str(tmp_path / "elsewhere"), fingerprint="x")
    assert code == 400


@pytest.mark.parametrize("route", ["/api/execute", "/api/undo"])
def test_the_acting_routes_reject_a_non_object_body(client, route):
    assert client.post(route, json=["not", "an", "object"]).status_code == 400


def test_undo_puts_the_run_back(client, library):
    _, season = library
    before = sorted(p.name for p in season.iterdir())
    _, plan = get_plan(client, path=str(season))
    _, run = execute_run(client, path=str(season), fingerprint=plan["fingerprint"])

    res = client.post("/api/undo", json={"manifest": run["manifest"]})
    assert res.status_code == 200
    assert res.get_json()["ok"] is True
    assert sorted(p.name for p in season.iterdir()) == before
    assert not (season.parent / "Season 01").exists()


def test_undo_of_an_unknown_manifest_is_a_404(client):
    res = client.post("/api/undo", json={"manifest": "20260101T000000Z-abc.json"})
    assert res.status_code == 404


@pytest.mark.parametrize("name", ["../../etc/passwd", "sub/x.json", ""])
def test_undo_will_not_read_a_manifest_from_anywhere_else(client, name):
    res = client.post("/api/undo", json={"manifest": name})
    assert res.status_code == 404


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


# ── Specials folders (season 0) ─────────────────────────────────────────────

@pytest.fixture
def specials_library(tmp_path):
    """A show whose Specials folder is already correctly named."""
    root = (tmp_path / "TV Shows").resolve()
    season = root / "Some Show (2015) {tvdb-255359}" / "Specials"
    season.mkdir(parents=True)
    for n in (2, 5):
        (season / "Some Show (2015) - S00E0{} - a tail.mp4".format(n)).touch()
    return root, season


@pytest.fixture
def specials_client(specials_library, tmp_path):
    root, _ = specials_library
    return create_app(Config(roots=[root],
                             undo_dir=tmp_path / "undo")).test_client()


def test_a_specials_folder_opens_as_a_no_op(specials_client, specials_library):
    """Before Specials was understood this folder opened proposing to renumber
    S00 into S01 and move both files out into 'Season 01' — a valid, executable
    plan, in 27 real folders."""
    _, season = specials_library
    code, data = get_plan(specials_client, path=str(season))
    assert code == 200
    assert data["inputs"]["season"] == 0
    assert data["move_count"] == 0
    assert data["ok"] is True
    assert data["season_dir_target"] == str(season)
    assert data["season_dir_was_year"] is False


def test_the_rename_button_has_nothing_to_do_in_a_clean_specials_folder(
        specials_client, specials_library):
    """move_count 0 and no show-folder change is what disables the button."""
    _, season = specials_library
    _, data = get_plan(specials_client, path=str(season))
    assert (data["move_count"], data["show_dir_changes"]) == (0, False)


def test_executing_a_clean_specials_folder_is_refused(specials_client, specials_library):
    _, season = specials_library
    _, plan = get_plan(specials_client, path=str(season))
    before = sorted(p.name for p in season.iterdir())
    code, data = execute_run(specials_client, path=str(season),
                             fingerprint=plan["fingerprint"])
    assert code == 409
    assert any("Nothing to rename" in r for r in data["refused"])
    assert sorted(p.name for p in season.iterdir()) == before


# ── History routes ─────────────────────────────────────────────────────────

def test_runs_is_empty_before_anything_has_happened(client):
    data = client.get("/api/runs").get_json()
    assert data["runs"] == []
    assert data["keep_runs"] == 200


def test_a_run_shows_up_in_the_history_with_its_inputs(client, library):
    _, season = library
    _, plan = get_plan(client, path=str(season), season=4, per_episode=2)
    _, run = execute_run(client, path=str(season), season=4, per_episode=2,
                         fingerprint=plan["fingerprint"])

    data = client.get("/api/runs").get_json()
    assert len(data["runs"]) == 1
    r = data["runs"][0]
    assert r["manifest"] == run["manifest"]
    assert r["renames"] == len(TIMECODES)
    assert r["errors"] == 0
    assert r["undone"] is False
    assert r["undoable"] is True
    assert r["inputs"]["season"] == 4
    assert r["inputs"]["per_episode"] == 2
    assert r["season_dir"] == str(season)


def test_run_detail_carries_every_operation(client, library):
    _, season = library
    _, plan = get_plan(client, path=str(season))
    _, run = execute_run(client, path=str(season), fingerprint=plan["fingerprint"])

    d = client.get("/api/runs/" + run["manifest"]).get_json()
    assert [o["op"] for o in d["operations"]] == \
        ["mkdir"] + ["move"] * len(TIMECODES) + ["rmdir"]
    assert all(o["applied"] for o in d["operations"][:-1])
    assert d["fingerprint"] == plan["fingerprint"]


def test_the_history_is_global_not_scoped_to_the_browsed_folder(client, library):
    """The point of it: reach a run after navigating away, which is the only
    way to undo one once the result dialog is closed."""
    _, season = library
    _, plan = get_plan(client, path=str(season))
    _, run = execute_run(client, path=str(season), fingerprint=plan["fingerprint"])
    # the recordings have left the folder the run started in (poster.jpg keeps
    # the folder itself alive, so it is the files that prove the move)
    assert [p.name for p in season.iterdir()] == ["poster.jpg"]
    assert client.get("/api/runs").get_json()["runs"][0]["manifest"] == run["manifest"]

    res = client.post("/api/undo", json={"manifest": run["manifest"]})
    assert res.status_code == 200
    assert len(list(season.iterdir())) == len(TIMECODES) + 1
    after = client.get("/api/runs").get_json()["runs"][0]
    assert (after["undone"], after["undoable"]) == (True, False)


@pytest.mark.parametrize("name", ["../../../etc/passwd", "nope.json", "x.txt"])
def test_run_detail_will_not_read_anything_else(client, name):
    assert client.get("/api/runs/" + name).status_code == 404


def test_runs_rejects_a_non_numeric_limit(client):
    assert client.get("/api/runs", query_string={"limit": "lots"}).status_code == 400


def test_a_refused_run_leaves_no_history_entry(client, library):
    """Refusals live in the log, not on disk: 'a manifest exists' has to keep
    meaning 'something happened'."""
    _, season = library
    first = "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[0])
    overrides = {first: "Renamed.mkv"}
    _, plan = get_plan(client, path=str(season), name_overrides=overrides)
    code, _ = execute_run(client, path=str(season), name_overrides=overrides,
                          fingerprint=plan["fingerprint"])
    assert code == 409
    assert client.get("/api/runs").get_json()["runs"] == []


# ── Page wiring ────────────────────────────────────────────────────────────

def _page_script():
    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text()
    return re.search(r"<script>(.*)</script>", html, re.S).group(1)


def test_the_history_loads_on_page_load_not_only_on_navigation():
    """Regression: loadRuns() was once wired into onhashchange instead of the
    initial call, so a fresh page load left History reading 'Loading…' for ever
    and it only populated after clicking a folder. A syntax check and an
    element-id audit both pass on that bug, so it needs pinning here.

    Top-level statements in this file sit at column 0, which is what
    distinguishes 'called on load' from 'called inside a handler'.
    """
    script = _page_script()
    assert re.search(r"^loadRuns\(\);$", script, re.M), \
        "loadRuns() is not called as a top-level statement on page load"
    assert re.search(r"^browse\(hashPath\(\)\);$", script, re.M)
    # ...and the hashchange handler must not have swallowed it
    handler = re.search(r"window\.onhashchange = .*", script).group(0)
    assert "loadRuns" not in handler


def test_the_history_section_is_outside_the_planner():
    """It has to be reachable when no folder is selected — that is the whole
    point of it, since the planner is hidden until a folder has renameable
    files."""
    html = (Path(__file__).resolve().parent.parent / "templates" / "index.html").read_text()
    planner = html.index('<div id="planner"')
    history = html.index('<section id="history-section">')
    # the planner div closes before the history section begins
    assert html.index('<div class="overlay"', planner) > history > planner


def test_a_browse_error_still_offers_a_way_back_to_the_roots():
    """A '#path=' deep link outlives the folder it points at — a renamed show, a
    deleted season, an NFS mount that has not come back. The error handler used
    to replace the whole breadcrumb with the message, taking the 'roots' button
    with it and leaving the page escapable only by editing the URL."""
    script = _page_script()
    handler = script[script.index("if (data.error) {"):]
    handler = handler[:handler.index("state.dirs = data.dirs")]
    assert 'data-path=""' in handler, "the error path renders no roots button"
    assert "open(null)" in handler, "the roots button is not wired"


# ── The opt-in season folder rename ────────────────────────────────────────

def test_plan_offers_the_season_rename_on_a_year_folder(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season))
    fr = data["folder_rename"]
    assert fr["available"] is True
    assert fr["reason"] == ""
    assert (fr["from"], fr["to"]) == ("Season 2026", "Season 01")
    # Offered, but not taken unless asked for.
    assert data["season_dir_rename_to"] is None
    assert data["inputs"]["rename_season_dir"] is False


def test_asking_for_the_season_rename_replans_it(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season), rename_season_dir=True)
    assert data["season_dir_rename_to"].endswith("Season 01")
    assert data["season_dir_target"] == str(season)
    assert data["inputs"]["rename_season_dir"] is True


def test_the_season_rename_is_not_offered_when_the_target_exists(client, library):
    """The reason replaces the hidden checkbox, and names the folder in the
    way — otherwise the cheap path just silently vanishes."""
    _, season = library
    (season.parent / "Season 01").mkdir()
    _, data = get_plan(client, path=str(season))
    fr = data["folder_rename"]
    assert fr["available"] is False
    assert "Season 01" in fr["reason"]


def test_asking_for_an_unavailable_season_rename_falls_back_quietly(client, library):
    """A page left open while the folder changed underneath should plan the
    move path, not fail to plan at all."""
    _, season = library
    (season.parent / "Season 01").mkdir()
    status, data = get_plan(client, path=str(season), rename_season_dir=True)
    assert status == 200
    assert data["season_dir_rename_to"] is None
    # The manifest records what the run did, not what the browser asked for.
    assert data["inputs"]["rename_season_dir"] is False


def test_the_leftovers_are_named_for_the_description(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season))
    fr = data["folder_rename"]
    assert fr["leftovers"] == ["poster.jpg"]
    assert fr["moves_out"] is True
    # The poster holds the folder open, so the move path cannot remove it.
    assert fr["source_removed"] is False


def test_a_folder_with_nothing_left_behind_reports_its_removal(client, library):
    _, season = library
    (season / "poster.jpg").unlink()
    _, data = get_plan(client, path=str(season))
    assert data["folder_rename"]["leftovers"] == []
    assert data["folder_rename"]["source_removed"] is True


def test_a_subdirectory_counts_as_a_leftover(client, library):
    """collect_files drops directories silently, so taking leftovers from the
    listing is what stops the page promising a removal that cannot happen."""
    _, season = library
    (season / "poster.jpg").unlink()
    (season / "extras").mkdir()
    _, data = get_plan(client, path=str(season))
    assert data["folder_rename"]["leftovers"] == ["extras"]
    assert data["folder_rename"]["source_removed"] is False


def test_executing_a_season_rename_needs_its_own_fingerprint(client, library):
    """The two routes reach the same names, so only the fingerprint separates
    them. A fingerprint from the move plan must not execute the rename plan."""
    _, season = library
    _, move = get_plan(client, path=str(season))
    res = client.post("/api/execute", json={
        "path": str(season), "rename_season_dir": True,
        "fingerprint": move["fingerprint"]})
    assert res.status_code == 409
    assert "changed since the plan was confirmed" in res.get_json()["error"]
    assert season.is_dir()          # nothing happened


def test_a_season_rename_runs_end_to_end(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season), rename_season_dir=True)
    res = client.post("/api/execute", json={
        "path": str(season), "rename_season_dir": True,
        "fingerprint": data["fingerprint"]})
    body = res.get_json()
    assert body["refused"] == []
    assert body["ok"] is True
    dest = season.parent / "Season 01"
    assert not season.exists()
    assert (dest / "poster.jpg").is_file()
    assert body["season_dir"] == str(dest)


# ── Advisory tail check on hand-typed names ────────────────────────────────

def test_a_mistyped_tail_reaches_the_row_as_a_warning_not_an_issue(client, library):
    _, season = library
    first = "Nagatan and Aoto (2026) - {}.mp4".format(TIMECODES[0])
    _, data = get_plan(client, path=str(season), name_overrides={
        first: "Nagatan and Aoto (2026) - S01E01 - 2026-06-22 23 00 09.mp4"})
    row = data["files"][0]
    assert row["issues"] == []
    assert len(row["warnings"]) == 1
    assert "2026-06-22 23 00 00" in row["warnings"][0]
    # Advisory: the plan is still runnable, which is the difference that matters.
    assert data["ok"] is True


def test_an_untouched_row_carries_no_warning(client, library):
    _, season = library
    _, data = get_plan(client, path=str(season))
    assert all(f["warnings"] == [] for f in data["files"])
