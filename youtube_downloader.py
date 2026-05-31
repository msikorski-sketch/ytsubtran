
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
    print('⚠️  BIBLIOTEKA DO TŁUMACZENIA NIE JEST ZAINSTALOWANA')
    print('=' * 70)
    print('\nDo tłumaczenia napisów potrzebna jest biblioteka deep-translator')
    print('(korzysta z darmowego Google Translate, bez klucza API).\n')
    print('📦 INSTALACJA:\n')
    print('   pip install -U deep-translator\n')
    print('Po instalacji uruchom skrypt ponownie.')
    print('=' * 70)


# Nazwy języków dla czytelnych komunikatów
LANGUAGE_NAMES = {
    'auto': 'auto-wykrywanie',
    'pl': 'polski', 'en': 'angielski', 'es': 'hiszpański', 'de': 'niemiecki',
    'fr': 'francuski', 'it': 'włoski', 'pt': 'portugalski', 'ru': 'rosyjski',
    'uk': 'ukraiński', 'cs': 'czeski', 'sk': 'słowacki', 'nl': 'holenderski',
    'ja': 'japoński', 'zh': 'chiński', 'ko': 'koreański', 'ar': 'arabski',
    'tr': 'turecki', 'sv': 'szwedzki', 'no': 'norweski', 'da': 'duński',
    'fi': 'fiński', 'hu': 'węgierski', 'ro': 'rumuński', 'el': 'grecki',
}


def lang_name(code):
    """Zwraca czytelną nazwę języka dla podanego kodu"""
    return LANGUAGE_NAMES.get(code, code)


def translate_texts(texts, target_lang, source_lang='auto'):
    """
    Tłumaczy listę tekstów na język docelowy używając Google Translate.

    Zwraca listę przetłumaczonych tekstów (tej samej długości co wejście).
    Jeśli pojedynczy segment się nie przetłumaczy, zostaje oryginalny tekst.
    """
    from deep_translator import GoogleTranslator

    # Google używa 'auto' do auto-wykrywania języka źródłowego
    translator = GoogleTranslator(source=source_lang or 'auto', target=target_lang)

    translated = []
    total = len(texts)
    for i, text in enumerate(texts, 1):
        original = text.strip()
        if not original:
            translated.append(text)
            continue
        try:
            result = translator.translate(original)
            translated.append(result if result else original)
        except Exception as e:
            print(f'   ⚠️  Segment {i}/{total} nieprzetłumaczony ({e}) — zostaje oryginał')
            translated.append(original)

        # Postęp co 10 segmentów (lub na końcu)
        if i % 10 == 0 or i == total:
            print(f'   ...przetłumaczono {i}/{total} segmentów')

    return translated


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
    print('⚠️  WHISPER NIE JEST ZAINSTALOWANY')
    print('=' * 70)
    print('\nWhisper to darmowy model AI od OpenAI do transkrypcji audio.')
    print('Działa lokalnie (offline) i doskonale obsługuje język polski!\n')

    print('📦 INSTALACJA WHISPER:\n')
    print('1. Zainstaluj Whisper:')
    print('   pip install -U openai-whisper\n')

    print('2. Zainstaluj ffmpeg (jeśli jeszcze nie masz):\n')
    print('   Linux/Ubuntu:')
    print('   sudo apt update && sudo apt install ffmpeg\n')
    print('   Windows:')
    print('   - Pobierz z: https://ffmpeg.org/download.html')
    print('   - Rozpakuj i dodaj do PATH\n')
    print('   macOS:')
    print('   brew install ffmpeg\n')

    print('3. Po instalacji uruchom skrypt ponownie z parametrem --subs\n')
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
    return 'cpu', 'CPU (GPU niedostępne)'


