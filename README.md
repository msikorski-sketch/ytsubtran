# 🎬 YouTube Downloader + AI Subtitles

> Download any YouTube video (or use a local file), generate subtitles with **OpenAI Whisper**, and **translate them into any language** — fully automated, GPU-accelerated, and resilient to errors.

<p align="center">
  <a href="https://github.com/msikorski-sketch/ytsubtran/actions/workflows/ci.yml"><img alt="CI" src="https://github.com/msikorski-sketch/ytsubtran/actions/workflows/ci.yml/badge.svg"></a>
  <img alt="Python" src="https://img.shields.io/badge/Python-3.9%2B-blue?logo=python&logoColor=white">
  <img alt="Whisper" src="https://img.shields.io/badge/AI-OpenAI%20Whisper-412991?logo=openai&logoColor=white">
  <img alt="GPU" src="https://img.shields.io/badge/GPU-CUDA%20accelerated-76B900?logo=nvidia&logoColor=white">
  <img alt="Platform" src="https://img.shields.io/badge/Platform-Windows%20%7C%20Linux%20%7C%20macOS-lightgrey">
  <img alt="License" src="https://img.shields.io/badge/License-MIT-green">
</p>

<p align="center">
  <img alt="Demo" src="assets/demo.svg" width="700">
</p>

---

## ✨ Why this tool?

Most "YouTube to subtitles" scripts break the moment YouTube changes something, mis-detect the spoken language, or produce garbled, looping text. This one is built to **just work**:

- 🛡️ **Self-healing downloads** — auto-installs/updates `yt-dlp`, tries multiple format strategies, resumes interrupted transfers, bypasses geo-blocks, and diagnoses failures in plain language.
- 🌐 **Smart language detection** — samples *several* points in the audio (not just the first 30 s), so an English intro on a dubbed video won't fool it.
- 🎯 **High-quality transcription** — anti-hallucination decoding (no more `"I'm sorry"` × 20 loops) and an optional context prompt for names & jargon.
- 🔁 **Built-in translation** — turn a Portuguese video into Polish subtitles in one command.
- ⚡ **GPU acceleration** — automatically uses your NVIDIA card (with CPU fallback if VRAM runs out).

## 🎥 See the difference

Real example — a Portuguese-dubbed video with an English intro:

**❌ Without language fix (wrong language detected from intro):**
```
We lost everything, that's it. This guy was able to make the biggest shit of all.
I'm sorry. I'm sorry. I'm sorry. I'm sorry. I'm sorry. I'm sorry...
```

**✅ With `--source-lang pt --model turbo` (correct):**
```
Hej! To nie twoja wina.
Dlaczego nie mogę za nim płakać?
Myślę, że lepiej obmyślimy plan powstrzymania Jaxa.
```

---

## 🚀 Quick start

```bash
# 1. Install (gets the `ytsubtran` command + all dependencies)
pip install .
# (also install ffmpeg and add it to PATH — see the full guide)

# 2. Download a video + Polish subtitles
ytsubtran "https://youtube.com/watch?v=VIDEO_ID" --subs
```

That's it. Subtitles (`.srt` + `.txt`) appear next to the downloaded file.

> Prefer not to install? You can always run the script directly:
> `python youtube_downloader.py "URL" --subs`

---

## 🧰 Features at a glance

| Capability | How |
|---|---|
| Download video (MP4) | `ytsubtran "URL"` |
| Download audio only (MP3) | `... --format mp3` |
| Generate subtitles | `... --subs` |
| Translate subtitles → Polish | `... --subs --source-lang es --translate-to pl` |
| Auto-detect spoken language | `... --source-lang auto` |
| Work on a local file (no download) | `--file "C:\videos\clip.mp4"` |
| Pick a Whisper model | `--model turbo` (or `large-v3`, `medium`, …) |
| Improve accuracy with context | `--prompt "Pomni, Ragatha, Jax, Caine"` |
| Age-restricted / "not a bot" videos | `--cookies-from-browser chrome` |
| Choose where files are saved | `--output-dir "C:\downloads"` |
| Also export WebVTT subtitles | `--vtt` |
| Burn subtitles into the video | `--burn` |
| Embed a toggleable subtitle track | `--embed` |
| Detect inserted clips / interstitials | `--find-inserts` |
| Cut them out (SponsorBlock → heuristic → AI) | `--cut-inserts` |
| Smart detection with Gemini (multimodal) | `--smart-inserts` |
| Save each insert as its own named clip (to reuse) | `--extract-inserts` |
| Reuse a saved cut list (no second Gemini call) | `--from-list FILE` |
| Snap cuts to scene boundaries (frame-accurate) | `--snap-cuts` |

