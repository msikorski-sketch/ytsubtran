
import re
import subprocess
import os
import argparse
import json
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
    """
    Czyszczenie tekstu i wyciąganie linku YouTube. Rozpoznaje linki watch,
    youtu.be, shorts, live i embed (te skrócone formy są kanonizowane do
    watch?v=..., żeby ochrona --no-playlist zawsze działała). Czysty link
    playlisty przechodzi bez zmian — patrz is_playlist_url().
    """
    clean_text = re.sub(r'[\[\]\(\)]', '', text).strip()
    match = re.search(r'(https?://(?:www\.|m\.)?youtube\.com/watch\?v=[0-9A-Za-z_-]{11})',
                      clean_text)
    if match:
        return match.group(1)
    match = re.search(r'https?://(?:youtu\.be/|(?:www\.|m\.)?youtube\.com/'
                      r'(?:shorts|live|embed)/)([0-9A-Za-z_-]{11})', clean_text)
    if match:
        return f'https://www.youtube.com/watch?v={match.group(1)}'
    return clean_text


# Wszystkie formy linków YouTube, w jakich trafiają do schowka / na Discorda
YOUTUBE_LINK_PATTERN = re.compile(
    r'(?:youtube\.com/(?:watch\?(?:[^\s&]*&)*v=|shorts/|live/|embed/)|youtu\.be/)'
    r'([0-9A-Za-z_-]{11})'
)


def extract_youtube_links(text):
    """
    Wyciąga WSZYSTKIE linki do filmów YouTube z dowolnego tekstu (np. bloku
    skopiowanego z Discorda), sprowadza je do postaci watch?v=... i usuwa
    duplikaty, zachowując kolejność. Czysta funkcja.
    """
    seen = []
    for video_id in YOUTUBE_LINK_PATTERN.findall(text or ''):
        url = f'https://www.youtube.com/watch?v={video_id}'
        if url not in seen:
            seen.append(url)
    return seen


def is_playlist_url(url):
    """
    Czy to link CAŁEJ playlisty (bez wskazanego filmu)? Dla takich linków
    yt-dlp IGNORUJE --no-playlist i pobiera wszystkie pozycje — skrypt musi
    o tym uprzedzić i pominąć kroki po-pobraniowe (napisy działają na jednym
    pliku, nie na całej liście).
    """
    u = (url or '').lower()
    return '/playlist' in u or ('list=' in u and 'v=' not in u)


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
    'yue': 'Cantonese', 'id': 'Indonesian', 'th': 'Thai', 'vi': 'Vietnamese',
    'hi': 'Hindi', 'ms': 'Malay', 'tl': 'Filipino', 'fa': 'Persian',
    'mk': 'Macedonian',
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


def _gemini_translation_prompt(texts, source_name, target_name, prev_pairs=None):
    """
    Buduje prompt do tłumaczenia segmentów napisów przez Gemini.
    Czysta funkcja (testowalna). `prev_pairs` to pary (oryginał, tłumaczenie)
    z końca poprzedniej partii — utrzymują spójność terminologii między partiami.
    """
    lines = [
        f'You are translating video subtitles from {source_name} to {target_name}.',
        'Rules:',
        '- Use the whole list as context (consistent terminology, natural grammar),',
        '  but translate each segment on its own.',
        f'- Return ONLY a JSON array of strings with exactly {len(texts)} items —',
        '  one translation per input segment, in the same order.',
        '- Never merge, split, reorder or drop segments.',
        '- Keep proper names, numbers and units accurate. Use concise, natural',
        '  subtitle phrasing.',
        '- Translate every segment that contains words — including single words and',
        '  short labels. Return a segment unchanged only when it contains no',
        '  translatable speech at all (music/noise markers, stray syllables).',
    ]
    if prev_pairs:
        lines.append('')
        lines.append('For consistency, the last translations from the previous part were:')
        for src, tgt in prev_pairs:
            lines.append(f'- {json.dumps(src, ensure_ascii=False)} -> '
                         f'{json.dumps(tgt, ensure_ascii=False)}')
    lines.append('')
    lines.append('Input segments (JSON):')
    lines.append(json.dumps(texts, ensure_ascii=False))
    return '\n'.join(lines)


def parse_gemini_translation(raw, expected_n):
    """
    Parsuje odpowiedź Gemini na listę `expected_n` tłumaczeń.
    Toleruje opakowanie w obiekt (np. {"translations": [...]}) i elementy
    nie-tekstowe (koercja na str). Zwraca listę str albo None, gdy odpowiedź
    nie daje się bezpiecznie dopasować 1:1 do wejścia.
    """
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if isinstance(data, dict):
        # Model czasem opakowuje wynik w obiekt — przyjmij pierwszą wartość-listę
        lists = [v for v in data.values() if isinstance(v, list)]
        data = lists[0] if len(lists) == 1 else None
    if not isinstance(data, list) or len(data) != expected_n:
        return None
    return [str(item).strip() if item is not None else '' for item in data]


def translate_texts_gemini(texts, target_lang, source_lang, model='gemini-2.5-flash',
                           batch_size=300):
    """
    Tłumaczy segmenty napisów przez Gemini — cały transkrypt z kontekstem,
    zamiast segment-po-segmencie jak Google Translate. Dłuższe materiały idą
    w partiach po `batch_size` segmentów, z końcówką poprzedniej partii jako
    kontekstem spójności.

    Zwraca listę tłumaczeń (1:1 z wejściem; puste odpowiedzi zastępuje
    oryginałem) albo None przy dowolnym problemie (brak SDK/klucza, błąd API,
    niesparsowalna odpowiedź) — wywołujący spada wtedy na Google Translate.
    """
    try:
        from google import genai
        from google.genai import types
    except ImportError:
        print('   (google-genai SDK not installed — pip install -U google-genai)')
        return None

    # Klucz API obsługiwany tak samo jak w --smart-inserts / --organize
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    try:
        import inserts
    except Exception:
        return None
    key = inserts.get_gemini_key()
    if not key:
        print('   (no Gemini API key available)')
        return None

    source_name = lang_name(source_lang)
    target_name = lang_name(target_lang)
    client = genai.Client(api_key=key)

    def translate_batch(batch, prev_pairs):
        prompt = _gemini_translation_prompt(batch, source_name, target_name, prev_pairs)
        resp = client.models.generate_content(
            model=model,
            contents=prompt,
            config=types.GenerateContentConfig(response_mime_type='application/json'),
        )
        return parse_gemini_translation(resp.text, len(batch))

    total = len(texts)
    batches = [texts[i:i + batch_size] for i in range(0, total, batch_size)]
    translated = []
    prev_pairs = None
    retried_key = False
    for bi, batch in enumerate(batches, 1):
        try:
            result = translate_batch(batch, prev_pairs)
        except Exception as e:
            # Nieważny/wygasły klucz: wyczyść i poproś o nowy (raz), jak w --organize
            if not retried_key and inserts._looks_like_bad_key(e):
                print('   Saved Gemini key is invalid/expired.')
                inserts.clear_gemini_key()
                key = inserts.get_gemini_key(force_prompt=True)
                if not key:
                    return None
                client = genai.Client(api_key=key)
                retried_key = True
                try:
                    result = translate_batch(batch, prev_pairs)
                except Exception as e2:
                    print(f'   Gemini request failed again: {e2}')
                    return None
            else:
                print(f'   Gemini request failed: {e}')
                return None
        if result is None:
            print('   Gemini returned a response that does not map 1:1 to the segments.')
            return None
        # Puste tłumaczenie -> zostaw oryginał (nie gubimy tekstu)
        result = [t if t.strip() else src for t, src in zip(result, batch)]
        translated.extend(result)
        if len(batches) > 1:
            print(f'   ...translated batch {bi}/{len(batches)} ({len(translated)}/{total} segments)')
        prev_pairs = list(zip(batch[-3:], result[-3:]))
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


