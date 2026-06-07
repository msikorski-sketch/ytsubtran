"""
Detecting and cutting short inserted clips / interstitials ("przerywniki").

DRAFT — not yet wired into the CLI. Functions shell out to ffmpeg/ffprobe.
Once verified, this will be exposed via --find-inserts / --cut-inserts.

Cascade (what the user asked for):
  1) SponsorBlock — if the YouTube video has community-labeled segments (e.g. the
     'filler' / jokes category), use those high-confidence timestamps.
  2) Heuristic — otherwise detect short spans that stand out by a sudden loudness
     jump (EBU R128) corroborated by a hard scene cut (ffmpeg scene detection).
  3) AI cross-check (optional) — if a local Ollama model is available, transcribe
     each heuristic candidate and ask the model whether it looks like an inserted
     bit, keeping only the confirmed ones.

Design principle: NEVER cut blindly. `find_inserts()` only proposes ranges; cutting
happens in a separate, explicit step after review.
"""
import getpass
import json
import os
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request

SPONSORBLOCK_API = 'https://sponsor.ajay.app/api/skipSegments'
OLLAMA_API = 'http://localhost:11434/api/generate'


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def get_video_id(url):
    """Extracts an 11-char YouTube video id from a URL, or None."""
    m = re.search(r'[?&]v=([0-9A-Za-z_-]{11})', url or '')
    return m.group(1) if m else None


def ffprobe_duration(path):
    """Returns media duration in seconds (0.0 on failure)."""
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=nw=1:nk=1', path],
            capture_output=True, text=True)
        return float(r.stdout.strip())
    except Exception:
        return 0.0


def merge_ranges(ranges, gap=0.3):
    """Merges overlapping/adjacent (start, end) ranges (ignores any extra tuple items)."""
    norm = sorted((float(r[0]), float(r[1])) for r in ranges)
    merged = []
    for s, e in norm:
        if merged and s <= merged[-1][1] + gap:
            merged[-1] = (merged[-1][0], max(merged[-1][1], e))
        else:
            merged.append((s, e))
    return merged


def clamp_segments(candidates, duration, min_seg=0.05):
    """
    Clamps each (start, end[, reason]) segment to the real media length and drops
    anything that falls outside it.

    Why this exists: external detectors (especially the Gemini multimodal API) can
    return timestamps PAST the end of the video — their internal sense of time
    "drifts" on longer clips. Trusting those blindly makes ffmpeg seek beyond the
    file and write empty 0-byte "clips" (junk). Validating against the true
    duration at this boundary is the fix.

    Returns (kept, dropped_count) where kept is a list of (start, end, reason).
    """
    kept, dropped = [], 0
    for seg in candidates:
        s, e = float(seg[0]), float(seg[1])
        reason = seg[2] if len(seg) > 2 and isinstance(seg[2], str) else ''
        if duration and duration > 0:
            s = max(0.0, min(s, duration))
            e = max(0.0, min(e, duration))
        if (e - s) >= min_seg:
            kept.append((s, e, reason))
        else:
            dropped += 1
    return kept, dropped



# ---------------------------------------------------------------------------
# 1) SponsorBlock
# ---------------------------------------------------------------------------

def fetch_sponsorblock_segments(video_id, categories=('filler',)):
    """
    Returns a list of (start, end, category) from SponsorBlock, or [] if the video
    has no labeled segments (HTTP 404) or on any network error.
    The 'filler' category = community-labeled tangents / jokes / fun inserts.
    """
    if not video_id:
        return []
    try:
        qs = urllib.parse.urlencode({
            'videoID': video_id,
            'categories': json.dumps(list(categories)),
        })
        req = urllib.request.Request(f'{SPONSORBLOCK_API}?{qs}',
                                     headers={'User-Agent': 'ytsubtran'})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        out = []
        for item in data:
            seg = item.get('segment') or [None, None]
            s, e = seg[0], seg[1]
            if s is not None and e is not None and e > s:
                out.append((float(s), float(e), item.get('category', '')))
        return sorted(out)
    except Exception:
        return []


