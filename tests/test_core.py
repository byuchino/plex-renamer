from pathlib import Path

import pytest

import core


# ── Derivation from the path ───────────────────────────────────────────────

@pytest.mark.parametrize("folder,expected", [
    ("Nagatan and Aoto (2026) {tvdb-428763}", ("Nagatan and Aoto", "2026", "428763", "tvdb")),
    ("Takasugi San Chi No O Bento (2024) {tmdb-12345}", ("Takasugi San Chi No O Bento", "2024", "12345", "tmdb")),
    ("Nagatan and Aoto (2026)", ("Nagatan and Aoto", "2026", None, None)),
    ("Nagatan and Aoto", ("Nagatan and Aoto", None, None, None)),
    ("Show With (Parens) In Name (1999)", ("Show With (Parens) In Name", "1999", None, None)),
])
def test_parse_show_dir(folder, expected):
    assert core.parse_show_dir(folder) == expected


@pytest.mark.parametrize("folder,expected", [
    ("Season 03", (3, False)),
    ("Season 3", (3, False)),
    ("season 12", (12, False)),
    ("Season 2026", (None, True)),   # Plex's "unknown season" marker
    ("Specials", (None, False)),
])
def test_parse_season_dir(folder, expected):
    assert core.parse_season_dir(folder) == expected


def test_derive_defaults_from_year_season_folder():
    d = core.derive_defaults(Path("/lib/Nagatan and Aoto (2026) {tvdb-428763}/Season 2026"))
    assert d.show_name == "Nagatan and Aoto"
    assert d.year == "2026"
    assert d.ident == "428763"
    assert d.ident_source == "tvdb"        # radio follows the folder, not the default
    assert d.season == 1                   # year folder => fall back to 01
    assert d.season_dir_was_year is True


def test_derive_defaults_falls_back_to_tmdb_when_folder_has_no_id():
    d = core.derive_defaults(Path("/lib/Some Show (2001)/Season 04"))
    assert d.ident is None
    assert d.ident_source == "tmdb"
    assert d.season == 4
    assert d.season_dir_was_year is False


# ── File collection ────────────────────────────────────────────────────────

def test_collect_files_sorts_by_timecode_and_reports_skips():
    entries = [
        Path("/s/Show (2026) - 2026-07-06 23 00 00.mp4"),
        Path("/s/Show (2026) - 2026-06-22 23 00 00.mp4"),
        Path("/s/Show (2026) - S01E01 - Real Title.mp4"),   # already named
        Path("/s/poster.jpg"),
    ]
    files, skipped = core.collect_files(entries)
    assert [f.name for f in files] == [
        "Show (2026) - 2026-06-22 23 00 00.mp4",
        "Show (2026) - 2026-07-06 23 00 00.mp4",
    ]
    reasons = {p.name: why for p, why in skipped}
    assert reasons["poster.jpg"] == "not a video file"
    assert "timecode" in reasons["Show (2026) - S01E01 - Real Title.mp4"]


def test_timecode_found_in_raw_recording_form():
    p = Path("/s/Show (2026) - 2026-06-23 17 00 00 - Show.ts")
    assert core.timecode_of(p) == "2026-06-23 17 00 00"


# ── Naming ─────────────────────────────────────────────────────────────────

def test_build_target_name_matches_pipeline_convention():
    assert core.build_target_name(
        "Nagatan and Aoto", "2026", 1, 2, "2026-06-23 17 00 00", ".mp4"
    ) == "Nagatan and Aoto (2026) - S01E02 - 2026-06-23 17 00 00.mp4"


def test_build_target_name_omits_year_when_unknown():
    assert core.build_target_name(
        "Nagatan and Aoto", None, 1, 2, "2026-06-23 17 00 00", ".mp4"
    ) == "Nagatan and Aoto - S01E02 - 2026-06-23 17 00 00.mp4"


def test_build_target_name_preserves_suffix_and_pads():
    assert core.build_target_name("S", "2020", 12, 7, "2026-01-01 00 00 00", ".mkv") \
        == "S (2020) - S12E07 - 2026-01-01 00 00 00.mkv"


@pytest.mark.parametrize("ident,source,expected", [
    ("428763", "tvdb", "Show (2026) {tvdb-428763}"),
    ("55", "tmdb", "Show (2026) {tmdb-55}"),
    (None, "tmdb", "Show (2026)"),
])
def test_build_show_dir_name(ident, source, expected):
    assert core.build_show_dir_name("Show", "2026", ident, source) == expected


