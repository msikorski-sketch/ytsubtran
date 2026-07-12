"""
Silnik transkrypcji Qwen3-ASR (Alibaba, 2026) — dokładniejszy od Whispera.

Dwa tryby pracy:
- 'qwen'   — pełna transkrypcja Qwen3-ASR + timestampy z Qwen3-ForcedAligner.
             Najwyższa jakość, ale aligner zna tylko 11 języków (m.in. japoński,
             angielski, hiszpański — BEZ polskiego).
- 'hybrid' — Whisper wyznacza segmenty czasowe, a Qwen3-ASR transkrybuje
             każdy segment od nowa (tekst lepszej jakości na sprawdzonych
             znacznikach czasu). Działa dla wszystkich 30 języków Qwena,
             w tym polskiego, i służy zarazem jako weryfikacja Whispera.

Import tego modułu wymaga tylko biblioteki standardowej; ciężkie zależności
(torch, qwen_asr, numpy) są ładowane leniwie wewnątrz funkcji.
"""
import difflib
import os
import subprocess

DEFAULT_QWEN_MODEL = 'Qwen/Qwen3-ASR-1.7B'
ALIGNER_MODEL = 'Qwen/Qwen3-ForcedAligner-0.6B'
SAMPLE_RATE = 16000

# Kod ISO -> kanoniczna nazwa języka w Qwen3-ASR (30 obsługiwanych języków)
QWEN_LANGUAGES = {
    'zh': 'Chinese', 'en': 'English', 'yue': 'Cantonese', 'ar': 'Arabic',
    'de': 'German', 'fr': 'French', 'es': 'Spanish', 'pt': 'Portuguese',
    'id': 'Indonesian', 'it': 'Italian', 'ko': 'Korean', 'ru': 'Russian',
    'th': 'Thai', 'vi': 'Vietnamese', 'ja': 'Japanese', 'tr': 'Turkish',
    'hi': 'Hindi', 'ms': 'Malay', 'nl': 'Dutch', 'sv': 'Swedish',
    'da': 'Danish', 'fi': 'Finnish', 'pl': 'Polish', 'cs': 'Czech',
    'tl': 'Filipino', 'fa': 'Persian', 'el': 'Greek', 'ro': 'Romanian',
    'hu': 'Hungarian', 'mk': 'Macedonian',
}
_NAME_TO_CODE = {name.lower(): code for code, name in QWEN_LANGUAGES.items()}

# Języki oficjalnie obsługiwane przez Qwen3-ForcedAligner (timestampy).
# Poza tą listą timestampy alignera są niewiarygodne -> tryb hybrydowy.
ALIGNER_LANGS = {'zh', 'en', 'yue', 'fr', 'de', 'it', 'ja', 'ko', 'pt', 'ru', 'es'}

# Języki pisane bez spacji — inne łączenie tekstu i krótsze linie napisów
CJK_LANGS = {'zh', 'ja', 'yue'}

_SENTENCE_END = '.!?。！？…'


class QwenEngineError(Exception):
    """Błąd silnika Qwen — wywołujący może wrócić do Whispera."""


class AlignerUnsupported(QwenEngineError):
    """Wykryty język nie ma wsparcia alignera — trzeba przejść na tryb hybrydowy."""

    def __init__(self, lang_code):
        self.lang_code = lang_code
        super().__init__(f'ForcedAligner does not support language: {lang_code}')


def check_qwen_installed():
    """Sprawdza czy pakiet qwen-asr jest zainstalowany. Zwraca (bool, wersja|None)."""
    try:
        from importlib.metadata import version
        return True, version('qwen-asr')
    except Exception:
        return False, None


def cuda_available():
    """Czy jest dostępne GPU CUDA (bez niego Qwen jest niepraktycznie wolny)."""
    try:
        import torch
        return torch.cuda.is_available()
    except Exception:
        return False