def _import_qwen_engine():
    """
    Importuje lokalny moduł qwen_engine (leży obok tego pliku) — także wtedy,
    gdy skrypt uruchomiono przez zainstalowaną komendę konsolową.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    if here not in sys.path:
        sys.path.insert(0, here)
    import qwen_engine
    return qwen_engine


def generate_subtitles(video_file, model_size='base', source_lang='pl',
                       translate_to=None, initial_prompt=None, output_dir=None,
                       also_vtt=False, burn=False, embed=False,
                       engine='auto', qwen_model=None,
                       translate_engine='google', translate_model='gemini-2.5-flash'):
    """
    Generuje napisy wybranym silnikiem ASR, opcjonalnie z tłumaczeniem.

    Parametry:
    - video_file: ścieżka do pliku wideo
    - model_size: nazwa modelu Whisper (tiny, base, small, medium, large,
                  large-v3, turbo... — dowolna obsługiwana przez whisper)
    - source_lang: język audio w nagraniu (np. 'pl', 'es', 'en') lub 'auto'
                   do automatycznego wykrycia
    - translate_to: docelowy język napisów (np. 'pl'). Jeśli None, napisy
                    pozostają w języku oryginału (sama transkrypcja).
    - initial_prompt: podpowiedź kontekstowa (nazwy własne, terminologia) —
                      poprawia dokładność i pisownię (Whisper i Qwen).
    - output_dir: katalog na pliki napisów. Jeśli None, zapisuje obok pliku wideo.
    - also_vtt: dodatkowo zapisz napisy w formacie WebVTT (.vtt), nie tylko .srt.
    - burn: wtop napisy NA STAŁE w obraz (hardsub, ponowne kodowanie).
    - embed: osadź napisy jako miękką, przełączalną ścieżkę w MP4 (bez kodowania).
            Przy tłumaczeniu używana jest wersja przetłumaczona, inaczej oryginał.
    - engine: silnik transkrypcji:
        'auto'    — Qwen3-ASR, jeśli zainstalowany i jest GPU (wyższa jakość),
                    w przeciwnym razie Whisper;
        'whisper' — klasycznie, sam Whisper;
        'qwen'    — Qwen3-ASR + timestampy ForcedAlignera (11 języków, m.in.
                    japoński; dla pozostałych automatycznie tryb hybrydowy);
        'hybrid'  — znaczniki czasu z Whispera + tekst z Qwena, z raportem
                    zgodności obu silników (działa też dla polskiego).
    - qwen_model: nazwa modelu Qwen3-ASR (domyślnie Qwen/Qwen3-ASR-1.7B).
    - translate_engine: 'google' (deep-translator, darmowe, segment po segmencie)
                        lub 'gemini' (cały transkrypt z kontekstem — lepsza
                        spójność; wymaga klucza API; przy błędzie spada na Google).
    - translate_model: model Gemini dla translate_engine='gemini'.
    """
    print('\n' + '=' * 70)
    print('🎙️  GENERATING SUBTITLES')
    print('=' * 70)

    # ffmpeg jest potrzebny każdemu silnikowi (dekodowanie audio)
    if not check_ffmpeg_installed():
        print('✗ ffmpeg is not installed!')
        print('  Install ffmpeg so the audio can be processed.')
        return False

    print('✓ ffmpeg installed')

    # If translating — verify the translation library before transcription starts
    # (dla silnika 'gemini' deep-translator jest tylko awaryjnym zapasem)
    if translate_to and translate_to != source_lang:
        if check_translator_installed():
            print('✓ deep-translator installed')
        elif translate_engine == 'gemini':
            print('⚠️  deep-translator is not installed — there will be no Google '
                  'fallback if the Gemini translation fails.')
        else:
            show_translator_installation_guide()
            return False

    # Check whether the file exists
    if not os.path.exists(video_file):
        print(f'✗ File not found: {video_file}')
        return False

    # Wybór trybu: Qwen3-ASR (najlepsza jakość), Whisper albo hybryda.
    try:
        qe = _import_qwen_engine()
    except Exception:
        qe = None  # brak modułu qwen_engine.py — zachowanie jak dotąd (Whisper)
    if qe is not None:
        qwen_ok, _qwen_ver = qe.check_qwen_installed()
        mode, notes = qe.choose_asr_mode(engine, source_lang, qwen_ok, qe.cuda_available())
    else:
        mode, notes = 'whisper', []
    for note in notes:
        print(note)
    if mode == 'install-qwen':
        qe.show_qwen_installation_guide()
        return False

    engine_desc = {
        'whisper': f'Whisper ({model_size})',
        'qwen': 'Qwen3-ASR (ForcedAligner timestamps)',
        'hybrid': f'hybrid — Whisper ({model_size}) timing + Qwen3-ASR text',
    }
    print(f'\n📁 Video file: {video_file}')
    print(f'🧠 Engine: {engine_desc[mode]}')
    print(f'🌍 Audio language (transcription): {lang_name(source_lang)}')
    if translate_to and translate_to != source_lang:
        print(f'🔁 Translating subtitles to: {lang_name(translate_to)}')
    if initial_prompt:
        print(f'📝 Context prompt: "{initial_prompt}"')
    print()

    if qe is not None and not qwen_model:
        qwen_model = qe.DEFAULT_QWEN_MODEL

    try:
        transcription = None

        if mode == 'qwen':
            try:
                transcription = qe.transcribe_qwen_full(video_file, source_lang,
                                                        qwen_model, context=initial_prompt)
            except qe.AlignerUnsupported as e:
                print(f'ℹ️  The Qwen aligner cannot timestamp {lang_name(e.lang_code)} — '
                      'switching to hybrid mode (Whisper timing + Qwen text).\n')
                mode = 'hybrid'
                source_lang = e.lang_code
            except Exception as e:
                print(f'⚠️  Qwen3-ASR failed: {e}')
                print('   Falling back to Whisper.\n')
                mode = 'whisper'

        if mode in ('whisper', 'hybrid'):
            transcription = _transcribe_with_whisper(video_file, model_size,
                                                     source_lang, initial_prompt)
            if transcription is None:
                return False

        segments, full_text, detected_lang = transcription

        # Tryb hybrydowy: Qwen transkrybuje od nowa każdy segment Whispera
        # (lepszy tekst na sprawdzonych znacznikach czasu) i raportuje zgodność
        # obu silników. Błąd Qwena niczego nie psuje — zostaje wynik Whispera.
        if mode == 'hybrid':
            try:
                texts, report = qe.refine_segments_with_qwen(
                    video_file, segments, detected_lang, qwen_model,
                    context=initial_prompt)
                qe.print_refine_report(report)
                for seg, text in zip(segments, texts):
                    seg['text'] = text
                joiner = '' if detected_lang in qe.CJK_LANGS else ' '
                full_text = joiner.join(t.strip() for t in texts if t.strip())
            except Exception as e:
                print(f'⚠️  Qwen refinement failed ({e}) — keeping the Whisper text.\n')

        return _finalize_subtitle_files(video_file, segments, full_text, detected_lang,
                                        translate_to, output_dir, also_vtt, burn, embed,
                                        translate_engine, translate_model)

    except Exception as e:
        print(f'\n✗ Error while generating subtitles: {e}')
        print('\nCheck that:')
        print('- the video file is not corrupted')
        print('- you have enough RAM/VRAM (at least 2 GB free)')
        print('- ffmpeg is installed correctly')
        return False


def _transcribe_with_whisper(video_file, model_size, source_lang, initial_prompt):
    """
    Transkrypcja Whisperem: ładowanie modelu (z awaryjnym zejściem na CPU),
    wykrycie języka (dla 'auto') i transkrypcja z parametrami ograniczającymi
    halucynacje. Zwraca (segments, pełny_tekst, kod_języka) albo None przy błędzie.
    """
    # Check whether Whisper is installed
    whisper_installed, version = check_whisper_installed()
    if not whisper_installed:
        show_whisper_installation_guide()
        return None

    print(f'✓ Whisper installed (version: {version})')

    # Import Whisper
    try:
        import whisper
    except ImportError:
        show_whisper_installation_guide()
        return None

    print(f'🤖 Whisper model: {model_size}')

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
                return None
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
        return segments, result['text'], whisper_lang

    except Exception as e:
        print(f'\n✗ Whisper transcription failed: {e}')
        return None


def _finalize_subtitle_files(video_file, segments, full_text, detected_lang,
                             translate_to, output_dir, also_vtt, burn, embed,
                             translate_engine='google', translate_model='gemini-2.5-flash'):
    """
    Wspólny finał dla wszystkich silników ASR: zapis napisów w języku
    oryginału, opcjonalne tłumaczenie segmentów (Gemini z pełnym kontekstem
    lub Google Translate), zapis wersji przetłumaczonej oraz wtopienie/
    osadzenie napisów w wideo.
    """
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
        f.write(full_text)
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

        translated_texts = None
        if translate_engine == 'gemini':
            print(f'   Translator: Gemini ({translate_model}) — full-context translation')
            translated_texts = translate_texts_gemini(original_texts, translate_to,
                                                      source_for_translate, translate_model)
            if translated_texts is None:
                print('⚠️  Gemini translation failed — falling back to Google Translate.')
        if translated_texts is None:
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
    # Oficjalny skrypt yt-dlp rozwiązujący wyzwania JS YouTube'a (wymaga
    # zainstalowanego Deno). Bez niego yt-dlp używa okrojonego klienta
    # "android vr", który nie oferuje wszystkich formatów (np. avc1 dla
    # presetu --resolve). Ignorowane bezpiecznie, gdy Deno nie ma.
    '--remote-components', 'ejs:github',
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


def parse_timestamp(s):
    """
    Parse a timestamp like "90", "1:30", "01:30.5", "00:01:30.250" → seconds (float).
    Returns None when the input is empty or malformed.
    """
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    parts = s.split(':')
    try:
        nums = [float(p) for p in parts]
    except ValueError:
        return None
    if any(n < 0 for n in nums):
        return None
    if len(nums) == 1:
        return nums[0]
    if len(nums) == 2:
        return nums[0] * 60 + nums[1]
    if len(nums) == 3:
        return nums[0] * 3600 + nums[1] * 60 + nums[2]
    return None


def build_section_args(start_sec, end_sec, precise=False):
    """
    Build the yt-dlp args needed to download only the [start, end] fragment.
    Either bound may be None (open-ended). Returns [] when both are None.
    With precise=True adds `--force-keyframes-at-cuts` for frame-accurate trims
    (re-encodes around the boundary). Off by default because re-encoding the
    boundary chunk can desync audio on some YouTube streams; the default keeps
    audio/video in lockstep at the cost of starting at the nearest keyframe.
    """
    if start_sec is None and end_sec is None:
        return []
    s = f'{start_sec:.3f}' if start_sec is not None else '0'
    e = f'{end_sec:.3f}' if end_sec is not None else 'inf'
    args = ['--download-sections', f'*{s}-{e}']
    if precise:
        args.append('--force-keyframes-at-cuts')
    return args


def remux_for_resolve(filepath):
    """
    Stream-copy remux of an MP4 into a clean container that DaVinci Resolve
    (free, on Windows) reliably opens. Fixes:
      • edit lists left over from --download-sections (Resolve mis-syncs them),
      • negative starting timestamps after a mid-stream cut,
      • moov atom written at the END of the file (slow scrub / failed import).
    No re-encoding → no quality loss, finishes in ~1 s for a typical clip.
    Returns the (possibly unchanged) path.
    """
    if not filepath or not os.path.exists(filepath):
        return filepath
    if not filepath.lower().endswith('.mp4'):
        return filepath
    if not check_ffmpeg_installed():
        return filepath

    tmp = filepath + '.resolve.tmp.mp4'
    cmd = [
        'ffmpeg', '-y',
        '-i', filepath,
        '-map', '0',
        '-c', 'copy',
        '-movflags', '+faststart',
        '-avoid_negative_ts', 'make_zero',
        tmp,
    ]
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding='utf-8', errors='replace',
        )
    except FileNotFoundError:
        return filepath

    if result.returncode != 0 or not os.path.exists(tmp):
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        print('⚠️  DaVinci-Resolve remux step failed — keeping original file.')
        return filepath

    try:
        os.replace(tmp, filepath)
    except OSError:
        return filepath
    print('🎬 Remuxed for DaVinci Resolve compatibility (clean MP4, +faststart).')
    return filepath


def _parse_rate(rate):
    """Parsuje ffprobe'owy ułamek klatkarzu ('30000/1001') na float. 0.0 gdy brak/zła wartość."""
    try:
        if '/' in str(rate):
            num, den = str(rate).split('/', 1)
            num, den = float(num), float(den)
            return num / den if den else 0.0
        return float(rate)
    except (TypeError, ValueError):
        return 0.0