## 📦 Installation

You need **Python 3.9+** and **ffmpeg** on your PATH.

```bash
python -m venv .venv
# Windows:  .\.venv\Scripts\activate
# Linux/mac: source .venv/bin/activate

# Install the package (provides the `ytsubtran` command):
pip install .

# …or, if you just want the dependencies without installing the command:
pip install -r requirements.txt
```

**ffmpeg:** download from [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) (Windows) or `sudo apt install ffmpeg` / `brew install ffmpeg`.

> 📖 **Full step-by-step Windows guide (Polish):** [`YouTube_Downloader_Instrukcja_Windows.html`](YouTube_Downloader_Instrukcja_Windows.html)

## ⚡ GPU acceleration (NVIDIA)

By default PyTorch runs on CPU, which is slow for big models. With an NVIDIA card:

```bash
pip uninstall -y torch
pip install torch --index-url https://download.pytorch.org/whl/cu126
python -c "import torch; print(torch.cuda.is_available())"   # should print True
```

The script auto-detects the GPU, reports its name and VRAM, transcribes with `fp16`, and falls back to CPU if it runs out of memory. Match the `cu1XX` number to your driver (check `nvidia-smi`).

## 🧠 Whisper models

| Model | RAM/VRAM | Speed | Quality | Best for |
|---|---|---|---|---|
| `tiny` | ~1 GB | fastest | low | quick tests |
| `base` | ~1 GB | fast | fair | simple speech |
| `small` | ~2 GB | medium | good | clearer transcripts |
| **`turbo`** ⭐ | ~6 GB | fast | very good | **best all-round, esp. on 8 GB GPUs** |
| `medium` | ~5 GB | slow | very good | real videos without a strong GPU |
| `large-v3` | ~10 GB | slowest | best | maximum accuracy |

Any name Whisper supports works — newer models will too, no code changes needed.

## 💡 Getting the best quality

1. **Set the correct language** (`--source-lang`) — the #1 cause of bad subtitles is wrong language detection on dubbed content.
2. **Use a bigger model** — `turbo` or `large-v3`.
3. **Add `--prompt`** with character names / terminology (in the spoken language).
4. **Use a GPU** to make big models practical.

## 📄 Output

Files are written next to the source, suffixed with the language code. When translating, **both** versions are saved:

```
video.mp4
video_PT.srt   video_PT.txt   # original transcription
video_PL.srt   video_PL.txt   # translated subtitles
```

Load the `.srt` in VLC (Subtitles → Add Subtitle File) or keep it next to the video.

## 🩹 Troubleshooting

| Problem | Fix |
|---|---|
| `HTTP 403` / `Unable to extract` | Script auto-updates `yt-dlp` and retries. Else: `pip install -U yt-dlp` |
| "Sign in to confirm your age / not a bot" | `python -m yt_dlp --cookies-from-browser chrome "URL"` |
| `HTTP 429 Too Many Requests` | Wait a few minutes / switch network or VPN |
| Garbled or looping subtitles | Set `--source-lang` explicitly; use `--model turbo`; add `--prompt` |
| CUDA out of memory | Use `--model turbo`/`medium`; close other GPU apps |
| Transcription very slow | Install CUDA PyTorch, or use a smaller model |

## ✂️ Inserts: extract them as clips, or cut them out

Short spliced-in clips ("bumpers", meme cuts, joke skits, intros/outros) can be
**extracted as separate named clips** (to reuse in your own edits) *or* **cut out**
of the video. Detection uses a layered cascade — each layer cross-checks the others:

1. **SponsorBlock** — if the YouTube video has community-labeled segments (the
   `filler` category = tangents/jokes), use those exact timestamps.
2. **Heuristic** — otherwise detect short spans with a sudden loudness jump (EBU
   R128) corroborated by a hard scene cut.
3. **AI cross-check** (optional, `--insert-ai`) — a local Ollama model confirms
   each candidate from its transcript.

```bash
ytsubtran "URL" --find-inserts                 # analyze only: prints/saves a cut list
ytsubtran "URL" --cut-inserts                  # also remove them (asks first; --yes to skip)
ytsubtran --file clip.mp4 --find-inserts --insert-jump 7 --insert-min-len 2
```

`--find-inserts` never edits the video — it only proposes a cut list for review.
`--cut-inserts` writes a new `*_nocuts.mp4`, leaving the original untouched.