def generate_subtitles_with_whisper(video_file, model_size='base', source_lang='pl',
                                    translate_to=None, initial_prompt=None, output_dir=None):
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
    """
    print('\n' + '=' * 70)
    print('🎙️  GENEROWANIE NAPISÓW Z WHISPER')
    print('=' * 70)

    # Sprawdź czy Whisper jest zainstalowany
    whisper_installed, version = check_whisper_installed()
    if not whisper_installed:
        show_whisper_installation_guide()
        return False

    print(f'✓ Whisper zainstalowany (wersja: {version})')

    # Sprawdź czy ffmpeg jest zainstalowany
    if not check_ffmpeg_installed():
        print('✗ ffmpeg nie jest zainstalowany!')
        print('  Zainstaluj ffmpeg, aby Whisper mógł przetwarzać audio.')
        return False

    print('✓ ffmpeg zainstalowany')

    # Jeśli mamy tłumaczyć — sprawdź bibliotekę tłumaczącą zanim ruszy transkrypcja
    if translate_to and translate_to != source_lang:
        if not check_translator_installed():
            show_translator_installation_guide()
            return False
        print('✓ deep-translator zainstalowany')

    # Import Whisper
    try:
        import whisper
    except ImportError:
        show_whisper_installation_guide()
        return False

    # Sprawdź czy plik istnieje
    if not os.path.exists(video_file):
        print(f'✗ Nie znaleziono pliku: {video_file}')
        return False

    print(f'\n📁 Plik wideo: {video_file}')
    print(f'🤖 Model Whisper: {model_size}')
    print(f'🌍 Język audio (transkrypcja): {lang_name(source_lang)}')
    if translate_to and translate_to != source_lang:
        print(f'🔁 Tłumaczenie napisów na: {lang_name(translate_to)}')
    print()

    # Informacja o modelach
    model_info = {
        'tiny': '~1 GB RAM, najszybszy, słaba jakość (tylko do szybkich testów)',
        'base': '~1 GB RAM, szybki, przeciętna jakość',
        'small': '~2 GB RAM, dobra jakość',
        'medium': '~5 GB RAM, bardzo dobra jakość (zalecany do realnych filmów)',
        'large': '~10 GB RAM, najlepsza jakość, najwolniejszy',
        'large-v2': '~10 GB RAM, najlepsza jakość (wariant v2)',
        'large-v3': '~10 GB RAM, najlepsza jakość (najnowszy duży model)',
        'turbo': '~6 GB RAM, jakość zbliżona do large-v3, ale dużo szybszy',
    }
    print(f'ℹ️  {model_info.get(model_size, "model niestandardowy/nowy — używam jak podano")}\n')

    if initial_prompt:
        print(f'📝 Podpowiedź kontekstowa: "{initial_prompt}"\n')

    # Podpowiedź: małe modele słabo radzą sobie z muzyką/wieloma głosami
    if model_size in ('tiny', 'base'):
        print('💡 Wskazówka: dla filmów z muzyką, wieloma rozmówcami lub gwarą model '
              f'"{model_size}" może dawać błędy i powtórzenia.')
        print('   Dla lepszej jakości użyj: --model medium / large-v3 / turbo\n')

    # Wybór urządzenia: GPU jeśli dostępne (dużo szybciej), inaczej CPU
    device, device_desc = pick_device()
    print(f'🖥️  Urządzenie obliczeniowe: {device_desc}')
    if device == 'cpu':
        print('   (Masz kartę NVIDIA? Zainstaluj torch z CUDA, aby liczyć na GPU — '
              'kilkukrotnie szybciej.)')
    print()

    try:
        # Załaduj model na wybrane urządzenie
        print(f'⏳ Ładowanie modelu Whisper ({model_size}) na {device.upper()}...')
        print('   (Przy pierwszym użyciu model zostanie pobrany - może to chwilę potrwać)')
        try:
            model = whisper.load_model(model_size, device=device)
        except RuntimeError as e:
            msg = str(e).lower()
            if 'out of memory' in msg or 'cuda' in msg:
                # Za mało VRAM na ten model — spróbuj awaryjnie na CPU
                print(f'⚠️  Nie udało się załadować "{model_size}" na GPU ({e}).')
                print('   Przełączam na CPU. Dla GPU wybierz mniejszy model (np. turbo lub medium).')
                device = 'cpu'
                model = whisper.load_model(model_size, device=device)
            else:
                # Nieznana nazwa modelu — pokaż dostępne i przerwij czytelnie
                available = ', '.join(whisper.available_models())
                print(f'✗ Nie można załadować modelu "{model_size}": {e}')
                print(f'  Dostępne modele w tej wersji whisper: {available}')
                print('  (Aby mieć najnowsze modele, zaktualizuj: pip install -U openai-whisper)')
                return False
        print('✓ Model załadowany\n')

        # Wykrywanie języka (przy 'auto') — próbkując kilka momentów, by intro
        # w innym języku nie zafałszowało wyniku
        if source_lang == 'auto':
            print('🔎 Wykrywanie języka audio (próbkuję kilka momentów, pomijam czołówkę)...')
            whisper_lang, conf = detect_language_robust(model, video_file)
            print(f'   Wykryty język: {lang_name(whisper_lang)} ({whisper_lang}), pewność ~{conf:.0%}\n')
        else:
            whisper_lang = source_lang

        # Transkrypcja
        print('⏳ Transkrypcja audio... (może potrwać kilka minut)')
        print('   Postęp pojawi się poniżej:\n')

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
                # Zabrakło VRAM w trakcie — zwolnij pamięć i dokończ na CPU
                print(f'\n⚠️  Zabrakło pamięci GPU ({e}).')
                print('   Przełączam transkrypcję na CPU (wolniej). '
                      'Następnym razem użyj mniejszego modelu, np. --model turbo.')
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

        print(f'\n💾 Zapisywanie napisów (oryginał): {srt_orig}')
        write_srt(srt_orig, segments)
        with open(txt_orig, 'w', encoding='utf-8') as f:
            f.write(result['text'])
        created_files.append(srt_orig)
        created_files.append(txt_orig)

        # 2) Jeśli trzeba — przetłumacz i zapisz wersję docelową
        if translate_to and translate_to != detected_lang:
            print(f'\n🔁 Tłumaczenie {len(segments)} segmentów na {lang_name(translate_to)}...')
            # Znamy język oryginału — przekazujemy go wprost (pewniejsze niż 'auto')
            source_for_translate = detected_lang
            original_texts = [s['text'] for s in segments]
            translated_texts = translate_texts(original_texts, translate_to, source_for_translate)

            tgt_code = translate_to.upper()
            srt_tgt = f'{base_name}_{tgt_code}.srt'
            txt_tgt = f'{base_name}_{tgt_code}.txt'

            print(f'\n💾 Zapisywanie napisów (tłumaczenie): {srt_tgt}')
            write_srt(srt_tgt, segments, texts=translated_texts)
            with open(txt_tgt, 'w', encoding='utf-8') as f:
                f.write('\n'.join(t.strip() for t in translated_texts))
            created_files.append(srt_tgt)
            created_files.append(txt_tgt)

        print('\n' + '=' * 70)
        print('✓✓✓ NAPISY WYGENEROWANE POMYŚLNIE ✓✓✓')
        print('=' * 70)
        print('\n📄 Utworzone pliki:')
        for path in created_files:
            print(f'   • {path}')
        print('\n💡 Pliki .srt możesz użyć w odtwarzaczach wideo (VLC, MPC-HC, itp.)')
        print('=' * 70)

        return True

    except Exception as e:
        print(f'\n✗ Błąd podczas generowania napisów: {e}')
        print('\nSprawdź czy:')
        print('- Plik wideo nie jest uszkodzony')
        print('- Masz wystarczająco RAM (min. 2GB wolnego)')
        print('- ffmpeg jest poprawnie zainstalowany')
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
            print(f'✓ yt-dlp dostępny (wersja: {check.stdout.strip()})')
            return True
    except Exception:
        pass

    print('⚠️  yt-dlp nie jest zainstalowany — próbuję zainstalować automatycznie...')
    return update_ytdlp()


def update_ytdlp():
    """Instaluje lub aktualizuje yt-dlp do najnowszej wersji przez pip."""
    print('⏳ Instalacja/aktualizacja yt-dlp (pip install -U yt-dlp)...')
    try:
        res = subprocess.run([sys.executable, '-m', 'pip', 'install', '-U', 'yt-dlp'])
        if res.returncode == 0:
            check = subprocess.run(
                [sys.executable, '-m', 'yt_dlp', '--version'],
                capture_output=True, text=True
            )
            if check.returncode == 0:
                print(f'✓ yt-dlp gotowy (wersja: {check.stdout.strip()})')
                return True
    except Exception as e:
        print(f'✗ Błąd instalacji yt-dlp: {e}')
        return False

    print('✗ Nie udało się zainstalować/zaktualizować yt-dlp.')
    print('  Spróbuj ręcznie: pip install -U yt-dlp')
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
        return 1, f'Nie można uruchomić yt-dlp: {e}'

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

    # Problem z odczytem ciasteczek przeglądarki (częsty na Windows: przeglądarka
    # blokuje plik z ciasteczkami, gdy jest otwarta). NIE pomaga aktualizacja yt-dlp.
    if ('could not copy' in low and 'cookie' in low) or 'cookie database' in low \
            or 'could not find' in low and 'cookies' in low:
        return {
            'message': 'Nie udało się odczytać ciasteczek przeglądarki. ZAMKNIJ całkowicie '
                       'przeglądarkę (Chrome/Edge blokują plik ciasteczek, gdy są otwarte) '
                       'i spróbuj ponownie — albo użyj innej: --cookies-from-browser firefox. '
                       'Jeśli wideo działa bez logowania, po prostu pomiń --cookies-from-browser.',
            'suggest_update': False,
        }

    # Problemy, które zwykle naprawia aktualizacja yt-dlp (zmiany po stronie YouTube)
    update_signals = [
        'unable to extract', 'nsig extraction', 'signature extraction',
        'failed to extract any player response', 'precondition check failed',
        'http error 403', 'unable to download webpage', 'players/', 'jsinterp',
    ]
    if any(s in low for s in update_signals):
        return {
            'message': 'YouTube prawdopodobnie zmienił coś po swojej stronie '
                       '(błąd ekstrakcji). Zwykle pomaga aktualizacja yt-dlp.',
            'suggest_update': True,
        }

    if 'sign in to confirm your age' in low or 'age-restricted' in low or 'inappropriate' in low:
        return {
            'message': 'Wideo z ograniczeniem wiekowym — wymaga zalogowania. '
                       'Wyeksportuj ciasteczka z przeglądarki i użyj '
                       'yt-dlp --cookies-from-browser.',
            'suggest_update': False,
        }

    if "confirm you're not a bot" in low or 'sign in to confirm' in low:
        return {
            'message': 'YouTube żąda potwierdzenia, że nie jesteś botem. '
                       'Pomaga logowanie przez ciasteczka (--cookies-from-browser) '
                       'lub odczekanie i zmiana sieci/VPN.',
            'suggest_update': False,
        }

    if 'private video' in low:
        return {'message': 'To wideo jest prywatne — nie da się go pobrać.', 'suggest_update': False}

    if 'this video has been removed' in low or 'video unavailable' in low or 'account associated' in low:
        return {'message': 'Wideo jest niedostępne lub zostało usunięte.', 'suggest_update': False}

    if 'not available in your country' in low or 'blocked it in your country' in low or 'geo' in low:
        return {
            'message': 'Wideo zablokowane regionalnie. Mimo --geo-bypass nie udało '
                       'się obejść — spróbuj VPN.',
            'suggest_update': False,
        }

    if 'drm' in low:
        return {'message': 'Wideo chronione DRM — nie można go pobrać.', 'suggest_update': False}

    if 'http error 429' in low or 'too many requests' in low:
        return {
            'message': 'YouTube ogranicza liczbę żądań (HTTP 429). Odczekaj kilka '
                       'minut lub zmień sieć/VPN.',
            'suggest_update': False,
        }

    if any(s in low for s in ['getaddrinfo', 'timed out', 'connection', 'network', 'temporary failure']):
        return {'message': 'Problem z połączeniem internetowym.', 'suggest_update': False}

    return {
        'message': 'Nieznany błąd pobierania. Warto spróbować aktualizacji yt-dlp.',
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

    print(f'\n[Próba: {format_name}]')
    print(f'Format: {format_spec}')

    before_files = set(os.listdir(output_dir)) if os.path.isdir(output_dir) else set()
    returncode, output = run_ytdlp(command)

    if returncode == 0:
        filename = detect_downloaded_file(before_files, output_dir, format_choice)
        print(f'✓ Sukces! Pobrano używając: {format_name}')
        return True, filename, output

    print(f'✗ Nieudane: {format_name}')
    return False, None, output


def attempt_all_strategies(url, strategies, output_template, output_dir, format_choice,
                           cookies_from_browser=None):
    """
    Przechodzi przez wszystkie strategie po kolei aż któraś zadziała.
    Zwraca: (success, nazwa_pliku, połączony_output_wszystkich_prób)
    """
    combined_output = []
    for i, (format_spec, format_name, extra_args) in enumerate(strategies, 1):
        print(f'\nStrategia {i}/{len(strategies)}')
        success, filename, output = try_download(
            url, format_spec, format_name, output_template, extra_args, output_dir,
            format_choice, cookies_from_browser
        )
        combined_output.append(output)

        if success:
            return True, filename, '\n'.join(combined_output)

        if i < len(strategies):
            print('Próbuję następną strategię...')

    return False, None, '\n'.join(combined_output)


def download_youtube(raw_url, format_choice='mp4', generate_subs=False, whisper_model='base',
                     source_lang='pl', translate_to=None, initial_prompt=None,
                     cookies_from_browser=None, output_dir=None):
    url = extract_url(raw_url)
    if not url:
        print('Nie wykryto poprawnego linku YouTube.')
        return

    if output_dir:
        os.makedirs(output_dir, exist_ok=True)
    else:
        output_dir = os.getcwd()
    output_template = os.path.join(output_dir, '%(title)s.%(ext)s')

    print(f'\nPobieranie z: {url}')
    print(f'Katalog docelowy: {output_dir}')
    print(f'Format: {format_choice}')
    if generate_subs:
        print(f'Generowanie napisów: TAK (model: {whisper_model})')
        print(f'  Język audio: {lang_name(source_lang)}')
        if translate_to and translate_to != source_lang:
            print(f'  Tłumaczenie na: {lang_name(translate_to)}')
    print('\n' + '=' * 60)

    # Upewnij się, że yt-dlp jest dostępny (w razie potrzeby zainstaluj)
    if not ensure_ytdlp():
        print('\n✗ Bez yt-dlp nie da się pobierać. Przerywam.')
        return

    downloaded_file = None

    if format_choice == 'mp4':
        strategies = [
            ('bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]',
             'Najlepsza jakość MP4 z osobnym audio',
             ['--merge-output-format', 'mp4']),

            ('bestvideo+bestaudio/best',
             'Najlepsza jakość (dowolne kodeki) z łączeniem',
             ['--merge-output-format', 'mp4']),

            ('22',
             'Format 22 (720p MP4)',
             None),

            ('18',
             'Format 18 (360p MP4)',
             None),

            ('best[ext=mp4]',
             'Najlepszy dostępny MP4',
             None),

            ('best',
             'Najlepszy dostępny format (fallback)',
             ['--merge-output-format', 'mp4', '--recode-video', 'mp4']),
        ]

    elif format_choice == 'mp3':
        strategies = [
            ('bestaudio[ext=m4a]/bestaudio',
             'Najlepsza jakość audio z konwersją na MP3',
             ['-x', '--audio-format', 'mp3', '--audio-quality', '0']),

            ('bestaudio',
             'Najlepsza jakość audio (alternatywna konwersja)',
             ['-x', '--audio-format', 'mp3']),

            ('140',
             'Format 140 (M4A) z konwersją na MP3',
             ['-x', '--audio-format', 'mp3']),

            ('best',
             'Najlepszy format z ekstrakcją audio',
             ['-x', '--audio-format', 'mp3']),
        ]
    else:
        print('Nieobsługiwany format.')
        return

    # Próbuj wszystkich strategii po kolei
    success, downloaded_file, output = attempt_all_strategies(
        url, strategies, output_template, output_dir, format_choice, cookies_from_browser
    )

    # Jeśli wszystko zawiodło — zdiagnozuj i ewentualnie zaktualizuj yt-dlp i spróbuj raz jeszcze
    if not success:
        diag = diagnose_failure(output)
        print('\n' + '=' * 60)
        print('⚠️  Wszystkie strategie zawiodły.')
        print(f'Diagnoza: {diag["message"]}')
        print('=' * 60)

        if diag['suggest_update']:
            print('\n🔄 Próbuję zaktualizować yt-dlp i ponowić pobieranie...')
            if update_ytdlp():
                success, downloaded_file, output = attempt_all_strategies(
                    url, strategies, output_template, output_dir, format_choice, cookies_from_browser
                )

    if success:
        print('\n' + '=' * 60)
        print('✓✓✓ POBRANO POMYŚLNIE ✓✓✓')
        print(f'Lokalizacja: {output_dir}')
        if downloaded_file:
            print(f'Plik: {downloaded_file}')
        print('=' * 60)
    else:
        diag = diagnose_failure(output)
        print('\n' + '=' * 60)
        print('✗✗✗ BŁĄD: Nie udało się pobrać wideo ✗✗✗')
        print(f'\nPrawdopodobna przyczyna: {diag["message"]}')
        print('\nCo możesz zrobić:')
        print('- Sprawdź, czy wideo otwiera się w przeglądarce')
        print('- Dla wideo z logowaniem/ograniczeniem wieku: --cookies-from-browser')
        print('- Przy blokadzie regionalnej: włącz VPN')
        print('- Przy HTTP 429: odczekaj kilka minut')
        print('- Zaktualizuj ręcznie: pip install -U yt-dlp')
        print('=' * 60)
        return

    # Jeśli pobrano pomyślnie i włączono generowanie napisów
    if success and generate_subs and format_choice == 'mp4':
        # Jeśli nie znamy dokładnej nazwy pliku, spróbuj znaleźć
        if not downloaded_file:
            # Znajdź najnowszy plik MP4 w katalogu
            mp4_files = [f for f in os.listdir(output_dir) if f.endswith('.mp4')]
            if mp4_files:
                mp4_files.sort(key=lambda x: os.path.getmtime(os.path.join(output_dir, x)), reverse=True)
                downloaded_file = mp4_files[0]

        if downloaded_file:
            full_path = os.path.join(output_dir, downloaded_file) if not os.path.isabs(downloaded_file) else downloaded_file
            generate_subtitles_with_whisper(full_path, whisper_model, source_lang, translate_to, initial_prompt, output_dir)
        else:
            print('\n⚠️  Nie można znaleźć pobranego pliku do generowania napisów.')
    elif generate_subs and format_choice == 'mp3':
        print('\n⚠️  Generowanie napisów jest dostępne tylko dla formatu MP4.')


def main():
    parser = argparse.ArgumentParser(
        description='Pobieranie z YouTube w mp4 lub mp3 z automatycznym obchodzeniem błędów',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Przykłady użycia:
  %(prog)s "https://youtube.com/watch?v=..."
  %(prog)s "https://youtube.com/watch?v=..." --format mp3
  %(prog)s "https://youtube.com/watch?v=..." --subs
  %(prog)s "https://youtube.com/watch?v=..." --subs --model medium

  # Film po hiszpańsku -> polskie napisy (tłumaczenie):
  %(prog)s "https://youtube.com/watch?v=..." --subs --source-lang es --translate-to pl

  # Auto-wykrycie języka oryginału -> polskie napisy:
  %(prog)s "https://youtube.com/watch?v=..." --subs --source-lang auto --translate-to pl

  # Plik lokalny na dysku -> przetłumaczone polskie napisy (bez pobierania):
  %(prog)s --file "C:\\filmy\\wideo.mp4" --source-lang es --translate-to pl

  # Film 18+/z logowaniem (ciasteczka z przeglądarki) i własny katalog wyników:
  %(prog)s "https://youtube.com/watch?v=..." --cookies-from-browser chrome --output-dir "C:\\pobrane"
        """
    )
    parser.add_argument('url', nargs='?', help='Link do filmu na YouTube')
    parser.add_argument(
        '--file',
        help='Ścieżka do pliku wideo/audio JUŻ na dysku. Pomija pobieranie '
             'i od razu generuje (i opcjonalnie tłumaczy) napisy.'
    )
    parser.add_argument(
        '--format',
        choices=['mp4', 'mp3'],
        default='mp4',
        help='Format do pobrania (domyślnie: mp4)'
    )
    parser.add_argument(
        '--subs',
        action='store_true',
        help='Generuj napisy używając Whisper AI (tylko dla MP4). '
             'Domyślnie w języku oryginału; użyj --translate-to aby przetłumaczyć.'
    )
    parser.add_argument(
        '--model',
        default='base',
        help='Model Whisper (domyślnie: base). Dowolna nazwa obsługiwana przez Whisper, '
             'np.: tiny, base, small, medium, large, large-v3, turbo. '
             'Większy = dokładniejszy, ale wolniejszy. Nowsze modele działają '
             'automatycznie, gdy tylko pojawią się w bibliotece whisper.'
    )
    parser.add_argument(
        '--source-lang',
        default='pl',
        help='Język audio w nagraniu, np. es, en, de (domyślnie: pl). '
             'Użyj "auto" do automatycznego wykrycia języka przez Whisper.'
    )
    parser.add_argument(
        '--translate-to',
        default=None,
        help='Docelowy język napisów, np. pl. Włącza tłumaczenie (Google Translate). '
             'Bez tego napisy pozostają w języku oryginału.'
    )
    parser.add_argument(
        '--prompt',
        default=None,
        help='Podpowiedź kontekstowa dla Whisper (initial_prompt): nazwy własne, '
             'imiona postaci, terminologia, poprawna pisownia. Wyraźnie poprawia '
             'dokładność, np.: --prompt "Pomni, Ragatha, Jax, Caine, Gangle, Zooble".'
    )
    parser.add_argument(
        '--cookies-from-browser',
        default=None,
        metavar='BROWSER',
        help='Pobieraj używając ciasteczek z przeglądarki (chrome, firefox, edge, '
             'brave, opera...). Rozwiązuje filmy 18+ / "potwierdź, że nie jesteś botem".'
    )
    parser.add_argument(
        '--output-dir',
        default=None,
        metavar='KATALOG',
        help='Katalog na pliki wynikowe (wideo i/lub napisy). Domyślnie bieżący katalog.'
    )

    args = parser.parse_args()

    if args.file:
        # Tryb pliku lokalnego — bez pobierania, od razu napisy/tłumaczenie
        if not os.path.exists(args.file):
            print(f'✗ Nie znaleziono pliku: {args.file}')
            sys.exit(1)
        generate_subtitles_with_whisper(args.file, args.model, args.source_lang,
                                        args.translate_to, args.prompt, args.output_dir)
    elif args.url:
        download_youtube(args.url, args.format, args.subs, args.model,
                         args.source_lang, args.translate_to, args.prompt,
                         args.cookies_from_browser, args.output_dir)
    else:
        parser.error('Podaj link do YouTube albo użyj --file ze ścieżką do pliku na dysku.')


if __name__ == '__main__':
    main()
