
import re
import subprocess
import os
import argparse
import sys
import shutil

# Wymuś UTF-8 na wyjściu konsoli (Windows) — inaczej polskie znaki (ł, ż...)
# i emoji powodują UnicodeEncodeError przy domyślnym kodowaniu cp1252.
try:
    sys.stdout.reconfigure(encoding='utf-8')
    sys.stderr.reconfigure(encoding='utf-8')
except (AttributeError, ValueError):
    pass


def extract_url(text):
    """Czyszczenie tekstu i wyciąganie linku YouTube"""
    clean_text = re.sub(r'[\[\]\(\)]', '', text).strip()
    pattern = r'(https?://(?:www\.)?youtube\.com/watch\?v=[0-9A-Za-z_-]{11})'
    match = re.search(pattern, clean_text)
    if match:
        return match.group(1)
    return clean_text


def check_whisper_installed():
    """Sprawdza czy Whisper jest zainstalowany"""
    try:
        import whisper
        return True, whisper.__version__
    except ImportError:
        return False, None


def check_ffmpeg_installed():
    """Sprawdza czy ffmpeg jest zainstalowany"""
    return shutil.which('ffmpeg') is not None


def check_translator_installed():
    """Sprawdza czy biblioteka deep-translator jest zainstalowana"""
    try:
        import deep_translator  # noqa: F401
        return True
    except ImportError:
        return False


def show_translator_installation_guide():
    """Wyświetla instrukcję instalacji deep-translator"""
    print('\n' + '=' * 70)
    print('⚠️  TRANSLATION LIBRARY IS NOT INSTALLED')
    print('=' * 70)
    print('\nTranslating subtitles requires the deep-translator library')
    print('(uses the free Google Translate, no API key).\n')
    print('📦 INSTALL:\n')
    print('   pip install -U deep-translator\n')
    print('After installing, run the script again.')
    print('=' * 70)


# Human-readable language names for messages
LANGUAGE_NAMES = {
    'auto': 'auto-detect',
    'pl': 'Polish', 'en': 'English', 'es': 'Spanish', 'de': 'German',
    'fr': 'French', 'it': 'Italian', 'pt': 'Portuguese', 'ru': 'Russian',
    'uk': 'Ukrainian', 'cs': 'Czech', 'sk': 'Slovak', 'nl': 'Dutch',
    'ja': 'Japanese', 'zh': 'Chinese', 'ko': 'Korean', 'ar': 'Arabic',
    'tr': 'Turkish', 'sv': 'Swedish', 'no': 'Norwegian', 'da': 'Danish',
    'fi': 'Finnish', 'hu': 'Hungarian', 'ro': 'Romanian', 'el': 'Greek',
}


def lang_name(code):
    """Zwraca czytelną nazwę języka dla podanego kodu"""
    return LANGUAGE_NAMES.get(code, code)


def translate_texts(texts, target_lang, source_lang='auto', max_workers=8):
    """
    Tłumaczy listę tekstów na język docelowy używając Google Translate.

    Segmenty tłumaczone są RÓWNOLEGLE (pula wątków) — przy długich filmach
    to wielokrotne przyspieszenie względem tłumaczenia jeden po drugim, a kolejność
    i przyporządkowanie do znaczników czasu pozostają dokładne (mapowanie 1:1).
    Jeśli pojedynczy segment się nie przetłumaczy, zostaje oryginalny tekst.

    Zwraca listę przetłumaczonych tekstów (tej samej długości co wejście).
    """
    from concurrent.futures import ThreadPoolExecutor

    from deep_translator import GoogleTranslator

    src = source_lang or 'auto'  # Google używa 'auto' do auto-wykrywania źródła
    total = len(texts)

    def work(item):
        idx, text = item
        original = text.strip()
        if not original:
            return idx, text
        try:
            # Nowy translator na wywołanie — bezpieczne wątkowo (brak współdzielonego stanu)
            result = GoogleTranslator(source=src, target=target_lang).translate(original)
            return idx, (result if result else original)
        except Exception:
            return idx, original  # silently keep the original — don't abort everything

    translated = [None] * total
    completed = 0
    # Network I/O releases the GIL, so threads give a real speedup
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        for idx, text in executor.map(work, enumerate(texts)):
            translated[idx] = text
            completed += 1
            if completed % 25 == 0 or completed == total:
                print(f'   ...translated {completed}/{total} segments')

    return translated


def format_timestamp_vtt(seconds):
    """Konwertuje sekundy na format WebVTT (00:00:00.000 — kropka zamiast przecinka)."""
    return format_timestamp_srt(seconds).replace(',', '.')


def write_vtt(vtt_file, segments, texts=None):
    """
    Zapisuje plik napisów w formacie WebVTT (.vtt) — używany m.in. w odtwarzaczach WWW.
    Różni się od SRT nagłówkiem 'WEBVTT' i kropką w znaczniku czasu.
    """
    with open(vtt_file, 'w', encoding='utf-8') as f:
        f.write('WEBVTT\n\n')
        for i, segment in enumerate(segments, 1):
            start = format_timestamp_vtt(segment['start'])
            end = format_timestamp_vtt(segment['end'])
            text = (texts[i - 1] if texts is not None else segment['text']).strip()
            f.write(f'{start} --> {end}\n')
            f.write(f'{text}\n\n')


def write_srt(srt_file, segments, texts=None):
    """
    Zapisuje plik napisów SRT.

    - segments: lista segmentów Whisper (zawierają 'start', 'end', 'text')
    - texts: opcjonalna lista tekstów nadpisująca 'text' z segmentów
             (używana dla wersji przetłumaczonej)
    """
    with open(srt_file, 'w', encoding='utf-8') as f:
        for i, segment in enumerate(segments, 1):
            start = format_timestamp_srt(segment['start'])
            end = format_timestamp_srt(segment['end'])
            text = (texts[i - 1] if texts is not None else segment['text']).strip()

            f.write(f'{i}\n')
            f.write(f'{start} --> {end}\n')
            f.write(f'{text}\n\n')


def show_whisper_installation_guide():
    """Wyświetla instrukcję instalacji Whisper"""
    print('\n' + '=' * 70)
    print('⚠️  WHISPER IS NOT INSTALLED')
    print('=' * 70)
    print('\nWhisper is a free OpenAI AI model for audio transcription.')
    print('It runs locally (offline) and handles many languages very well!\n')

    print('📦 INSTALL WHISPER:\n')
    print('1. Install Whisper:')
    print('   pip install -U openai-whisper\n')

    print('2. Install ffmpeg (if you do not have it yet):\n')
    print('   Linux/Ubuntu:')
    print('   sudo apt update && sudo apt install ffmpeg\n')
    print('   Windows:')
    print('   - Download from: https://ffmpeg.org/download.html')
    print('   - Unpack and add to PATH\n')
    print('   macOS:')
    print('   brew install ffmpeg\n')

    print('3. After installing, run the script again with the --subs option\n')
    print('=' * 70)


