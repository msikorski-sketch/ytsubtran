"""
Unit tests for the pure (side-effect-free) helper functions.

These import youtube_downloader directly, which only needs the standard library
at import time (whisper/torch/yt-dlp are imported lazily inside functions), so the
tests run fast and without the heavy dependencies installed.
"""
import os

import youtube_downloader as yt


# ---------------------------------------------------------------------------
# extract_url
# ---------------------------------------------------------------------------

def test_extract_url_plain():
    url = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert yt.extract_url(url) == url


def test_extract_url_strips_brackets():
    assert yt.extract_url("[https://www.youtube.com/watch?v=dQw4w9WgXcQ]") == \
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_extract_url_drops_extra_params():
    # Trailing &t=, &list= etc. must be cut off after the 11-char video id.
    messy = "https://youtube.com/watch?v=dQw4w9WgXcQ&t=10s&list=PLxyz"
    assert yt.extract_url(messy) == "https://youtube.com/watch?v=dQw4w9WgXcQ"


def test_extract_url_passthrough_for_non_youtube():
    text = "just some text, not a link"
    assert yt.extract_url(text) == text


# ---------------------------------------------------------------------------
# format_timestamp_srt
# ---------------------------------------------------------------------------

def test_format_timestamp_zero():
    assert yt.format_timestamp_srt(0) == "00:00:00,000"


def test_format_timestamp_minutes_and_millis():
    assert yt.format_timestamp_srt(65.25) == "00:01:05,250"


def test_format_timestamp_hours():
    assert yt.format_timestamp_srt(3661.0) == "01:01:01,000"


def test_format_timestamp_vtt_uses_dot():
    # WebVTT uses a dot before milliseconds instead of a comma
    assert yt.format_timestamp_vtt(65.25) == "00:01:05.250"
    assert yt.format_timestamp_vtt(0) == "00:00:00.000"


# ---------------------------------------------------------------------------
# diagnose_failure
# ---------------------------------------------------------------------------

def test_diagnose_suggests_update_on_extraction_error():
    diag = yt.diagnose_failure("ERROR: unable to extract player response")
    assert diag["suggest_update"] is True


def test_diagnose_private_video_does_not_suggest_update():
    diag = yt.diagnose_failure("ERROR: Private video. This video is private.")
    assert diag["suggest_update"] is False


def test_diagnose_rate_limit():
    diag = yt.diagnose_failure("HTTP Error 429: Too Many Requests")
    assert diag["suggest_update"] is False
    assert "429" in diag["message"]


def test_diagnose_unknown_defaults_to_suggest_update():
    diag = yt.diagnose_failure("something completely unexpected happened")
    assert diag["suggest_update"] is True


def test_diagnose_missing_js_runtime():
    out = ('WARNING: [youtube] No supported JavaScript runtime could be found. '
           'Only deno is enabled by default ... See ...EJS\n'
           'ERROR: [youtube] XX: This video is not available')
    diag = yt.diagnose_failure(out)
    assert diag["suggest_update"] is False          # updating yt-dlp won't help
    assert "deno" in diag["message"].lower()


def test_diagnose_cookie_db_error_does_not_suggest_update():
    diag = yt.diagnose_failure("ERROR: Could not copy Chrome cookie database. See ...")
    assert diag["suggest_update"] is False
    assert "cookie" in diag["message"].lower()


# ---------------------------------------------------------------------------
# lang_name
# ---------------------------------------------------------------------------

def test_lang_name_known_and_unknown():
    assert yt.lang_name("pl") == "Polish"
    assert yt.lang_name("xx") == "xx"  # unknown code returns the code itself


# ---------------------------------------------------------------------------
# prompt_output_dir
# ---------------------------------------------------------------------------

def test_prompt_output_dir_defaults_to_cwd_when_non_interactive():
    import os
    # Under pytest stdin is not a TTY, so it must return the current folder
    # without blocking on input().
    assert yt.prompt_output_dir() == os.getcwd()


# ---------------------------------------------------------------------------
# inserts module (pure helpers)
# ---------------------------------------------------------------------------

def test_inserts_get_video_id():
    import inserts
    assert inserts.get_video_id("https://youtube.com/watch?v=dQw4w9WgXcQ&t=10s") == "dQw4w9WgXcQ"
    assert inserts.get_video_id("https://example.com/no-id") is None


def test_inserts_merge_ranges():
    import inserts
    # overlapping + adjacent ranges collapse; tuples may carry extra items
    ranges = [(0.0, 2.0, 'x'), (1.5, 3.0, 'y'), (10.0, 11.0, 'z')]
    assert inserts.merge_ranges(ranges) == [(0.0, 3.0), (10.0, 11.0)]


