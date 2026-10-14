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
    # a missing course is NOT backfilled from `category` — no invented topic folders
    cat_only = {'course': '', 'title': 'Joins', 'category': 'SQL', 'confidence': 0.8}
    assert organize.target_for(cat_only, '.mp4') == (organize.UNCATEGORIZED, None, True)


def test_organize_target_for_standalone_video_goes_uncategorized():
    import organize
    # Standalone (not a multi-lesson course): course == title and no chapter number
    # → left in _Uncategorized instead of getting its own one-file "course" folder.
    standalone = {'course': 'Foundations of Management', 'title': 'Foundations of Management',
                  'number': '', 'confidence': 0.9}
    assert organize.target_for(standalone, '.mp4') == (organize.UNCATEGORIZED, None, True)
    # case-insensitive match still counts as standalone
    standalone_ci = {'course': 'INTRO TALK', 'title': 'Intro Talk', 'confidence': 0.9}
    assert organize.target_for(standalone_ci, '.mp4') == (organize.UNCATEGORIZED, None, True)
    # but a real lesson of a course (course != title) is still filed normally
    lesson = {'course': 'Intermediate Docker', 'title': 'Persistent volumes', 'confidence': 1.0}
    assert organize.target_for(lesson, '.mp4') == ('Intermediate Docker', 'Persistent volumes.mp4', False)
    # course == title but WITH a chapter number → genuine numbered lesson, kept
    numbered = {'course': 'Docker', 'title': 'Docker', 'number': '1', 'confidence': 0.9}
    assert organize.target_for(numbered, '.mp4') == ('Docker', '1 - Docker.mp4', False)


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


def test_organize_build_plan_consolidates_course_casing(tmp_path):
    import organize
    # Gemini returned the SAME course with different casing for two videos.
    # Both must land in ONE folder (the first-seen casing), not two near-dupes.
    (tmp_path / 'a.mp4').write_bytes(b'x')
    (tmp_path / 'b.mp4').write_bytes(b'x')
    plan = organize.build_plan(str(tmp_path), [
        ('a.mp4', {'course': 'Intermediate Docker', 'title': 'Volumes', 'confidence': 1.0}),
        ('b.mp4', {'course': 'INTERMEDIATE DOCKER', 'title': 'Networking', 'confidence': 1.0}),
    ])
    folders = {os.path.basename(os.path.dirname(p.dst)) for p in plan}
    assert folders == {'Intermediate Docker'}  # one folder, first casing wins


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


def test_organize_frame_times_generalizes_to_any_n():
    import organize
    # Works for ANY requested frame count (not a fixed list), so a bigger
    # --organize-frames really samples more points.
    for n in (1, 3, 5, 10, 16):
        times = organize._frame_times(300.0, n)
        assert len(times) == n                       # one point per requested frame
        assert times == sorted(times)                # increasing
        assert len(set(times)) == n                  # all distinct
        assert 0.0 <= times[0] and times[-1] < 300.0  # inside the video, off the end
        assert times[0] <= 0.10 * 300.0              # first sample biased to the start
    # front-loaded: for many frames, most land in the first half of the runtime
    many = organize._frame_times(300.0, 10)
    assert sum(t < 150.0 for t in many) > len(many) / 2
    # unknown duration still returns exactly n distinct points
    assert len(organize._frame_times(0.0, 10)) == 10



# ---------------------------------------------------------------------------
# Gemini translation — prompt building and response parsing (pure helpers)
# ---------------------------------------------------------------------------

def test_gemini_prompt_states_count_langs_and_payload():
    prompt = yt._gemini_translation_prompt(['a', 'b'], 'Japanese', 'Polish')
    assert 'exactly 2 items' in prompt
    assert 'from Japanese to Polish' in prompt
    assert '["a", "b"]' in prompt


def test_gemini_prompt_includes_consistency_pairs():
    prompt = yt._gemini_translation_prompt(['x'], 'English', 'Polish',
                                           prev_pairs=[('hello', 'cześć')])
    assert '"hello" -> "cześć"' in prompt


def test_parse_gemini_translation_plain_list():
    assert yt.parse_gemini_translation('["ala", "ma kota"]', 2) == ['ala', 'ma kota']