def detect_language_robust(model, audio_path, fractions=(0.15, 0.4, 0.65)):
    """
    Wykrywa język mowy próbkując KILKA momentów nagrania (a nie tylko pierwsze
    30 s, jak domyślnie Whisper). Dzięki temu czołówka/intro w innym języku nie
    zafałszowuje wyniku. Zwraca: (kod_języka, pewność 0..1).
    """
    import whisper

    audio = whisper.load_audio(audio_path)
    window = whisper.audio.N_SAMPLES  # 30 s materiału (Whisper przetwarza okna 30 s)
    total = len(audio)
    n_mels = model.dims.n_mels

    if total <= window:
        positions = [0]
    else:
        positions = [min(int(f * total), total - window) for f in fractions]

    scores = {}
    for pos in positions:
        segment = whisper.pad_or_trim(audio[pos:pos + window])
        mel = whisper.log_mel_spectrogram(segment, n_mels=n_mels).to(model.device)
        _, probs = model.detect_language(mel)
        for lang, p in probs.items():
            scores[lang] = scores.get(lang, 0.0) + p

    best = max(scores, key=scores.get)
    confidence = scores[best] / len(positions)
    return best, confidence


def pick_device():
    """
    Wybiera urządzenie obliczeniowe dla Whisper: GPU (CUDA), jeśli dostępne,
    inaczej CPU. Zwraca: (device, czytelny_opis).
    """
    try:
        import torch
        if torch.cuda.is_available():
            name = torch.cuda.get_device_name(0)
            total = torch.cuda.get_device_properties(0).total_memory / (1024 ** 3)
            return 'cuda', f'GPU: {name} ({total:.1f} GB VRAM)'
    except Exception:
        pass
    return 'cpu', 'CPU (no GPU available)'


def run_ffmpeg(command, cwd=None):
    """Uruchamia ffmpeg, zbierając output (pokazywany tylko przy błędzie). Zwraca (rc, output)."""
    try:
        proc = subprocess.Popen(
            command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, encoding='utf-8', errors='replace', bufsize=1, cwd=cwd
        )
    except FileNotFoundError as e:
        return 1, f'Could not start ffmpeg: {e}'
    collected = list(proc.stdout)
    proc.wait()
    return proc.returncode, ''.join(collected)


def burn_subtitles(video_file, srt_file, output_file):
    """
    Wtapia napisy NA STAŁE w obraz (hardsub) — ponowne kodowanie wideo.
    Zwraca ścieżkę pliku wynikowego lub None przy błędzie.
    """
    import shutil as _sh
    import tempfile

    if not check_ffmpeg_installed():
        print('✗ ffmpeg not available — cannot burn in subtitles.')
        return None

    # Copy the SRT to a temp dir under a plain name to avoid path-escaping issues
    # (drive colon, spaces, special chars) in the ffmpeg subtitles filter.
    workdir = tempfile.mkdtemp(prefix='ytsub_')
    try:
        _sh.copyfile(srt_file, os.path.join(workdir, 'subs.srt'))
        print('\n🔥 Burning subtitles into the picture (re-encoding — may take a while):')
        print(f'   {output_file}')
        cmd = [
            'ffmpeg', '-y', '-i', os.path.abspath(video_file),
            '-vf', 'subtitles=subs.srt',
            '-c:a', 'copy',
            os.path.abspath(output_file),
        ]
        rc, out = run_ffmpeg(cmd, cwd=workdir)
    finally:
        _sh.rmtree(workdir, ignore_errors=True)

    if rc == 0 and os.path.exists(output_file):
        print('✓ Subtitles burned in permanently.')
        return output_file
    print('✗ Burning subtitles failed:')
    print('   ' + '\n   '.join(out.strip().splitlines()[-5:]))
    return None


def embed_subtitles(video_file, srt_file, output_file, lang_code='und'):
    """
    Osadza napisy jako MIĘKKĄ ścieżkę w kontenerze MP4 (bez ponownego kodowania —
    szybkie; widz włącza/wyłącza napisy w odtwarzaczu). Zwraca ścieżkę lub None.
    """
    if not check_ffmpeg_installed():
        print('✗ ffmpeg not available — cannot embed subtitles.')
        return None

    print('\n🎬 Embedding a soft subtitle track (no re-encoding):')
    print(f'   {output_file}')
    cmd = [
        'ffmpeg', '-y',
        '-i', os.path.abspath(video_file),
        '-i', os.path.abspath(srt_file),
        '-map', '0', '-map', '1',
        '-c', 'copy', '-c:s', 'mov_text',
        '-metadata:s:s:0', f'language={lang_code}',
        os.path.abspath(output_file),
    ]
    rc, out = run_ffmpeg(cmd)

    if rc == 0 and os.path.exists(output_file):
        print('✓ Subtitles embedded (toggle them on in your player).')
        return output_file
    print('✗ Embedding subtitles failed:')
    print('   ' + '\n   '.join(out.strip().splitlines()[-5:]))
    return None