# ---------------------------------------------------------------------------
# 2) Heuristic: loudness jumps + scene cuts
# ---------------------------------------------------------------------------

def _run_ffmpeg_progress(cmd, duration=0.0, label='analyzing'):
    """
    Runs an ffmpeg analysis command, streaming a live progress percentage so the
    user can see it's working (these full-file passes can take minutes). Returns
    the collected stdout+stderr text for parsing.
    """
    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1)
    except FileNotFoundError:
        return ''
    collected = []
    last_shown = -10.0
    for line in proc.stdout:
        collected.append(line)
        m = re.search(r'time=(\d+):(\d+):(\d+(?:\.\d+)?)', line)
        if m and duration > 0:
            secs = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
            if secs - last_shown >= 3:
                last_shown = secs
                pct = min(100.0, secs / duration * 100.0)
                sys.stdout.write(f'\r   {label}: {pct:5.1f}%  ')
                sys.stdout.flush()
    proc.wait()
    sys.stdout.write(f'\r   {label}: done.        \n')
    sys.stdout.flush()
    return ''.join(collected)


def loudness_timeline(path, duration=0.0):
    """
    Momentary loudness (LUFS) over time via the ffmpeg ebur128 filter.
    Returns (times, loudness) parallel lists. `-vn` skips video decode (faster).
    """
    # ametadata=print reliably emits the momentary loudness (lavfi.r128.M) for every
    # frame to stdout — ffmpeg's ebur128 does not log those lines in every build.
    out = _run_ffmpeg_progress(
        ['ffmpeg', '-hide_banner', '-vn', '-i', path,
         '-af', 'ebur128=metadata=1,ametadata=mode=print:key=lavfi.r128.M:file=-',
         '-f', 'null', '-'],
        duration=duration, label='loudness scan')
    times, loud = [], []
    cur_t = None
    for line in out.splitlines():
        mt = re.search(r'pts_time:([\d.]+)', line)
        if mt:
            cur_t = float(mt.group(1))
            continue
        mm = re.search(r'lavfi\.r128\.M=(-?[\d.]+|-?inf|nan)', line)
        if mm and cur_t is not None:
            v = mm.group(1)
            loud.append(-120.0 if ('inf' in v or v == 'nan') else float(v))
            times.append(cur_t)
    return times, loud


def scene_cut_times(path, threshold=0.3, duration=0.0):
    """
    Timestamps (s) of hard scene cuts. The frames are downscaled to 320px wide
    before scene analysis, which speeds up decoding dramatically without hurting
    cut detection. `-an` skips the audio.
    """
    out = _run_ffmpeg_progress(
        ['ffmpeg', '-hide_banner', '-an', '-i', path,
         '-filter:v', f"scale=320:-2,select='gt(scene,{threshold})',showinfo",
         '-f', 'null', '-'],
        duration=duration, label='scene scan')
    cuts = []
    for line in out.splitlines():
        m = re.search(r'pts_time:([\d.]+)', line)
        if m:
            cuts.append(float(m.group(1)))
    return cuts