def test_parse_gemini_translation_accepts_single_wrapper_object():
    raw = '{"translations": ["jeden", "dwa"]}'
    assert yt.parse_gemini_translation(raw, 2) == ['jeden', 'dwa']


def test_parse_gemini_translation_rejects_wrong_count():
    assert yt.parse_gemini_translation('["tylko jeden"]', 2) is None


def test_parse_gemini_translation_rejects_invalid_json_and_non_list():
    assert yt.parse_gemini_translation('not json at all', 1) is None
    assert yt.parse_gemini_translation('"just a string"', 1) is None
    assert yt.parse_gemini_translation(None, 1) is None


def test_parse_gemini_translation_rejects_ambiguous_wrapper():
    # Two candidate lists -> cannot decide safely, must give up (fallback path)
    raw = '{"a": ["x"], "b": ["y"]}'
    assert yt.parse_gemini_translation(raw, 1) is None


def test_parse_gemini_translation_coerces_and_strips():
    assert yt.parse_gemini_translation('[1, "  spacja  ", null]', 3) == ['1', 'spacja', '']


# ---------------------------------------------------------------------------
# DaVinci Resolve preset — frame-rate parsing and the transcode decision
# ---------------------------------------------------------------------------

def test_parse_rate_fraction_and_plain():
    assert abs(yt._parse_rate('30000/1001') - 29.97) < 0.01
    assert yt._parse_rate('25/1') == 25.0
    assert yt._parse_rate('0/0') == 0.0
    assert yt._parse_rate(None) == 0.0


def test_is_vfr_detects_mismatch_only():
    assert yt._is_vfr('30/1', '25/1') is True          # >1% apart -> VFR
    assert yt._is_vfr('30000/1001', '30000/1001') is False
    assert yt._is_vfr('0/0', '25/1') is False          # unknown -> assume CFR


def test_high_depth_pix_fmts():
    assert yt._is_high_depth('yuv420p') is False
    assert yt._is_high_depth('yuvj420p') is False
    assert yt._is_high_depth('yuv420p10le') is True    # 10-bit
    assert yt._is_high_depth('yuv422p') is True        # 4:2:2
    assert yt._is_high_depth(None) is False


def test_plan_compliant_file_needs_nothing():
    plan = yt.resolve_transcode_plan('h264', 'yuv420p', 'aac', vfr=False)
    assert plan == {'video': False, 'audio': False, 'target': 'h264', 'reasons': []}


def test_plan_vp9_opus_transcodes_both_to_h264_aac():
    plan = yt.resolve_transcode_plan('vp9', 'yuv420p', 'opus')
    assert plan['video'] and plan['audio']
    assert plan['target'] == 'h264'


def test_plan_audio_only_fix_keeps_video_copy():
    # H.264 video is fine, only the Opus audio needs re-encoding
    plan = yt.resolve_transcode_plan('h264', 'yuv420p', 'opus')
    assert plan['video'] is False
    assert plan['audio'] is True


def test_plan_ten_bit_source_goes_dnxhr_with_pcm():
    plan = yt.resolve_transcode_plan('hevc', 'yuv420p10le', 'aac')
    assert plan['target'] == 'dnxhr'
    assert plan['video'] is True
    assert plan['audio'] is True   # AAC -> PCM in the MOV editing container


def test_plan_vfr_forces_video_reencode():
    plan = yt.resolve_transcode_plan('h264', 'yuv420p', 'aac', vfr=True)
    assert plan['video'] is True
    assert plan['target'] == 'h264'


def test_plan_linux_h264_aac_goes_dnxhr_pcm():
    # Free Resolve on Linux decodes neither H.264 nor AAC
    plan = yt.resolve_transcode_plan('h264', 'yuv420p', 'aac', linux=True)
    assert plan['target'] == 'dnxhr'
    assert plan['video'] is True
    assert plan['audio'] is True


def test_plan_linux_dnxhr_pcm_needs_nothing():
    plan = yt.resolve_transcode_plan('dnxhd', 'yuv422p', 'pcm_s16le', linux=True)
    assert plan['video'] is False
    assert plan['audio'] is False


def test_plan_linux_prores_with_aac_only_fixes_audio():
    plan = yt.resolve_transcode_plan('prores', 'yuv422p10le', 'aac', linux=True)
    assert plan['video'] is False
    assert plan['audio'] is True