def show_qwen_installation_guide():
    """Wyświetla instrukcję instalacji qwen-asr."""
    print('\n' + '=' * 70)
    print('⚠️  QWEN3-ASR IS NOT INSTALLED')
    print('=' * 70)
    print('\nQwen3-ASR is a free, open-source speech recognition model that is')
    print('more accurate than Whisper (especially for Japanese, Chinese, Korean).')
    print('It runs locally on your GPU.\n')
    print('📦 INSTALL:\n')
    print('   pip install -U qwen-asr\n')
    print('On first use the models are downloaded automatically (~4 GB + ~1.5 GB).')
    print('Requirement: an NVIDIA GPU with CUDA (PyTorch CUDA build).')
    print('=' * 70)


def choose_asr_mode(engine, source_lang, qwen_ok, gpu_ok):
    """
    Czysta logika wyboru trybu transkrypcji.

    Zwraca (mode, notes):
    - mode: 'whisper' | 'qwen' | 'hybrid' | 'install-qwen'
      ('install-qwen' = użytkownik jawnie zażądał Qwena, a nie jest zainstalowany)
    - notes: komunikaty do wypisania użytkownikowi.
    """
    notes = []

    if engine == 'whisper':
        return 'whisper', notes

    if engine not in ('auto', 'qwen', 'hybrid'):
        notes.append(f"Unknown engine '{engine}' — using Whisper.")
        return 'whisper', notes

    if not qwen_ok:
        if engine == 'auto':
            notes.append('💡 Tip: install Qwen3-ASR for noticeably better transcription '
                         'quality (pip install -U qwen-asr). Using Whisper for now.')
            return 'whisper', notes
        return 'install-qwen', notes

    if not gpu_ok:
        if engine == 'auto':
            notes.append('Qwen3-ASR is installed but no CUDA GPU is available — '
                         'using Whisper (Qwen on CPU is impractically slow).')
            return 'whisper', notes
        notes.append('⚠️  No CUDA GPU detected — Qwen3-ASR will run on CPU and be VERY slow.')

    if engine == 'hybrid':
        return 'hybrid', notes

    # engine == 'qwen' lub 'auto' z dostępnym Qwenem
    if source_lang == 'auto':
        notes.append('Language auto-detect: Qwen3-ASR will identify the language first; '
                     'if its aligner cannot timestamp that language, the hybrid '
                     'Whisper+Qwen mode is used instead.')
        return 'qwen', notes

    if source_lang in ALIGNER_LANGS:
        return 'qwen', notes

    if source_lang in QWEN_LANGUAGES:
        notes.append(f'Qwen3-ASR supports {QWEN_LANGUAGES[source_lang]} but its aligner '
                     'cannot produce timestamps for it — using hybrid mode '
                     '(Whisper timing + Qwen text).')
        return 'hybrid', notes

    notes.append(f"Qwen3-ASR does not support language '{source_lang}' — using Whisper.")
    return 'whisper', notes


def qwen_lang_name(code):
    """Kod ISO -> kanoniczna nazwa języka Qwena (np. 'ja' -> 'Japanese')."""
    return QWEN_LANGUAGES.get(code)


def code_from_qwen_language(merged_language):
    """
    Wynikowe pole .language Qwena ('Japanese' albo 'Japanese,English' przy
    materiale wielojęzycznym) -> kod ISO pierwszego (dominującego) języka.
    """
    if not merged_language:
        return None
    first = merged_language.split(',')[0].strip().lower()
    return _NAME_TO_CODE.get(first)