def detect_inserts_heuristic(path, min_len=2.0, max_len=20.0, jump_lu=8.0,
                             scene_window=1.5, require_scene_cut=True):
    """
    Finds short spans whose momentary loudness deviates from the baseline by at
    least `jump_lu` LU, lasting between `min_len` and `max_len` seconds. If scene
    cuts are detected and `require_scene_cut` is True, a candidate is kept only
    when a hard cut happens near its start (corroboration → fewer false positives).

    Returns a list of (start, end, 'heuristic').
    """
    duration = ffprobe_duration(path)
    if duration > 600:
        print(f'   (long file: ~{duration / 60:.0f} min — full-file analysis may take '
              'a few minutes; press Ctrl+C to stop)')

    times, loud = loudness_timeline(path, duration=duration)
    if len(times) < 5:
        return []

    # Baseline = median of the non-silent samples (so quiet pauses don't skew it).
    speech = [v for v in loud if v > -50.0]
    baseline = statistics.median(speech) if speech else statistics.median(loud)
    # Flag samples that are markedly LOUDER than the baseline (the "audio jump").
    anomalous = [(value - baseline) >= jump_lu for value in loud]

    spans = []
    i, n = 0, len(times)
    while i < n:
        if anomalous[i]:
            j = i
            while j + 1 < n and anomalous[j + 1]:
                j += 1
            start, end = times[i], times[j]
            if min_len <= (end - start) <= max_len:
                spans.append((start, end))
            i = j + 1
        else:
            i += 1

    if require_scene_cut:
        cuts = scene_cut_times(path, duration=duration)
        if cuts:
            spans = [(s, e) for (s, e) in spans
                     if any(abs(c - s) <= scene_window for c in cuts)]

    return [(s, e, 'heuristic') for (s, e) in spans]


# ---------------------------------------------------------------------------
# 3) Optional AI cross-check via local Ollama
# ---------------------------------------------------------------------------

def _ollama_available():
    try:
        with urllib.request.urlopen('http://localhost:11434/api/tags', timeout=3):
            return True
    except Exception:
        return False


def _transcribe_span(path, start, end, model_size='base'):
    """Transcribes a single [start, end] span to plain text (best-effort)."""
    try:
        import whisper
    except Exception:
        return ''
    tmp = os.path.join(tempfile.mkdtemp(prefix='ytins_'), 'span.wav')
    try:
        subprocess.run(
            ['ffmpeg', '-y', '-hide_banner', '-ss', f'{start:.3f}', '-to', f'{end:.3f}',
             '-i', os.path.abspath(path), '-ac', '1', '-ar', '16000', tmp],
            capture_output=True, text=True)
        model = whisper.load_model(model_size)
        result = model.transcribe(tmp, fp16=False)
        return (result.get('text') or '').strip()
    except Exception:
        return ''
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


def ai_confirm_insert(text, model='llama3.1'):
    """
    Asks a local Ollama model whether a transcript snippet is an inserted clip /
    interstitial (vs. main content). Returns True/False. On any error returns True
    (the AI is only a filter — don't silently drop candidates when it's unavailable).
    """
    if not text:
        return True
    prompt = (
        'You are reviewing a short segment cut from a longer video. Based on the '
        'transcript snippet below, decide whether it is an INSERTED clip / '
        'interstitial / break (e.g. a meme, a joke skit, a clip from another video, '
        'an intro sting) rather than part of the main spoken content.\n\n'
        f'Transcript: "{text}"\n\n'
        'Reply with strict JSON: {"insert": true} or {"insert": false}.'
    )
    try:
        payload = json.dumps({
            'model': model, 'prompt': prompt, 'stream': False, 'format': 'json',
        }).encode('utf-8')
        req = urllib.request.Request(OLLAMA_API, data=payload,
                                     headers={'Content-Type': 'application/json'})
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.load(resp)
        answer = json.loads(data.get('response', '{}'))
        return bool(answer.get('insert', True))
    except Exception:
        return True


def ai_filter_candidates(path, candidates, model='llama3.1'):
    """Keeps only candidates the AI confirms as inserts. No-op if Ollama is down."""
    if not candidates or not _ollama_available():
        return candidates
    kept = []
    for (s, e, src) in candidates:
        text = _transcribe_span(path, s, e)
        if ai_confirm_insert(text, model=model):
            kept.append((s, e, src))
    return kept


# ---------------------------------------------------------------------------
# Gemini (multimodal API) — the strong detector for visually-defined inserts
# ---------------------------------------------------------------------------

CONFIG_PATH = os.path.join(os.path.expanduser('~'), '.ytsubtran.json')