def test_plan_linux_vfr_forces_reencode_even_for_intra_codec():
    plan = yt.resolve_transcode_plan('dnxhd', 'yuv422p', 'pcm_s16le', vfr=True, linux=True)
    assert plan['video'] is True


def test_plan_no_audio_track():
    plan = yt.resolve_transcode_plan('av1', 'yuv420p', None)
    assert plan['audio'] is False
    assert plan['video'] is True


# ---------------------------------------------------------------------------
# Persistent app config (~/.ytsubtran.json) — default output folder
# ---------------------------------------------------------------------------

def test_app_config_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setattr(yt, 'APP_CONFIG_PATH', str(tmp_path / 'cfg.json'))
    assert yt.load_app_config() == {}
    assert yt.save_app_config_value('default_output_dir', 'D:/edit') is True
    assert yt.load_app_config() == {'default_output_dir': 'D:/edit'}


def test_app_config_merge_preserves_other_keys(tmp_path, monkeypatch):
    # The same file stores the Gemini API key — saving a setting must not eat it
    cfg = tmp_path / 'cfg.json'
    cfg.write_text('{"gemini_api_key": "secret"}', encoding='utf-8')
    monkeypatch.setattr(yt, 'APP_CONFIG_PATH', str(cfg))
    yt.save_app_config_value('default_output_dir', 'X:/y')
    data = yt.load_app_config()
    assert data['gemini_api_key'] == 'secret'
    assert data['default_output_dir'] == 'X:/y'


def test_app_config_corrupt_file_reads_as_empty(tmp_path, monkeypatch):
    cfg = tmp_path / 'cfg.json'
    cfg.write_text('definitely not json', encoding='utf-8')
    monkeypatch.setattr(yt, 'APP_CONFIG_PATH', str(cfg))
    assert yt.load_app_config() == {}


def test_resolve_default_output_dir_creates_missing_folder(tmp_path, monkeypatch):
    monkeypatch.setattr(yt, 'APP_CONFIG_PATH', str(tmp_path / 'cfg.json'))
    target = tmp_path / 'downloads' / 'edit'
    yt.save_app_config_value('default_output_dir', str(target))
    assert yt.resolve_default_output_dir() == str(target)
    assert target.is_dir()  # created on demand


def test_resolve_default_output_dir_none_when_unset(tmp_path, monkeypatch):
    monkeypatch.setattr(yt, 'APP_CONFIG_PATH', str(tmp_path / 'cfg.json'))
    assert yt.resolve_default_output_dir() is None


def test_inserts_clear_gemini_key_preserves_other_settings(tmp_path, monkeypatch):
    import inserts
    cfg = tmp_path / 'cfg.json'
    cfg.write_text('{"gemini_api_key": "old", "default_output_dir": "D:/edit"}',
                   encoding='utf-8')
    monkeypatch.setattr(inserts, 'CONFIG_PATH', str(cfg))
    inserts.clear_gemini_key()
    import json as _json
    data = _json.loads(cfg.read_text(encoding='utf-8'))
    assert 'gemini_api_key' not in data
    assert data['default_output_dir'] == 'D:/edit'


# ---------------------------------------------------------------------------
# extract_url — short link forms and playlist detection
# ---------------------------------------------------------------------------

def test_extract_url_youtu_be_is_canonicalized():
    assert yt.extract_url("https://youtu.be/dQw4w9WgXcQ?t=10") == \
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_extract_url_shorts_and_live():
    assert yt.extract_url("https://www.youtube.com/shorts/dQw4w9WgXcQ") == \
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"
    assert yt.extract_url("https://youtube.com/live/dQw4w9WgXcQ?feature=share") == \
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_is_playlist_url_pure_playlist():
    assert yt.is_playlist_url("https://www.youtube.com/playlist?list=PLxyz") is True


def test_is_playlist_url_watch_with_list_is_single_video():
    # --no-playlist protects this form, so it is NOT playlist mode
    assert yt.is_playlist_url("https://youtube.com/watch?v=dQw4w9WgXcQ&list=PLxyz") is False


def test_is_playlist_url_plain_video():
    assert yt.is_playlist_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") is False
    assert yt.is_playlist_url(None) is False


