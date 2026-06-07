"""
Unit tests for the pure (side-effect-free) helper functions.

These import youtube_downloader directly, which only needs the standard library
at import time (whisper/torch/yt-dlp are imported lazily inside functions), so the
tests run fast and without the heavy dependencies installed.
"""
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