def get_gemini_key(force_prompt=False):
    """
    Returns a Gemini API key. When `force_prompt` is False, it tries (in order):
    env var, saved config file, then an interactive prompt. When `force_prompt` is
    True, it skips the env var and saved file and asks the user directly (used to
    recover from an expired/invalid saved key). Returns None if unavailable or the
    session is non-interactive.
    """
    if not force_prompt:
        key = os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')
        if key:
            return key.strip()
        try:
            with open(CONFIG_PATH, encoding='utf-8') as f:
                key = (json.load(f) or {}).get('gemini_api_key')
                if key:
                    return key.strip()
        except Exception:
            pass
    if not sys.stdin or not sys.stdin.isatty():
        return None
    print('\nA Gemini API key is required for --smart-inserts.')
    print('Get a free key at: https://aistudio.google.com/apikey')
    if force_prompt and (os.environ.get('GEMINI_API_KEY') or os.environ.get('GOOGLE_API_KEY')):
        print('  Note: GEMINI_API_KEY is set in your environment and overrides the')
        print('        saved key on the next run — update or unset it there too.')
    try:
        # getpass hides the key as you type so it never appears on screen / in logs.
        key = getpass.getpass('Paste your Gemini API key (hidden, it will be saved): ').strip()
    except (EOFError, KeyboardInterrupt):
        print()
        return None
    if key:
        try:
            with open(CONFIG_PATH, 'w', encoding='utf-8') as f:
                json.dump({'gemini_api_key': key}, f)
            print(f'✓ Saved to {CONFIG_PATH} (delete that file to remove the key).')
        except OSError:
            pass
    return key or None


def clear_gemini_key():
    """Deletes the saved Gemini API key file (e.g. after it expired). No-op if absent."""
    try:
        os.remove(CONFIG_PATH)
    except OSError:
        pass


def _parse_time(value):
    """Accepts seconds (number) or 'M:SS' / 'H:MM:SS' strings → float seconds."""
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    if ':' in s:
        parts = [float(p) for p in s.split(':')]
        total = 0.0
        for p in parts:
            total = total * 60 + p
        return total
    try:
        return float(s)
    except ValueError:
        return 0.0


def _ascii_upload_path(video_file):
    """
    The Gemini SDK puts the file name into an ASCII-only HTTP header, which breaks
    for non-ASCII paths (e.g. Polish characters like 'ó'). If the path is already
    pure ASCII, it is returned unchanged. Otherwise we expose the file under an
    ASCII name without copying its bytes when possible:
      1) a hardlink in the same (ASCII) directory  → instant, same volume;
      2) failing that, a full copy into a temp dir  → cross-volume fallback.
    Returns (upload_path, cleanup) where cleanup() removes any temporary artifact.
    """
    abspath = os.path.abspath(video_file)
    try:
        abspath.encode('ascii')
        return abspath, (lambda: None)
    except UnicodeEncodeError:
        pass

    ext = os.path.splitext(video_file)[1] or '.mp4'
    parent = os.path.dirname(abspath)

    def _safe_remove(path):
        try:
            os.remove(path)
        except OSError:
            pass

    # 1) Hardlink with an ASCII name next to the original (no data copied).
    try:
        parent.encode('ascii')
        link_path = os.path.join(parent, f'ytins_upload_{os.getpid()}{ext}')
        os.link(abspath, link_path)
        return link_path, (lambda: _safe_remove(link_path))
    except (UnicodeEncodeError, OSError):
        pass

    # 2) Full copy into a temp directory (different volume / FS without hardlinks).
    tmp_dir = tempfile.mkdtemp(prefix='ytins_up_')
    tmp_path = os.path.join(tmp_dir, 'upload' + ext)
    shutil.copy2(abspath, tmp_path)

    def cleanup():
        _safe_remove(tmp_path)
        try:
            os.rmdir(tmp_dir)
        except OSError:
            pass

    return tmp_path, cleanup