# ---------------------------------------------------------------------------
# _read_printed_paths — yt-dlp --print-to-file output
# ---------------------------------------------------------------------------

def test_read_printed_paths(tmp_path):
    f = tmp_path / 'paths.txt'
    f.write_text('C:\a.mp4\n\n  \nC:\b.mp4\n', encoding='utf-8')
    assert yt._read_printed_paths(str(f)) == ['C:\a.mp4', 'C:\b.mp4']


def test_read_printed_paths_missing_file():
    assert yt._read_printed_paths('Z:/definitely/not/there.txt') == []


# ---------------------------------------------------------------------------
# Duplicate detection — "already downloaded, skip it"
# ---------------------------------------------------------------------------

def test_video_id_from_url():
    assert yt.video_id_from_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ") == "dQw4w9WgXcQ"
    assert yt.video_id_from_url("https://youtube.com/watch?v=dQw4w9WgXcQ&t=5") == "dQw4w9WgXcQ"
    assert yt.video_id_from_url("https://example.com/whatever") is None
    assert yt.video_id_from_url(None) is None


def test_section_signature():
    assert yt.section_signature(None) == 'full'
    assert yt.section_signature([]) == 'full'
    assert yt.section_signature(['--download-sections', '*10.000-25.000']) == '10.000-25.000'
    # flag present but no value -> still 'full', never crashes
    assert yt.section_signature(['--download-sections']) == 'full'


def test_download_key_separates_format_and_range():
    full_mp4 = yt.download_key('abc12345678', 'mp4')
    frag_mp4 = yt.download_key('abc12345678', 'mp4', ['--download-sections', '*0-10.000'])
    full_mp3 = yt.download_key('abc12345678', 'mp3')
    # A fragment is NOT a duplicate of the full video, mp3 is not a duplicate of mp4
    assert len({full_mp4, frag_mp4, full_mp3}) == 3
    # ...but the same request maps to the same key
    assert yt.download_key('abc12345678', 'mp4') == full_mp4


def test_verify_entry_ok_when_size_matches():
    entry = {'file': 'clip.mp4', 'size': 100}
    status, path = yt.verify_download_entry(
        entry, 'D:/out', exists=lambda p: not p.endswith('.part'), size_of=lambda p: 100)
    assert status == 'ok'
    assert path.endswith('clip.mp4')


def test_verify_entry_missing_file_forces_redownload():
    entry = {'file': 'clip.mp4', 'size': 100}
    status, _ = yt.verify_download_entry(
        entry, 'D:/out', exists=lambda p: False, size_of=lambda p: 100)
    assert status == 'missing'


def test_verify_entry_detects_partial_download():
    entry = {'file': 'clip.mp4', 'size': 100}
    # a leftover clip.mp4.part means the previous download never finished
    status, _ = yt.verify_download_entry(
        entry, 'D:/out', exists=lambda p: True, size_of=lambda p: 100)
    assert status == 'incomplete'


def test_verify_entry_zero_byte_file_is_incomplete():
    entry = {'file': 'clip.mp4', 'size': 100}
    status, _ = yt.verify_download_entry(
        entry, 'D:/out', exists=lambda p: not p.endswith('.part'), size_of=lambda p: 0)
    assert status == 'incomplete'


def test_verify_entry_size_mismatch_is_modified():
    # e.g. the file was remuxed/normalized later — caller re-checks with ffprobe
    entry = {'file': 'clip.mp4', 'size': 100}
    status, _ = yt.verify_download_entry(
        entry, 'D:/out', exists=lambda p: not p.endswith('.part'), size_of=lambda p: 250)
    assert status == 'modified'


def test_verify_entry_without_file_name():
    assert yt.verify_download_entry({}, 'D:/out')[0] == 'missing'
    assert yt.verify_download_entry(None, 'D:/out')[0] == 'missing'


def test_download_index_roundtrip_and_record(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'x' * 42)
    key = yt.download_key('abc12345678', 'mp4')
    assert yt.record_download(str(tmp_path), key, 'abc12345678',
                              'https://youtube.com/watch?v=abc12345678',
                              str(media), 'mp4', None) is True
    index = yt.load_download_index(str(tmp_path))
    entry = index[key]
    assert entry['file'] == 'clip.mp4'      # stored relative to the output folder
    assert entry['size'] == 42
    assert entry['video_id'] == 'abc12345678'
    assert yt.verify_download_entry(entry, str(tmp_path))[0] == 'ok'