def load_audio_mono16k(media_path):
    """
    Dekoduje dowolny plik audio/wideo do mono 16 kHz float32 przez ffmpeg
    (ten sam wymóg co Whisper — ffmpeg i tak musi być zainstalowany).
    """
    import numpy as np

    if not os.path.exists(media_path):
        raise QwenEngineError(f'File not found: {media_path}')
    command = [
        'ffmpeg', '-nostdin', '-v', 'error',
        '-i', media_path,
        '-f', 'f32le', '-ac', '1', '-ar', str(SAMPLE_RATE),
        '-',
    ]
    try:
        proc = subprocess.run(command, capture_output=True, check=True)
    except FileNotFoundError:
        raise QwenEngineError('ffmpeg not found — install it and add to PATH.')
    except subprocess.CalledProcessError as e:
        detail = (e.stderr or b'').decode('utf-8', errors='replace').strip()
        raise QwenEngineError(f'ffmpeg could not decode the audio: {detail}')
    audio = np.frombuffer(proc.stdout, dtype=np.float32)
    if audio.size == 0:
        raise QwenEngineError('No audio track found in the file.')
    return audio


def load_qwen_model(model_id=DEFAULT_QWEN_MODEL, with_aligner=True,
                    max_batch_size=None):
    """
    Ładuje Qwen3-ASR (opcjonalnie z ForcedAlignerem do timestampów).
    Na GPU używa bfloat16; bez GPU spada na CPU/float32 (bardzo wolne).
    """
    import gc

    import torch
    from qwen_asr import Qwen3ASRModel

    if torch.cuda.is_available():
        # Zwolnij VRAM po ewentualnym wcześniejszym Whisperze (tryb hybrydowy):
        # bez gc.collect() porzucony model Whispera wciąż trzyma pamięć karty.
        gc.collect()
        torch.cuda.empty_cache()
        device, dtype = 'cuda:0', torch.bfloat16
    else:
        device, dtype = 'cpu', torch.float32

    if max_batch_size is None:
        # Ostrożny domyślny batch: fragmenty audio bywają długie (do 3 minut),
        # a karty konsumenckie mają 8-12 GB VRAM.
        max_batch_size = 2 if with_aligner else 8

    kwargs = dict(
        dtype=dtype,
        device_map=device,
        max_inference_batch_size=max_batch_size,
    )
    if with_aligner:
        kwargs['forced_aligner'] = ALIGNER_MODEL
        kwargs['forced_aligner_kwargs'] = dict(dtype=dtype, device_map=device)

    return Qwen3ASRModel.from_pretrained(model_id, **kwargs)


def _item_fields(item):
    """Adapter: ForcedAlignItem (atrybuty) albo dict -> (text, start, end)."""
    if hasattr(item, 'text'):
        return item.text, float(item.start_time), float(item.end_time)
    return item['text'], float(item['start_time']), float(item['end_time'])


def group_align_items(items, lang_code, max_dur=7.0, pause=0.8, max_chars=None):
    """
    Grupuje słowa/znaki z alignera (word-level) w segmenty napisów.

    Nowy segment zaczyna się, gdy: przerwa w mowie > `pause` s, segment
    przekroczyłby `max_dur` s lub `max_chars` znaków, albo poprzednie słowo
    kończyło zdanie (.!?…). Zwraca listę {'start','end','text'} — ten sam
    format co segmenty Whispera, więc dalszy zapis SRT/VTT jest wspólny.
    """
    cjk = lang_code in CJK_LANGS
    joiner = '' if cjk else ' '
    if max_chars is None:
        max_chars = 42 if cjk else 84

    segments = []
    cur = None

    def flush():
        nonlocal cur
        if cur is not None and cur['texts']:
            segments.append({
                'start': cur['start'],
                'end': cur['end'],
                'text': joiner.join(cur['texts']),
            })
        cur = None

    for item in items:
        text, start, end = _item_fields(item)
        text = (text or '').strip()
        if not text:
            continue
        end = max(end, start)

        if cur is not None:
            too_long = (end - cur['start']) > max_dur
            too_wide = len(joiner.join(cur['texts'] + [text])) > max_chars
            big_gap = (start - cur['end']) > pause
            if big_gap or too_long or too_wide or cur['sentence_end']:
                flush()

        if cur is None:
            cur = {'start': start, 'end': end, 'texts': [], 'sentence_end': False}
        cur['texts'].append(text)
        cur['end'] = max(cur['end'], end)
        cur['sentence_end'] = text[-1] in _SENTENCE_END

    flush()
    return segments