def test_inserts_kind_filter_keeps_only_requested():
    import inserts
    data = [
        {"start": 10, "end": 12, "kind": "clip", "reason": "meme"},
        {"start": 20, "end": 22, "kind": "screenshot", "reason": "tweet on screen"},
        {"start": 30, "end": 33, "kind": "caption", "reason": "editor title"},
        {"start": 5, "end": 4, "kind": "clip", "reason": "bad range"},  # dropped (e<=s)
    ]
    segs, skipped = inserts._segments_from_gemini(data, kinds=("clip",))
    assert [(s, e) for s, e, _ in segs] == [(10.0, 12.0)]
    assert skipped == 2  # screenshot + caption filtered out
    # broadening keeps more
    segs2, _ = inserts._segments_from_gemini(data, kinds=("clip", "screenshot"))
    assert len(segs2) == 2


def test_resolve_markers_pure_helpers(tmp_path):
    import resolve_markers as rm
    # time parsing
    assert rm.parse_time("1:30") == 90.0
    assert rm.parse_time(12.5) == 12.5
    # seconds → frame
    assert rm.seconds_to_frame(2.0, 25) == 50
    assert rm.seconds_to_frame(1.0, 23.976) == 24
    # match a cut list to the open video's base name
    lists = ["/x/Other_inserts.txt", "/x/My Clip_inserts.txt"]
    assert rm.list_for_video("/vids/My Clip.mp4", lists) == "/x/My Clip_inserts.txt"
    assert rm.list_for_video("/vids/Nope.mp4", lists) is None
    # load a cut list (round-trips the saved format, ignores comments)
    p = tmp_path / "v_inserts.txt"
    p.write_text("# comment\n10.0\t12.0\t# meme\n5\t4\t# bad\n20\t22\n", encoding="utf-8")
    assert rm.load_cut_list(str(p)) == [(10.0, 12.0, "meme"), (20.0, 22.0, "")]


def test_inserts_clip_filename_includes_kind():
    import inserts
    # kind is pulled out of the [..] tag into its own label
    assert inserts._clip_filename(3, 131.0, "[clip] Animated intro") == \
        "03_02m11s_clip_Animated intro.mp4"
    assert inserts._clip_filename(1, 20.0, "[screenshot] tweet") == \
        "01_00m20s_screenshot_tweet.mp4"
    # no kind tag → just index, time, description
    assert inserts._clip_filename(2, 5.0, "something") == "02_00m05s_something.mp4"


def test_inserts_clamp_segments_drops_out_of_range():
    import inserts
    # Video is 783.8s long; detector returned a valid early segment, one that runs
    # past the end (should be clamped), and two fully beyond the end (junk → dropped).
    duration = 783.766
    cands = [
        (17.0, 18.0, 'intro'),       # valid, untouched
        (780.0, 800.0, 'tail'),      # end clamped to duration, still long enough
        (804.0, 805.0, 'junk1'),     # entirely past end → dropped
        (1302.0, 1303.0, 'junk2'),   # entirely past end → dropped
    ]
    kept, dropped = inserts.clamp_segments(cands, duration)
    assert dropped == 2
    assert kept[0] == (17.0, 18.0, 'intro')
    assert kept[1][0] == 780.0 and abs(kept[1][1] - duration) < 1e-6
    assert kept[1][2] == 'tail'


def test_inserts_clamp_segments_no_duration_is_noop():
    import inserts
    cands = [(5.0, 9.0, 'a'), (12.0, 13.0, 'b')]
    kept, dropped = inserts.clamp_segments(cands, 0.0)
    assert dropped == 0
    assert kept == [(5.0, 9.0, 'a'), (12.0, 13.0, 'b')]


# ---------------------------------------------------------------------------
# organize module (catalog mode — pure helpers)
# ---------------------------------------------------------------------------

def test_organize_normalize_info_tolerant_of_aliases_and_bad_confidence():
    import organize
    info = organize.normalize_info({
        'series': 'Introduction to Docker',   # alias for "course"
        'lesson': 'Docker images',            # alias for "title"
        'topic': 'Docker',                    # alias for "category"
        'chapter': 2,                         # alias for "number", numeric
        'confidence': 'high',                 # non-numeric → None
    })
    assert info['course'] == 'Introduction to Docker'
    assert info['title'] == 'Docker images'
    assert info['category'] == 'Docker'
    assert info['number'] == '2'
    assert info['confidence'] is None
    # confidence is clamped to [0, 1]
    assert organize.normalize_info({'confidence': 1.5})['confidence'] == 1.0
    # missing/garbage input never crashes
    assert organize.normalize_info(None)['course'] == ''