def generate_subtitles_with_whisper(video_file, model_size='base', source_lang='pl',
                                    translate_to=None, initial_prompt=None, output_dir=None,
                                    also_vtt=False, burn=False, embed=False):
    """
    Generuje napisy używając Whisper, opcjonalnie z tłumaczeniem.

    Parametry:
    - video_file: ścieżka do pliku wideo
    - model_size: nazwa modelu Whisper (tiny, base, small, medium, large,
                  large-v3, turbo... — dowolna obsługiwana przez whisper)
    - source_lang: język audio w nagraniu (np. 'pl', 'es', 'en') lub 'auto'
                   do automatycznego wykrycia przez Whisper
    - translate_to: docelowy język napisów (np. 'pl'). Jeśli None, napisy
                    pozostają w języku oryginału (sama transkrypcja).
    - initial_prompt: podpowiedź kontekstowa dla Whisper (nazwy własne,
                      terminologia) — poprawia dokładność i pisownię.
    - output_dir: katalog na pliki napisów. Jeśli None, zapisuje obok pliku wideo.
    - also_vtt: dodatkowo zapisz napisy w formacie WebVTT (.vtt), nie tylko .srt.
    - burn: wtop napisy NA STAŁE w obraz (hardsub, ponowne kodowanie).
    - embed: osadź napisy jako miękką, przełączalną ścieżkę w MP4 (bez kodowania).
            Przy tłumaczeniu używana jest wersja przetłumaczona, inaczej oryginał.
    """
    print('\n' + '=' * 70)
    print('🎙️  GENERATING SUBTITLES WITH WHISPER')
    print('=' * 70)

    # Check whether Whisper is installed
    whisper_installed, version = check_whisper_installed()
    if not whisper_installed:
        show_whisper_installation_guide()
        return False

    print(f'✓ Whisper installed (version: {version})')

    # Check whether ffmpeg is installed
    if not check_ffmpeg_installed():
        print('✗ ffmpeg is not installed!')
        print('  Install ffmpeg so Whisper can process the audio.')
        return False

    print('✓ ffmpeg installed')

    # If translating — verify the translation library before transcription starts
    if translate_to and translate_to != source_lang:
        if not check_translator_installed():
            show_translator_installation_guide()
            return False
        print('✓ deep-translator installed')

    # Import Whisper
    try:
        import whisper
    except ImportError:
        show_whisper_installation_guide()
        return False

    # Check whether the file exists
    if not os.path.exists(video_file):
        print(f'✗ File not found: {video_file}')
        return False

    print(f'\n📁 Video file: {video_file}')
    print(f'🤖 Whisper model: {model_size}')
    print(f'🌍 Audio language (transcription): {lang_name(source_lang)}')
    if translate_to and translate_to != source_lang:
        print(f'🔁 Translating subtitles to: {lang_name(translate_to)}')
    print()

    # Model information
    model_info = {
        'tiny': '~1 GB RAM, fastest, low quality (quick tests only)',
        'base': '~1 GB RAM, fast, average quality',
        'small': '~2 GB RAM, good quality',
        'medium': '~5 GB RAM, very good quality (recommended for real videos)',
        'large': '~10 GB RAM, best quality, slowest',
        'large-v2': '~10 GB RAM, best quality (v2 variant)',
        'large-v3': '~10 GB RAM, best quality (latest large model)',
        'turbo': '~6 GB RAM, quality close to large-v3 but much faster',
    }
    print(f'ℹ️  {model_info.get(model_size, "custom/new model — using as provided")}\n')

    if initial_prompt:
        print(f'📝 Context prompt: "{initial_prompt}"\n')

    # Hint: small models struggle with music / many speakers
    if model_size in ('tiny', 'base'):
        print(f'💡 Hint: for videos with music, many speakers or slang, the "{model_size}" '
              'model may produce errors and repetitions.')
        print('   For better quality use: --model medium / large-v3 / turbo\n')

    # Device selection: GPU if available (much faster), otherwise CPU
    device, device_desc = pick_device()
    print(f'🖥️  Compute device: {device_desc}')
    if device == 'cpu':
        print('   (Got an NVIDIA card? Install torch with CUDA to run on the GPU — '
              'several times faster.)')
    print()

    try:
        # Load the model on the selected device
        print(f'⏳ Loading Whisper model ({model_size}) on {device.upper()}...')
        print('   (On first use the model will be downloaded - this may take a moment)')
        try:
            model = whisper.load_model(model_size, device=device)
        except RuntimeError as e:
            msg = str(e).lower()
            if 'out of memory' in msg or 'cuda' in msg:
                # Not enough VRAM for this model — fall back to CPU
                print(f'⚠️  Could not load "{model_size}" on the GPU ({e}).')
                print('   Falling back to CPU. For GPU pick a smaller model (e.g. turbo or medium).')
                device = 'cpu'
                model = whisper.load_model(model_size, device=device)
            else:
                # Unknown model name — show available models and stop clearly
                available = ', '.join(whisper.available_models())
                print(f'✗ Could not load model "{model_size}": {e}')
                print(f'  Models available in this whisper version: {available}')
                print('  (For the newest models, update: pip install -U openai-whisper)')
                return False
        print('✓ Model loaded\n')

        # Language detection (when 'auto') — sampling several points so an intro
        # in another language does not skew the result
        if source_lang == 'auto':
            print('🔎 Detecting audio language (sampling several points, skipping the intro)...')
            whisper_lang, conf = detect_language_robust(model, video_file)
            print(f'   Detected language: {lang_name(whisper_lang)} ({whisper_lang}), confidence ~{conf:.0%}\n')
        else:
            whisper_lang = source_lang

        # Transcription
        print('⏳ Transcribing audio... (this may take a few minutes)')
        print('   Progress will appear below:\n')

        # Parametry transkrypcji (fp16 tylko na GPU)
        transcribe_kwargs = dict(
            language=whisper_lang,
            task='transcribe',  # zawsze transkrypcja w oryginale; tłumaczymy osobno (Whisper translate umie tylko EN)
            verbose=True,  # Pokaż postęp
            fp16=(device == 'cuda'),  # half precision na GPU = szybciej; na CPU wymuszone FP32
            initial_prompt=initial_prompt,  # kontekst/nazwy własne — poprawia dokładność
            # --- Parametry ograniczające halucynacje i zapętlenia ---
            condition_on_previous_text=False,  # KLUCZOWE: zapobiega pętlom powtórzeń ("I'm sorry" w kółko)
            beam_size=5,                       # lepsze dekodowanie niż domyślne zachłanne
            temperature=(0.0, 0.2, 0.4, 0.6, 0.8, 1.0),  # automatyczny fallback przy słabym sygnale
            compression_ratio_threshold=2.4,  # odrzuca segmenty wyglądające na halucynację
            logprob_threshold=-1.0,            # odrzuca segmenty o niskiej pewności
            no_speech_threshold=0.6,           # lepiej wykrywa muzykę/ciszę (nie wymyśla tekstu)
        )

        try:
            result = model.transcribe(video_file, **transcribe_kwargs)
        except RuntimeError as e:
            if device == 'cuda' and 'out of memory' in str(e).lower():
                # Ran out of VRAM mid-run — free memory and finish on CPU
                print(f'\n⚠️  Ran out of GPU memory ({e}).')
                print('   Switching transcription to CPU (slower). '
                      'Next time use a smaller model, e.g. --model turbo.')
                import torch
                del model
                torch.cuda.empty_cache()
                device = 'cpu'
                model = whisper.load_model(model_size, device=device)
                transcribe_kwargs['fp16'] = False
                result = model.transcribe(video_file, **transcribe_kwargs)
            else:
                raise

        segments = result['segments']

        # Język oryginału (ustalony wcześniej: podany lub wykryty) — także źródło dla tłumaczenia
        detected_lang = whisper_lang

        # Gdzie zapisać napisy: w output_dir (jeśli podano) lub obok pliku wideo
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
            base_name = os.path.join(output_dir, os.path.splitext(os.path.basename(video_file))[0])
        else:
            base_name = os.path.splitext(video_file)[0]
        created_files = []

        # 1) Zapisz napisy w języku ORYGINAŁU
        orig_code = detected_lang.upper()
        srt_orig = f'{base_name}_{orig_code}.srt'
        txt_orig = f'{base_name}_{orig_code}.txt'

        print(f'\n💾 Saving subtitles (original): {srt_orig}')
        write_srt(srt_orig, segments)
        with open(txt_orig, 'w', encoding='utf-8') as f:
            f.write(result['text'])
        created_files.append(srt_orig)
        created_files.append(txt_orig)
        if also_vtt:
            vtt_orig = f'{base_name}_{orig_code}.vtt'
            write_vtt(vtt_orig, segments)
            created_files.append(vtt_orig)

        # „Główny" plik napisów do wtopienia/osadzenia (na razie: oryginał)
        primary_srt = srt_orig
        primary_code = orig_code

        # 2) Jeśli trzeba — przetłumacz i zapisz wersję docelową
        if translate_to and translate_to != detected_lang:
            print(f'\n🔁 Translating {len(segments)} segments to {lang_name(translate_to)}...')
            # We know the source language — pass it explicitly (more reliable than 'auto')
            source_for_translate = detected_lang
            original_texts = [s['text'] for s in segments]
            translated_texts = translate_texts(original_texts, translate_to, source_for_translate)

            tgt_code = translate_to.upper()
            srt_tgt = f'{base_name}_{tgt_code}.srt'
            txt_tgt = f'{base_name}_{tgt_code}.txt'

            print(f'\n💾 Saving subtitles (translation): {srt_tgt}')
            write_srt(srt_tgt, segments, texts=translated_texts)
            with open(txt_tgt, 'w', encoding='utf-8') as f:
                f.write('\n'.join(t.strip() for t in translated_texts))
            created_files.append(srt_tgt)
            created_files.append(txt_tgt)
            if also_vtt:
                vtt_tgt = f'{base_name}_{tgt_code}.vtt'
                write_vtt(vtt_tgt, segments, texts=translated_texts)
                created_files.append(vtt_tgt)

            # Przy tłumaczeniu wtapiamy/osadzamy wersję przetłumaczoną
            primary_srt = srt_tgt
            primary_code = tgt_code

        # 3) Opcjonalnie: wtop (hardsub) lub osadź (miękka ścieżka) napisy w wideo
        if burn:
            out = f'{base_name}_{primary_code}_hardsub.mp4'
            if burn_subtitles(video_file, primary_srt, out):
                created_files.append(out)
        if embed:
            out = f'{base_name}_{primary_code}_soft.mp4'
            if embed_subtitles(video_file, primary_srt, out, primary_code.lower()):
                created_files.append(out)

        print('\n' + '=' * 70)
        print('✓✓✓ SUBTITLES GENERATED SUCCESSFULLY ✓✓✓')
        print('=' * 70)
        print('\n📄 Created files:')
        for path in created_files:
            print(f'   • {path}')
        print('\n💡 You can use the .srt files in video players (VLC, MPC-HC, etc.)')
        print('=' * 70)

        return True

    except Exception as e:
        print(f'\n✗ Error while generating subtitles: {e}')
        print('\nCheck that:')
        print('- the video file is not corrupted')
        print('- you have enough RAM (at least 2 GB free)')
        print('- ffmpeg is installed correctly')
        return False