def _is_invalid_key_error(error):
    """True if a Gemini error looks like an expired / invalid API key."""
    text = str(error)
    return ('API_KEY_INVALID' in text
            or 'API key expired' in text
            or 'API key not valid' in text)


def _run_gemini(video_file, model, key, genai, types):
    """Single Gemini attempt with a given key. Raises on API errors (e.g. bad key)."""
    import time
    client = genai.Client(api_key=key)

    print('   uploading video to Gemini (may take a while for large files)...')
    upload_path, _cleanup_upload = _ascii_upload_path(video_file)
    try:
        uploaded = client.files.upload(file=upload_path)
    finally:
        _cleanup_upload()
    # Wait until the file is processed and ready
    while getattr(uploaded.state, 'name', str(uploaded.state)) == 'PROCESSING':
        time.sleep(2)
        uploaded = client.files.get(name=uploaded.name)
    if getattr(uploaded.state, 'name', '') == 'FAILED':
        print('✗ Gemini could not process the uploaded video.')
        return []

    prompt = (
        'This video is mostly a host/people talking to camera. Find every '
        'INSERTED clip / interstitial / cutaway — short segments (often a few '
        'seconds, sometimes only a few frames) taken from OTHER footage: memes, '
        'jokes, reaction clips, bumpers, or clips from a different video that '
        'interrupt the main talking. Do NOT include normal cuts between shots of '
        'the same scene. For each insert, give precise start and end times in '
        'SECONDS from the beginning. Respond as strict JSON: a list of objects '
        '{"start": <seconds>, "end": <seconds>, "reason": "<short description>"}. '
        'If there are none, return [].'
    )
    print('   asking Gemini to locate inserts...')
    resp = client.models.generate_content(
        model=model,
        contents=[uploaded, prompt],
        config=types.GenerateContentConfig(response_mime_type='application/json'),
    )
    data = json.loads(resp.text)
    out = []
    for item in data:
        s = _parse_time(item.get('start'))
        e = _parse_time(item.get('end'))
        if e > s:
            out.append((s, e, item.get('reason', '')))
    return sorted(out)


def smart_find_inserts(video_file, model='gemini-2.5-flash'):
    """
    Uploads the video to Gemini and asks it to list inserted clips / interstitials
    with timestamps. Returns (list of (start, end, reason), 'gemini').
    If the saved key is expired/invalid, clears it and prompts for a new one once.
    Returns ([], 'gemini') on any error (missing SDK/key/network).
    """
    try:
        from google import genai
        from google.genai import types
    except Exception:
        print('✗ The google-genai SDK is not installed.')
        print('  Install it with:  pip install -U google-genai')
        return [], 'gemini'

    key = get_gemini_key()
    if not key:
        print('✗ No Gemini API key available — skipping smart detection.')
        return [], 'gemini'

    for attempt in (1, 2):
        try:
            return _run_gemini(video_file, model, key, genai, types), 'gemini'
        except Exception as e:
            if attempt == 1 and _is_invalid_key_error(e):
                print('✗ The saved Gemini API key is invalid or has expired.')
                clear_gemini_key()
                key = get_gemini_key(force_prompt=True)
                if key:
                    print('   retrying with the new key...')
                    continue
                print('✗ No new key provided — skipping smart detection.')
                return [], 'gemini'
            print(f'✗ Gemini request failed: {e}')
            return [], 'gemini'
    return [], 'gemini'


# ---------------------------------------------------------------------------
# Cutting
# ---------------------------------------------------------------------------

