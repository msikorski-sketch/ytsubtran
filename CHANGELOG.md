# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [1.0.0] - 2026-05-30

First public release. 🎉

### Fixed
- Diagnose the missing-JavaScript-runtime case ("This video is not available" on
  watchable videos): the script now points to installing Deno instead of pointlessly
  re-updating yt-dlp.

### Added
- **DaVinci Resolve marker script** (`resolve_markers.py`): adds a coloured timeline
  marker at every detected insert (run from Workspace → Scripts). Auto-matches the
  cut list to the open timeline's video; if it doesn't match, shows a picker of the
  lists found in that folder and warns about the mismatch.
- **Download from YouTube** as MP4 or MP3 with resilient, self-healing logic:
  auto-installs/updates `yt-dlp`, tries multiple format strategies, resumes
  interrupted transfers, bypasses geo-blocks, and diagnoses failures.
- **Local file mode** (`--file`) — generate subtitles for a file already on disk,
  without downloading.
- **Subtitle generation** with OpenAI Whisper, saved as `.srt` and `.txt`.
- **Translation** of subtitles into any language (`--translate-to`) via Google
  Translate; both the original and translated versions are saved.
- **Robust language detection** (`--source-lang auto`) that samples several points
  in the audio, so an intro in another language doesn't fool it.
- **Any Whisper model** via `--model` (incl. `large-v3`, `turbo`); future models
  work without code changes.
- **Context prompt** (`--prompt`) to improve accuracy of names and terminology.
- **GPU acceleration** — automatic CUDA detection with `fp16`, plus graceful
  fallback to CPU if VRAM runs out.
- Anti-hallucination decoding parameters to prevent repetition loops.
- **`--cookies-from-browser`** — pass browser cookies to yt-dlp for age-restricted
  or "confirm you're not a bot" videos.
- **`--output-dir`** — choose where downloaded media and subtitle files are written.
  When omitted, the script now **asks interactively** where to save (Enter = current
  folder); in non-interactive contexts it silently uses the current folder.
- **Installable package** (`pyproject.toml`) exposing a `ytsubtran` console command.
- **Tests** (`pytest`) for the pure helper functions and **CI** (GitHub Actions:
  `ruff` lint + `pytest` on Python 3.9 and 3.12).
- **Parallel translation** — subtitle segments are translated concurrently (thread
  pool), greatly speeding up long videos while keeping exact 1:1 timing.
- **`--vtt`** — additionally export subtitles in WebVTT format (for web players).
- **`--burn`** — hardcode subtitles into the video picture (via ffmpeg `subtitles` filter).
- **`--embed`** — mux subtitles as a soft, toggleable track into the MP4 (no re-encode).
- **`--find-inserts` / `--cut-inserts`** — detect (and optionally remove) short inserted
  clips / interstitials via a cascade: SponsorBlock → audio-jump + scene-cut heuristic →
  optional local-Ollama AI cross-check (`--insert-ai`). Analysis-only by default; cutting
  writes a new `*_nocuts.mp4` and never touches the original. (New module `inserts.py`.)
- **`--smart-inserts`** — detect inserts with the Gemini multimodal API (best for visual
  cutaways with no audio signature). Prompts for an API key on first use and saves it to
  `~/.ytsubtran.json` (or `GEMINI_API_KEY`). Requires `pip install google-genai`.
  Gemini classifies each hit as `clip` / `screenshot` / `caption`; `--insert-kinds`
  controls which are kept (default `clip` only — excludes on-screen screenshots and
  editor captions). With `--extract-inserts`, the type label is included in each clip's
  filename (e.g. `03_02m11s_clip_Animated intro.mp4`).
- **`--extract-inserts`** — save each detected insert as its own descriptively-named clip
  (`NN_MMmSSs_<reason>.mp4`) in a `<video>_clips` folder, ready to reuse in your own edits.
  Non-destructive: the original is never modified. Re-encodes for frame-accurate boundaries
  by default, or `--clips-copy` for instant keyframe-aligned stream copies.
- **`--from-list FILE`** — extract or cut from a previously saved/edited cut list
  (`<video>_inserts.txt`) instead of re-detecting, so a paid Gemini analysis runs only once.
  The list is plain text — delete lines to drop segments before re-running.
- **`--snap-cuts`** — snap insert boundaries to the nearest detected scene cut for
  frame-accurate trims (refines the model's ±1 s timestamps).
- Non-ASCII filenames (e.g. Polish characters) no longer crash the Gemini upload, and an
  expired/invalid saved API key is detected and re-prompted automatically.
- Insert timestamps are now validated against the real video length before extracting or
  cutting: segments past the end of the video (Gemini timing drift) are dropped with a
  warning instead of producing empty 0-byte "clips", and header-only clip files are
  cleaned up automatically.
- Early **ffmpeg check** before downloading, with a clear warning if it's missing.
- Full Polish installation & usage guide (HTML) and an English technical guide
  (`docs/HOW_IT_WORKS.md`).

[1.0.0]: https://github.com/msikorski-sketch/ytsubtran/releases/tag/v1.0.0