def _is_vfr(r_frame_rate, avg_frame_rate, tolerance=0.01):
    """
    Zmienna klatkarz (VFR)? Porównuje deklarowany (r_frame_rate) i faktyczny
    średni (avg_frame_rate) klatkarz strumienia — rozjazd > 1% oznacza VFR,
    który w DaVinci Resolve powoduje dryf audio na osi czasu.
    """
    r, avg = _parse_rate(r_frame_rate), _parse_rate(avg_frame_rate)
    if r <= 0 or avg <= 0:
        return False
    return abs(r - avg) / r > tolerance


def _is_high_depth(pix_fmt):
    """
    Czy format pikseli przekracza 8-bit 4:2:0 (10/12/16-bit albo chroma
    4:2:2 / 4:4:4)? Darmowy Resolve na Windows dekoduje H.264 tylko 8-bit,
    więc taki materiał wymaga DNxHR zamiast H.264 (żeby nie tracić głębi).
    """
    if not pix_fmt:
        return False
    p = pix_fmt.lower()
    return any(tag in p for tag in ('10', '12', '16', '422', '444'))


def resolve_transcode_plan(vcodec, pix_fmt, acodec, vfr=False):
    """
    Decyzja (czysta, testowalna): co przekodować, żeby plik montował się
    gładko w DaVinci Resolve (także darmowym, na Windows) — stan na
    oficjalną listę kodeków Resolve 20 (2026):

    - free/Windows dekoduje H.264 wyłącznie w profilach 8-bit; VP9/AV1/HEVC
      są czytane, ale long-GOP bez akceleracji tnie się na osi czasu,
      a starsze wersje Resolve (18/19) czytają je zawodnie → normalizujemy;
    - materiał 10/12-bit lub 4:2:2/4:4:4 → DNxHR HQX w MOV (natywny kodek
      Blackmagic, pełna głębia nawet w wersji free) + audio PCM;
    - materiał 8-bit → H.264 8-bit 4:2:0 MP4 + AAC;
    - Opus/Vorbis → AAC (Opus w MP4 czyta dopiero Resolve 20);
    - zmienna klatkarz (VFR) → CFR (inaczej dryfuje audio na timeline).

    Zwraca {'video': bool, 'audio': bool, 'target': 'h264'|'dnxhr',
            'reasons': [str, ...]}.
    """
    reasons = []
    target = 'dnxhr' if _is_high_depth(pix_fmt) else 'h264'

    video = False
    if target == 'dnxhr':
        video = True
        reasons.append(f'{vcodec} {pix_fmt} (10-bit/4:2:2 class) → DNxHR HQX '
                       '(free Resolve on Windows decodes H.264 only in 8-bit)')
    elif vcodec != 'h264':
        video = True
        reasons.append(f'video codec "{vcodec}" → H.264 8-bit (smooth timeline, '
                       'works in every Resolve version)')
    elif pix_fmt and pix_fmt not in ('yuv420p', 'yuvj420p'):
        video = True
        reasons.append(f'pixel format "{pix_fmt}" → yuv420p (8-bit 4:2:0)')
    elif vfr:
        video = True
        reasons.append('variable frame rate → constant (CFR) — VFR drifts audio in Resolve')

    if target == 'dnxhr':
        # Kontener MOV z DNxHR dostaje montażowy standard: audio PCM
        audio = acodec is not None and not str(acodec).startswith('pcm_')
        if audio:
            reasons.append(f'audio "{acodec}" → PCM 16-bit (editing standard for MOV)')
    else:
        audio = acodec is not None and acodec not in (
            'aac', 'mp3', 'pcm_s16le', 'pcm_s24le', 'pcm_s16be', 'pcm_s24be')
        if audio:
            reasons.append(f'audio codec "{acodec}" → AAC')

    return {'video': video, 'audio': audio, 'target': target, 'reasons': reasons}


def _ffprobe_streams(path):
    """
    Odczytuje ffprobe'em parametry pierwszego strumienia wideo i audio.
    Zwraca {'vcodec','pix_fmt','acodec','vfr'} albo None przy błędzie.
    """
    cmd = ['ffprobe', '-v', 'error', '-print_format', 'json', '-show_streams', path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True,
                                encoding='utf-8', errors='replace')
    except FileNotFoundError:
        return None
    if result.returncode != 0:
        return None
    try:
        streams = json.loads(result.stdout).get('streams', [])
    except (ValueError, AttributeError):
        return None
    video = next((s for s in streams if s.get('codec_type') == 'video'), None)
    audio = next((s for s in streams if s.get('codec_type') == 'audio'), None)
    if video is None:
        return None
    return {
        'vcodec': video.get('codec_name'),
        'pix_fmt': video.get('pix_fmt'),
        'acodec': audio.get('codec_name') if audio else None,
        'vfr': _is_vfr(video.get('r_frame_rate'), video.get('avg_frame_rate')),
    }