def _detect_language(model, audio, fractions=(0.15, 0.4, 0.65)):
    """
    Wykrywa język próbkując kilka 30-sekundowych fragmentów nagrania (jak
    detect_language_robust dla Whispera — czołówka w innym języku nie
    zafałszuje wyniku). Zwraca kod ISO albo None.
    """
    window = 30 * SAMPLE_RATE
    total = len(audio)
    if total <= window:
        positions = [0]
    else:
        positions = sorted({min(int(f * total), total - window) for f in fractions})

    samples = [(audio[pos:pos + window], SAMPLE_RATE) for pos in positions]
    results = model.transcribe(audio=samples, language=None)

    votes = {}
    for r in results:
        code = code_from_qwen_language(r.language)
        if code:
            votes[code] = votes.get(code, 0) + 1
    if not votes:
        return None
    return max(votes, key=votes.get)


def transcribe_qwen_full(media_path, source_lang='auto', model_id=DEFAULT_QWEN_MODEL,
                         context=None):
    """
    Pełna transkrypcja Qwen3-ASR z timestampami (tryb 'qwen').

    Zwraca (segments, full_text, lang_code) w formacie zgodnym z Whisperem.
    Rzuca AlignerUnsupported, gdy wykryty język nie ma wsparcia alignera
    (wywołujący powinien wtedy przejść na tryb hybrydowy).
    """
    audio = load_audio_mono16k(media_path)
    duration = len(audio) / SAMPLE_RATE
    print(f'   Audio length: {duration / 60:.1f} min')

    print('⏳ Loading Qwen3-ASR + ForcedAligner...')
    print('   (On first use the models are downloaded — ~4 GB + ~1.5 GB)')
    model = load_qwen_model(model_id, with_aligner=True)
    print('✓ Models loaded\n')

    lang_code = source_lang
    if source_lang == 'auto':
        print('🔎 Detecting audio language (sampling several points)...')
        lang_code = _detect_language(model, audio)
        if not lang_code:
            raise QwenEngineError('Could not detect the language (silent audio?).')
        print(f'   Detected language: {QWEN_LANGUAGES.get(lang_code, lang_code)} ({lang_code})\n')
        if lang_code not in ALIGNER_LANGS:
            raise AlignerUnsupported(lang_code)

    language = qwen_lang_name(lang_code)
    if language is None:
        raise QwenEngineError(f'Qwen3-ASR does not support language: {lang_code}')

    print('⏳ Transcribing with Qwen3-ASR (this may take a few minutes)...')
    results = model.transcribe(
        audio=[(audio, SAMPLE_RATE)],
        context=context or '',
        language=[language],
        return_time_stamps=True,
    )
    result = results[0]

    align = result.time_stamps
    items = list(getattr(align, 'items', align or []))
    if not items:
        raise QwenEngineError('Qwen produced no aligned segments (silent audio?).')

    segments = group_align_items(items, lang_code)
    full_text = result.text or ''
    print(f'✓ Transcription done: {len(segments)} subtitle segments\n')
    return segments, full_text, lang_code


def _normalize_for_compare(text):
    """Normalizuje tekst do porównania: małe litery, bez interpunkcji i spacji."""
    return ''.join(ch for ch in text.casefold() if ch.isalnum())


def _similarity(a, b):
    """Podobieństwo 0..1 dwóch tekstów (po normalizacji)."""
    na, nb = _normalize_for_compare(a), _normalize_for_compare(b)
    if not na and not nb:
        return 1.0
    return difflib.SequenceMatcher(None, na, nb).ratio()


