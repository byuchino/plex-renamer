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
    ("Specials", (0, False)),        # Plex's name for season 0
    ("Featurettes", (None, False)),  # not a season folder at all
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

def test_collect_files_orders_numbered_files_before_undated_ones():
    """Mixed folder: already-numbered files lead, in episode order, then the
    fallback recordings in time order — so new recordings continue after the
    episodes that exist rather than colliding with them."""
    entries = [
        Path("/s/Show (2026) - 2026-07-06 23 00 00.mp4"),
        Path("/s/Show (2026) - 2026-06-22 23 00 00.mp4"),
        Path("/s/Show (2026) - S01E02 - Real Title.mp4"),
        Path("/s/Show (2026) - S01E01 - Real Title.mp4"),
        Path("/s/poster.jpg"),
    ]
    files, skipped = core.collect_files(entries)
    assert [f.name for f in files] == [
        "Show (2026) - S01E01 - Real Title.mp4",
        "Show (2026) - S01E02 - Real Title.mp4",
        "Show (2026) - 2026-06-22 23 00 00.mp4",
        "Show (2026) - 2026-07-06 23 00 00.mp4",
    ]
    assert [f.kind for f in files] == ["episodic", "episodic", "timecode", "timecode"]
    assert [(p.name, why) for p, why in skipped] == [("poster.jpg", "not a video file")]


def test_a_file_in_neither_form_is_skipped_with_a_reason():
    files, skipped = core.collect_files([Path("/s/random home video.mp4")])
    assert files == []
    assert "no S00E00 marker" in skipped[0][1]


@pytest.mark.parametrize("name, tail, season, episode", [
    # Real names from the library: release tails, no year, dashes in the tail.
    ("11.22.63 - S01E01 - 720p.BluRay.x264.ShAaNiG.mkv",
     "720p.BluRay.x264.ShAaNiG", 1, 1),
    ("1 Litre of Tears - S01E03 - [Kioku] 1 Litre of Tears - 03.avi",
     "[Kioku] 1 Litre of Tears - 03", 1, 3),
    ("24 Legacy - S01E11 - 1080P WEB-DL DD5 1 H264-LIGAS.mkv",
     "1080P WEB-DL DD5 1 H264-LIGAS", 1, 11),
    ("Show - S02E04.mkv", "", 2, 4),
])
def test_classify_decomposes_real_episodic_names(name, tail, season, episode):
    f = core.classify(Path("/s/" + name))
    assert (f.kind, f.tail, f.season, f.episode) == ("episodic", tail, season, episode)


@pytest.mark.parametrize("name", [
    "11.22.63 - S01E01 - 720p.BluRay.x264.ShAaNiG.mkv",
    "1 Litre of Tears - S01E03 - [Kioku] 1 Litre of Tears - 03.avi",
    "24 Legacy - S01E11 - 1080P WEB-DL DD5 1 H264-LIGAS.mkv",
    "Show - S02E04.mkv",
])
def test_reassembly_round_trips_an_already_correct_name(name):
    """The whole basis of 'an untouched episodic folder proposes no changes'."""
    p = Path("/s/" + name)
    f = core.classify(p)
    head, year, _, _ = core.parse_show_dir(name.split(" - S")[0])
    assert core.build_target_name(head, year, f.season, f.episode, f.tail_raw, p.suffix) == name


def test_timecode_found_in_raw_recording_form():
    p = Path("/s/Show (2026) - 2026-06-23 17 00 00 - Show.ts")
    assert core.timecode_of(p) == "2026-06-23 17 00 00"


# ── Naming ─────────────────────────────────────────────────────────────────

def test_build_target_name_matches_pipeline_convention():
    assert core.build_target_name(
        "Nagatan and Aoto", "2026", 1, 2, " - 2026-06-23 17 00 00", ".mp4"
    ) == "Nagatan and Aoto (2026) - S01E02 - 2026-06-23 17 00 00.mp4"


def test_build_target_name_omits_year_when_unknown():
    assert core.build_target_name(
        "Nagatan and Aoto", None, 1, 2, " - 2026-06-23 17 00 00", ".mp4"
    ) == "Nagatan and Aoto - S01E02 - 2026-06-23 17 00 00.mp4"