def ensure_resolve_ready(path, in_place=True):
    """
    Preset DaVinci Resolve: gwarantuje plik, który darmowy Resolve na Windows
    otwiera z pełnym obrazem i dźwiękiem (H.264 8-bit 4:2:0, CFR, AAC, MP4).

    Sprawdza kodeki ffprobe'em i przekoduje TYLKO to, co konieczne (np. sam
    dźwięk Opus→AAC przy skopiowaniu wideo 1:1). Gdy plik już jest zgodny —
    nic nie robi. `in_place=True` podmienia plik (ścieżka pobierania);
    `in_place=False` zapisuje obok jako <nazwa>_resolve.mp4 i zostawia
    oryginał (tryb --file). Zwraca ścieżkę pliku zdatnego dla Resolve.
    """
    if not path or not os.path.exists(path):
        return path
    if not check_ffmpeg_installed():
        print('⚠️  DaVinci preset: ffmpeg is missing — cannot verify/transcode the file.')
        return path

    info = _ffprobe_streams(path)
    if info is None:
        print('⚠️  DaVinci preset: could not read stream info (ffprobe) — leaving file as is.')
        return path

    plan = resolve_transcode_plan(info['vcodec'], info['pix_fmt'],
                                  info['acodec'], info['vfr'])
    if not plan['video'] and not plan['audio']:
        print('🎬 DaVinci preset: file is already Resolve-ready '
              '(H.264 8-bit 4:2:0 + AAC, constant frame rate).')
        return path

    print('🎬 DaVinci preset: transcoding for Resolve compatibility:')
    for reason in plan['reasons']:
        print(f'   • {reason}')

    base = os.path.splitext(path)[0]
    if plan['target'] == 'dnxhr':
        # DNxHR żyje w kontenerze MOV — zawsze nowy plik obok (inne rozszerzenie)
        out = f'{base}_resolve.mov'
        tmp = out
        in_place = False
    elif in_place:
        out = path
        tmp = f'{base}.resolve.tmp.mp4'
    else:
        out = f'{base}_resolve.mp4'
        tmp = out

    cmd = ['ffmpeg', '-y', '-i', path, '-map', '0:v:0', '-map', '0:a:0?']
    if plan['target'] == 'dnxhr':
        cmd += ['-c:v', 'dnxhd', '-profile:v', 'dnxhr_hqx', '-pix_fmt', 'yuv422p10le']
        cmd += ['-c:a', 'pcm_s16le'] if plan['audio'] else ['-c:a', 'copy']
    else:
        if plan['video']:
            cmd += ['-c:v', 'libx264', '-crf', '18', '-preset', 'medium',
                    '-pix_fmt', 'yuv420p']
        else:
            cmd += ['-c:v', 'copy']
        cmd += ['-c:a', 'aac', '-b:a', '320k'] if plan['audio'] else ['-c:a', 'copy']
        cmd += ['-movflags', '+faststart']
    if plan['video'] and info['vfr']:
        cmd += ['-fps_mode', 'cfr']
    cmd += ['-avoid_negative_ts', 'make_zero', tmp]

    if plan['video']:
        print('   (re-encoding video — this may take a while)')
    rc, ffmpeg_output = run_ffmpeg(cmd)
    if rc != 0 or not os.path.exists(tmp):
        try:
            if tmp != out and os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass
        print('⚠️  DaVinci preset: transcode failed — keeping the original file.')
        tail = '\n'.join((ffmpeg_output or '').strip().splitlines()[-4:])
        if tail:
            print(tail)
        return path

    if in_place:
        try:
            os.replace(tmp, out)
        except OSError:
            return tmp
    if plan['target'] == 'dnxhr':
        print('   ℹ️  DNxHR is an editing (intra-frame) codec — the file is much '
              'larger than the source, but scrubs perfectly and keeps full bit depth.')
    print(f'✓ DaVinci preset: Resolve-ready file: {out}')
    return out


def try_download(url, format_spec, format_name, output_template, extra_args, output_dir,
                 format_choice, cookies_from_browser=None):
    """
    Próbuje pobrać wideo z danym formatem.
    Zwraca: (success, ścieżka_pliku_lub_None, output)

    Ścieżkę ukończonego pliku podaje SAM yt-dlp (--print-to-file
    after_move:filepath) — bez zgadywania po zawartości katalogu i dacie
    modyfikacji, które przy playlistach / ponownych pobraniach potrafiły
    wskazać stary plik. Stara detekcja zostaje tylko jako fallback.
    """
    import tempfile
    fd, print_file = tempfile.mkstemp(prefix='ytsubtran_paths_', suffix='.txt')
    os.close(fd)

    command = [
        sys.executable, '-m', 'yt_dlp',
        '-f', format_spec,
        '-o', output_template,
        # --print-to-file samo w sobie włączyłoby tryb symulacji — --no-simulate
        # gwarantuje, że pobieranie faktycznie nastąpi.
        '--no-simulate',
        '--print-to-file', 'after_move:filepath', print_file,
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
    try:
        returncode, output = run_ytdlp(command)

        if returncode == 0:
            filename = None
            for line in reversed(_read_printed_paths(print_file)):
                if os.path.exists(line):
                    filename = line  # ostatni ukończony plik (ścieżka absolutna)
                    break
            if not filename:
                filename = detect_downloaded_file(before_files, output_dir, format_choice)
            print(f'✓ Success! Downloaded using: {format_name}')
            return True, filename, output

        print(f'✗ Failed: {format_name}')
        return False, None, output
    finally:
        try:
            os.remove(print_file)
        except OSError:
            pass


def _read_printed_paths(print_file):
    """Niepuste linie z pliku --print-to-file (jedna ścieżka na ukończony plik)."""
    try:
        with open(print_file, encoding='utf-8') as f:
            return [line.strip() for line in f if line.strip()]
    except OSError:
        return []


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


# ---------------------------------------------------------------------------
# Duplicate detection — never download the same video twice
# ---------------------------------------------------------------------------

# Indeks pobrań leży W KATALOGU DOCELOWYM, więc "już pobrane" znaczy zawsze
# "jest w tym folderze" — folder pozostaje samowystarczalny (można go przenieść
# razem z historią pobrań).
DOWNLOAD_INDEX_NAME = '.ytsubtran_downloads.json'
# Archiwum yt-dlp (jego własny mechanizm) — używane tylko w trybie playlisty,
# gdzie pomijanie musi działać per pozycja listy, wewnątrz jednego wywołania.
DOWNLOAD_ARCHIVE_NAME = '.ytsubtran_archive.txt'


def video_id_from_url(url):
    """11-znakowy identyfikator filmu YouTube z adresu, albo None."""
    match = re.search(r'[?&]v=([0-9A-Za-z_-]{11})', url or '')
    return match.group(1) if match else None


def section_signature(section_args):
    """
    Podpis zakresu pobrania: 'full' dla całego filmu albo np. '10.000-25.000'.
    Fragment NIE jest duplikatem całego filmu (ani innego fragmentu).
    """
    for i, arg in enumerate(section_args or []):
        if arg == '--download-sections' and i + 1 < len(section_args):
            return str(section_args[i + 1]).lstrip('*')
    return 'full'


def download_key(video_id, format_choice, section_args=None):
    """
    Klucz duplikatu: ten sam film + ten sam format + ten sam zakres.
    Czysta funkcja (testowalna).
    """
    return f'{video_id}|{format_choice}|{section_signature(section_args)}'


def fragment_suffix(section_args):
    """
    Dopisek do nazwy pliku dla pobrania fragmentu, np. ' [5-12s]'. Pusty dla
    całego filmu. Dzięki temu dwa różne fragmenty tego samego filmu NIE trafiają
    na tę samą nazwę — yt-dlp pominąłby wtedy drugi jako „już pobrany", a plik
    zawierałby inny materiał, niż mówi indeks. Czysta funkcja.
    """
    signature = section_signature(section_args)
    if signature == 'full':
        return ''

    def trim(value):
        try:
            number = float(value)
        except ValueError:
            return value
        return str(int(number)) if number == int(number) else f'{number:g}'

    start, _, end = signature.partition('-')
    end = 'end' if end in ('inf', '') else trim(end)
    return f' [{trim(start)}-{end}s]'


def download_index_path(output_dir):
    return os.path.join(output_dir, DOWNLOAD_INDEX_NAME)


def load_download_index(output_dir):
    """Czyta indeks pobrań z katalogu docelowego. {} przy braku/uszkodzeniu."""
    try:
        with open(download_index_path(output_dir), encoding='utf-8') as f:
            data = json.load(f) or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_download_index(output_dir, index):
    """Zapisuje indeks pobrań. Zwraca True/False (brak zapisu nie jest błędem krytycznym)."""
    try:
        with open(download_index_path(output_dir), 'w', encoding='utf-8') as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def verify_download_entry(entry, output_dir, exists=os.path.exists, size_of=os.path.getsize):
    """
    Sprawdza, czy zapisane pobranie NAPRAWDĘ jest na dysku i jest kompletne.
    Czysta logika (I/O wstrzykiwane, więc testowalna bez plików).

    Zwraca (status, ścieżka):
      'ok'         — plik istnieje i ma dokładnie zapisany rozmiar → duplikat,
      'missing'    — wpis jest, pliku nie ma (skasowany/przeniesiony) → pobierz ponownie,
      'incomplete' — plik pusty albo obok leży niedokończony .part → pobierz ponownie,
      'modified'   — plik istnieje, ale rozmiar się nie zgadza (np. późniejszy
                     remux/--resolve) → wywołujący potwierdza go ffprobe'em.
    """
    name = (entry or {}).get('file')
    if not name:
        return 'missing', None
    path = name if os.path.isabs(name) else os.path.join(output_dir, name)
    if not exists(path):
        return 'missing', path
    if exists(path + '.part'):
        return 'incomplete', path
    try:
        actual = size_of(path)
    except OSError:
        return 'missing', path
    if actual <= 0:
        return 'incomplete', path
    recorded = entry.get('size')
    if isinstance(recorded, int) and recorded > 0 and actual != recorded:
        return 'modified', path
    return 'ok', path


def _human_size(num_bytes):
    """Rozmiar pliku w czytelnej postaci (np. '1.2 GB')."""
    size = float(num_bytes or 0)
    for unit in ('B', 'KB', 'MB', 'GB'):
        if size < 1024 or unit == 'GB':
            return f'{size:.1f} {unit}' if unit != 'B' else f'{int(size)} B'
        size /= 1024


def record_download(output_dir, key, video_id, url, path, format_choice, section_args):
    """Zapisuje ukończone pobranie w indeksie katalogu docelowego."""
    from datetime import datetime
    try:
        size = os.path.getsize(path)
    except OSError:
        return False
    index = load_download_index(output_dir)
    index[key] = {
        'video_id': video_id,
        'url': url,
        # Ścieżka względna, dopóki plik leży w katalogu docelowym — dzięki temu
        # przeniesienie całego folderu nie unieważnia indeksu.
        'file': os.path.basename(path) if os.path.dirname(os.path.abspath(path))
        == os.path.abspath(output_dir) else path,
        'size': size,
        'format': format_choice,
        'section': section_signature(section_args),
        'downloaded_at': datetime.now().isoformat(timespec='seconds'),
    }
    return save_download_index(output_dir, index)


def expected_media_duration(video_duration, section_args=None):
    """
    Ile sekund powinien mieć pobrany plik: cały film albo długość fragmentu.
    Czysta funkcja; 0.0 gdy nie da się tego ustalić.
    """
    signature = section_signature(section_args)
    if signature == 'full':
        return float(video_duration or 0.0)
    start, _, end = signature.partition('-')
    try:
        start = float(start)
        if end in ('inf', ''):
            return max(0.0, float(video_duration or 0.0) - start) if video_duration else 0.0
        return max(0.0, float(end) - start)
    except ValueError:
        return 0.0


def probe_video_duration(url, cookies_from_browser=None):
    """Długość filmu wg YouTube (sekundy) — bez pobierania. 0.0 przy błędzie."""
    command = [sys.executable, '-m', 'yt_dlp', '--skip-download', '--no-warnings',
               '--print', '%(duration)s', '--no-playlist']
    if cookies_from_browser:
        command.extend(['--cookies-from-browser', cookies_from_browser])
    command.append(url)
    try:
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=120)
        return float((result.stdout or '').strip().splitlines()[-1])
    except Exception:
        return 0.0


