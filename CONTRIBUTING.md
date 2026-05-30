# Contributing

Thanks for your interest in improving this project! 🎉

## Reporting bugs

Open an issue and include:

- The exact command you ran
- The full output (especially any error or diagnosis message the script prints)
- Your OS, Python version (`python --version`), and whether you're on CPU or GPU
- For subtitle quality issues: the video's language and the model you used

## Suggesting features

Open an issue describing the use case. Good areas to explore:

- Alternative transcription backends (e.g. `faster-whisper` for speed + built-in VAD)
- Word-level alignment (WhisperX) for tighter subtitle timing
- Better translation (DeepL, or sentence-level context instead of per-segment)
- Speaker diarization

## Pull requests

1. Fork the repo and create a branch (`git checkout -b feature/my-change`).
2. Keep the style consistent with the existing code (clear comments, helpful console messages).
3. Test on at least one real download and one local file (`--file`).
4. Describe what you changed and why in the PR.

## Development notes

- The whole pipeline lives in a single file: [`youtube_downloader.py`](youtube_downloader.py).
- Downloading is independent from subtitle generation — you can test each separately.
- No API keys are required; translation uses the free Google Translate endpoint via `deep-translator`.

Be kind and constructive. 🙂