def test_build_target_name_preserves_suffix_and_pads():
    assert core.build_target_name("S", "2020", 12, 7, " - 2026-01-01 00 00 00", ".mkv") \
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
    return [core.classify(Path("/s/Show (2026) - 2026-01-{:02d} 00 00 00.mp4".format(i + 1)))
            for i in range(n)]


def episodic(*numbers):
    return [core.classify(Path("/s/Show (2026) - S01E{:02d} - Title.mp4".format(n)))
            for n in numbers]


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


# ── Mixed folders and already-episodic files (Phase 2.5) ───────────────────

def test_numbered_files_hold_their_own_numbers():
    assert core.assign_episodes(episodic(1, 2, 3), {}) == [1, 2, 3]


def test_numbered_files_keep_a_gap_rather_than_being_renumbered():
    """E01, E05, E06 stays that way — the names are the source of truth."""
    assert core.assign_episodes(episodic(1, 5, 6), {}) == [1, 5, 6]


def test_undated_recordings_continue_after_the_episodes_that_exist():
    """The mixed-folder rule: four new recordings beside E01-E03 become E04+,
    not a second E01."""
    mixed = episodic(1, 2, 3) + files(4)
    assert core.assign_episodes(mixed, {}) == [1, 2, 3, 4, 5, 6, 7]


def test_per_episode_groups_only_the_undated_run():
    mixed = episodic(1, 2) + files(4)
    assert core.assign_episodes(mixed, {}, per_episode=2) == [1, 2, 3, 3, 4, 4]


def test_an_explicit_anchor_moves_one_numbered_file_and_no_others():
    """A pick overrides the file it was made on. It does NOT cascade through
    later numbered files, because those carry their own numbers — renumbering
    a whole run off one pick would silently destroy deliberate gaps."""
    fs = episodic(1, 2, 3)
    assert core.assign_episodes(fs, {fs[1].name: 9}) == [1, 9, 3]


def test_an_anchor_on_a_numbered_file_still_cascades_into_undated_ones():
    fs = episodic(1, 2) + files(2)
    assert core.assign_episodes(fs, {fs[1].name: 9}) == [1, 9, 10, 11]


def test_an_untouched_episodic_folder_proposes_nothing():
    """Point the tool at a correct folder and it must be a visible no-op."""
    season = Path("/lib/24 Legacy/Season 01")
    entries = [
        season / "24 Legacy - S01E01 - 1080P WEB-DL DD5 1 H264-LIGAS.mkv",
        season / "24 Legacy - S01E02 - 1080P AMZN WEBRIP DD5 1 HEVC X265.mkv",
    ]
    plan = core.build_plan(season_dir=season, entries=entries,
                           show_name="24 Legacy", year=None, season=1)
    assert plan.ok
    assert plan.moves == []
    assert all(f.unchanged for f in plan.files)


def test_an_unpadded_season_folder_is_left_alone():
    """'Season 1' already denotes season 1; targeting 'Season 01' would split
    the season across two folders. 116 'Season 01' and 55 'Season 1' folders
    both exist in the real library."""
    season = Path("/lib/Show/Season 1")
    entries = [season / "Show - S01E01 - Tail.mkv"]
    plan = core.build_plan(season_dir=season, entries=entries,
                           show_name="Show", year=None, season=1)
    assert plan.season_dir_target == season
    assert plan.moves == []


def test_a_real_season_change_still_moves_into_a_padded_folder():
    season = Path("/lib/Show/Season 1")
    entries = [season / "Show - S01E01 - Tail.mkv"]
    plan = core.build_plan(season_dir=season, entries=entries,
                           show_name="Show", year=None, season=2)
    assert plan.season_dir_target == Path("/lib/Show/Season 02")
    assert plan.files[0].target_name == "Show - S02E01 - Tail.mkv"


def test_a_season_disagreement_is_noted_not_silently_renumbered():
    season = Path("/lib/Show/Season 01")
    entries = [season / "Show - S02E05 - Tail.mkv"]
    plan = core.build_plan(season_dir=season, entries=entries,
                           show_name="Show", year=None, season=1)
    assert plan.ok                                    # never blocks
    assert any("named S02" in n for n in plan.notes)