def durations_match(actual, expected, fragment=False, keyframe_slack=15.0):
    """
    Czy plik o długości `actual` odpowiada oczekiwanym `expected` sekundom?
    Czysta funkcja; None, gdy którakolwiek długość jest nieznana.

    Fragmenty pobierane bez --precise-cut zaczynają się od najbliższej klatki
    kluczowej, więc bywają o kilka sekund DŁUŻSZE od żądanego zakresu — stąd
    osobna, luźniejsza tolerancja (wciąż wyłapuje zupełnie inny materiał).
    """
    if actual <= 0 or expected <= 0:
        return None
    if fragment:
        return -1.0 <= (actual - expected) <= keyframe_slack
    return abs(actual - expected) <= max(2.0, expected * 0.02)


def existing_file_matches_video(path, url, section_args=None, cookies_from_browser=None):
    """
    Czy plik, który już leżał w katalogu (yt-dlp pominął pobranie, bo nazwa się
    zgadzała), to NA PEWNO ten film? Porównuje długość pliku z długością
    zgłaszaną przez YouTube. Zwraca True / False / None (nie dało się ustalić).
    """
    expected = expected_media_duration(probe_video_duration(url, cookies_from_browser),
                                       section_args)
    if expected <= 0:
        return None
    try:
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        import inserts
        actual = inserts.ffprobe_duration(path)
    except Exception:
        return None
    return durations_match(actual, expected,
                           fragment=section_signature(section_args) != 'full')


def check_already_downloaded(url, output_dir, format_choice, section_args=None):
    """
    Czy ten film (w tym formacie i zakresie) jest już pobrany w tym katalogu?

    Dopasowanie po IDENTYFIKATORZE filmu z YouTube — nie po nazwie pliku, bo
    tytuły bywają zmieniane. Zwraca (ścieżka_istniejącego_pliku | None, klucz).
    Wypisuje powód pominięcia albo powód ponownego pobrania.
    """
    video_id = video_id_from_url(url)
    if not video_id:
        return None, None  # nie umiemy wskazać filmu — nie ryzykujemy pominięcia

    key = download_key(video_id, format_choice, section_args)
    entry = load_download_index(output_dir).get(key)
    if not entry:
        return None, key

    status, path = verify_download_entry(entry, output_dir)

    if status == 'modified':
        # Plik żyje, ale ma inny rozmiar niż w chwili pobrania (typowo: późniejszy
        # remux / --resolve). Potwierdzamy ffprobe'em, że jest sprawny.
        duration = 0.0
        try:
            here = os.path.dirname(os.path.abspath(__file__))
            if here not in sys.path:
                sys.path.insert(0, here)
            import inserts
            duration = inserts.ffprobe_duration(path)
        except Exception:
            duration = 0.0
        if duration > 0:
            status = 'ok'
        else:
            print(f'\n♻️  Recorded as downloaded, but the file cannot be read '
                  f'({path}) — downloading it again.')
            return None, key

    if status == 'missing':
        print(f'\n♻️  This video was downloaded before, but the file is no longer there '
              f'({path}) — downloading it again.')
        return None, key
    if status == 'incomplete':
        print(f'\n♻️  A previous download of this video is incomplete '
              f'({path}) — downloading it again.')
        return None, key

    section = entry.get('section', 'full')
    print('\n' + '=' * 70)
    print('⏭️  ALREADY DOWNLOADED — skipping the download')
    print('=' * 70)
    print(f'Video id:   {video_id}  (matched by YouTube id, not by file name)')
    print(f'File:       {path}')
    print(f'Size:       {_human_size(entry.get("size"))}')
    if entry.get('downloaded_at'):
        print(f'Downloaded: {entry["downloaded_at"].replace("T", " ")}')
    print(f'Match:      same video + same format ({entry.get("format", format_choice)}) '
          f'+ same range ({"whole video" if section == "full" else section + " s"})')
    print('\nUse --force-download to download it again anyway.')
    print('=' * 70)
    return path, key


