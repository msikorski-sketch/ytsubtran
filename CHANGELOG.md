# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [Unreleased]

### Added
- **The clipboard-watch queue is now stored on disk** (`.ytsubtran_queue.json` in the
  output folder) instead of living only in memory. Closing the window, a crash or a
  reboot no longer loses the queued links: everything still pending — including the
  link that was being downloaded when it died — is resumed automatically at the next
  `--watch-clipboard` start, and the file can be opened (or edited) any time to see
  what is waiting. An entry is removed only once its download finishes, and the file
  is deleted when the queue empties.
- **Speed limit** (`--limit-rate 5M`) and **exponential retry backoff**: YouTube
  throttles long, fast download sessions, answering with HTTP 503 and broken
  transfers. Retries now wait 2 s, 4 s, 8 s … up to a minute instead of firing
  immediately (which burned all 10 attempts within seconds and killed the download),
  the retry budget went from 10 to 20 (plus 5 extractor retries), and `--limit-rate`
  keeps the transfer at a steady pace — often faster overall than 100 MB/s bursts
  followed by retry storms.
- **Clipboard watch mode** (`--watch-clipboard`, `--watch-interval`): leave it
  running and every YouTube link you copy downloads by itself, using the same options
  as a normal run (`--resolve`, `--subs`, `--output-dir`...). The clipboard is watched
  in a background thread and links go through a **queue**, so you can keep copying
  while a download is running instead of waiting for it to finish; each message shows
  how many links are still waiting, a failed download does not stop the queue, and
  Ctrl+C prints whatever was still queued. Copying a whole block of
  text queues every link found in it — handy for a list of posts copied out of
  Discord — and duplicate links inside one copy are collapsed. All link forms are
  recognized (`watch?v=`, `youtu.be`, `shorts`, `live`, `embed`). Whatever sits in the
  clipboard before the mode starts is deliberately ignored, so starting it never
  downloads something copied earlier for another purpose. Reads the clipboard through
  pyperclip when available, otherwise tkinter, otherwise the OS clipboard tool.
- **Duplicate detection** (on by default, `--force-download` to override): each
  output folder keeps a `.ytsubtran_downloads.json` index of finished downloads, and
  a repeated link is skipped with a printed reason (video id, file, size, date and
  what matched). Matching is by **YouTube video id**, not file name, and includes the
  format and the `--start/--end` range, so an MP3 never counts as an MP4 and a
  fragment is never mistaken for the full video. A skip only happens when the file is
  really on disk and complete — a deleted, moved, empty or half-downloaded (`.part`)
  file triggers a fresh download with an explanation, and a file the index does not
  know is accepted only after its real duration is verified against YouTube's
  (otherwise it is reported and left unrecorded rather than skipped by mistake).
  Skipping the download still runs subtitles / `--resolve` / insert detection on the
  existing file. Playlist links use yt-dlp's own download archive so items are
  skipped individually.
- Fragment downloads now carry their range in the file name (e.g.
  `Title [5-12s].mp4`), so two different fragments of one video no longer collide on
  the same name (previously yt-dlp skipped the second one as "already downloaded").

### Fixed
- **Clipboard watch mode now reacts to re-copying the same link.** Detection compared
  clipboard *text*, so copying a link that was already in the clipboard (the usual
  case right after starting the mode, which deliberately ignores what it finds there)
  looked like "nothing happened" and was silently missed. On Windows the clipboard
  change counter (`GetClipboardSequenceNumber`) is now used as well, which ticks on
  every copy even when the text is identical; other systems keep the text comparison.
  The clipboard is also read on every poll — that is what keeps the counter fresh —
  and a copy that momentarily reads back empty (another app still holding the
  clipboard) is retried instead of being dropped.
- **A throttled download no longer locks in a bad copy silently.** When YouTube
  blocks the good formats, the strategy chain can succeed on a fallback like format
  18 (360p) — and that file was then recorded as "downloaded", so every later run
  skipped the video and the full-quality version was never fetched. The resolution is
  now stored in the index, printed in the "already downloaded" message, and anything
  below 720p triggers an explicit warning with the `--force-download` hint, both right
  after such a download and on every later skip (entries saved before this change are
  filled in on the fly).
- **Skipping a duplicate no longer rewrites the file.** The "already downloaded" path
  ran the Resolve remux again on every run, rewriting a several-hundred-megabyte MP4
  for nothing (and changing the size recorded in the index). The remux now happens
  only for a freshly downloaded file; `--resolve` still re-checks the codecs, which
  costs one ffprobe call.