def test_changing_the_show_name_rewrites_episodic_files_too():
    season = Path("/lib/Show/Season 01")
    entries = [season / "Show - S01E01 - Tail.mkv"]
    plan = core.build_plan(season_dir=season, entries=entries,
                           show_name="Better Name", year="2019", season=1)
    assert plan.files[0].target_name == "Better Name (2019) - S01E01 - Tail.mkv"


@pytest.mark.parametrize("name", [
    # All real, all previously "tidied" into a different name by mistake.
    "Ally McBeal - S05E01 - .avi",                       # dangling separator
    "Beverly Hills, 90210 - S02E14 - The Next Fifty Years .avi",   # trailing space
    "Battlestar Galactica - S04E01 - He That Believeth in Me  (1080p).mkv",  # double space
])
def test_untidy_real_names_are_preserved_not_cleaned_up(name):
    """Trailing spaces and dangling separators are left exactly as found. They
    look like defects, but 'fixing' them renames files nobody asked about."""
    p = Path("/s/" + name)
    f = core.classify(p)
    head, year, _, _ = core.parse_show_dir(name.split(" - S")[0])
    assert core.build_target_name(head, year, f.season, f.episode,
                                  f.tail_raw, p.suffix) == name


def test_defaults_follow_the_files_when_the_folder_disagrees(tmp_path):
    """'Battlestar Galactica (2003)' full of 'Battlestar Galactica - S04E01…'
    must open as a no-op, not propose adding the year to all 84 files."""
    season = tmp_path / "Battlestar Galactica (2003)" / "Season 04"
    season.mkdir(parents=True)
    for n in (1, 2):
        (season / "Battlestar Galactica - S04E0{} - x265.mkv".format(n)).touch()

    d = core.derive_defaults(season, sorted(season.iterdir()))
    assert d.show_name == "Battlestar Galactica"
    assert d.year is None                       # the files carry no year

    plan = core.build_plan(season_dir=season, entries=sorted(season.iterdir()),
                           show_name=d.show_name, year=d.year, season=d.season)
    assert plan.moves == []


def test_the_folder_still_wins_when_the_files_disagree_with_each_other(tmp_path):
    season = tmp_path / "Real Name (2001)" / "Season 01"
    season.mkdir(parents=True)
    (season / "One Name - S01E01 - a.mkv").touch()
    (season / "Other Name - S01E02 - b.mkv").touch()
    d = core.derive_defaults(season, sorted(season.iterdir()))
    assert (d.show_name, d.year) == ("Real Name", "2001")


# ── Move ordering (Phase 3) ────────────────────────────────────────────────

def P(*names):
    return [Path("/s/" + n) for n in names]


def test_independent_moves_keep_their_order():
    moves = [(Path("/s/a"), Path("/s/x")), (Path("/s/b"), Path("/s/y"))]
    assert core.order_moves(moves) == moves


def test_a_chain_is_ordered_so_nothing_is_overwritten():
    """Shifting a run down: E02 becomes E01 while E03 becomes E02. Applied in
    listing order that would overwrite E01 and lose a file."""
    e1, e2, e3 = P("E01.mkv", "E02.mkv", "E03.mkv")
    ordered = core.order_moves([(e2, e1), (e3, e2)])
    assert ordered == [(e2, e1), (e3, e2)]

    # ...and the same set given in the other order still moves E02 out first.
    ordered = core.order_moves([(e3, e2), (e2, e1)])
    assert ordered.index((e2, e1)) < ordered.index((e3, e2))


def test_every_target_is_free_when_its_move_runs():
    """The property that matters, checked by simulating the sequence."""
    a, b, c, d = P("a", "b", "c", "d")
    ordered = core.order_moves([(a, b), (b, c), (c, d)])
    on_disk = {a, b, c}          # d is the one free name the chain shifts into
    for src, dst in ordered:
        assert src in on_disk, "{} was not there to move".format(src)
        assert dst not in on_disk, "{} would have been overwritten".format(dst)
        on_disk.discard(src)
        on_disk.add(dst)


def test_a_swap_is_broken_with_a_temporary_name():
    a, b = P("Show - S01E01 - t.mkv", "Show - S01E02 - t.mkv")
    ordered = core.order_moves([(a, b), (b, a)])
    assert len(ordered) == 3                      # one move became two
    on_disk = {a, b}
    for src, dst in ordered:
        assert src in on_disk
        assert dst not in on_disk
        on_disk.discard(src)
        on_disk.add(dst)
    assert on_disk == {a, b}                      # and the swap completed
    # the parking name stays in the same directory, so it cannot cross a device
    assert all(src.parent == dst.parent for src, dst in ordered)