def format_timestamp_srt(seconds):
    """Konwertuje sekundy na format SRT (00:00:00,000)"""
    hours = int(seconds // 3600)
    minutes = int((seconds % 3600) // 60)
    secs = int(seconds % 60)
    millis = int((seconds % 1) * 1000)
    return f'{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}'


# Flagi dodawane do KAŻDEGO wywołania yt-dlp — czynią pobieranie odpornym
# na typowe problemy (sieć, blokady regionalne, przerwane transfery, playlisty).
COMMON_YTDLP_FLAGS = [
    '--no-playlist',            # nie pobieraj całej playlisty, gdy URL ma &list=
    '--retries', '10',          # ponów próby przy błędach sieci
    '--fragment-retries', '10', # ponów pobieranie pojedynczych fragmentów
    '--socket-timeout', '30',   # nie zawieszaj się w nieskończoność
    '--continue',               # wznawiaj przerwane pobierania (pliki .part)
    '--geo-bypass',             # próba automatycznego obejścia blokady regionalnej
    '--newline',                # postęp w osobnych liniach (czytelny log)
    '--ignore-errors',          # nie wywalaj się na pojedynczym drobnym błędzie
]

# Rozszerzenia plików medialnych — do wykrywania, co faktycznie pobrano
MEDIA_EXTENSIONS = {
    'mp4': ('.mp4', '.mkv', '.webm', '.mov', '.flv'),
    'mp3': ('.mp3', '.m4a', '.opus', '.aac', '.wav', '.ogg'),
}


def ensure_ytdlp():
    """Sprawdza czy yt-dlp jest dostępny; jeśli nie — próbuje zainstalować."""
    try:
        check = subprocess.run(
            [sys.executable, '-m', 'yt_dlp', '--version'],
            capture_output=True, text=True
        )
        if check.returncode == 0:
            print(f'✓ yt-dlp available (version: {check.stdout.strip()})')
            return True
    except Exception:
        pass

    print('⚠️  yt-dlp is not installed — trying to install it automatically...')
    return update_ytdlp()


def update_ytdlp():
    """Installs or updates yt-dlp to the latest version via pip."""
    print('⏳ Installing/updating yt-dlp (pip install -U yt-dlp)...')
    try:
        res = subprocess.run([sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp'])
        if res.returncode == 0:
            check = subprocess.run(
                [sys.executable, '-m', 'yt_dlp', '--version'],
                capture_output=True, text=True
            )
            if check.returncode == 0:
                print(f'✓ yt-dlp ready (version: {check.stdout.strip()})')
                return True
    except Exception as e:
        print(f'✗ yt-dlp install error: {e}')
        return False

    print('✗ Could not install/update yt-dlp.')
    print('  Try manually: pip install -U yt-dlp')
    return False


def run_ytdlp(command):
    """
    Uruchamia yt-dlp, streamując output na żywo (widać pasek postępu),
    a jednocześnie zbiera go do analizy błędów.

    Zwraca: (returncode, pełny_output_jako_tekst)
    """
    try:
        process = subprocess.Popen(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding='utf-8',
            errors='replace',
            bufsize=1,
        )
    except FileNotFoundError as e:
        return 1, f'Could not start yt-dlp: {e}'

    collected = []
    for line in process.stdout:
        sys.stdout.write(line)
        sys.stdout.flush()
        collected.append(line)
    process.wait()
    return process.returncode, ''.join(collected)


def detect_downloaded_file(before_files, output_dir, format_choice):
    """
    Wykrywa pobrany plik porównując zawartość katalogu sprzed i po pobraniu.
    Najpewniejsza metoda — niezależna od formatu komunikatów yt-dlp.
    """
    exts = MEDIA_EXTENSIONS.get(format_choice, ())
    try:
        after_files = set(os.listdir(output_dir))
    except OSError:
        return None

    # Najpierw nowe pliki; jeśli plik już istniał (re-download) — bierzemy najnowszy
    new_files = [f for f in (after_files - before_files) if f.lower().endswith(exts)]
    pool = new_files if new_files else [f for f in after_files if f.lower().endswith(exts)]
    if not pool:
        return None

    pool.sort(key=lambda f: os.path.getmtime(os.path.join(output_dir, f)), reverse=True)
    return pool[0]


def diagnose_failure(output):
    """
    Analizuje output yt-dlp i zwraca słownik:
    - 'message': czytelne wyjaśnienie problemu
    - 'suggest_update': czy aktualizacja yt-dlp może pomóc (warto ponowić)
    """
    low = (output or '').lower()

    # Browser cookie read problem (common on Windows: the browser locks the
    # cookie file while it is open). Updating yt-dlp does NOT help here.
    if ('could not copy' in low and 'cookie' in low) or 'cookie database' in low \
            or 'could not find' in low and 'cookies' in low:
        return {
            'message': 'Could not read the browser cookies. CLOSE the browser completely '
                       '(Chrome/Edge lock the cookie file while open) and try again — '
                       'or use another one: --cookies-from-browser firefox. '
                       'If the video works without logging in, just drop --cookies-from-browser.',
            'suggest_update': False,
        }

    # Missing JavaScript runtime — modern yt-dlp needs one (Deno) to extract
    # YouTube. Without it, it falls back to a limited client that often returns
    # "This video is not available". Updating yt-dlp does NOT fix this.
    if 'no supported javascript runtime' in low or 'js-runtimes' in low:
        return {
            'message': 'yt-dlp needs a JavaScript runtime to extract YouTube videos, '
                       'and none was found. This is the usual cause of "This video is '
                       'not available" on otherwise-watchable videos. Install Deno '
                       '(auto-detected once present) and re-run:\n'
                       '    winget install DenoLand.Deno      (Windows)\n'
                       '    or: https://github.com/yt-dlp/yt-dlp/wiki/EJS\n'
                       'After installing, open a NEW terminal so Deno is on PATH.',
            'suggest_update': False,
        }

    # Problems usually fixed by updating yt-dlp (YouTube-side changes)
    update_signals = [
        'unable to extract', 'nsig extraction', 'signature extraction',
        'failed to extract any player response', 'precondition check failed',
        'http error 403', 'unable to download webpage', 'players/', 'jsinterp',
    ]
    if any(s in low for s in update_signals):
        return {
            'message': 'YouTube likely changed something on their side '
                       '(extraction error). Updating yt-dlp usually fixes it.',
            'suggest_update': True,
        }

    if 'sign in to confirm your age' in low or 'age-restricted' in low or 'inappropriate' in low:
        return {
            'message': 'Age-restricted video — requires logging in. '
                       'Use browser cookies: --cookies-from-browser <chrome/firefox/...>.',
            'suggest_update': False,
        }

    if "confirm you're not a bot" in low or 'sign in to confirm' in low:
        return {
            'message': 'YouTube wants to confirm you are not a bot. '
                       'Logging in via cookies (--cookies-from-browser) helps, '
                       'or wait and switch network/VPN.',
            'suggest_update': False,
        }

    if 'private video' in low:
        return {'message': 'This video is private — it cannot be downloaded.', 'suggest_update': False}

    if 'this video has been removed' in low or 'video unavailable' in low or 'account associated' in low:
        return {'message': 'The video is unavailable or has been removed.', 'suggest_update': False}

    if 'not available in your country' in low or 'blocked it in your country' in low or 'geo' in low:
        return {
            'message': 'Video is geo-blocked. --geo-bypass did not get around it — try a VPN.',
            'suggest_update': False,
        }

    if 'drm' in low:
        return {'message': 'DRM-protected video — it cannot be downloaded.', 'suggest_update': False}

    if 'http error 429' in low or 'too many requests' in low:
        return {
            'message': 'YouTube is rate-limiting requests (HTTP 429). Wait a few '
                       'minutes or switch network/VPN.',
            'suggest_update': False,
        }

    if any(s in low for s in ['getaddrinfo', 'timed out', 'connection', 'network', 'temporary failure']):
        return {'message': 'Internet connection problem.', 'suggest_update': False}

    return {
        'message': 'Unknown download error. Updating yt-dlp is worth a try.',
        'suggest_update': True,
    }


def try_download(url, format_spec, format_name, output_template, extra_args, output_dir,
                 format_choice, cookies_from_browser=None):
    """
    Próbuje pobrać wideo z danym formatem.
    Zwraca: (success, nazwa_pliku_lub_None, output)
    """
    command = [
        sys.executable, '-m', 'yt_dlp',
        '-f', format_spec,
        '-o', output_template,
    ]
    command.extend(COMMON_YTDLP_FLAGS)
    if cookies_from_browser:
        # Logowanie przez ciasteczka przeglądarki (filmy 18+ / "nie jesteś botem")
        command.extend(['--cookies-from-browser', cookies_from_browser])
    if extra_args:
        command.extend(extra_args)
    command.append(url)

    print(f'\n[Attempt: {format_name}]')
    print(f'Format: {format_spec}')

    before_files = set(os.listdir(output_dir)) if os.path.isdir(output_dir) else set()
    returncode, output = run_ytdlp(command)

    if returncode == 0:
        filename = detect_downloaded_file(before_files, output_dir, format_choice)
        print(f'✓ Success! Downloaded using: {format_name}')
        return True, filename, output

    print(f'✗ Failed: {format_name}')
    return False, None, output


def attempt_all_strategies(url, strategies, output_template, output_dir, format_choice,
                           cookies_from_browser=None):
    """
    Przechodzi przez wszystkie strategie po kolei aż któraś zadziała.
    Zwraca: (success, nazwa_pliku, połączony_output_wszystkich_prób)
    """
    combined_output = []
    for i, (format_spec, format_name, extra_args) in enumerate(strategies, 1):
        print(f'\nStrategy {i}/{len(strategies)}')
        success, filename, output = try_download(
            url, format_spec, format_name, output_template, extra_args, output_dir,
            format_choice, cookies_from_browser
        )
        combined_output.append(output)

        if success:
            return True, filename, '\n'.join(combined_output)

        if i < len(strategies):
            print('Trying the next strategy...')

    return False, None, '\n'.join(combined_output)


def process_inserts(video_file, url=None, output_dir=None, do_cut=False, use_ai=False,
                    ai_model='llama3.1', assume_yes=False,
                    min_len=1.5, max_len=15.0, jump_lu=8.0, require_scene_cut=True,
                    smart=False, smart_model='gemini-2.5-flash',
                    from_list=None, snap=False, extract=False, clips_copy=False,
                    insert_kinds=('clip',)):
    """
    Detects inserts/interstitials and then EITHER extracts them as separate named
    clips (extract=True, the default action), OR removes them from the video
    (do_cut=True), OR just lists them (analysis only).

    Default engine: SponsorBlock → audio/scene heuristic → optional AI cross-check.
    With smart=True: use the Gemini multimodal API to locate inserts (best for
    visually-defined cutaways that have no audio signature).
    With from_list=PATH: skip detection entirely and use a saved/edited list
    (no second Gemini call, fully under your control).
    With snap=True: snap boundaries to the nearest detected scene cut (frame-accurate).
    """
    # inserts.py sits next to this file; make sure that directory is importable
    # even when launched via the installed `ytsubtran` console command (where the
    # current working directory is not automatically on sys.path).
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import inserts
    except Exception as e:
        print(f'\n✗ Insert-detection module unavailable: {e}')
        return

    print('\n' + '=' * 70)
    print('✂️  DETECTING INSERTS / INTERSTITIALS')
    print('=' * 70)

    if from_list:
        print(f'📄 Reading cut list from: {from_list}')
        try:
            candidates = inserts.load_cut_list(from_list)
            source = 'saved list'
        except OSError as e:
            print(f'✗ Could not read the cut list: {e}')
            return
    elif smart:
        print(f'🤖 Smart mode: asking Gemini ({smart_model}) to watch the video...')
        print(f'   keeping kinds: {", ".join(insert_kinds)} (change with --insert-kinds)')
        candidates, source = inserts.smart_find_inserts(video_file, model=smart_model,
                                                        kinds=insert_kinds)
    else:
        print('🔎 Checking SponsorBlock, then the audio/scene heuristic'
              + (' + AI cross-check' if use_ai else '') + '...')
        candidates, source = inserts.find_inserts(
            video_file, url=url, use_ai=use_ai, ai_model=ai_model,
            min_len=min_len, max_len=max_len, jump_lu=jump_lu,
            require_scene_cut=require_scene_cut,
        )

    if not candidates:
        print('No inserts detected.')
        return

    # Optionally snap model/list times to real scene cuts for frame accuracy
    duration = inserts.ffprobe_duration(video_file)
    if snap:
        print('🎯 Snapping boundaries to detected scene cuts (frame-accurate)...')
        cuts = inserts.scene_cut_times(video_file, duration=duration)
        candidates = inserts.snap_to_scene_cuts(candidates, cuts)

    # Validate against the real video length: detectors (esp. Gemini) can report
    # times PAST the end of the video, which otherwise become empty 0-byte clips.
    candidates, dropped = inserts.clamp_segments(candidates, duration)
    if dropped:
        dm, ds = int(duration // 60), duration % 60
        print(f'⚠️  Dropped {dropped} segment(s) outside the video length '
              f'({dm:02d}:{ds:04.1f}) — likely detector timing drift.')
    if not candidates:
        print('No valid inserts remain after checking against the video length.')
        return

    print(f'\nFound {len(candidates)} candidate segment(s) via: {source}')
    total = 0.0
    for i, seg in enumerate(candidates, 1):
        s, e = float(seg[0]), float(seg[1])
        total += (e - s)
        reason = seg[2] if len(seg) > 2 and isinstance(seg[2], str) and seg[2] not in ('heuristic',) else ''
        extra = f'  — {reason}' if reason else ''
        print(f'  {i:>2}. {int(s // 60):02d}:{s % 60:04.1f} → {int(e // 60):02d}:{e % 60:04.1f}  ({e - s:.1f}s){extra}')
    print(f'  total: {total:.1f}s across {len(candidates)} segment(s)')

    # Save the cut list next to the output for review (human-editable).
    # Skip re-saving when we are working from an existing list.
    base = os.path.splitext(video_file)[0]
    if output_dir:
        base = os.path.join(output_dir, os.path.splitext(os.path.basename(video_file))[0])
    list_path = f'{base}_inserts.txt'
    if not from_list:
        try:
            inserts.save_cut_list(list_path, candidates)
            print(f'\n📝 Cut list saved: {list_path}')
            print('   ✏️  Review/edit it (delete lines to drop segments you don\'t want), then:')
            print(f'      extract clips:  --from-list "{list_path}" --extract-inserts')
            print(f'      or remove them: --from-list "{list_path}" --cut-inserts')
        except OSError:
            pass

    # --- Action 1: extract each insert as its own named clip (non-destructive) ---
    if extract:
        clips_dir = f'{base}_clips'
        print(f'\n🎬 Extracting {len(candidates)} clip(s) → {clips_dir}\\')
        if clips_copy:
            print('   (stream-copy mode: instant, but each clip starts at the nearest keyframe)')
        else:
            print('   (re-encoding for frame-accurate, editor-ready clips — may take a while)')
        created = inserts.extract_clips(video_file, candidates, clips_dir,
                                        reencode=not clips_copy)
        if created:
            print(f'\n✓ Done: {len(created)} clip(s) saved in {clips_dir}\\')
        else:
            print('✗ No clips were created (see ffmpeg output above).')
        return

    if not do_cut:
        print('\nℹ️  Analysis only.')
        print('   • Extract these as separate clips:  --extract-inserts')
        print('   • Or remove them from the video:    --cut-inserts')
        return

    # --- Action 2: remove the segments from the video (destructive to a NEW file) ---
    # Confirm before cutting (unless --yes or non-interactive with --yes)
    if not assume_yes and sys.stdin and sys.stdin.isatty():
        try:
            answer = input('\nRemove these segments from the video? [y/N] ').strip().lower()
        except (EOFError, KeyboardInterrupt):
            answer = ''
        if answer not in ('y', 'yes', 't', 'tak'):
            print('Aborted — nothing was cut.')
            return

    out = f'{base}_nocuts.mp4'
    print(f'\n✂️  Cutting {len(candidates)} segment(s) → {out}')
    result = inserts.cut_segments(video_file, candidates, out)
    if result:
        print(f'✓ Done: {result}')
    else:
        print('✗ Cutting failed (see ffmpeg output above).')


def download_youtube(raw_url, format_choice='mp4', generate_subs=False, whisper_model='base',
                     source_lang='pl', translate_to=None, initial_prompt=None,
                     cookies_from_browser=None, output_dir=None, also_vtt=False,
                     burn=False, embed=False, inserts_opts=None):
    url = extract_url(raw_url)
    if not url:
        print('No valid YouTube link detected.')
        return

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    else:
        output_dir = os.getcwd()
    output_template = os.path.join(output_dir, '%(title)s.%(ext)s')

    print(f'\nDownloading from: {url}')
    print(f'Output directory: {output_dir}')
    print(f'Format: {format_choice}')
    if generate_subs:
        print(f'Generating subtitles: YES (model: {whisper_model})')
        print(f'  Audio language: {lang_name(source_lang)}')
        if translate_to and translate_to != source_lang:
            print(f'  Translating to: {lang_name(translate_to)}')
    print('\n' + '=' * 60)

    # Early warning about missing ffmpeg — it's needed to merge formats (best
    # quality) and for subtitles. We don't abort: formats 18/22 and MP3 can work
    # without it.
    if not check_ffmpeg_installed():
        print('⚠️  ffmpeg not detected. Merging best-quality video+audio and '
              'generating subtitles require it.')
        print('   Install ffmpeg and add it to PATH (see the guide). Trying anyway...\n')

    # Make sure yt-dlp is available (install if needed)
    if not ensure_ytdlp():
        print('\n✗ Cannot download without yt-dlp. Aborting.')
        return

    downloaded_file = None

    if format_choice == 'mp4':
        strategies = [
            ('bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
             'Best MP4 with separate audio',
             ['--merge-output-format', 'mp4']),

            ('bestvideo+bestaudio/best',
             'Best quality (any codecs) with merging',
             ['--merge-output-format', 'mp4']),

            ('22',
             'Format 22 (720p MP4)',
             None),

            ('18',
             'Format 18 (360p MP4)',
             None),

            ('best[ext=mp4]',
             'Best available MP4',
             None),

            ('best',
             'Best available format (fallback)',
             ['--merge-output-format', 'mp4', '--recode-video', 'mp4']),
        ]

    elif format_choice == 'mp3':
        strategies = [
            ('bestaudio[ext=m4a]/bestaudio',
             'Best audio quality, converted to MP3',
             ['-x', '--audio-format', 'mp3', '--audio-quality', '0']),

            ('bestaudio',
             'Best audio quality (alternative conversion)',
             ['-x', '--audio-format', 'mp3']),

            ('140',
             'Format 140 (M4A) converted to MP3',
             ['-x', '--audio-format', 'mp3']),

            ('best',
             'Best format with audio extraction',
             ['-x', '--audio-format', 'mp3']),
        ]
    else:
        print('Unsupported format.')
        return

    # Try all strategies in order
    success, downloaded_file, output = attempt_all_strategies(
        url, strategies, output_template, output_dir, format_choice, cookies_from_browser
    )

    # If everything failed — diagnose and possibly update yt-dlp and retry once
    if not success:
        diag = diagnose_failure(output)
        print('\n' + '=' * 60)
        print('⚠️  All strategies failed.')
        print(f'Diagnosis: {diag["message"]}')
        print('=' * 60)

        if diag['suggest_update']:
            print('\n🔄 Trying to update yt-dlp and retry the download...')
            if update_ytdlp():
                success, downloaded_file, output = attempt_all_strategies(
                    url, strategies, output_template, output_dir, format_choice, cookies_from_browser
                )

    if success:
        print('\n' + '=' * 60)
        print('✓✓✓ DOWNLOADED SUCCESSFULLY ✓✓✓')
        print(f'Location: {output_dir}')
        if downloaded_file:
            print(f'File: {downloaded_file}')
        print('=' * 60)
    else:
        diag = diagnose_failure(output)
        print('\n' + '=' * 60)
        print('✗✗✗ ERROR: Could not download the video ✗✗✗')
        print(f'\nLikely cause: {diag["message"]}')
        print('\nWhat you can do:')
        print('- Check that the video opens in a browser')
        print('- "This video is not available" on watchable videos usually means a')
        print('  missing JavaScript runtime: install Deno (winget install DenoLand.Deno)')
        print('- For login/age-restricted videos: --cookies-from-browser <browser>')
        print('- For geo-blocking: turn on a VPN')
        print('- For HTTP 429: wait a few minutes')
        print('- Update manually: pip install -U yt-dlp')
        print('=' * 60)
        return

    # Resolve the absolute path of the downloaded file (used by subtitles + inserts)
    final_path = None
    if not downloaded_file:
        exts = MEDIA_EXTENSIONS.get(format_choice, ())
        media = [f for f in os.listdir(output_dir) if f.lower().endswith(exts)]
        if media:
            media.sort(key=lambda x: os.path.getmtime(os.path.join(output_dir, x)), reverse=True)
            downloaded_file = media[0]
    if downloaded_file:
        final_path = downloaded_file if os.path.isabs(downloaded_file) \
            else os.path.join(output_dir, downloaded_file)

    # Subtitle generation (MP4 only)
    if generate_subs and format_choice == 'mp4':
        if final_path:
            generate_subtitles_with_whisper(final_path, whisper_model, source_lang, translate_to,
                                            initial_prompt, output_dir, also_vtt, burn, embed)
        else:
            print('\n⚠️  Could not find the downloaded file to generate subtitles.')
    elif generate_subs and format_choice == 'mp3':
        print('\n⚠️  Subtitle generation is only available for the MP4 format.')

    # Insert / interstitial detection (and optional cutting)
    if inserts_opts:
        if final_path:
            process_inserts(final_path, url=url, output_dir=output_dir, **inserts_opts)
        else:
            print('\n⚠️  Could not find the downloaded file for insert detection.')


def prompt_output_dir():
    """
    Asks the user where to save the output files. Pressing Enter keeps the
    current folder. In a non-interactive context (no terminal) it silently
    returns the current folder so the script never hangs.
    """
    default = os.getcwd()
    if not sys.stdin or not sys.stdin.isatty():
        return default
    try:
        answer = input(f'\n📂 Where to save the output files?\n'
                       f'   [Enter = current folder: {default}]\n> ')
    except (EOFError, KeyboardInterrupt):
        print()
        return default
    # Trim whitespace and any quotes the user might have pasted around the path
    answer = answer.strip().strip('"').strip("'").strip()
    return answer or default


def main():
    parser = argparse.ArgumentParser(
        description='Download from YouTube as mp4 or mp3 with automatic error workarounds.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s "https://youtube.com/watch?v=..."
  %(prog)s "https://youtube.com/watch?v=..." --format mp3
  %(prog)s "https://youtube.com/watch?v=..." --subs
  %(prog)s "https://youtube.com/watch?v=..." --subs --model medium

  # Spanish video -> Polish subtitles (translation):
  %(prog)s "https://youtube.com/watch?v=..." --subs --source-lang es --translate-to pl

  # Auto-detect the original language -> Polish subtitles:
  %(prog)s "https://youtube.com/watch?v=..." --subs --source-lang auto --translate-to pl

  # Local file on disk -> translated Polish subtitles (no download):
  %(prog)s --file "C:\\videos\\clip.mp4" --source-lang es --translate-to pl

  # Age-restricted/login video (browser cookies) and a custom output folder:
  %(prog)s "https://youtube.com/watch?v=..." --cookies-from-browser chrome --output-dir "C:\\downloads"
        """
    )
    parser.add_argument('url', nargs='?', help='YouTube video link')
    parser.add_argument(
        '--file',
        help='Path to a video/audio file ALREADY on disk. Skips downloading and '
             'generates (and optionally translates) subtitles right away.'
    )
    parser.add_argument(
        '--format',
        choices=['mp4', 'mp3'],
        default='mp4',
        help='Download format (default: mp4)'
    )
    parser.add_argument(
        '--subs',
        action='store_true',
        help='Generate subtitles using Whisper AI (MP4 only). In the original '
             'language by default; use --translate-to to translate.'
    )
    parser.add_argument(
        '--model',
        default='base',
        help='Whisper model (default: base). Any name supported by Whisper, '
             'e.g.: tiny, base, small, medium, large, large-v3, turbo. '
             'Bigger = more accurate but slower. Newer models work automatically '
             'once they appear in the whisper library.'
    )
    parser.add_argument(
        '--source-lang',
        default='pl',
        help='Spoken language of the audio, e.g. es, en, de (default: pl). '
             'Use "auto" to let Whisper detect the language automatically.'
    )
    parser.add_argument(
        '--translate-to',
        default=None,
        help='Target subtitle language, e.g. pl. Enables translation (Google Translate). '
             'Without it, subtitles stay in the original language.'
    )
    parser.add_argument(
        '--prompt',
        default=None,
        help='Context hint for Whisper (initial_prompt): proper nouns, character '
             'names, terminology, correct spelling. Noticeably improves accuracy, '
             'e.g.: --prompt "Pomni, Ragatha, Jax, Caine, Gangle, Zooble".'
    )
    parser.add_argument(
        '--cookies-from-browser',
        default=None,
        metavar='BROWSER',
        help='Download using cookies from your browser (chrome, firefox, edge, '
             'brave, opera...). Fixes age-restricted / "confirm you are not a bot" videos.'
    )
    parser.add_argument(
        '--output-dir',
        default=None,
        metavar='DIR',
        help='Folder for output files (video and/or subtitles). If omitted, the script '
             'asks interactively (Enter = current directory).'
    )
    parser.add_argument(
        '--vtt',
        action='store_true',
        help='Also save subtitles in WebVTT format (.vtt), alongside .srt '
             '(useful for web players).'
    )
    parser.add_argument(
        '--burn',
        action='store_true',
        help='Burn subtitles permanently INTO the video (hardsub). Creates a new MP4 '
             'with subtitles baked into the picture (re-encodes — slower).'
    )
    parser.add_argument(
        '--embed',
        action='store_true',
        help='Embed subtitles as a soft, toggleable track in the MP4 (no re-encode '
             '— fast). The viewer can turn them on/off in the player.'
    )
    parser.add_argument(
        '--find-inserts',
        action='store_true',
        help='Detect short inserted clips / interstitials (SponsorBlock, then an '
             'audio-jump + scene-cut heuristic). Analysis only — prints/saves a cut list.'
    )
    parser.add_argument(
        '--cut-inserts',
        action='store_true',
        help='Like --find-inserts, but also removes the detected segments and saves a '
             'new video (asks for confirmation first; use --yes to skip the prompt).'
    )
    parser.add_argument(
        '--insert-ai',
        action='store_true',
        help='Cross-check heuristic insert candidates with a local Ollama model '
             '(keeps only confirmed ones). Ignored if Ollama is unavailable.'
    )
    parser.add_argument(
        '--insert-jump', type=float, default=8.0, metavar='LU',
        help='Loudness jump (LU) that flags an insert candidate (default: 8.0).'
    )
    parser.add_argument(
        '--insert-min-len', type=float, default=1.5, metavar='SEC',
        help='Minimum insert length in seconds (default: 1.5).'
    )
    parser.add_argument(
        '--insert-max-len', type=float, default=15.0, metavar='SEC',
        help='Maximum insert length in seconds (default: 15).'
    )
    parser.add_argument(
        '--insert-any-audio',
        action='store_true',
        help='Do not require a scene cut next to the audio jump (more candidates, '
             'more false positives).'
    )
    parser.add_argument(
        '--smart-inserts',
        action='store_true',
        help='Use the Gemini multimodal API to detect inserts (best for visual '
             'cutaways with no audio signature). Asks for an API key on first use '
             'and saves it. Combine with --cut-inserts to also remove them.'
    )
    parser.add_argument(
        '--smart-model',
        default='gemini-2.5-flash',
        metavar='MODEL',
        help='Gemini model for --smart-inserts (default: gemini-2.5-flash).'
    )
    parser.add_argument(
        '--insert-kinds',
        default='clip',
        metavar='KINDS',
        help='With --smart-inserts: which kinds of inserts to keep, comma-separated. '
             'Options: clip (other-video footage / meme / b-roll), screenshot '
             '(static image on screen), caption (editor text/graphics). '
             'Default: clip (real interstitials only). E.g. --insert-kinds clip,screenshot'
    )
    parser.add_argument(
        '--from-list',
        metavar='FILE',
        help='Cut from a previously saved/edited cut list (e.g. *_inserts.txt) '
             'instead of re-detecting. Skips any second Gemini call; delete lines '
             'in that file to drop segments. Use with --extract-inserts or --cut-inserts.'
    )
    parser.add_argument(
        '--extract-inserts',
        action='store_true',
        help='Save each detected insert as its own named clip (e.g. '
             '"03_02m11s_clip_Animated intro.mp4" — the type label is in the name) '
             'in a "<video>_clips" folder, ready to reuse in your own videos. '
             'Non-destructive; the original is untouched.'
    )
    parser.add_argument(
        '--clips-copy',
        action='store_true',
        help='With --extract-inserts: stream-copy clips (instant, no re-encode), but '
             'each clip starts at the nearest keyframe. Default re-encodes for '
             'frame-accurate boundaries.'
    )
    parser.add_argument(
        '--snap-cuts',
        action='store_true',
        help='Snap insert boundaries to the nearest detected scene cut for '
             'frame-accurate trims (refines the model\'s ±1 s timestamps).'
    )
    parser.add_argument(
        '--yes',
        action='store_true',
        help='Skip confirmation prompts (e.g. when cutting inserts).'
    )

    args = parser.parse_args()

    if not args.file and not args.url:
        parser.error('Provide a YouTube link or use --file with a path to a file on disk.')

    # Ask where to save results, unless already set via --output-dir (Enter = current folder)
    if not args.output_dir:
        args.output_dir = prompt_output_dir()

    # Bundle insert-detection options (only if requested)
    inserts_opts = None
    if (args.find_inserts or args.cut_inserts or args.smart_inserts
            or args.from_list or args.extract_inserts):
        inserts_opts = dict(
            do_cut=args.cut_inserts,
            use_ai=args.insert_ai,
            assume_yes=args.yes,
            jump_lu=args.insert_jump,
            min_len=args.insert_min_len,
            max_len=args.insert_max_len,
            require_scene_cut=not args.insert_any_audio,
            smart=args.smart_inserts,
            smart_model=args.smart_model,
            from_list=args.from_list,
            snap=args.snap_cuts,
            extract=args.extract_inserts,
            clips_copy=args.clips_copy,
            insert_kinds=tuple(k.strip().lower() for k in args.insert_kinds.split(',') if k.strip()) or ('clip',),
        )

    if args.file:
        # Local-file mode — no download
        if not os.path.exists(args.file):
            print(f'✗ File not found: {args.file}')
            sys.exit(1)
        # Generate subtitles unless the user asked ONLY for insert detection
        wants_subs = args.subs or bool(args.translate_to) or inserts_opts is None
        if wants_subs:
            generate_subtitles_with_whisper(args.file, args.model, args.source_lang,
                                            args.translate_to, args.prompt, args.output_dir,
                                            args.vtt, args.burn, args.embed)
        if inserts_opts:
            process_inserts(args.file, url=None, output_dir=args.output_dir, **inserts_opts)
    else:  # args.url
        download_youtube(args.url, args.format, args.subs, args.model,
                         args.source_lang, args.translate_to, args.prompt,
                         args.cookies_from_browser, args.output_dir, args.vtt,
                         args.burn, args.embed, inserts_opts)


if __name__ == '__main__':
    main()
