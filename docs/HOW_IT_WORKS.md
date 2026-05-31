# How It Works — A Build-It-Yourself Technical Guide

This document explains **every concept and design decision** behind the
YouTube Downloader + AI Subtitles script. It is written so that, starting from
nothing, you could re-implement the whole tool yourself and understand *why*
each part works the way it does.

The entire program lives in a single file, [`youtube_downloader.py`](../youtube_downloader.py).
Keeping it in one file is a deliberate choice: it makes the tool trivial to copy,
share, and run without packaging. This guide follows the natural order in which
you would build it.

---

## Table of contents

1. [The big picture](#1-the-big-picture)
2. [Pipeline overview & data flow](#2-pipeline-overview--data-flow)
3. [Console encoding on Windows (UTF-8)](#3-console-encoding-on-windows-utf-8)
4. [Parsing the input URL](#4-parsing-the-input-url)
5. [The download engine](#5-the-download-engine)
   - 5.1 [Why shell out to yt-dlp](#51-why-shell-out-to-yt-dlp)
   - 5.2 [Ensuring yt-dlp exists (and self-updating)](#52-ensuring-yt-dlp-exists-and-self-updating)
   - 5.3 [Robustness flags](#53-robustness-flags)
   - 5.4 [Format strategies and fallbacks](#54-format-strategies-and-fallbacks)
   - 5.5 [Streaming output live while capturing it](#55-streaming-output-live-while-capturing-it)
   - 5.6 [Detecting the downloaded file](#56-detecting-the-downloaded-file)
   - 5.7 [Diagnosing failures](#57-diagnosing-failures)
   - 5.8 [Auto-update-and-retry](#58-auto-update-and-retry)
6. [Speech-to-text with Whisper](#6-speech-to-text-with-whisper)
   - 6.1 [What Whisper is](#61-what-whisper-is)
   - 6.2 [Choosing CPU vs GPU](#62-choosing-cpu-vs-gpu)
   - 6.3 [Loading a model (and accepting any model name)](#63-loading-a-model-and-accepting-any-model-name)
   - 6.4 [Robust language detection](#64-robust-language-detection)
   - 6.5 [Transcription parameters that fight hallucinations](#65-transcription-parameters-that-fight-hallucinations)
   - 6.6 [The context prompt](#66-the-context-prompt)
7. [Subtitles: the SRT format](#7-subtitles-the-srt-format)
8. [Translation](#8-translation)
9. [Saving outputs](#9-saving-outputs)
10. [The command-line interface](#10-the-command-line-interface)
11. [Error-handling philosophy](#11-error-handling-philosophy)
12. [Building it from scratch — suggested order](#12-building-it-from-scratch--suggested-order)
13. [Alternative and more advanced approaches](#13-alternative-and-more-advanced-approaches)

---

## 1. The big picture

The tool does three conceptually separate jobs, glued together:

1. **Acquire media** — either download a YouTube video/audio, or use a local file.
2. **Transcribe** — turn the spoken audio into timed text using an AI model.
3. **Translate (optional)** — render that text into another language.

The key engineering insight is that these three jobs are **independent**. You can
test and reason about each one alone. The download step does not need to know
anything about subtitles; the transcription step only needs a media file path; the
translation step only needs a list of text segments. This separation is what makes
the program understandable despite doing a lot.

Two external command-line programs do the heavy lifting and we orchestrate them:

- **`yt-dlp`** — the de-facto standard YouTube downloader (a maintained fork of
  youtube-dl). It handles the constantly-changing YouTube internals.
- **`ffmpeg`** — audio/video processing. Whisper uses it under the hood to decode
  audio, and yt-dlp uses it to merge/convert formats.

And two Python libraries:

- **`openai-whisper`** — the speech-recognition model (pulls in PyTorch).
- **`deep-translator`** — a thin wrapper over free translation backends (we use
  Google Translate).

---

## 2. Pipeline overview & data flow

```
                 ┌───────────────────────── input ─────────────────────────┐
                 │   YouTube URL                         local file (--file) │
                 └───────────────┬──────────────────────────────┬───────────┘
                                 │                                │
                    download_youtube()                           │
                                 │                                │
             ┌───────────────────▼───────────────────┐           │
             │  ensure_ytdlp()  → install/update      │           │
             │  attempt_all_strategies()              │           │
             │     └─ try_download() ×N (fallbacks)   │           │
             │           └─ run_ytdlp() (live output) │           │
             │  detect_downloaded_file()              │           │
             │  on failure: diagnose_failure()        │           │
             │              + update & retry          │           │
             └───────────────────┬────────────────────┘          │
                                 │ media file path                │ media file path
                                 └───────────────┬────────────────┘
                                                 │
                         generate_subtitles_with_whisper()
                                                 │
                 ┌───────────────────────────────▼────────────────────────────┐
                 │  pick_device()           → cuda or cpu                       │
                 │  whisper.load_model()                                        │
                 │  detect_language_robust()  (if --source-lang auto)           │
                 │  model.transcribe()      → segments [{start,end,text}, …]    │
                 │  write_srt()  + .txt     → original-language files           │
                 │  translate_texts()       → translated segments (optional)    │
                 │  write_srt()  + .txt     → translated files (optional)       │
                 └─────────────────────────────────────────────────────────────┘
```

The unit of data that flows through transcription and translation is the
**segment**: a dict with a `start` time, an `end` time (both in seconds, as
floats), and a `text` string. Whisper produces a list of these; everything
downstream just transforms that list.

---

## 3. Console encoding on Windows (UTF-8)

The very first thing the script does after imports:

```python
import sys
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass
```

**Why this matters.** On Windows, the default console encoding is often a legacy
code page (e.g. `cp1252`) that *cannot represent* Polish letters like `ł` or emoji.
Printing them raises `UnicodeEncodeError` and crashes the program — even on
something as innocent as `--help`. `reconfigure(encoding='utf-8')` (Python 3.7+)
forces UTF-8 output. The `try/except` keeps it harmless on platforms/streams where
`reconfigure` is unavailable.

**Lesson:** if your program prints non-ASCII text, set the output encoding
explicitly. Do not rely on the OS default.

---

## 4. Parsing the input URL

```python
def extract_url(text):
    clean_text = re.sub(r'[\[\]\(\)]', '', text).strip()
    pattern = r'(https?://(?:www\.)?youtube\.com/watch\?v=[0-9A-Za-z_-]{11})'
    match = re.search(pattern, clean_text)
    return match.group(1) if match else clean_text
```

Users paste messy strings: surrounded by brackets, with extra query parameters
(`&list=…&t=…`), or copied from chat apps. This function:

1. Strips brackets/parentheses that often wrap pasted links.
2. Looks for a canonical `watch?v=<11-char-id>` URL. YouTube video IDs are always
   11 characters from the set `[0-9A-Za-z_-]`, so the regex is precise.
3. Falls back to returning the cleaned text as-is, so other URL shapes still reach
   yt-dlp (which understands far more than we parse).

**Design note:** we don't try to validate *everything* — yt-dlp is the real
authority on what's a valid URL. We just clean up the most common paste problems.

---

## 5. The download engine

This is the most "defensive" part of the program. YouTube actively changes its
internals, so a downloader must be resilient.

### 5.1 Why shell out to yt-dlp

yt-dlp can be imported as a Python library, but we run it as a **subprocess**
(`python -m yt_dlp …`). Reasons:

- We always use the user's installed yt-dlp, which can be updated independently
  (crucial — see §5.8).
- The CLI is a stable, well-documented contract.
- We get its excellent progress output for free.

### 5.2 Ensuring yt-dlp exists (and self-updating)

```python
def ensure_ytdlp():
    check = subprocess.run([sys.executable, '-m', 'yt_dlp', '--version'],
                           capture_output=True, text=True)
    if check.returncode == 0:
        return True
    return update_ytdlp()          # not installed → install it

def update_ytdlp():
    subprocess.run([sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp'])
    # …then verify --version again
```

`sys.executable` is the path to the *current* Python interpreter. Using it (instead
of a bare `python`/`pip`) guarantees we install into the same environment we're
running in — avoiding the classic "installed it but it's not found" problem with
multiple Pythons / virtualenvs.

### 5.3 Robustness flags

Every yt-dlp invocation includes a shared set of flags:

```python
COMMON_YTDLP_FLAGS = [
    '--no-playlist',            # a URL with &list= must not pull the whole playlist
    '--retries', '10',          # retry on transient network errors
    '--fragment-retries', '10', # retry individual stream fragments
    '--socket-timeout', '30',   # don't hang forever
    '--continue',               # resume partially-downloaded .part files
    '--geo-bypass',             # attempt to bypass region locks
    '--newline',                # progress on separate lines (clean logs)
    '--ignore-errors',
]
```

Each flag maps to a real-world failure mode. `--no-playlist` in particular saves
beginners from accidentally downloading 200 videos because their URL contained a
playlist id.

### 5.4 Format strategies and fallbacks

YouTube serves video and audio as separate streams in many formats, and which ones
are available varies per video. Instead of betting on one format string, we keep an
**ordered list of strategies** and try them until one works:

```python
strategies = [
    ('bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
     'Best MP4 with separate audio', ['--merge-output-format', 'mp4']),
    ('bestvideo+bestaudio/best', 'Best quality, any codec', ['--merge-output-format', 'mp4']),
    ('22', 'Format 22 (720p MP4)', None),
    ('18', 'Format 18 (360p MP4)', None),
    ('best[ext=mp4]', 'Best available MP4', None),
    ('best', 'Last-resort fallback', ['--merge-output-format', 'mp4', '--recode-video', 'mp4']),
]
```

The first entries aim for the **highest quality** (separate video+audio streams
merged together); later entries are increasingly forgiving, ending with a
"give me anything and re-encode it to MP4" fallback. MP3 mode has its own analogous
list using `-x` (extract audio) and `--audio-format mp3`.

Each tuple is `(format_spec, human_label, extra_args)`. `attempt_all_strategies()`
loops through them and stops at the first success.

### 5.5 Streaming output live while capturing it

A naive `subprocess.run(..., capture_output=True)` hides yt-dlp's progress bar for
minutes, then dumps everything at the end. But if we *don't* capture, we can't
analyze errors afterwards. The solution is to stream line-by-line and tee into a
buffer:

```python
def run_ytdlp(command):
    process = subprocess.Popen(command, stdout=subprocess.PIPE,
                               stderr=subprocess.STDOUT, text=True,
                               encoding='utf-8', errors='replace', bufsize=1)
    collected = []
    for line in process.stdout:
        sys.stdout.write(line)   # show the user live progress
        sys.stdout.flush()
        collected.append(line)   # keep it for later analysis
    process.wait()
    return process.returncode, ''.join(collected)
```

Key details:
- `stderr=subprocess.STDOUT` merges both streams so ordering is preserved.
- `errors='replace'` prevents a stray byte from crashing the reader.
- We return both the **exit code** and the **full text** — the exit code tells us
  success/failure, the text powers diagnosis.

### 5.6 Detecting the downloaded file

How do you know *which* file yt-dlp produced? Parsing its log lines is fragile
(messages change between versions, merging renames files). A far more robust trick
is a **before/after directory diff**:

```python
def detect_downloaded_file(before_files, output_dir, format_choice):
    after_files = set(os.listdir(output_dir))
    exts = MEDIA_EXTENSIONS[format_choice]      # e.g. ('.mp4', '.mkv', …)
    new_files = [f for f in (after_files - before_files) if f.lower().endswith(exts)]
    pool = new_files or [f for f in after_files if f.lower().endswith(exts)]
    pool.sort(key=lambda f: os.path.getmtime(os.path.join(output_dir, f)), reverse=True)
    return pool[0] if pool else None
```

We snapshot the directory's filenames *before* downloading, then see what new media
file appeared *after*. This is independent of yt-dlp's output format and correctly
ignores intermediate files that get deleted during merging. If nothing is "new"
(e.g. the file already existed), we fall back to the most recently modified media
file.

### 5.7 Diagnosing failures

When all strategies fail, we don't print a generic error — we inspect the captured
output for known signatures and produce an actionable diagnosis:

```python
def diagnose_failure(output):
    low = output.lower()
    if any(s in low for s in ['unable to extract', 'nsig', 'http error 403', …]):
        return {'message': 'YouTube likely changed something; updating yt-dlp usually fixes it.',
                'suggest_update': True}
    if 'private video' in low:        return {'message': 'Video is private.', 'suggest_update': False}
    if 'http error 429' in low:       return {'message': 'Rate-limited; wait a few minutes.', 'suggest_update': False}
    # …age restriction, geo-block, DRM, network, …
    return {'message': 'Unknown error.', 'suggest_update': True}
```

The crucial output field is `suggest_update`: a boolean saying "is this the kind of
error that updating yt-dlp tends to fix?". That drives the next step.

### 5.8 Auto-update-and-retry

The single most common reason a YouTube downloader breaks is that **YouTube changed
something and the local yt-dlp is now outdated**. So when diagnosis says
`suggest_update`, the program updates yt-dlp and retries the whole strategy list
once:

```python
if not success:
    diag = diagnose_failure(output)
    if diag['suggest_update'] and update_ytdlp():
        success, downloaded_file, output = attempt_all_strategies(...)
```

This one behavior resolves the majority of real-world failures automatically.

---

## 6. Speech-to-text with Whisper

### 6.1 What Whisper is

Whisper is an open-source neural model from OpenAI that converts speech to text. It
runs **locally** (no API, no internet, free), supports ~100 languages, and is
robust to accents and background noise. You call it like:

```python
import whisper
model = whisper.load_model('turbo')
result = model.transcribe('audio_or_video_file')
# result['segments'] → list of {start, end, text}
# result['text']     → the full transcript as one string
# result['language'] → detected language code
```

Whisper internally uses ffmpeg to decode the audio, which is why ffmpeg is a hard
requirement.

### 6.2 Choosing CPU vs GPU

Whisper is a deep-learning model; it runs **much** faster on an NVIDIA GPU via CUDA.
We detect the GPU and report it:

```python
def pick_device():
    import torch
    if torch.cuda.is_available():
        name = torch.cuda.get_device_name(0)
        total = torch.cuda.get_device_properties(0).total_memory / (1024**3)
        return 'cuda', f'GPU: {name} ({total:.1f} GB VRAM)'
    return 'cpu', 'CPU (no GPU available)'
```

Two important practical points:

- **`fp16` (half precision)** is enabled only on GPU (`fp16=(device=='cuda')`).
  It roughly halves memory use and speeds things up, but isn't supported on CPU.
- **VRAM limits.** The `large-v3` model needs ~10 GB of VRAM; on an 8 GB card it
  may not fit. We catch CUDA "out of memory" errors and **fall back to CPU**
  automatically so the job still completes:

```python
try:
    result = model.transcribe(video_file, **kwargs)
except RuntimeError as e:
    if device == 'cuda' and 'out of memory' in str(e).lower():
        torch.cuda.empty_cache()
        model = whisper.load_model(model_size, device='cpu')
        kwargs['fp16'] = False
        result = model.transcribe(video_file, **kwargs)
    else:
        raise
```

> **Installing the GPU build.** By default `pip install openai-whisper` pulls in a
> CPU-only PyTorch. To use a GPU you must reinstall the CUDA build, e.g.
> `pip install torch --index-url https://download.pytorch.org/whl/cu126`, matching
> the `cu1XX` number to your driver (`nvidia-smi`).

### 6.3 Loading a model (and accepting any model name)

Whisper ships several model sizes: `tiny`, `base`, `small`, `medium`, `large`,
`large-v3`, `turbo`, … Bigger = more accurate but slower and heavier.

We deliberately do **not** restrict `--model` to a fixed list. Whichever name the
user passes is forwarded straight to `whisper.load_model()`. This future-proofs the
tool: when OpenAI releases a new model, it works after a `pip install -U
openai-whisper` with no code change. We just handle the "unknown name" error
gracefully by printing the available models.

### 6.4 Robust language detection

This is a subtle but important correctness fix. By default Whisper detects the
language from only the **first 30 seconds** of audio. If a video has, say, an
English intro but Portuguese dialogue, Whisper locks onto English and then
*transcribes Portuguese speech as if it were English* — producing phonetic garbage
and repetition loops.

The fix is to **sample several points** across the audio and vote:

```python
def detect_language_robust(model, audio_path, fractions=(0.15, 0.4, 0.65)):
    audio = whisper.load_audio(audio_path)            # 16 kHz mono float array
    window = whisper.audio.N_SAMPLES                  # 30 s worth of samples
    positions = [min(int(f*len(audio)), len(audio)-window) for f in fractions]
    scores = {}
    for pos in positions:
        segment = whisper.pad_or_trim(audio[pos:pos+window])
        mel = whisper.log_mel_spectrogram(segment, n_mels=model.dims.n_mels).to(model.device)
        _, probs = model.detect_language(mel)         # {lang: probability}
        for lang, p in probs.items():
            scores[lang] = scores.get(lang, 0.0) + p
    best = max(scores, key=scores.get)
    return best, scores[best] / len(positions)
```

We take three 30-second windows at 15%, 40%, and 65% through the file (skipping the
intro), run Whisper's `detect_language` on each, sum the probabilities, and pick the
winner. This single change turns "garbled English" into a confident, correct
language. The pipeline: raw audio → 30 s window → log-mel spectrogram → the model's
language classifier.

**Takeaway:** auto-detection is convenient, but explicitly passing the known
language (`--source-lang pt`) is always the most reliable, because it skips
detection entirely.

### 6.5 Transcription parameters that fight hallucinations

Whisper can "hallucinate" — invent text or loop the same phrase — especially on
music or silence. We pass several parameters to suppress this:

```python
result = model.transcribe(
    video_file,
    language=whisper_lang,             # fixed (detected or user-specified)
    task='transcribe',                 # NOT 'translate' (see below)
    fp16=(device == 'cuda'),
    initial_prompt=initial_prompt,
    condition_on_previous_text=False,  # *the* key anti-loop setting
    beam_size=5,                       # better search than greedy decoding
    temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),  # fallback if a segment looks bad
    compression_ratio_threshold=2.4,   # reject segments that look like gibberish
    logprob_threshold=-1.0,            # reject low-confidence segments
    no_speech_threshold=0.6,           # detect music/silence and emit nothing
)
```

- **`condition_on_previous_text=False`** is the most important. Normally Whisper
  feeds each segment's output as context for the next; on a bad segment this creates
  feedback loops ("I'm sorry. I'm sorry. I'm sorry…"). Disabling it breaks the loop.
- **`temperature` tuple** tells Whisper to retry a segment at higher randomness if
  the low-temperature attempt fails the quality thresholds.
- **`compression_ratio_threshold` / `logprob_threshold` / `no_speech_threshold`**
  are quality gates that drop nonsense and silence instead of inventing words.

> **`transcribe` vs `translate`.** Whisper has a built-in `task='translate'`, but it
> *only translates to English*. To translate into any other language (e.g. Polish)
> we always `transcribe` in the original language and do translation as a separate
> step (§8).

### 6.6 The context prompt

`initial_prompt` lets you prime Whisper with vocabulary and spelling — character
names, jargon, brand names. Given `--prompt "Pomni, Ragatha, Jax, Caine"`, Whisper
is far more likely to spell those correctly instead of inventing "Pomi" or
"Genglee". Best written in the spoken language, since it biases the transcription.

---

## 7. Subtitles: the SRT format

SRT (SubRip) is the most widely supported subtitle format. It's plain text, one
"cue" per block:

```
1
00:00:07,000 --> 00:00:10,000
Why can't I cry for him?

2
00:00:12,000 --> 00:00:18,000
I think we'd better come up with a plan.
```

A block is: an index, a `start --> end` timestamp line, the text, and a blank line.
Timestamps are `HH:MM:SS,mmm` (note the **comma** before milliseconds). Converting
Whisper's float seconds to that format:

```python
def format_timestamp_srt(seconds):
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}'
```

Writing the file just iterates segments. We make the writer reusable so the same
function serves both the original and the translated subtitles — the only
difference is which text array it uses:

```python
def write_srt(srt_file, segments, texts=None):
    with open(srt_file, 'w', encoding='utf-8') as f:
        for i, segment in enumerate(segments, 1):
            start = format_timestamp_srt(segment['start'])
            end = format_timestamp_srt(segment['end'])
            text = (texts[i-1] if texts is not None else segment['text']).strip()
            f.write(f'{i}\n{start} --> {end}\n{text}\n\n')
```

Crucially, the **timestamps stay identical** between languages — we only swap the
text — so translated subtitles remain perfectly in sync.

---

## 8. Translation

Whisper gives us text in the original language. For any target other than English,
we translate the segment texts ourselves:

```python
def translate_texts(texts, target_lang, source_lang='auto'):
    from deep_translator import GoogleTranslator
    translator = GoogleTranslator(source=source_lang or 'auto', target=target_lang)
    translated = []
    for text in texts:
        original = text.strip()
        try:
            translated.append(translator.translate(original) or original)
        except Exception:
            translated.append(original)   # on failure keep the original, don't crash
    return translated
```

Design decisions:

- **`deep-translator`** wraps Google Translate's free endpoint — no API key, good
  Polish quality, needs internet.
- We pass the **known source language** (from detection or `--source-lang`) rather
  than `'auto'`, which is more reliable than letting Google guess again.
- We translate **segment by segment** to keep the 1:1 mapping with timestamps. This
  is simple and robust; the trade-off is some loss of cross-sentence context (see
  §13 for how to improve it).
- If a single segment fails to translate, we keep the original text for that line
  instead of aborting the whole job.

---

## 9. Saving outputs

Files are written next to the source, suffixed with the language code so they never
collide. When translating, **both** the original and the translation are kept:

```
episode.mp4
episode_PT.srt   episode_PT.txt     # original transcription
episode_PL.srt   episode_PL.txt     # translated subtitles (only if --translate-to)
```

Keeping the original is valuable: if the translation has a glitch you can always
check against the source. `.srt` is for video players; `.txt` is the full text in
one blob, handy for reading or feeding into other tools.

---

## 10. The command-line interface

All argument parsing lives in a `main()` function (so it can be exposed as a console
command — see §10.1). `argparse` defines the user contract:

```python
def main():
    parser = argparse.ArgumentParser(...)
    parser.add_argument('url', nargs='?')          # optional positional
    parser.add_argument('--file')                  # local file → skip download
    parser.add_argument('--format', choices=['mp4', 'mp3'], default='mp4')
    parser.add_argument('--subs', action='store_true')
    parser.add_argument('--source-lang', default='pl')        # or 'auto'
    parser.add_argument('--translate-to', default=None)
    parser.add_argument('--model', default='base')            # no choices → any model name
    parser.add_argument('--prompt', default=None)
    parser.add_argument('--cookies-from-browser', default=None)  # chrome / firefox / edge …
    parser.add_argument('--output-dir', default=None)            # where results go
    args = parser.parse_args()
    ...
```

Dispatch logic chooses the mode:

```python
    if args.file:
        generate_subtitles_with_whisper(args.file, args.model, args.source_lang,
                                        args.translate_to, args.prompt, args.output_dir)
    elif args.url:
        download_youtube(args.url, args.format, args.subs, args.model,
                         args.source_lang, args.translate_to, args.prompt,
                         args.cookies_from_browser, args.output_dir)
    else:
        parser.error('Provide a YouTube link or use --file …')


if __name__ == '__main__':
    main()
```

Making `url` optional (`nargs='?'`) is what lets `--file` work without a URL. The
explicit `parser.error()` gives a friendly message when the user supplies neither.

Two operational flags worth highlighting:

- **`--cookies-from-browser BROWSER`** is forwarded straight to yt-dlp. It reads your
  logged-in browser cookies, which lets you download age-restricted videos and pass
  the "confirm you're not a bot" check — the most common downloads our diagnosis
  flags as needing a login.
- **`--output-dir DIR`** redirects where files land. For downloads we build the
  yt-dlp output template as `os.path.join(output_dir, '%(title)s.%(ext)s')`; for
  subtitles we compute the base name inside that directory. The directory is created
  with `os.makedirs(..., exist_ok=True)` if missing.

### 10.1 Packaging and the `ytsubtran` command

The project ships a `pyproject.toml` so it can be installed with `pip install .`.
The key part is the **console entry point**:

```toml
[project.scripts]
ytsubtran = "youtube_downloader:main"

[tool.setuptools]
py-modules = ["youtube_downloader"]
```

This tells pip to generate a small launcher named `ytsubtran` that calls our
`main()` function — which is exactly why `main()` exists as a function instead of
living inside the `if __name__ == '__main__'` block. After installation, users run
`ytsubtran "URL" --subs` instead of `python youtube_downloader.py …`. Because it's a
single-module project, `py-modules` (not `packages`) points at the one `.py` file.

### 10.2 Tests and continuous integration

The pure, side-effect-free helpers (`extract_url`, `format_timestamp_srt`,
`diagnose_failure`, `lang_name`) are covered by `pytest` tests in `tests/`. They
import `youtube_downloader` directly, which is cheap because the heavy libraries
(whisper, torch, yt-dlp) are imported **lazily inside functions**, not at module
top level — so the tests need only the standard library.

A GitHub Actions workflow (`.github/workflows/ci.yml`) runs `ruff` (lint) and
`pytest` on every push and pull request, across Python 3.9 and 3.12. Because the
tests don't need the heavy deps, CI installs only `pytest` and `ruff` and stays fast.
This is a deliberate benefit of the lazy-import structure.

---

## 11. Error-handling philosophy

A few principles run through the whole program:

- **Fail loudly but helpfully.** Every failure path prints *why* and *what to try*,
  not just a stack trace. Diagnosis messages map error signatures to plain-language
  advice.
- **Degrade gracefully.** Out of VRAM? Fall back to CPU. A segment won't translate?
  Keep the original. yt-dlp outdated? Update and retry.
- **Check prerequisites early.** Before transcribing we verify Whisper, ffmpeg, and
  (if translating) deep-translator are present, and print install instructions if
  not — so the user learns the problem in one second, not after a long download.
- **Use the right interpreter.** Always `sys.executable -m pip/yt_dlp` so installs
  and tools land in the active environment.

---

## 12. Building it from scratch — suggested order

If you wanted to recreate this yourself, build and test in this order. Each step is
independently runnable, so you always have something working:

1. **`format_timestamp_srt` + `write_srt`.** Pure functions, no dependencies. Unit-
   test them with a hand-made segment list.
2. **A minimal download.** Shell out to `yt-dlp -f best -o '%(title)s.%(ext)s' URL`
   and confirm a file appears. No fallbacks yet.
3. **`run_ytdlp` streaming wrapper** and **`detect_downloaded_file`.** Now you know
   what you downloaded and can see progress.
4. **Strategy list + `attempt_all_strategies`.** Add the quality→fallback ladder.
5. **`ensure_ytdlp` / `update_ytdlp` / `diagnose_failure`** and the auto-retry. Now
   downloading is robust.
6. **Basic Whisper transcription.** `load_model('base')` → `transcribe()` →
   `write_srt()`. You now produce subtitles.
7. **`pick_device` + fp16 + OOM fallback.** Add GPU support.
8. **`detect_language_robust`.** Fix the intro-language problem.
9. **Anti-hallucination parameters** and **`--prompt`.** Raise quality.
10. **`translate_texts`** and the second set of output files. Add translation.
11. **`argparse` CLI** tying it all together, plus the `--file` mode.
12. **UTF-8 console fix.** Do this early in practice; it's listed late only because
    it's a one-liner.

---

## 13. Alternative and more advanced approaches

The current design favors simplicity and zero API keys. If you want to push further:

- **faster-whisper** (CTranslate2 backend): same models, ~4× faster, lower VRAM, and
  a built-in **VAD** (voice-activity detector) that skips music/silence — which
  reduces hallucinations more cleanly than threshold tuning. A near drop-in upgrade
  for the transcription step.
- **WhisperX**: adds **forced alignment** for word-level timestamps (tighter
  subtitle timing) and optional **speaker diarization** ("who said what").
- **Better translation**: feed whole sentences (merge adjacent segments on
  punctuation) rather than per-segment, so the translator has context; or swap in
  **DeepL** (often higher quality for some language pairs) or an LLM with the full
  scene as context.
- **Subtitle styling / burn-in**: use ffmpeg to hard-burn subtitles into the video,
  or mux them as a soft track into an `.mp4`/`.mkv` container.
- **Batch mode**: accept multiple URLs or a folder of files and loop.
- **`.vtt` output**: emit WebVTT (almost identical to SRT, uses `.` for the
  millisecond separator and a `WEBVTT` header) for web players.

Each of these is an isolated change to one stage of the pipeline — which is exactly
the benefit of keeping the three jobs independent.

---

*This document describes the implementation in
[`youtube_downloader.py`](../youtube_downloader.py). If you change the code, update
this guide too.*