def test_ordering_preserves_the_set_of_real_moves():
    moves = [(Path("/s/a"), Path("/s/b")), (Path("/s/c"), Path("/s/d"))]
    ordered = core.order_moves(moves)
    assert sorted(str(s) for s, _ in ordered) == ["/s/a", "/s/c"]
    assert sorted(str(d) for _, d in ordered) == ["/s/b", "/s/d"]


def test_order_moves_of_nothing_is_nothing():
    assert core.order_moves([]) == []


# ── Specials is season 0 ───────────────────────────────────────────────────

@pytest.mark.parametrize("folder,expected", [
    ("Specials", (0, False)),
    ("specials", (0, False)),
    ("Specials ", (0, False)),
    ("Season 00", (0, False)),        # the other spelling, already understood
    ("Season 0", (0, False)),
    ("Featurettes", (None, False)),   # still means nothing to the tool
])
def test_parse_season_dir_knows_specials(folder, expected):
    assert core.parse_season_dir(folder) == expected


def test_a_specials_folder_opens_as_a_no_op(tmp_path):
    """The whole point of the fix. Before it, a Specials folder defaulted to
    season 1 and proposed renumbering S00 into S01 *and* moving every file out
    into 'Season 01' beside the real episodes — 27 folders in the real library,
    all of them a valid, executable plan."""
    season = tmp_path / "Some Show (2015) {tvdb-1}" / "Specials"
    season.mkdir(parents=True)
    for n in (1, 2):
        (season / "Some Show (2015) - S00E0{} - a tail.mp4".format(n)).touch()

    entries = sorted(season.iterdir())
    d = core.derive_defaults(season, entries)
    assert d.season == 0
    assert d.season_dir_was_year is False

    plan = core.build_plan(season_dir=season, entries=entries,
                           show_name=d.show_name, year=d.year, season=d.season)
    assert plan.moves == []
    assert plan.ok
    assert plan.season_dir_target == season       # left exactly as named


def test_specials_files_are_written_as_season_00(tmp_path):
    """A timecode recording dropped in a Specials folder gets S00, not S01."""
    season = tmp_path / "Some Show (2015)" / "Specials"
    season.mkdir(parents=True)
    (season / "Some Show (2015) - 2026-06-22 23 00 00.mp4").touch()
    plan = core.build_plan(season_dir=season, entries=sorted(season.iterdir()),
                           show_name="Some Show", year="2015", season=0)
    assert [f.target.name for f in plan.files] == \
        ["Some Show (2015) - S00E01 - 2026-06-22 23 00 00.mp4"]
    assert plan.season_dir_target == season


@pytest.mark.parametrize("season,expected", [
    (0, "Specials"), (1, "Season 01"), (3, "Season 03"), (12, "Season 12"),
])
def test_season_dir_name(season, expected):
    assert core.season_dir_name(season) == expected


def test_a_new_season_zero_folder_is_called_specials(tmp_path):
    """Moving specials out of a year folder creates 'Specials', matching the
    library's own convention rather than 'Season 00'."""
    season = tmp_path / "Some Show (2015)" / "Season 2026"
    season.mkdir(parents=True)
    (season / "Some Show (2015) - 2026-06-22 23 00 00.mp4").touch()
    plan = core.build_plan(season_dir=season, entries=sorted(season.iterdir()),
                           show_name="Some Show", year="2015", season=0)
    assert plan.season_dir_target == season.parent / "Specials"


def test_an_existing_season_00_folder_is_not_renamed_to_specials(tmp_path):
    """Both spellings are valid to Plex, so a folder that already denotes
    season 0 is left alone — same rule that protects 'Season 1'."""
    season = tmp_path / "Some Show (2015)" / "Season 00"
    season.mkdir(parents=True)
    (season / "Some Show (2015) - S00E01 - a tail.mp4").touch()
    plan = core.build_plan(season_dir=season, entries=sorted(season.iterdir()),
                           show_name="Some Show", year="2015", season=0)
    assert plan.season_dir_target == season
    assert plan.moves == []
