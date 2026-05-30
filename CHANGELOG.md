# Changelog

All notable changes to this project are documented here.
The format is based on [Keep a Changelog](https://keepachangelog.com/).

## [1.0.0] - 2026-05-30

First public release. 🎉

### Added
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
- Full Polish installation & usage guide (HTML).

[1.0.0]: https://github.com/msikorski-sketch/ytsubtran/releases/tag/v1.0.0