### Mark inserts in DaVinci Resolve

[`resolve_markers.py`](resolve_markers.py) drops a coloured **timeline marker** at
every detected insert, so you can jump between them in Resolve instead of scrubbing.
Copy it into Resolve's script folder
(`…/Blackmagic Design/DaVinci Resolve/Support/Fusion/Scripts/Utility/`) and run it
from **Workspace → Scripts**. It auto-finds the `*_inserts.txt` next to the open
timeline's video; if the open clip doesn't match, it shows a **picker** of the lists
in that folder and warns about the mismatch. (If your Resolve build blocks scripting,
use the planned SRT-subtitle export instead.)

### Extract each insert as its own clip (`--extract-inserts`)

When you want to **reuse** the inserts (not delete them), add `--extract-inserts`.
Each detected segment is saved as a separate, descriptively-named file in a
`<video>_clips` folder — the original is never modified:

```bash
ytsubtran --file clip.mp4 --smart-inserts --extract-inserts
# →  clip_clips/01_00m05s_Animated intro sequence.mp4
#    clip_clips/02_02m11s_Reaction meme.mp4   …
```

File names are `NN_MMmSSs_<reason>.mp4` (index, timestamp, and the model's
description), so you can tell at a glance what each clip is.

### The cut list is editable — and reusable (`--from-list`)

Every detection run writes a plain-text cut list next to the video
(`<video>_inserts.txt`). It's human-readable and editable:

```
# Insert cut list — delete a line to drop that segment.
130.000	143.000	# Animated intro sequence
756.000	767.000	# Reaction meme
```

**Delete any line** to drop that segment, then feed the file back with
`--from-list` to extract or cut **without re-running detection** — so a paid
Gemini analysis only ever runs **once**:

```bash
# Re-uses the saved list — zero Gemini calls:
ytsubtran --file clip.mp4 --from-list "clip_inserts.txt" --extract-inserts
ytsubtran --file clip.mp4 --from-list "clip_inserts.txt" --cut-inserts
```

### Fine-tuning the boundaries

- `--snap-cuts` — snap each in/out point to the nearest detected scene cut for
  frame-accurate trims (refines the model's ±1 s timestamps).
- `--clips-copy` — with `--extract-inserts`, stream-copy clips (instant, no
  re-encode) instead of the default frame-accurate re-encode. Faster, but each
  clip starts at the nearest keyframe.

### Smart detection (Gemini)

For tightly-edited videos where inserts are *visual* (a cut to other footage, with
no loudness spike), the audio/scene heuristic has high recall but low precision. For
those, `--smart-inserts` sends the video to the **Gemini multimodal API**, which
actually watches it and returns the insert timestamps:

```bash
pip install google-genai          # one-time
ytsubtran --file clip.mp4 --smart-inserts                    # list inserts
ytsubtran --file clip.mp4 --smart-inserts --extract-inserts  # save them as clips
ytsubtran --file clip.mp4 --smart-inserts --cut-inserts      # remove them
```

On first use it asks for a Gemini API key ([get one free](https://aistudio.google.com/apikey))
and saves it to `~/.ytsubtran.json` (or read it from `GEMINI_API_KEY`). Note: this
uploads the video to Google's API.

Gemini classifies each hit as **`clip`** (footage from another video / meme / B-roll),
**`screenshot`** (a still image shown on screen), or **`caption`** (editor text/graphics).
By default only real `clip` interstitials are kept — pick others with
`--insert-kinds clip,screenshot,caption`. Run `--smart-inserts` **once**, then reuse the
saved `<video>_inserts.txt` with `--from-list` so Gemini is never billed twice.

## 📚 How it works

Want to understand the internals — or rebuild this from scratch? The
[**technical guide**](docs/HOW_IT_WORKS.md) explains every design decision: the
download fallback engine, robust language detection, anti-hallucination Whisper
parameters, GPU handling, the SRT format, translation, and a step-by-step build
order.

## 🤝 Contributing

Issues and pull requests are welcome! See [CONTRIBUTING.md](CONTRIBUTING.md).

## 📜 License

[MIT](LICENSE) — free to use, modify, and share.

## ⚖️ Disclaimer

For personal use. Respect YouTube's Terms of Service and the copyright of the content you download.

---

<p align="center"><sub>Built with ❤️ using <a href="https://github.com/openai/whisper">OpenAI Whisper</a> and <a href="https://github.com/yt-dlp/yt-dlp">yt-dlp</a>.</sub></p>