def download_youtube(raw_url, format_choice='mp4', generate_subs=False, whisper_model='base',
                     source_lang='pl', translate_to=None, initial_prompt=None,
                     cookies_from_browser=None, output_dir=None, also_vtt=False,
                     burn=False, embed=False, inserts_opts=None, section_args=None,
                     engine='auto', qwen_model=None,
                     translate_engine='google', translate_model='gemini-2.5-flash',
                     resolve=False, force_download=False):
    url = extract_url(raw_url)
    if not url:
        print('No valid YouTube link detected.')
        return

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    else:
        output_dir = os.getcwd()
    # Fragmenty dostają zakres w nazwie — inaczej drugi fragment tego samego
    # filmu trafiłby na istniejącą nazwę i yt-dlp pominąłby pobranie.
    output_template = os.path.join(
        output_dir, f'%(title)s{fragment_suffix(section_args)}.%(ext)s')

    # Czysty link playlisty: yt-dlp pobierze WSZYSTKIE pozycje (--no-playlist
    # chroni tylko linki do pojedynczego filmu z doklejonym &list=...).
    playlist_mode = is_playlist_url(url)
    if playlist_mode:
        print('\n📃 Playlist link detected — every item on the list will be downloaded.')
        print('   Post-processing (subtitles / --resolve normalization / inserts) works')
        print('   on a single file, so it is SKIPPED in playlist mode. To use those,')
        print('   pass a link to one video (the part before "&list=" in the address).')

    print(f'\nDownloading from: {url}')
    print(f'Output directory: {output_dir}')
    print(f'Format: {format_choice}')
    if resolve and format_choice == 'mp4':
        print('DaVinci preset: ON — preferring native H.264+AAC, then verifying '
              'codecs and normalizing only if needed')
    elif resolve:
        print('ℹ️  --resolve only affects video (mp4) downloads — ignored for mp3.')
    if generate_subs:
        print(f'Generating subtitles: YES (model: {whisper_model})')
        print(f'  Audio language: {lang_name(source_lang)}')
        if translate_to and translate_to != source_lang:
            print(f'  Translating to: {lang_name(translate_to)}')
    print('\n' + '=' * 60)

    # Wykrywanie duplikatów: ten sam film (po ID z YouTube), format i zakres.
    # Pomijamy TYLKO wtedy, gdy plik naprawdę leży na dysku i jest kompletny.
    video_id = video_id_from_url(url)
    dup_key = download_key(video_id, format_choice, section_args) if video_id else None
    if dup_key and not playlist_mode:
        if force_download:
            print('🔁 --force-download: duplicate check skipped, downloading again.')
        else:
            existing_path, _ = check_already_downloaded(url, output_dir, format_choice,
                                                        section_args)
            if existing_path:
                # Plik już jest — pomijamy pobieranie, ale dalsze kroki (napisy,
                # preset DaVinci, inserty) wykonujemy na istniejącym pliku.
                final_path = _post_process_download(
                    existing_path, url=url, output_dir=output_dir,
                    format_choice=format_choice, resolve=resolve,
                    generate_subs=generate_subs, whisper_model=whisper_model,
                    source_lang=source_lang, translate_to=translate_to,
                    initial_prompt=initial_prompt, also_vtt=also_vtt, burn=burn,
                    embed=embed, engine=engine, qwen_model=qwen_model,
                    translate_engine=translate_engine, translate_model=translate_model,
                    inserts_opts=inserts_opts,
                )
                # Normalizacja (--resolve) mogła zmienić plik/rozmiar — odśwież wpis.
                if final_path:
                    record_download(output_dir, dup_key, video_id, url, final_path,
                                    format_choice, section_args)
                return

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
        ]
        if resolve:
            # Preset DaVinci: najpierw natywne H.264 (avc1) + AAC — zero
            # transkodowania, Resolve czyta to gładko w każdej wersji. YouTube
            # daje avc1 zwykle do 1080p; wyższe rozdzielczości (VP9/AV1) złapie
            # kolejna strategia, a normalizację zrobi ensure_resolve_ready().
            strategies.insert(0, (
                'bestvideo[vcodec^=avc1]+bestaudio[acodec^=mp4a]/best[vcodec^=avc1]',
                'Native H.264 (avc1) + AAC — DaVinci Resolve preset',
                ['--merge-output-format', 'mp4']))
        strategies += [

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

    # --force-download: yt-dlp samo pomija pliki o istniejącej nazwie, więc żeby
    # wymuszenie faktycznie coś zrobiło, każemy mu nadpisać plik.
    if force_download:
        strategies = [
            (spec, name, list(extra or []) + ['--force-overwrites'])
            for spec, name, extra in strategies
        ]

    # Tryb playlisty: pomijanie już pobranych pozycji musi działać per film
    # wewnątrz jednego wywołania, więc korzystamy z własnego mechanizmu yt-dlp
    # (archiwum identyfikatorów w katalogu docelowym).
    if playlist_mode and not force_download:
        archive = os.path.join(output_dir, DOWNLOAD_ARCHIVE_NAME)
        strategies = [
            (spec, name, list(extra or []) + ['--download-archive', archive])
            for spec, name, extra in strategies
        ]
        print(f'\n🗂️  Playlist duplicate check: items listed in {archive} are skipped.')
        print('   (--force-download ignores the archive and re-downloads everything)')

    # Fragment download (--start/--end): append --download-sections to every
    # strategy's extra args so the fallback chain still works.
    if section_args:
        strategies = [
            (spec, name, list(extra or []) + list(section_args))
            for spec, name, extra in strategies
        ]
        print(f'\n✂️  Fragment download: yt-dlp args → {" ".join(section_args)}')

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

    # Tryb playlisty: pliki pobrane, ale kroki po-pobraniowe (napisy, --resolve,
    # inserty) działają na pojedynczym pliku — kończymy z czytelnym komunikatem.
    if playlist_mode:
        if generate_subs or inserts_opts or resolve:
            print('\nℹ️  Playlist mode: subtitles / --resolve / insert detection were '
                  'skipped. Re-run with --file "<path>" for individual files.')
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

    final_path = _post_process_download(
        final_path, url=url, output_dir=output_dir, format_choice=format_choice,
        resolve=resolve, generate_subs=generate_subs, whisper_model=whisper_model,
        source_lang=source_lang, translate_to=translate_to, initial_prompt=initial_prompt,
        also_vtt=also_vtt, burn=burn, embed=embed, engine=engine, qwen_model=qwen_model,
        translate_engine=translate_engine, translate_model=translate_model,
        inserts_opts=inserts_opts,
    )

    # Zapisz pobranie w indeksie katalogu (po wszystkich krokach, żeby zapamiętany
    # rozmiar odpowiadał plikowi, który faktycznie został na dysku).
    if final_path and dup_key:
        # yt-dlp mogło pominąć pobieranie, bo plik o TAKIEJ NAZWIE już tam był
        # (np. z wcześniejszej wersji skryptu albo z innego narzędzia). Wtedy nie
        # wolno bezrefleksyjnie zapisać go jako „ten film" — najpierw sprawdzamy
        # długością, czy to na pewno ten materiał.
        if 'has already been downloaded' in (output or '').lower():
            print('\nℹ️  A file with this name was already in the folder — yt-dlp did not '
                  're-download it.')
            print('   Verifying that it really is this video (comparing durations)...')
            verdict = existing_file_matches_video(final_path, url, section_args,
                                                  cookies_from_browser)
            if verdict is True:
                print('   ✓ Length matches — recorded as this video.')
            else:
                print('   ⚠️  Could not confirm it is this video'
                      + (' (the length does NOT match).' if verdict is False else '.'))
                print('   Not recording it, so nothing is skipped by mistake next time.')
                print('   Use --force-download (or a different --output-dir) to fetch a '
                      'fresh copy.')
                return
        record_download(output_dir, dup_key, video_id_from_url(url), url,
                        final_path, format_choice, section_args)


def _post_process_download(final_path, url, output_dir, format_choice, resolve,
                           generate_subs, whisper_model, source_lang, translate_to,
                           initial_prompt, also_vtt, burn, embed, engine, qwen_model,
                           translate_engine, translate_model, inserts_opts):
    """
    Kroki po pobraniu, wspólne dla świeżo pobranego pliku i dla pliku, który już
    był na dysku (wykryty duplikat): remux/preset DaVinci, napisy, inserty.
    Zwraca finalną ścieżkę pliku (może się zmienić przy normalizacji --resolve).
    """
    # Make every MP4 we hand off downstream readable by DaVinci Resolve (free):
    # stream-copy remux that drops edit lists, fixes negative timestamps and
    # moves the moov atom to the front. Especially important for fragment
    # downloads (--start/--end), where yt-dlp leaves an edit-list MP4 that
    # Resolve mis-syncs.
    if final_path and format_choice == 'mp4':
        final_path = remux_for_resolve(final_path)
        # Preset DaVinci: zweryfikuj kodeki i znormalizuj plik tylko w razie
        # potrzeby (np. VP9/Opus po strategii awaryjnej, materiał 10-bit).
        if resolve:
            final_path = ensure_resolve_ready(final_path, in_place=True)

    # Subtitle generation (MP4 only)
    if generate_subs and format_choice == 'mp4':
        if final_path:
            generate_subtitles(final_path, whisper_model, source_lang, translate_to,
                               initial_prompt, output_dir, also_vtt, burn, embed,
                               engine=engine, qwen_model=qwen_model,
                               translate_engine=translate_engine,
                               translate_model=translate_model)
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

    return final_path


# Wspólny plik ustawień narzędzia (dzielony z kluczem Gemini z inserts.py)
APP_CONFIG_PATH = os.path.join(os.path.expanduser('~'), '.ytsubtran.json')


def load_app_config():
    """Czyta ~/.ytsubtran.json. Zwraca {} przy braku/uszkodzeniu pliku."""
    try:
        with open(APP_CONFIG_PATH, encoding='utf-8') as f:
            data = json.load(f) or {}
            return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def save_app_config_value(key, value):
    """
    Zapisuje jedno ustawienie do ~/.ytsubtran.json, scalając z istniejącą
    zawartością (nie niszczy np. zapisanego klucza Gemini). Zwraca True/False.
    """
    cfg = load_app_config()
    cfg[key] = value
    try:
        with open(APP_CONFIG_PATH, 'w', encoding='utf-8') as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        return True
    except OSError:
        return False


def resolve_default_output_dir():
    """
    Zwraca zapisaną domyślną lokalizację pobierania (default_output_dir) albo
    None. Gdy zapisany katalog nie istnieje, próbuje go utworzyć; przy
    niepowodzeniu (np. odpięty dysk) zwraca None z ostrzeżeniem.
    """
    saved = load_app_config().get('default_output_dir')
    if not saved:
        return None
    try:
        os.makedirs(saved, exist_ok=True)
        return saved
    except OSError as e:
        print(f'⚠️  Saved default output folder is unavailable ({saved}): {e}')
        print('   Falling back to asking/current folder. Set a new one with --set-output-dir.')
        return None


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


# ---------------------------------------------------------------------------
# Clipboard watch mode — copy a link, the download starts by itself
# ---------------------------------------------------------------------------

def _pick_clipboard_reader():
    """
    Zwraca funkcję czytającą schowek. Kolejno próbuje: pyperclip (jeśli jest),
    tkinter (biblioteka standardowa) i wreszcie narzędzie systemowe. Zwraca
    None, gdy żadna metoda nie działa.
    """
    try:
        import pyperclip
        pyperclip.paste()  # sprawdź, że backend faktycznie działa
        return pyperclip.paste
    except Exception:
        pass

    try:
        import tkinter
        root = tkinter.Tk()
        root.withdraw()

        def read_tk():
            root.update()
            try:
                return root.clipboard_get()
            except tkinter.TclError:
                return ''  # pusty schowek albo zawartość nietekstowa (np. obraz)

        read_tk()
        return read_tk
    except Exception:
        pass

    if sys.platform == 'win32':
        command = ['powershell', '-NoProfile', '-Command', 'Get-Clipboard -Raw']
    elif sys.platform == 'darwin':
        command = ['pbpaste']
    else:
        command = ['xclip', '-selection', 'clipboard', '-o']

    def read_command():
        result = subprocess.run(command, capture_output=True, text=True,
                                encoding='utf-8', errors='replace')
        return result.stdout if result.returncode == 0 else ''

    try:
        read_command()
        return read_command
    except Exception:
        return None


def take_new_links(text, seen):
    """
    Linki z tekstu, których jeszcze nie widzieliśmy w tej sesji. Aktualizuje
    zbiór `seen` (identyfikatory filmów) i zwraca listę nowych adresów.
    Wydzielone z wątku nasłuchu, żeby dało się to przetestować.
    """
    fresh = []
    for url in extract_youtube_links(text):
        video_id = video_id_from_url(url)
        if video_id and video_id not in seen:
            seen.add(video_id)
            fresh.append(url)
    return fresh


def _clipboard_producer(pending, stop_event, interval, failure):
    """
    Wątek nasłuchu: obserwuje schowek i dokłada nowe linki do kolejki `pending`.
    Działa RÓWNOLEGLE do pobierania, więc linki skopiowane w trakcie ściągania
    nie giną — czekają w kolejce.
    """
    # Czytnik schowka tworzymy w TYM wątku — obiekty tkintera należą do wątku,
    # który je utworzył, więc nie wolno ich współdzielić.
    read_clipboard = _pick_clipboard_reader()
    if read_clipboard is None:
        failure.append('Cannot read the clipboard on this system. '
                       'Install the helper library:  pip install pyperclip')
        stop_event.set()
        return

    seen = set()
    # Cokolwiek leży w schowku PRZED startem, zostaje nietknięte — nasłuch reaguje
    # wyłącznie na nowe kopiowanie. Inaczej samo uruchomienie trybu ściągałoby
    # przypadkowy link skopiowany wcześniej, w innym celu.
    last_text = read_clipboard() or ''
    if extract_youtube_links(last_text):
        print('\nℹ️  Your clipboard already holds a link — leaving it alone '
              '(copy something else and it again if you do want it).')
    print('\n👀 Watching the clipboard... (Ctrl+C to stop)')

    while not stop_event.is_set():
        text = read_clipboard() or ''
        if text and text != last_text:
            last_text = text
            for url in take_new_links(text, seen):
                pending.put(url)
                waiting = pending.qsize()
                print(f'\n➕ Queued: {url}'
                      + (f'   ({waiting} waiting)' if waiting > 1 else ''))
        stop_event.wait(interval)


def watch_clipboard(interval=1.0, **download_kwargs):
    """
    Tryb nasłuchu schowka: skopiowanie linku (albo całego bloku tekstu z wieloma
    linkami — np. listy postów z Discorda) dokłada go do kolejki, a pobieranie
    idzie po kolei, z tymi samymi ustawieniami co zwykłe uruchomienie
    (--resolve, napisy, katalog...).

    Nasłuch chodzi w osobnym wątku, więc MOŻNA kopiować kolejne linki w trakcie
    pobierania — trafiają do kolejki zamiast przepaść. Każdy film jest brany raz
    na sesję, a wykrywanie duplikatów pilnuje, żeby nie pobrać ponownie czegoś,
    co już leży w katalogu docelowym.
    """
    import queue
    import threading

    print('\n' + '=' * 70)
    print('📋 CLIPBOARD WATCH MODE')
    print('=' * 70)
    print('Copy a YouTube link anywhere (Ctrl+C) and it is downloaded for you.')
    print('You can keep copying while a download runs — links wait in a queue.')
    print('Copying a whole block of text queues every link inside it, which is')
    print('handy for a list of posts copied out of Discord.')
    print(f'Saving to: {download_kwargs.get("output_dir") or os.getcwd()}')
    print('\nPress Ctrl+C here to stop watching.')
    print('=' * 70)

    pending = queue.Queue()
    stop_event = threading.Event()
    failure = []
    watcher = threading.Thread(
        target=_clipboard_producer,
        args=(pending, stop_event, interval, failure),
        daemon=True,
    )
    watcher.start()

    done = 0
    try:
        while not stop_event.is_set():
            try:
                url = pending.get(timeout=0.5)
            except queue.Empty:
                continue
            waiting = pending.qsize()
            done += 1
            print(f'\n▶️  Download {done}: {url}'
                  + (f'   ({waiting} more in the queue)' if waiting else ''))
            try:
                download_youtube(url, **download_kwargs)
            except Exception as e:
                # Jeden nieudany film nie może zabić nasłuchu ani kolejki
                print(f'\n✗ Download failed for {url}: {e}')
            if pending.empty():
                print('\n👀 Queue empty — watching the clipboard... (Ctrl+C to stop)')
    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()

    if failure:
        print(f'\n✗ {failure[0]}')
        return

    leftover = []
    while not pending.empty():
        leftover.append(pending.get())
    print(f'\n\n👋 Stopped watching. Downloaded {done} video(s) this session.')
    if leftover:
        print(f'   {len(leftover)} link(s) were still queued and were NOT downloaded:')
        for url in leftover:
            print(f'   • {url}')


def build_download_options(args, inserts_opts=None, section_args=None):
    """
    Zbiera opcje wiersza poleceń w komplet argumentów dla download_youtube().
    Jedno miejsce dla wszystkich ścieżek uruchomienia (zwykłe pobranie, nasłuch
    schowka) — inaczej łatwo zgubić opcję w jednej z nich.
    """
    return dict(
        format_choice=args.format,
        generate_subs=args.subs,
        whisper_model=args.model,
        source_lang=args.source_lang,
        translate_to=args.translate_to,
        initial_prompt=args.prompt,
        cookies_from_browser=args.cookies_from_browser,
        output_dir=args.output_dir,
        also_vtt=args.vtt,
        burn=args.burn,
        embed=args.embed,
        inserts_opts=inserts_opts,
        section_args=section_args,
        engine=args.engine,
        qwen_model=args.qwen_model,
        translate_engine=args.translate_engine,
        translate_model=args.translate_model,
        resolve=args.resolve,
        force_download=args.force_download,
    )


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

  # Japanese video -> Polish subtitles, best quality (Qwen3-ASR + Gemini translation):
  %(prog)s "https://youtube.com/watch?v=..." --subs --engine qwen --source-lang ja --translate-to pl --translate-engine gemini

  # Polish video with a Whisper-vs-Qwen cross-check (hybrid engine):
  %(prog)s "https://youtube.com/watch?v=..." --subs --engine hybrid --model medium

  # Local file on disk -> translated Polish subtitles (no download):
  %(prog)s --file "C:\\videos\\clip.mp4" --source-lang es --translate-to pl

  # Age-restricted/login video (browser cookies) and a custom output folder:
  %(prog)s "https://youtube.com/watch?v=..." --cookies-from-browser chrome --output-dir "C:\\downloads"

  # Download only a fragment (1:30 → 3:00):
  %(prog)s "https://youtube.com/watch?v=..." --start 1:30 --end 3:00

  # Media guaranteed to edit smoothly in DaVinci Resolve (free/Windows too):
  %(prog)s "https://youtube.com/watch?v=..." --resolve

  # Set a permanent default output folder once, then download straight into it:
  %(prog)s --set-output-dir "D:\\edit"
  %(prog)s "https://youtube.com/watch?v=..." --resolve

  # Make an existing local file Resolve-ready (writes <name>_resolve next to it):
  %(prog)s --file "C:\\videos\\clip.mp4" --resolve
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
        help='Generate subtitles with AI speech recognition (MP4 only; see --engine). '
             'In the original language by default; use --translate-to to translate.'
    )
    parser.add_argument(
        '--model',
        default='base',
        help='Whisper model (default: base). Any name supported by Whisper, '
             'e.g.: tiny, base, small, medium, large, large-v3, turbo. '
             'Bigger = more accurate but slower. Newer models work automatically '
             'once they appear in the whisper library. '
             'Used by --engine whisper and for the timing pass of --engine hybrid.'
    )
    parser.add_argument(
        '--engine',
        choices=['auto', 'whisper', 'qwen', 'hybrid'],
        default='auto',
        help='Transcription engine (default: auto). "qwen" = Qwen3-ASR — more accurate '
             'than Whisper — with ForcedAligner timestamps (11 languages, incl. Japanese, '
             'English, Spanish; NOT Polish); for the other Qwen languages it switches to '
             '"hybrid" automatically. "hybrid" = Whisper timestamps + Qwen text, with an '
             'agreement report (cross-check of both engines; works for Polish too). '
             '"auto" prefers Qwen when qwen-asr is installed and a CUDA GPU is present, '
             'otherwise Whisper. Qwen requires: pip install -U qwen-asr.'
    )
    parser.add_argument(
        '--qwen-model',
        default=None,
        metavar='MODEL',
        help='Qwen3-ASR model for --engine qwen/hybrid/auto '
             '(default: Qwen/Qwen3-ASR-1.7B; lighter: Qwen/Qwen3-ASR-0.6B).'
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
        help='Target subtitle language, e.g. pl. Enables translation (see '
             '--translate-engine). Without it, subtitles stay in the original language.'
    )
    parser.add_argument(
        '--translate-engine',
        choices=['google', 'gemini'],
        default='google',
        help='How to translate subtitles (default: google). "google" = free Google '
             'Translate, segment by segment. "gemini" = the Gemini API translates the '
             'whole transcript with context — consistent terminology and better grammar '
             '(recommended for ja/zh/ko sources). Uses the same API key as '
             '--smart-inserts/--organize (asks once and saves it); falls back to '
             'Google Translate on any error.'
    )
    parser.add_argument(
        '--translate-model',
        default='gemini-2.5-flash',
        metavar='MODEL',
        help='Gemini model for --translate-engine gemini (default: gemini-2.5-flash).'
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
        '--set-output-dir',
        default=None,
        metavar='DIR',
        help='Save DIR as the DEFAULT output folder for all future runs (stored in '
             '~/.ytsubtran.json) and use it for this run too. Can be used alone '
             '(no URL) just to set it. A one-off --output-dir always wins; '
             'remove the "default_output_dir" entry from the file to reset.'
    )
    parser.add_argument(
        '--output-dir',
        default=None,
        metavar='DIR',
        help='Folder for output files (video and/or subtitles). If omitted, the script '
             'asks interactively (Enter = current directory).'
    )
    parser.add_argument(
        '--watch-clipboard',
        action='store_true',
        help='Watch mode: copy a YouTube link anywhere (Ctrl+C) and it downloads by '
             'itself — no need to paste anything here. Copying a whole block of text '
             '(e.g. a list of posts from Discord) queues every link found in it. '
             'Uses all the other options you pass (--resolve, --subs, --output-dir...). '
             'Runs until you stop it with Ctrl+C. Needs no URL argument.'
    )
    parser.add_argument(
        '--watch-interval',
        type=float, default=1.0, metavar='SEC',
        help='How often to check the clipboard in --watch-clipboard mode (default: 1 s).'
    )
    parser.add_argument(
        '--force-download',
        action='store_true',
        help='Download even if this video is already in the output folder. By default '
             'the script skips videos it has downloaded there before — matched by '
             'YouTube video id (not file name), and only when the file is really on '
             'disk and complete. Format (mp4/mp3) and --start/--end range are part of '
             'the match, so a fragment is never mistaken for the full video.'
    )
    parser.add_argument(
        '--resolve',
        action='store_true',
        help='DaVinci Resolve preset: guarantee media that edits smoothly in Resolve '
             '(incl. the free version on Windows). Prefers native H.264+AAC when '
             'downloading, then verifies the file with ffprobe and normalizes only '
             'what is needed: VP9/AV1/HEVC video → H.264 8-bit MP4, Opus/Vorbis '
             'audio → AAC, variable frame rate → constant. 10-bit / 4:2:2 sources '
             'become DNxHR HQX in MOV (+PCM audio) to keep full bit depth. Also '
             'works with --file (writes a sibling *_resolve file, original untouched).'
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
    parser.add_argument(
        '--start',
        default=None,
        metavar='TIME',
        help='Download only the fragment starting at TIME. Formats: SS (90), '
             'MM:SS (1:30), HH:MM:SS (00:01:30.5). Combine with --end.'
    )
    parser.add_argument(
        '--end',
        default=None,
        metavar='TIME',
        help='End timestamp for the fragment (same formats as --start). '
             'If omitted with --start, downloads from --start to the end of the video.'
    )
    parser.add_argument(
        '--precise-cut',
        action='store_true',
        help='Force frame-accurate trimming at --start/--end boundaries '
             '(re-encodes the boundary chunks). Off by default because that '
             're-encode can desync audio on some YouTube streams; without it '
             'the fragment starts at the nearest keyframe but audio stays in sync.'
    )
    parser.add_argument(
        '--organize',
        metavar='DIR',
        help='Catalog mode: scan DIR for generically-named lecture videos (e.g. '
             'DataCamp\'s "video (1).mp4"), figure out each one\'s course/title from '
             'its slides + a short transcript (Gemini), then rename and sort them '
             'into per-course sub-folders. Previews the plan and asks before moving '
             'anything (use --yes to skip the prompt).'
    )
    parser.add_argument(
        '--organize-model',
        default='gemini-2.5-flash',
        metavar='MODEL',
        help='Gemini model used by --organize (default: gemini-2.5-flash).'
    )
    parser.add_argument(
        '--organize-frames',
        type=int, default=10, metavar='N',
        help='How many frames to sample per video for --organize (default: 10).'
    )
    parser.add_argument(
        '--organize-transcript',
        action='store_true',
        help='With --organize: also transcribe each video\'s intro with Whisper and '
             'feed it to Gemini. Off by default — the title text on the slides is '
             'enough for most course videos, and skipping Whisper is much faster. '
             'Turn it on for videos with no on-screen title.'
    )

    args = parser.parse_args()

    if (not args.file and not args.url and not args.organize and not args.set_output_dir
            and not args.watch_clipboard):
        parser.error('Provide a YouTube link, use --file with a path to a file on disk, '
                     'use --organize with a folder to catalog, --watch-clipboard to '
                     'download whatever you copy, or --set-output-dir to save a default '
                     'output folder.')

    # Trwała domyślna lokalizacja pobierania (przechowywana między uruchomieniami)
    if args.set_output_dir:
        target = os.path.abspath(os.path.expanduser(args.set_output_dir.strip().strip('"')))
        try:
            os.makedirs(target, exist_ok=True)
        except OSError as e:
            parser.error(f'Cannot create/use folder {target!r}: {e}')
        if save_app_config_value('default_output_dir', target):
            print(f'✓ Default output folder saved: {target}')
            print(f'  (stored in {APP_CONFIG_PATH}; a one-off --output-dir still wins)')
        else:
            print(f'⚠️  Could not write {APP_CONFIG_PATH} — the default was NOT saved.')
        if not args.file and not args.url and not args.organize and not args.watch_clipboard:
            return  # samodzielne użycie: tylko zapisz ustawienie
        if not args.output_dir:
            args.output_dir = target

    # --start/--end: parse and validate the fragment timestamps (download-only).
    section_args = None
    if args.start or args.end:
        if args.file:
            parser.error('--start/--end work only when downloading from a URL, not with --file.')
        s = parse_timestamp(args.start) if args.start else None
        e = parse_timestamp(args.end) if args.end else None
        if args.start and s is None:
            parser.error(f'Invalid --start timestamp: {args.start!r} '
                         '(use SS, MM:SS or HH:MM:SS).')
        if args.end and e is None:
            parser.error(f'Invalid --end timestamp: {args.end!r} '
                         '(use SS, MM:SS or HH:MM:SS).')
        if s is not None and e is not None and e <= s:
            parser.error('--end must be greater than --start.')
        section_args = build_section_args(s, e, precise=args.precise_cut)

    # Catalog mode is a standalone action — it scans a folder and renames/sorts the
    # videos already there (no download, no subtitles). Handle it and exit early.
    if args.organize:
        # organize.py sits next to this file; make sure that directory is importable
        # even when launched via the installed `ytsubtran` console command.
        here = os.path.dirname(os.path.abspath(__file__))
        if here not in sys.path:
            sys.path.insert(0, here)
        try:
            import organize
        except Exception as e:
            print(f'✗ Catalog module unavailable: {e}')
            sys.exit(1)
        organize.organize_folder(
            args.organize,
            model=args.organize_model,
            assume_yes=args.yes,
            frames_n=args.organize_frames,
            do_transcript=args.organize_transcript,
            whisper_model=args.model,
        )
        return

    # Where to save: --output-dir > saved default (--set-output-dir) > interactive prompt
    if not args.output_dir:
        saved = resolve_default_output_dir()
        if saved:
            print(f'📂 Output folder (saved default): {saved}')
            args.output_dir = saved
        else:
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

    # Jeden komplet opcji pobierania dla WSZYSTKICH ścieżek (zwykłe pobranie i
    # nasłuch schowka) — dzięki temu żadna opcja nie zgubi się w jednej z nich.
    download_opts = build_download_options(args, inserts_opts, section_args)

    if args.watch_clipboard:
        watch_clipboard(interval=args.watch_interval, **download_opts)
        return

    if args.file:
        # Local-file mode — no download
        if not os.path.exists(args.file):
            print(f'✗ File not found: {args.file}')
            sys.exit(1)
        # Preset DaVinci dla pliku lokalnego: nowy plik *_resolve obok oryginału;
        # dalsze kroki (napisy, inserty) pracują na wersji zdatnej dla Resolve,
        # więc nazwy plików .srt pasują do pliku, który trafi na timeline.
        if args.resolve:
            args.file = ensure_resolve_ready(args.file, in_place=False)
        # Generate subtitles unless the user asked ONLY for insert detection
        wants_subs = args.subs or bool(args.translate_to) or inserts_opts is None
        if wants_subs:
            generate_subtitles(args.file, args.model, args.source_lang,
                               args.translate_to, args.prompt, args.output_dir,
                               args.vtt, args.burn, args.embed,
                               engine=args.engine, qwen_model=args.qwen_model,
                               translate_engine=args.translate_engine,
                               translate_model=args.translate_model)
        if inserts_opts:
            process_inserts(args.file, url=None, output_dir=args.output_dir, **inserts_opts)
    else:  # args.url
        download_youtube(args.url, **download_opts)


if __name__ == '__main__':
    main()