def test_load_download_index_missing_or_corrupt(tmp_path):
    assert yt.load_download_index(str(tmp_path)) == {}
    (tmp_path / yt.DOWNLOAD_INDEX_NAME).write_text('not json', encoding='utf-8')
    assert yt.load_download_index(str(tmp_path)) == {}


def test_check_already_downloaded_skips_existing_file(tmp_path, capsys):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'x' * 10)
    url = 'https://www.youtube.com/watch?v=abc12345678'
    key = yt.download_key('abc12345678', 'mp4')
    yt.record_download(str(tmp_path), key, 'abc12345678', url, str(media), 'mp4', None)

    path, returned_key = yt.check_already_downloaded(url, str(tmp_path), 'mp4')
    assert path == str(media)
    assert returned_key == key
    assert 'ALREADY DOWNLOADED' in capsys.readouterr().out


def test_check_already_downloaded_redownloads_when_file_deleted(tmp_path, capsys):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'x' * 10)
    url = 'https://www.youtube.com/watch?v=abc12345678'
    key = yt.download_key('abc12345678', 'mp4')
    yt.record_download(str(tmp_path), key, 'abc12345678', url, str(media), 'mp4', None)
    media.unlink()  # user deleted the file — the index must not cause a false skip

    path, _ = yt.check_already_downloaded(url, str(tmp_path), 'mp4')
    assert path is None
    assert 'no longer there' in capsys.readouterr().out


def test_check_already_downloaded_fragment_is_not_the_full_video(tmp_path):
    media = tmp_path / 'clip.mp4'
    media.write_bytes(b'x' * 10)
    url = 'https://www.youtube.com/watch?v=abc12345678'
    section = ['--download-sections', '*0-10.000']
    yt.record_download(str(tmp_path), yt.download_key('abc12345678', 'mp4', section),
                       'abc12345678', url, str(media), 'mp4', section)
    # asking for the WHOLE video must not match the stored 10-second fragment
    assert yt.check_already_downloaded(url, str(tmp_path), 'mp4')[0] is None
    # ...while the same fragment does match
    assert yt.check_already_downloaded(url, str(tmp_path), 'mp4', section)[0] == str(media)


def test_check_already_downloaded_without_video_id(tmp_path):
    # No id in the URL → we cannot be certain, so never skip
    assert yt.check_already_downloaded('https://example.com/x', str(tmp_path), 'mp4') == (None, None)


def test_human_size():
    assert yt._human_size(0) == '0 B'
    assert yt._human_size(1536) == '1.5 KB'
    assert yt._human_size(5 * 1024 ** 3) == '5.0 GB'


def test_fragment_suffix_keeps_filenames_unique():
    # Two different fragments of one video must not collide on the same file name
    assert yt.fragment_suffix(None) == ''
    assert yt.fragment_suffix(['--download-sections', '*5.000-12.000']) == ' [5-12s]'
    assert yt.fragment_suffix(['--download-sections', '*20.000-25.000']) == ' [20-25s]'
    assert yt.fragment_suffix(['--download-sections', '*1.500-3.250']) == ' [1.5-3.25s]'
    assert yt.fragment_suffix(['--download-sections', '*90.000-inf']) == ' [90-ends]'


def test_expected_media_duration():
    assert yt.expected_media_duration(300.0, None) == 300.0
    assert yt.expected_media_duration(300.0, ['--download-sections', '*5.000-12.000']) == 7.0
    # open-ended fragment: from start to the end of the video
    assert yt.expected_media_duration(300.0, ['--download-sections', '*90.000-inf']) == 210.0
    # unknown video length -> unknown expectation
    assert yt.expected_media_duration(0, None) == 0.0


def test_durations_match_full_video_is_strict():
    assert yt.durations_match(300.0, 300.0) is True
    assert yt.durations_match(301.5, 300.0) is True      # within 2s
    assert yt.durations_match(280.0, 300.0) is False     # a different video
    assert yt.durations_match(0, 300.0) is None          # unknown -> undecided