# ── Episode assignment and cascade ─────────────────────────────────────────

def files(n):
    return [Path("/s/Show (2026) - 2026-01-{:02d} 00 00 00.mp4".format(i + 1)) for i in range(n)]


def test_episodes_start_at_one_and_increment():
    assert core.assign_episodes(files(4), {}) == [1, 2, 3, 4]


def test_anchor_cascades_forward_and_leaves_earlier_rows_alone():
    fs = files(5)
    eps = core.assign_episodes(fs, {fs[2].name: 7})
    assert eps == [1, 2, 7, 8, 9]


def test_multiple_anchors_each_restart_the_run():
    fs = files(5)
    eps = core.assign_episodes(fs, {fs[1].name: 5, fs[3].name: 20})
    assert eps == [1, 5, 6, 20, 21]


# ── Whole plan ─────────────────────────────────────────────────────────────

SEASON_DIR = Path("/lib/Nagatan and Aoto (2026) {tvdb-428763}/Season 2026")


def a_plan(**kw):
    entries = [
        SEASON_DIR / "Nagatan and Aoto (2026) - 2026-06-22 23 00 00.mp4",
        SEASON_DIR / "Nagatan and Aoto (2026) - 2026-06-23 17 00 00.mp4",
    ]
    params = dict(season_dir=SEASON_DIR, entries=entries,
                  show_name="Nagatan and Aoto", year="2026", season=1)
    params.update(kw)
    return core.build_plan(**params)


def test_plan_moves_files_into_a_real_season_folder():
    plan = a_plan()
    assert plan.season_dir_target == Path("/lib/Nagatan and Aoto (2026) {tvdb-428763}/Season 01")
    assert [f.target.name for f in plan.files] == [
        "Nagatan and Aoto (2026) - S01E01 - 2026-06-22 23 00 00.mp4",
        "Nagatan and Aoto (2026) - S01E02 - 2026-06-23 17 00 00.mp4",
    ]
    assert all(f.target.parent == plan.season_dir_target for f in plan.files)
    assert plan.ok


def test_show_dir_rename_is_opt_in():
    assert a_plan().show_dir_target is None
    plan = a_plan(rename_show_dir=True, ident="428763", ident_source="tvdb")
    assert plan.show_dir_target == Path("/lib/Nagatan and Aoto (2026) {tvdb-428763}")


def test_show_dir_rename_can_switch_source_and_id():
    plan = a_plan(rename_show_dir=True, ident="99", ident_source="tmdb")
    assert plan.show_dir_target == Path("/lib/Nagatan and Aoto (2026) {tmdb-99}")


def test_changing_season_updates_every_row_and_the_destination():
    plan = a_plan(season=3)
    assert plan.season_dir_target.name == "Season 03"
    assert all("- S03E" in f.target.name for f in plan.files)


def test_duplicate_episode_numbers_are_allowed_and_only_noted():
    """Most Hawaii series air an episode and re-broadcast it the next day, and
    Plex records both — so two files on one episode is the correct plan, not an
    error. They never clash on disk because the timecode stays in the name."""
    fs = [SEASON_DIR / "Show (2026) - 2026-01-01 00 00 00.mp4",
          SEASON_DIR / "Show (2026) - 2026-01-02 00 00 00.mp4"]
    plan = core.build_plan(season_dir=SEASON_DIR, entries=fs, show_name="Show",
                           year="2026", season=1,
                           anchors={fs[0].name: 4, fs[1].name: 4})
    assert plan.ok                                                  # not blocked
    assert plan.files[0].target.name != plan.files[1].target.name   # no name clash
    assert plan.notes == ["Episode 04 has 2 files"]


def test_per_episode_pairs_each_episode_with_its_rebroadcast():
    """The regular Hawaii case: 16 recordings are 8 episodes aired twice."""
    eps = core.assign_episodes(files(16), {}, per_episode=2)
    assert eps == [1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6, 7, 7, 8, 8]


def test_per_episode_of_one_is_unchanged_behaviour():
    assert core.assign_episodes(files(4), {}, per_episode=1) == [1, 2, 3, 4]


