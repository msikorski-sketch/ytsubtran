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
    assert "429" in diag["message"] or "żąda" in diag["message"].lower()


def test_diagnose_unknown_defaults_to_suggest_update():
    diag = yt.diagnose_failure("something completely unexpected happened")
    assert diag["suggest_update"] is True


def test_diagnose_cookie_db_error_does_not_suggest_update():
    diag = yt.diagnose_failure("ERROR: Could not copy Chrome cookie database. See ...")
    assert diag["suggest_update"] is False
    assert "ciastecz" in diag["message"].lower()


# ---------------------------------------------------------------------------
# lang_name
# ---------------------------------------------------------------------------

def test_lang_name_known_and_unknown():
    assert yt.lang_name("pl") == "polski"
    assert yt.lang_name("xx") == "xx"  # unknown code returns the code itself