def cut_segments(path, cut_ranges, output_file):
    """
    Removes the given (start, end) ranges and concatenates the kept parts
    (frame-accurate → re-encode). Returns the output path or None on failure.
    """
    duration = ffprobe_duration(path)
    cuts = merge_ranges(cut_ranges)

    keeps, cursor = [], 0.0
    for s, e in cuts:
        if s > cursor:
            keeps.append((cursor, s))
        cursor = max(cursor, e)
    if duration and cursor < duration:
        keeps.append((cursor, duration))
    keeps = [(s, e) for s, e in keeps if (e - s) > 0.05]
    if not keeps:
        return None

    parts, labels = [], []
    for idx, (s, e) in enumerate(keeps):
        parts.append(
            f"[0:v]trim=start={s:.3f}:end={e:.3f},setpts=PTS-STARTPTS[v{idx}];"
            f"[0:a]atrim=start={s:.3f}:end={e:.3f},asetpts=PTS-STARTPTS[a{idx}]"
        )
        labels.append(f"[v{idx}][a{idx}]")
    filtergraph = ';'.join(parts) + ';' + ''.join(labels) + \
        f"concat=n={len(keeps)}:v=1:a=1[outv][outa]"

    cmd = ['ffmpeg', '-y', '-hide_banner', '-i', os.path.abspath(path),
           '-filter_complex', filtergraph,
           '-map', '[outv]', '-map', '[outa]', os.path.abspath(output_file)]
    r = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    if r.returncode == 0 and os.path.exists(output_file):
        return output_file
    return None


def _safe_filename(text, max_len=60):
    """Turns a free-text reason into a safe, readable file name (keeps PL letters)."""
    text = (text or '').strip()
    text = re.sub(r'[\\/:*?"<>|]+', '', text)   # characters illegal on Windows
    text = re.sub(r'\s+', ' ', text).strip()
    text = text[:max_len].rstrip(' .')
    return text or 'clip'


def extract_clips(path, segments, output_dir, reencode=True):
    """
    Saves EACH segment as its own standalone clip file (does not touch the original
    or join anything). File names are numbered and include the start time and the
    Gemini/heuristic reason, e.g. ``03_02m11s_Animated intro sequence.mp4`` — ready
    to drop into an editor. Returns the list of created file paths.

    reencode=True  → frame-accurate cut, re-encoded to H.264/AAC (editor-friendly).
    reencode=False → stream copy (instant, but starts at the nearest keyframe).
    """
    os.makedirs(output_dir, exist_ok=True)
    created = []
    for idx, seg in enumerate(segments, 1):
        s, e = float(seg[0]), float(seg[1])
        if e <= s:
            continue
        reason = seg[2] if len(seg) > 2 and isinstance(seg[2], str) else ''
        tag = f'{int(s // 60):02d}m{int(s % 60):02d}s'
        name = f'{idx:02d}_{tag}_{_safe_filename(reason)}.mp4'
        out = os.path.join(output_dir, name)
        if reencode:
            cmd = ['ffmpeg', '-y', '-hide_banner', '-ss', f'{s:.3f}',
                   '-i', os.path.abspath(path), '-t', f'{e - s:.3f}',
                   '-c:v', 'libx264', '-crf', '18', '-preset', 'medium',
                   '-c:a', 'aac', '-b:a', '192k', os.path.abspath(out)]
        else:
            cmd = ['ffmpeg', '-y', '-hide_banner', '-ss', f'{s:.3f}',
                   '-i', os.path.abspath(path), '-t', f'{e - s:.3f}',
                   '-c', 'copy', os.path.abspath(out)]
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        # ffmpeg can exit 0 yet write a header-only file (~hundreds of bytes) when
        # -ss seeks at/past the end of the video. A real encoded clip is always far
        # bigger than 1 KB, so treat anything smaller as a failure and delete it.
        if r.returncode == 0 and os.path.exists(out) and os.path.getsize(out) >= 1024:
            created.append(out)
            print(f'   ✓ {name}')
        else:
            if os.path.exists(out) and os.path.getsize(out) < 1024:
                try:
                    os.remove(out)
                except OSError:
                    pass
            print(f'   ✗ failed: {name}')
    return created


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def find_inserts(video_file, url=None, use_ai=False, ai_model='llama3.1',
                 sponsorblock_categories=('filler',), **heuristic_kwargs):
    """
    Runs the cascade and returns (candidates, source) where source is
    'sponsorblock' or 'heuristic'. Does NOT cut anything.
    """
    video_id = get_video_id(url)
    sb = fetch_sponsorblock_segments(video_id, sponsorblock_categories)
    if sb:
        return sb, 'sponsorblock'

    candidates = detect_inserts_heuristic(video_file, **heuristic_kwargs)
    if use_ai and candidates:
        candidates = ai_filter_candidates(video_file, candidates, model=ai_model)
    return candidates, 'heuristic'