- **"New link downloads an old video"**: the downloaded-file detection no longer
  guesses from directory listings and modification times (which could point at an
  older file after playlist walks, re-downloads or interrupted runs) — yt-dlp now
  reports the exact path of every finished file (`--print-to-file
  after_move:filepath`), and the old detection remains only as a fallback.
- **Pure playlist links** (`youtube.com/playlist?list=...`): yt-dlp ignores
  `--no-playlist` for these and downloads every item. The script now says so up
  front and skips the single-file post-processing steps (subtitles / `--resolve` /
  inserts) instead of running them on an arbitrary file.
- `extract_url` now recognizes `youtu.be`, `shorts`, `live` and `embed` links and
  canonicalizes them to `watch?v=...`, so the `--no-playlist` protection applies
  to them too.
- Added `--remote-components ejs:github` to every yt-dlp call: with Deno installed,
  YouTube's JS challenges are solved by yt-dlp's official solver and the full
  format list is available (without it, the limited "android vr" client often
  offers only AV1 — which also defeated the `--resolve` preset's native-H.264
  preference).

### Added
- **Persistent default output folder** (`--set-output-dir DIR`): saved in
  `~/.ytsubtran.json` and used automatically whenever `--output-dir` is not given
  (a one-off `--output-dir` still wins; the interactive prompt only appears when
  no default is saved). Can be run standalone (no URL) just to set it. Saving the
  Gemini API key and saving settings now merge into the shared config file instead
  of overwriting each other.
- **DaVinci Resolve preset** (`--resolve`): guarantees media that edits smoothly in
  Resolve, including the free version on Windows. Prefers native H.264 (avc1) + AAC
  when downloading (no transcode, no quality loss), then ffprobe-verifies the final
  file and normalizes only what breaks editing: VP9/AV1/HEVC → H.264 8-bit 4:2:0,
  Opus/Vorbis → AAC, variable frame rate → CFR; 10-bit / 4:2:2 sources become
  DNxHR HQX in MOV with PCM audio (free Resolve decodes H.264 only in 8-bit —
  DNxHR keeps full bit depth). Decisions follow the official Resolve 20 supported
  codec list. Also works on local files (`--file ... --resolve` writes a sibling
  `*_resolve` file and leaves the original untouched). Complements the automatic
  clean-container remux that already ran for every MP4.
- **Gemini translation engine** (`--translate-engine gemini`, `--translate-model`):
  translates the whole transcript in one context-aware LLM request instead of
  segment-by-segment Google Translate — consistent terminology and natural grammar
  across segments (biggest gain for ja/zh/ko sources). Long transcripts go in
  batches of 300 segments with the previous batch's tail passed as consistency
  context; a strict 1:1 JSON contract is validated and any failure (no SDK, no
  key, bad response) falls back to Google Translate. Reuses the `--smart-inserts`/
  `--organize` API-key handling. Google Translate remains the default.
- **Qwen3-ASR engine** (`--engine qwen|hybrid|auto|whisper`, `--qwen-model`): optional
  higher-accuracy transcription with Alibaba's Qwen3-ASR-1.7B (`pip install qwen-asr`,
  CUDA GPU recommended). `qwen` uses ForcedAligner timestamps (11 languages, incl.
  Japanese — not Polish); for the other Qwen languages it automatically switches to
  the new `hybrid` mode: Whisper provides segment timing, Qwen re-transcribes each
  segment for better text, and an agreement report cross-checks both engines
  (strongly differing segments are printed with both versions). `auto` (new default)
  prefers Qwen when installed and a GPU is present, and quietly behaves as before
  otherwise; any Qwen failure falls back to Whisper instead of aborting. `--prompt`
  context biasing works with both engines. Implemented in the new `qwen_engine.py`
  module; subtitle saving/translation/burn/embed is now shared by all engines.
- **Catalog mode** (`--organize DIR`): tidy a folder of generically-named lecture
  videos (e.g. DataCamp's `video (1).mp4`). It reads a few frames from each video
  (the slides usually print the course + chapter title), asks Gemini what it is, then
  renames each file to its lesson title and sorts it into a per-course sub-folder.
  Previews the full plan and asks before moving anything (`--yes` to skip);
  unidentifiable files are left in `_Uncategorized\`; matching `.srt`/`.vtt`/`.txt`
  side-cars follow their video. Classifies from frames only by default (fast, no
  Whisper); add `--organize-transcript` to also feed Whisper's intro transcript.
  Press Ctrl+C to stop scanning early and still get a plan for what was processed.
  Tunable via `--organize-model` and `--organize-frames`. Reuses the same Gemini key
  handling as `--smart-inserts` and uploads only a few frames per video (not the
  whole file), so it stays cheap.

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