def test_anchor_starts_a_fresh_group_when_a_rebroadcast_is_missing():
    """A week whose re-broadcast never aired: anchor at the break and the rest
    re-pairs correctly, instead of every later row needing a correction."""
    fs = files(6)
    eps = core.assign_episodes(fs, {fs[3].name: 3}, per_episode=2)
    assert eps == [1, 1, 2, 3, 3, 4]


def test_per_episode_flows_through_build_plan():
    fs = [SEASON_DIR / "Show (2026) - 2026-01-0{} 00 00 00.mp4".format(i) for i in range(1, 5)]
    plan = core.build_plan(season_dir=SEASON_DIR, entries=fs, show_name="Show",
                           year="2026", season=1, per_episode=2)
    assert [f.episode for f in plan.files] == [1, 1, 2, 2]
    assert plan.ok
    assert plan.notes == ["Episode 01 has 2 files", "Episode 02 has 2 files"]


def test_distinct_episode_numbers_are_clean():
    fs = [SEASON_DIR / "Show (2026) - 2026-01-01 00 00 00.mp4",
          SEASON_DIR / "Show (2026) - 2026-01-02 00 00 00.mp4"]
    plan = core.build_plan(season_dir=SEASON_DIR, entries=fs, show_name="Show",
                           year="2026", season=1, anchors={fs[0].name: 4})
    assert plan.ok
    assert [f.episode for f in plan.files] == [4, 5]


def test_empty_show_name_blocks_the_plan():
    assert not a_plan(show_name="   ").ok


def test_no_matching_files_is_an_issue_not_a_crash():
    plan = core.build_plan(season_dir=SEASON_DIR, entries=[SEASON_DIR / "poster.jpg"],
                           show_name="Show", year="2026", season=1)
    assert not plan.ok
    assert plan.files == []
    assert len(plan.skipped) == 1


def test_fingerprint_tracks_the_moves():
    base = a_plan().fingerprint()
    assert base == a_plan().fingerprint()          # stable
    assert base != a_plan(season=2).fingerprint()  # season change is a different plan


def test_existing_target_detected(tmp_path):
    season = tmp_path / "Show (2026)" / "Season 2026"
    season.mkdir(parents=True)
    src = season / "Show (2026) - 2026-06-22 23 00 00.mp4"
    src.write_bytes(b"x")
    dest_dir = tmp_path / "Show (2026)" / "Season 01"
    dest_dir.mkdir()
    (dest_dir / "Show (2026) - S01E01 - 2026-06-22 23 00 00.mp4").write_bytes(b"other")

    plan = core.build_plan(season_dir=season, entries=[src], show_name="Show",
                           year="2026", season=1)
    assert not plan.ok
    assert "already exists" in plan.files[0].issues[0]


# ── Hand-typed names ───────────────────────────────────────────────────────

@pytest.mark.parametrize("typed, expected", [
    ("Fine Name.mp4", []),
    ("", ["Name is empty."]),
    ("   ", ["Name is empty."]),
    ("sub/dir.mp4", ["Name cannot contain a path separator."]),
    ("..\\up.mp4", ["Name cannot contain a path separator."]),
    ("Renamed.mkv", ["Extension must stay .mp4."]),
])
def test_check_override(typed, expected):
    assert core.check_override(typed, ".mp4") == expected


FIRST = "Nagatan and Aoto (2026) - 2026-06-22 23 00 00.mp4"
SECOND = "Nagatan and Aoto (2026) - 2026-06-23 17 00 00.mp4"


def test_override_replaces_one_row_and_leaves_the_rest_derived():
    plan = a_plan(name_overrides={FIRST: "Custom.mp4"})
    assert plan.files[0].target_name == "Custom.mp4"
    assert plan.files[1].target_name == \
        "Nagatan and Aoto (2026) - S01E02 - 2026-06-23 17 00 00.mp4"
    assert plan.ok


def test_a_rejected_override_keeps_the_derived_name_and_blocks_the_plan():
    plan = a_plan(name_overrides={FIRST: "../esc.mp4"})
    assert plan.files[0].target_name.startswith("Nagatan and Aoto (2026) - S01E01")
    assert not plan.ok


def test_overrides_are_collision_checked_like_derived_names():
    plan = a_plan(name_overrides={FIRST: "Same.mp4", SECOND: "Same.mp4"})
    assert not plan.ok
    assert any("Same target as" in i for i in plan.files[0].issues)