def test_durations_match_fragment_allows_keyframe_slack():
    # A 7s request can legitimately yield a ~9s file (cut starts at a keyframe)
    assert yt.durations_match(9.0, 7.0, fragment=True) is True
    assert yt.durations_match(6.5, 5.0, fragment=True) is True
    # ...but a full-length video where a 7s fragment was expected is not a match
    assert yt.durations_match(600.0, 7.0, fragment=True) is False
    # a file shorter than requested means it is not the fragment either
    assert yt.durations_match(3.0, 7.0, fragment=True) is False


# ---------------------------------------------------------------------------
# extract_youtube_links — clipboard / pasted-text scanning
# ---------------------------------------------------------------------------

def test_extract_links_from_a_pasted_block():
    # A block copied out of Discord: several posts, mixed link forms, noise
    text = (
        "[15.01.2026, godz. 18:00, 2 godz., Kamil Jarosinski]\n"
        "Link: https://www.youtube.com/watch?v=toafJ1CdEBk\n"
        "[08.01.2026] Link: https://youtu.be/dQw4w9WgXcQ?t=30\n"
        "shorts: https://www.youtube.com/shorts/aaaaaaaaaaa\n"
        "not a video: https://sekurak.pl/szkolenia\n"
    )
    assert yt.extract_youtube_links(text) == [
        "https://www.youtube.com/watch?v=toafJ1CdEBk",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=aaaaaaaaaaa",
    ]


def test_extract_links_deduplicates_keeping_order():
    text = ("https://youtu.be/dQw4w9WgXcQ and again "
            "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLx "
            "https://www.youtube.com/watch?v=toafJ1CdEBk")
    assert yt.extract_youtube_links(text) == [
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://www.youtube.com/watch?v=toafJ1CdEBk",
    ]


def test_extract_links_handles_params_before_v():
    text = "https://www.youtube.com/watch?app=desktop&feature=share&v=toafJ1CdEBk"
    assert yt.extract_youtube_links(text) == ["https://www.youtube.com/watch?v=toafJ1CdEBk"]


def test_extract_links_empty_and_none():
    assert yt.extract_youtube_links('') == []
    assert yt.extract_youtube_links(None) == []
    assert yt.extract_youtube_links('just some text, no links here') == []


def test_build_download_options_matches_download_youtube_signature():
    """
    Guards a real bug: clipboard watch mode used to build its own argument list
    and silently dropped --start/--end, so a "5 second fragment" downloaded the
    whole 14 GB video. Every option must map to a real download_youtube keyword.
    """
    import argparse
    import inspect

    args = argparse.Namespace(
        format='mp4', subs=True, model='base', source_lang='pl', translate_to='pl',
        prompt=None, cookies_from_browser=None, output_dir='D:/out', vtt=False,
        burn=False, embed=False, engine='auto', qwen_model=None,
        translate_engine='google', translate_model='gemini-2.5-flash',
        resolve=True, resolve_linux=False, force_download=False, limit_rate='5M',
        dnxhr_profile='lb',
    )
    section = ['--download-sections', '*5.000-10.000']
    opts = yt.build_download_options(args, inserts_opts=None, section_args=section)

    accepted = set(inspect.signature(yt.download_youtube).parameters) - {'raw_url'}
    assert set(opts) <= accepted           # no unknown keywords
    assert accepted <= set(opts)           # and nothing silently left out
    assert opts['section_args'] == section  # the option that used to go missing


# ---------------------------------------------------------------------------
# take_new_links — what the clipboard watcher queues
# ---------------------------------------------------------------------------

def test_take_new_links_queues_each_video_once():
    seen = set()
    first = yt.take_new_links("https://youtu.be/dQw4w9WgXcQ", seen)
    assert first == ["https://www.youtube.com/watch?v=dQw4w9WgXcQ"]
    # the same video copied again (any link form) is not queued a second time
    assert yt.take_new_links("https://www.youtube.com/watch?v=dQw4w9WgXcQ", seen) == []
    assert yt.take_new_links("https://youtu.be/dQw4w9WgXcQ&t=9", seen) == []


