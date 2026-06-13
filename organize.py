"""
Catalog mode: tidy a folder of generically-named lecture videos.

Course platforms like DataCamp let you download a lesson with a "download video"
button, but the file lands on disk as ``video (1).mp4``, ``video (2).mp4`` … with
no hint of WHICH course it belongs to. This module figures out what each file is
about and files it away for you:

  1) grab a handful of frames (the slides usually show the COURSE name and the
     CHAPTER / lesson title right on screen) plus a short Whisper transcript of
     the intro;
  2) ask Gemini to read them and return {course, title, category, number};
  3) build a plan that renames each file to its lesson title and moves it into a
     per-course sub-folder, e.g.  ``Introduction to Docker/02 - Docker images.mp4``.

Design principle (same as inserts.py): NEVER move blindly. ``organize_folder``
builds the full plan and prints it; the actual renaming/moving happens only after
an explicit confirmation (or ``--yes``). Subtitle/transcript side-car files that
share a video's name are moved and renamed alongside it.

Reuses the Gemini API-key handling, the Windows-safe filename helper and
``ffprobe_duration`` from inserts.py so there is one source of truth for those.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile

import inserts  # Gemini key handling + _safe_filename + ffprobe_duration

# Video containers we treat as "a lecture to catalog". Side-car files that share
# a video's base name (subtitles/transcripts produced earlier) ride along with it.
VIDEO_EXTS = ('.mp4', '.mkv', '.webm', '.mov', '.flv', '.m4v')
SIDECAR_EXTS = ('.srt', '.vtt', '.txt', '.ass')

# Anything classified below this confidence (or missing a course/title) is left
# untouched in an "_Uncategorized" folder rather than renamed to a wild guess.
CONFIDENCE_FLOOR = 0.35
UNCATEGORIZED = '_Uncategorized'

# Prompt Gemini sees alongside the frames (+ optional transcript). Tuned for
# slide-based course videos (DataCamp, Coursera, conference talks, …).
CLASSIFY_PROMPT = (
    'These images are frames sampled from a single lecture/tutorial video (often '
    'from an online course platform such as DataCamp). The slides usually print '
    'the COURSE name and the CHAPTER or lesson title directly on screen.\n'
    'Read the frames (and the transcript snippet if given) and identify the video.\n'
    'Respond as a STRICT JSON object with these keys:\n'
    '  "course":     the overall course / series name (e.g. "Introduction to Docker"). '
    'Empty string if you cannot tell.\n'
    '  "title":      the title of THIS specific video / lesson / chapter '
    '(e.g. "Docker images"). Empty string if unclear.\n'
    '  "category":   one short topic/technology label (e.g. "Docker", "Python", "SQL").\n'
    '  "number":     any visible lesson/chapter number such as "3" or "1.2", '
    'else "".\n'
    '  "confidence": your confidence from 0 to 1 that the above is correct.\n'
    'Use the on-screen slide text as the primary source of truth. '
    'Return ONLY the JSON object.'
)


# ---------------------------------------------------------------------------
# Discovery
# ---------------------------------------------------------------------------

def find_videos(folder):
    """Top-level video files in `folder` (non-recursive), sorted by name."""
    try:
        names = os.listdir(folder)
    except OSError:
        return []
    vids = [
        n for n in names
        if os.path.isfile(os.path.join(folder, n))
        and os.path.splitext(n)[1].lower() in VIDEO_EXTS
    ]
    return sorted(vids)


def collect_sidecars(folder, video_filename):
    """
    Side-car files (subtitles/transcripts) that belong to `video_filename`: either
    exactly ``<stem>.<srt|vtt|txt|ass>`` or ``<stem>_*.<…>`` (e.g. ``_EN.srt`` as
    produced by the subtitle step). The trailing ``_`` guard means ``video (1)``
    does not greedily grab ``video (10)``'s files. Returns a sorted name list.
    """
    stem = os.path.splitext(video_filename)[0]
    out = []
    try:
        names = os.listdir(folder)
    except OSError:
        return out
    for name in names:
        if name == video_filename:
            continue
        if not os.path.isfile(os.path.join(folder, name)):
            continue
        base, ext = os.path.splitext(name)
        if ext.lower() not in SIDECAR_EXTS:
            continue
        if base == stem or name.startswith(stem + '_'):
            out.append(name)
    return sorted(out)


# ---------------------------------------------------------------------------
# Evidence: frames + a short transcript
# ---------------------------------------------------------------------------

# How frames are spread across the runtime. The sampling fractions are generated
# from a power curve so points cluster toward the START (where the title slide
# usually is) yet still cover the body of the video. The curve is mapped onto
# [_FRAME_START, _FRAME_SPAN] so the first sample is just inside the video (not a
# black frame 0) and the last stays off the very end (end cards / credits). This
# stays strictly increasing for any --organize-frames N.
_FRAME_BIAS = 1.7    # >1 pulls samples toward the start; 1.0 = evenly spaced
_FRAME_START = 0.01  # first sample at ~1% of the runtime
_FRAME_SPAN = 0.85   # last sample at ~85% of the runtime
_ASSUMED_DURATION = 600.0  # used only when the real duration can't be probed


def _frame_times(duration, n):
    """
    Choose `n` timestamps (seconds) to sample, biased toward the start where the
    title slide usually lives. Generalizes to ANY `n` (not a fixed list), so a
    larger --organize-frames really does sample more points. Returns up to `n`
    distinct, strictly increasing times inside the video; uses an assumed runtime
    if `duration` is unknown. Pure/testable.
    """
    n = max(1, n)
    if n == 1:
        fracs = [0.05]
    else:
        # (i/(n-1)) walks 0→1; raising to _FRAME_BIAS front-loads it; mapping onto
        # [_FRAME_START, _FRAME_SPAN] keeps it monotonic and off both ends.
        span = _FRAME_SPAN - _FRAME_START
        fracs = [round(_FRAME_START + span * (i / (n - 1)) ** _FRAME_BIAS, 4)
                 for i in range(n)]

    runtime = duration if (duration and duration > 0) else _ASSUMED_DURATION
    # De-duplicate after rounding (very short clips can collapse adjacent points)
    seen, times = set(), []
    for f in fracs:
        t = round(f * runtime, 3)
        if t not in seen:
            seen.add(t)
            times.append(t)
    return times


def _grab_frame(video, when, out_path, width=720):
    """Extract a single JPEG frame at `when` seconds. Returns True on success."""
    cmd = [
        'ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
        '-ss', f'{when:.3f}', '-i', os.path.abspath(video),
        '-frames:v', '1', '-vf', f'scale={width}:-2', '-q:v', '3',
        os.path.abspath(out_path),
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return False
    return r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0


def extract_frames(video, n=5, width=720):
    """
    Sample up to `n` frames from the video and return them as a list of JPEG byte
    blobs (ready to hand to Gemini inline). Empty list if ffmpeg/ffprobe fail.
    """
    duration = inserts.ffprobe_duration(video)
    times = _frame_times(duration, n)
    workdir = tempfile.mkdtemp(prefix='ytorg_fr_')
    frames = []
    try:
        for idx, t in enumerate(times):
            out = os.path.join(workdir, f'f{idx}.jpg')
            if _grab_frame(video, t, out, width=width):
                with open(out, 'rb') as f:
                    frames.append(f.read())
    finally:
        shutil.rmtree(workdir, ignore_errors=True)
    return frames


def transcribe_intro(video, model, seconds=90):
    """
    Transcribe the first `seconds` of audio with a preloaded Whisper model and
    return the text (trimmed to ~2 kB — it is only a hint for Gemini). Returns ''
    if no model is available or anything goes wrong.
    """
    if model is None:
        return ''
    workdir = tempfile.mkdtemp(prefix='ytorg_au_')
    wav = os.path.join(workdir, 'intro.wav')
    try:
        subprocess.run(
            ['ffmpeg', '-y', '-hide_banner', '-loglevel', 'error',
             '-t', str(seconds), '-i', os.path.abspath(video),
             '-ac', '1', '-ar', '16000', wav],
            capture_output=True, text=True, encoding='utf-8', errors='replace')
        if not os.path.exists(wav):
            return ''
        result = model.transcribe(wav, fp16=False)
        return (result.get('text') or '').strip()[:2000]
    except Exception:
        return ''
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


def _load_whisper(model_size='base'):
    """Load a Whisper model once (reused across all videos). None if unavailable."""
    try:
        import whisper
    except Exception:
        print('   ⚠️  Whisper not installed — classifying from video frames only.')
        print('      (install it for better accuracy:  pip install -U openai-whisper)')
        return None
    try:
        print(f'   ⏳ Loading Whisper model ({model_size}) for intro transcripts...')
        return whisper.load_model(model_size)
    except Exception as e:
        print(f'   ⚠️  Could not load Whisper ({e}) — using frames only.')
        return None


# ---------------------------------------------------------------------------
# Classification (Gemini) + normalization
# ---------------------------------------------------------------------------

def normalize_info(raw):
    """
    Coerce Gemini's JSON into a clean dict with the keys we rely on. Tolerant of
    missing/oddly-named fields and non-numeric confidence. Pure/testable.
    """
    raw = raw if isinstance(raw, dict) else {}

    def text(*keys):
        for k in keys:
            v = raw.get(k)
            if isinstance(v, (str, int, float)) and str(v).strip():
                return str(v).strip()
        return ''

    conf = raw.get('confidence')
    try:
        conf = float(conf)
    except (TypeError, ValueError):
        conf = None
    if conf is not None:
        conf = max(0.0, min(1.0, conf))

    return {
        'course': text('course', 'series', 'course_name'),
        'title': text('title', 'lesson', 'video_title', 'chapter_title'),
        'category': text('category', 'topic', 'technology'),
        'number': text('number', 'chapter', 'lesson_number'),
        'confidence': conf,
    }


def _gemini_classify(client, types, model, frames, transcript):
    """One Gemini call: frames (+ transcript) → normalized info dict. May raise."""
    parts = [types.Part.from_bytes(data=b, mime_type='image/jpeg') for b in frames]
    prompt = CLASSIFY_PROMPT
    if transcript:
        prompt += f'\n\nTranscript snippet from the intro:\n"{transcript}"'
    parts.append(prompt)
    resp = client.models.generate_content(
        model=model,
        contents=parts,
        config=types.GenerateContentConfig(response_mime_type='application/json'),
    )
    return normalize_info(json.loads(resp.text))


# ---------------------------------------------------------------------------
# Planning (pure-ish) — what gets renamed/moved where
# ---------------------------------------------------------------------------

def _unique_name(name, taken):
    """Return `name`, or `name (2)`, `name (3)`… so it is not in the `taken` set
    (compared case-insensitively). Does NOT add it — the caller does. Pure."""
    if name.lower() not in taken:
        return name
    base, ext = os.path.splitext(name)
    n = 2
    while f'{base} ({n}){ext}'.lower() in taken:
        n += 1
    return f'{base} ({n}){ext}'


def target_for(info, ext):
    """
    Decide the destination sub-folder and base file name for one classified video.
    Returns (folder, filename, low_confidence). When confidence is too low or the
    course/title is missing, returns (UNCATEGORIZED, None, True) meaning "leave the
    original name, just move it aside for review". Pure/testable.
    """
    course = info.get('course') or info.get('category') or ''
    title = info.get('title') or ''
    conf = info.get('confidence')
    low = (conf is not None and conf < CONFIDENCE_FLOOR) or not course or not title
    if low:
        return UNCATEGORIZED, None, True

    folder = inserts._safe_filename(course, max_len=80)
    number = info.get('number') or ''
    stem = f'{number} - {title}' if number else title
    filename = inserts._safe_filename(stem, max_len=120) + ext
    return folder, filename, False


class PlanItem:
    """One planned move: a source path → destination path, plus its side-cars."""

    def __init__(self, src, dst, info, low, sidecars):
        self.src = src
        self.dst = dst
        self.info = info
        self.low = low
        self.sidecars = sidecars  # list of (src_path, dst_path)


def build_plan(folder, analyses):
    """
    Turn [(filename, info), …] into an ordered list of PlanItem, resolving name
    collisions within each destination folder (numbered suffixes) and routing each
    video's side-car files to the same place with the new base name.
    """
    plan = []
    taken = {}  # sub-folder (lower) -> set of taken file names (lower)
    # Consolidate course folders case-insensitively: Gemini may return the same
    # course as "Intermediate Docker" and "INTERMEDIATE DOCKER" — both must land in
    # ONE folder (Windows merges them by luck; Linux/network drives would not).
    # Seed from folders already on disk so we reuse their existing casing.
    canonical = _existing_dir_casing(folder)  # course (lower) -> folder name to use
    for filename, info in analyses:
        src = os.path.join(folder, filename)
        ext = os.path.splitext(filename)[1]
        sub, newname, low = target_for(info, ext)
        if newname is None:
            newname = filename  # _Uncategorized: keep original name

        key = sub.lower()
        sub = canonical.setdefault(key, sub)  # first-seen (or existing) casing wins
        dst_dir = os.path.join(folder, sub)
        bucket = taken.setdefault(key, _existing_names(dst_dir))
        unique = _unique_name(newname, bucket)
        bucket.add(unique.lower())
        dst = os.path.join(dst_dir, unique)

        new_stem = os.path.splitext(unique)[0]
        old_stem = os.path.splitext(filename)[0]
        sidecars = []
        for sc in collect_sidecars(folder, filename):
            suffix = sc[len(old_stem):]            # e.g. "_EN.srt" or ".srt"
            sc_name = _unique_name(new_stem + suffix, bucket)
            bucket.add(sc_name.lower())
            sidecars.append((os.path.join(folder, sc), os.path.join(dst_dir, sc_name)))

        plan.append(PlanItem(src, dst, info, low, sidecars))
    return plan


def _existing_names(dst_dir):
    """Lower-cased set of names already present in `dst_dir` (so we don't clobber)."""
    try:
        return {n.lower() for n in os.listdir(dst_dir)}
    except OSError:
        return set()


def _existing_dir_casing(folder):
    """
    Map of {lower-cased name: actual name} for sub-folders already in `folder`, so a
    new course that differs only in casing reuses the existing folder's name instead
    of spawning a near-duplicate. Empty when the folder can't be listed.
    """
    out = {}
    try:
        for n in os.listdir(folder):
            if os.path.isdir(os.path.join(folder, n)):
                out[n.lower()] = n
    except OSError:
        pass
    return out


# ---------------------------------------------------------------------------
# Presentation + application
# ---------------------------------------------------------------------------

def _rel(path, folder):
    """Path relative to the scanned folder, for compact display."""
    try:
        return os.path.relpath(path, folder)
    except ValueError:
        return path


def print_plan(plan, folder):
    """Print the planned renames/moves, grouped by destination folder."""
    print('\n' + '=' * 70)
    print('📋 PLAN — nothing has been moved yet')
    print('=' * 70)
    by_dir = {}
    for item in plan:
        by_dir.setdefault(os.path.dirname(item.dst), []).append(item)

    for d in sorted(by_dir):
        print(f'\n📁 {_rel(d, folder)}{os.sep}')
        for item in by_dir[d]:
            conf = item.info.get('confidence')
            tag = ''
            if item.low:
                tag = '   ⚠️  low confidence — left for review'
            elif conf is not None:
                tag = f'   (confidence {conf:.0%})'
            print(f'   • {os.path.basename(item.src)}  →  {os.path.basename(item.dst)}{tag}')
            for sc_src, sc_dst in item.sidecars:
                print(f'        ↳ {os.path.basename(sc_src)}  →  {os.path.basename(sc_dst)}')
    print()


def apply_plan(plan):
    """Execute the moves. Returns the number of videos successfully moved."""
    moved = 0
    for item in plan:
        try:
            os.makedirs(os.path.dirname(item.dst), exist_ok=True)
            shutil.move(item.src, item.dst)
            for sc_src, sc_dst in item.sidecars:
                try:
                    shutil.move(sc_src, sc_dst)
                except OSError as e:
                    print(f'   ⚠️  could not move side-car {os.path.basename(sc_src)}: {e}')
            moved += 1
        except OSError as e:
            print(f'   ✗ failed to move {os.path.basename(item.src)}: {e}')
    return moved


def _confirm(assume_yes):
    """Ask the user to confirm. --yes skips; non-interactive without --yes aborts."""
    if assume_yes:
        return True
    if not sys.stdin or not sys.stdin.isatty():
        print('Non-interactive session and --yes not given — not moving anything.')
        return False
    try:
        answer = input('Apply this plan (rename + move)? [y/N] ').strip().lower()
    except (EOFError, KeyboardInterrupt):
        print()
        return False
    return answer in ('y', 'yes', 't', 'tak')


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def organize_folder(folder, model='gemini-2.5-flash', assume_yes=False,
                    frames_n=5, do_transcript=False, whisper_model='base'):
    """
    Scan `folder`, classify each video with Gemini (frames + short transcript),
    then preview and (after confirmation) rename + sort them into per-course
    sub-folders. See the module docstring for the full picture.
    """
    print('\n' + '=' * 70)
    print('🗂️  CATALOG MODE — naming and sorting videos by content')
    print('=' * 70)

    if not os.path.isdir(folder):
        print(f'✗ Not a folder: {folder}')
        return
    if not shutil.which('ffmpeg'):
        print('✗ ffmpeg not found — it is needed to read frames/audio from the videos.')
        return

    videos = find_videos(folder)
    if not videos:
        print(f'No video files ({", ".join(VIDEO_EXTS)}) found in:\n   {folder}')
        return
    print(f'\nFound {len(videos)} video(s) in {folder}')

    try:
        from google import genai
        from google.genai import types
    except Exception:
        print('✗ The google-genai SDK is not installed.')
        print('  Install it with:  pip install -U google-genai')
        return

    key = inserts.get_gemini_key()
    if not key:
        print('✗ No Gemini API key available — cannot classify. '
              'Get a free key at https://aistudio.google.com/apikey')
        return
    client = genai.Client(api_key=key)

    wmodel = _load_whisper(whisper_model) if do_transcript else None

    analyses = []
    # Let Ctrl+C stop the scan early and still build a plan from what was done so
    # far (handy for big folders — no need to wait out every file to decide).
    try:
        for i, name in enumerate(videos, 1):
            path = os.path.join(folder, name)
            print(f'\n[{i}/{len(videos)}] {name}')
            frames = extract_frames(path, n=frames_n)
            if not frames:
                print('   ⚠️  could not read any frames — skipping (left in place).')
                analyses.append((name, normalize_info({})))  # routes to _Uncategorized
                continue
            print(f'   • read {len(frames)} frame(s)', end='')
            transcript = transcribe_intro(path, wmodel) if wmodel else ''
            if transcript:
                print(' + intro transcript', end='')
            print(' → asking Gemini...')

            try:
                info = _gemini_classify(client, types, model, frames, transcript)
            except KeyboardInterrupt:
                raise
            except Exception as e:
                if inserts._is_invalid_key_error(e):
                    print('   ✗ Saved Gemini key is invalid/expired.')
                    inserts.clear_gemini_key()
                    key = inserts.get_gemini_key(force_prompt=True)
                    if not key:
                        print('✗ No new key provided — aborting.')
                        return
                    client = genai.Client(api_key=key)
                    try:
                        info = _gemini_classify(client, types, model, frames, transcript)
                    except Exception as e2:
                        print(f'   ✗ Gemini request failed again: {e2} — aborting.')
                        return
                else:
                    print(f'   ✗ Gemini request failed: {e} — leaving this file in place.')
                    analyses.append((name, normalize_info({})))
                    continue

            course = info.get('course') or info.get('category') or '?'
            title = info.get('title') or '?'
            print(f'   → {course} / {title}')
            analyses.append((name, info))
    except KeyboardInterrupt:
        print(f'\n\n⏹  Stopped — building a plan from the {len(analyses)} '
              f'video(s) processed so far (of {len(videos)}).')

    if not analyses:
        print('\nNothing was classified — exiting without changes.')
        return

    plan = build_plan(folder, analyses)
    print_plan(plan, folder)

    if not _confirm(assume_yes):
        print('Aborted — no files were changed.')
        return

    moved = apply_plan(plan)
    print('\n' + '=' * 70)
    print(f'✓ Done — organized {moved}/{len(plan)} video(s) into {folder}')
    print('=' * 70)