def refine_segments_with_qwen(media_path, segments, lang_code,
                              model_id=DEFAULT_QWEN_MODEL, context=None,
                              min_seg_sec=0.3, pad_sec=0.15):
    """
    Tryb hybrydowy: ponowna transkrypcja segmentów Whispera Qwenem.

    Każdy segment (znaczniki czasu z Whispera) jest wycinany z audio
    i transkrybowany Qwenem z wymuszonym językiem. Zwraca (texts, report):
    - texts: finalny tekst każdego segmentu (Qwen; przy pustym wyniku Qwena
      zostaje tekst Whispera),
    - report: statystyki zgodności obu silników (weryfikacja Whispera).
    """
    language = qwen_lang_name(lang_code)
    if language is None:
        raise QwenEngineError(f'Qwen3-ASR does not support language: {lang_code}')

    audio = load_audio_mono16k(media_path)
    total = len(audio)

    print('⏳ Loading Qwen3-ASR (text refinement pass)...')
    print('   (On first use the model is downloaded — ~4 GB)')
    model = load_qwen_model(model_id, with_aligner=False)
    print('✓ Model loaded\n')

    # Wytnij fragmenty audio wg znaczników Whispera (z małym zapasem na końcu,
    # żeby nie ucinać ostatniej głoski przy minimalnie zaniżonym 'end').
    slices = []
    slice_index = []  # indeks segmentu dla każdego wycinka
    for i, seg in enumerate(segments):
        start = max(0, int(round(seg['start'] * SAMPLE_RATE)))
        end = min(total, int(round((seg['end'] + pad_sec) * SAMPLE_RATE)))
        if end - start < int(min_seg_sec * SAMPLE_RATE):
            continue
        slices.append((audio[start:end], SAMPLE_RATE))
        slice_index.append(i)

    texts = [seg['text'].strip() for seg in segments]
    report = {'compared': 0, 'avg_similarity': 1.0, 'low': [], 'empty_kept': 0}
    if not slices:
        return texts, report

    print(f'⏳ Re-transcribing {len(slices)} segments with Qwen3-ASR...')
    results = model.transcribe(
        audio=slices,
        context=context or '',
        language=[language] * len(slices),
    )

    similarities = []
    for i, r in zip(slice_index, results):
        whisper_text = texts[i]
        qwen_text = (r.text or '').strip()
        if not qwen_text:
            # Qwen nic nie usłyszał (muzyka/cisza?) — zostaw wersję Whispera
            report['empty_kept'] += 1
            continue
        sim = _similarity(whisper_text, qwen_text)
        similarities.append(sim)
        if sim < 0.6:
            report['low'].append({
                'index': i,
                'start': segments[i]['start'],
                'whisper': whisper_text,
                'qwen': qwen_text,
                'similarity': sim,
            })
        texts[i] = qwen_text

    report['compared'] = len(similarities)
    if similarities:
        report['avg_similarity'] = sum(similarities) / len(similarities)
    return texts, report


def print_refine_report(report):
    """Wypisuje raport zgodności Whisper vs Qwen (tryb hybrydowy)."""
    if not report['compared']:
        print('🔍 Cross-check: no segments were long enough to compare.')
        return
    print(f"🔍 Cross-check (Whisper vs Qwen3-ASR): {report['compared']} segments, "
          f"average agreement {report['avg_similarity']:.0%}")
    if report['empty_kept']:
        print(f"   {report['empty_kept']} segment(s): Qwen heard nothing "
              '(music/silence?) — kept the Whisper text.')
    low = report['low']
    if low:
        print(f'   {len(low)} segment(s) differ strongly (<60% similar) — '
              'Qwen version used:')
        for d in low[:5]:
            print(f"   • [{d['start']:7.1f}s] Whisper: {d['whisper']!r}")
            print(f"     {'':9} Qwen:    {d['qwen']!r}")
        if len(low) > 5:
            print(f'   ... and {len(low) - 5} more (see the subtitle files).')
    print()