def test_take_new_links_queues_a_whole_block_in_order():
    seen = set()
    block = ("post 1: https://youtu.be/aaaaaaaaaaa\n"
             "post 2: https://www.youtube.com/watch?v=bbbbbbbbbbb\n"
             "post 3 (repeat of 1): https://youtu.be/aaaaaaaaaaa\n")
    assert yt.take_new_links(block, seen) == [
        "https://www.youtube.com/watch?v=aaaaaaaaaaa",
        "https://www.youtube.com/watch?v=bbbbbbbbbbb",
    ]
    assert len(seen) == 2


def test_take_new_links_ignores_text_without_links():
    seen = set()
    assert yt.take_new_links("nothing to see here", seen) == []
    assert seen == set()


# ---------------------------------------------------------------------------
# low_quality_warning — do not silently lock in a fallback-quality copy
# ---------------------------------------------------------------------------

def test_low_quality_warning_flags_fallback_formats():
    # Throttled sessions fall back to format 18 (360p); that must be visible,
    # otherwise the duplicate index skips the video forever at that quality.
    assert '360p' in yt.low_quality_warning(360)
    assert yt.low_quality_warning(480)


def test_low_quality_warning_silent_for_good_quality():
    assert yt.low_quality_warning(720) == ''
    assert yt.low_quality_warning(1080) == ''
    assert yt.low_quality_warning(2160) == ''


def test_low_quality_warning_silent_when_unknown():
    assert yt.low_quality_warning(0) == ''
    assert yt.low_quality_warning(None) == ''


def test_clipboard_change_token_is_safe_everywhere():
    """
    Windows-only API (GetClipboardSequenceNumber) used to spot a re-copy of the
    very same text. Must degrade to None elsewhere — CI runs on Linux.
    """
    token = yt._clipboard_change_token()
    assert token is None or isinstance(token, int)
    # stable across calls when the clipboard does not change
    assert yt._clipboard_change_token() == token or token is None


# ---------------------------------------------------------------------------
# Persistent watch queue — survives a crash, resumable
# ---------------------------------------------------------------------------

def test_queue_state_roundtrip(tmp_path):
    assert yt.load_pending_queue(str(tmp_path)) == []
    yt.save_pending_queue(str(tmp_path), ['a', 'b'])
    assert yt.load_pending_queue(str(tmp_path)) == ['a', 'b']
    # emptying the queue removes the file instead of leaving an empty one behind
    yt.save_pending_queue(str(tmp_path), [])
    assert not os.path.exists(yt.queue_state_path(str(tmp_path)))


def test_queue_state_tolerates_garbage(tmp_path):
    yt.queue_state_path(str(tmp_path))
    (tmp_path / yt.QUEUE_STATE_NAME).write_text('not json', encoding='utf-8')
    assert yt.load_pending_queue(str(tmp_path)) == []


def test_persistent_queue_keeps_item_until_done(tmp_path):
    q = yt.PersistentQueue(str(tmp_path))
    q.put('https://www.youtube.com/watch?v=aaaaaaaaaaa')
    q.put('https://www.youtube.com/watch?v=bbbbbbbbbbb')
    assert yt.load_pending_queue(str(tmp_path)) == [
        'https://www.youtube.com/watch?v=aaaaaaaaaaa',
        'https://www.youtube.com/watch?v=bbbbbbbbbbb',
    ]

    url = q.get(timeout=0.1)
    # Taking it off the queue must NOT drop it from disk yet: a crash during the
    # download has to leave the link behind for the next run.
    assert url in yt.load_pending_queue(str(tmp_path))

    q.done(url)
    assert yt.load_pending_queue(str(tmp_path)) == [
        'https://www.youtube.com/watch?v=bbbbbbbbbbb']


def test_persistent_queue_resumes_previous_session(tmp_path):
    yt.save_pending_queue(str(tmp_path), ['https://www.youtube.com/watch?v=ccccccccccc'])
    q = yt.PersistentQueue(str(tmp_path), initial=yt.load_pending_queue(str(tmp_path)))
    assert q.get(timeout=0.1) == 'https://www.youtube.com/watch?v=ccccccccccc'
    assert q.remaining() == ['https://www.youtube.com/watch?v=ccccccccccc']


def test_persistent_queue_get_returns_none_when_empty(tmp_path):
    q = yt.PersistentQueue(str(tmp_path))
    assert q.get(timeout=0.05) is None
    assert q.empty() is True