def test_organize_target_for_builds_course_folder_and_numbered_name():
    import organize
    info = {'course': 'Introduction to Docker', 'title': 'Docker images',
            'category': 'Docker', 'number': '2', 'confidence': 0.9}
    folder, filename, low = organize.target_for(info, '.mp4')
    assert folder == 'Introduction to Docker'
    assert filename == '2 - Docker images.mp4'
    assert low is False
    # no visible number → title only
    info2 = dict(info, number='')
    assert organize.target_for(info2, '.mp4')[1] == 'Docker images.mp4'


def test_organize_target_for_low_confidence_goes_uncategorized():
    import organize
    # below the confidence floor → left for review, original name kept (None)
    low_conf = {'course': 'X', 'title': 'Y', 'confidence': 0.1}
    assert organize.target_for(low_conf, '.mp4') == (organize.UNCATEGORIZED, None, True)
    # missing course/title also routes to _Uncategorized
    missing = {'course': '', 'title': '', 'confidence': 0.99}
    assert organize.target_for(missing, '.mp4') == (organize.UNCATEGORIZED, None, True)
    # course missing but category present → category is used as the folder
    cat_only = {'course': '', 'title': 'Joins', 'category': 'SQL', 'confidence': 0.8}
    assert organize.target_for(cat_only, '.mp4')[0] == 'SQL'


def test_organize_target_for_sanitizes_illegal_filename_chars():
    import organize
    info = {'course': 'C++: Pointers/Refs', 'title': 'What? <Intro>',
            'number': '', 'confidence': 0.9}
    folder, filename, _ = organize.target_for(info, '.mp4')
    # Windows-illegal characters (\ / : * ? " < > |) are stripped
    for ch in '\\/:*?"<>|':
        assert ch not in folder
        assert ch not in filename


def test_organize_unique_name_numbers_collisions():
    import organize
    taken = {'docker images.mp4'}
    assert organize._unique_name('Docker images.mp4', taken) == 'Docker images (2).mp4'
    taken.add('docker images (2).mp4')
    assert organize._unique_name('Docker images.mp4', taken) == 'Docker images (3).mp4'
    # a free name is returned unchanged
    assert organize._unique_name('Fresh.mp4', taken) == 'Fresh.mp4'


def test_organize_build_plan_dedupes_and_routes_sidecars(tmp_path):
    import organize
    # Two videos classify to the SAME course+title → second must be numbered.
    # A side-car subtitle file rides along, renamed to the new stem.
    (tmp_path / 'video (1).mp4').write_bytes(b'x')
    (tmp_path / 'video (1)_EN.srt').write_text('subs', encoding='utf-8')
    (tmp_path / 'video (2).mp4').write_bytes(b'x')
    info = {'course': 'Intro to Docker', 'title': 'Docker images',
            'number': '', 'confidence': 0.9}
    plan = organize.build_plan(str(tmp_path), [
        ('video (1).mp4', info),
        ('video (2).mp4', info),
    ])
    names = [os.path.basename(p.dst) for p in plan]
    assert names == ['Docker images.mp4', 'Docker images (2).mp4']
    # both land in the per-course folder
    assert all(os.path.basename(os.path.dirname(p.dst)) == 'Intro to Docker' for p in plan)
    # the side-car follows the first video and gets the new stem
    assert plan[0].sidecars
    sc_src, sc_dst = plan[0].sidecars[0]
    assert os.path.basename(sc_src) == 'video (1)_EN.srt'
    assert os.path.basename(sc_dst) == 'Docker images_EN.srt'


def test_organize_collect_sidecars_does_not_grab_other_numbered_files(tmp_path):
    import organize
    # "video (1)" must not greedily match "video (10)"'s subtitles.
    (tmp_path / 'video (1).mp4').write_bytes(b'x')
    (tmp_path / 'video (1)_EN.srt').write_text('a', encoding='utf-8')
    (tmp_path / 'video (1).vtt').write_text('b', encoding='utf-8')
    (tmp_path / 'video (10)_EN.srt').write_text('c', encoding='utf-8')
    (tmp_path / 'video (1).mp3').write_text('d', encoding='utf-8')  # not a sidecar ext
    found = organize.collect_sidecars(str(tmp_path), 'video (1).mp4')
    # returned sorted; '.' sorts before '_', and the .mp3 is not a sidecar type
    assert found == ['video (1).vtt', 'video (1)_EN.srt']


def test_organize_frame_times_bias_to_start_and_clamp():
    import organize
    # with a known duration, times are fractions of it (front-loaded)
    times = organize._frame_times(200.0, 3)
    assert times == [4.0, 12.0, 24.0]
    assert all(t < 200.0 for t in times)
    # unknown duration → fixed early fallback, exactly n values
    assert len(organize._frame_times(0.0, 5)) == 5