# ---------------------------------------------------------------------------
# Cut-list persistence (human-editable) + scene-cut snapping
# ---------------------------------------------------------------------------

def save_cut_list(path, candidates):
    """
    Writes the cut list in a human-editable format. Each segment is one line:
        START_SECONDS<TAB>END_SECONDS<TAB># reason
    A header explains how to keep a segment (delete its line). Lines starting
    with '#' and blank lines are ignored on load, so the file round-trips.
    """
    lines = [
        '# Insert cut list — edit freely, then cut with: --cut-inserts --from-list THIS_FILE',
        '# Each line is one segment to REMOVE.  Delete a line to KEEP that part.',
        '# Format:  START_SECONDS <TAB> END_SECONDS <TAB> # reason',
        '#',
    ]
    for seg in candidates:
        s, e = float(seg[0]), float(seg[1])
        reason = seg[2] if len(seg) > 2 and isinstance(seg[2], str) else ''
        comment = f'\t# {reason}' if reason else ''
        lines.append(f'{s:.3f}\t{e:.3f}{comment}')
    with open(path, 'w', encoding='utf-8') as f:
        f.write('\n'.join(lines) + '\n')


def load_cut_list(path):
    """
    Reads a cut list saved by save_cut_list (or hand-written). Ignores blank lines
    and '#' comments. Accepts seconds or 'M:SS' for the two time columns and keeps
    any trailing '# reason'. Returns a sorted list of (start, end, reason).
    """
    out = []
    with open(path, encoding='utf-8') as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith('#'):
                continue
            reason = ''
            if '#' in line:
                line, reason = line.split('#', 1)
                reason = reason.strip()
            parts = line.replace(',', ' ').split()
            if len(parts) < 2:
                continue
            s, e = _parse_time(parts[0]), _parse_time(parts[1])
            if e > s:
                out.append((s, e, reason))
    return sorted(out)


def snap_to_scene_cuts(candidates, cuts, tolerance=1.5):
    """
    Snaps each segment's start/end to the nearest detected scene cut within
    `tolerance` seconds, giving frame-accurate boundaries instead of the model's
    rounded (±1 s) times. If no cut is near, the original value is kept. Snapped
    segments are merged so any resulting overlaps collapse into clean ranges.
    """
    if not cuts:
        return [(float(s), float(e), (seg[2] if len(seg) > 2 else ''))
                for seg in candidates for s, e in [(seg[0], seg[1])]]
    ordered = sorted(cuts)

    def nearest(t):
        best, best_d = t, tolerance
        for c in ordered:
            d = abs(c - t)
            if d <= best_d:
                best, best_d = c, d
            elif c - t > tolerance:
                break
        return best

    snapped = []
    for seg in candidates:
        s, e = nearest(float(seg[0])), nearest(float(seg[1]))
        reason = seg[2] if len(seg) > 2 else ''
        if e > s:
            snapped.append((s, e, reason))
    # Preserve reasons when merging adjacent/overlapping snapped ranges
    merged = merge_ranges([(s, e) for s, e, _ in snapped])
    result = []
    for ms, me in merged:
        reasons = [r for s, e, r in snapped if r and s >= ms - 0.01 and e <= me + 0.01]
        result.append((ms, me, '; '.join(dict.fromkeys(reasons))))
    return result
