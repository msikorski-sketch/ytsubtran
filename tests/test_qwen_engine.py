"""
Unit tests for the pure (side-effect-free) helpers of the Qwen3-ASR engine.

qwen_engine imports only the standard library at module level (torch/qwen_asr
are imported lazily inside functions), so these tests run fast and without the
heavy dependencies installed.
"""
import qwen_engine as qe


# ---------------------------------------------------------------------------
# choose_asr_mode — engine selection logic
# ---------------------------------------------------------------------------

def test_mode_whisper_explicit_ignores_qwen():
    mode, notes = qe.choose_asr_mode('whisper', 'ja', qwen_ok=True, gpu_ok=True)
    assert mode == 'whisper'
    assert notes == []


def test_mode_auto_without_qwen_falls_back_with_tip():
    mode, notes = qe.choose_asr_mode('auto', 'pl', qwen_ok=False, gpu_ok=True)
    assert mode == 'whisper'
    assert any('qwen-asr' in n for n in notes)  # installation tip


def test_mode_explicit_qwen_without_install_requests_guide():
    mode, _ = qe.choose_asr_mode('qwen', 'ja', qwen_ok=False, gpu_ok=True)
    assert mode == 'install-qwen'


def test_mode_auto_without_gpu_uses_whisper():
    mode, notes = qe.choose_asr_mode('auto', 'ja', qwen_ok=True, gpu_ok=False)
    assert mode == 'whisper'
    assert any('GPU' in n for n in notes)


def test_mode_explicit_qwen_without_gpu_allowed_with_warning():
    # An explicit request runs on CPU (user's choice), but must warn
    mode, notes = qe.choose_asr_mode('qwen', 'ja', qwen_ok=True, gpu_ok=False)
    assert mode == 'qwen'
    assert any('slow' in n.lower() for n in notes)


def test_mode_qwen_for_aligner_language():
    # Japanese has ForcedAligner support -> pure Qwen with timestamps
    mode, notes = qe.choose_asr_mode('qwen', 'ja', qwen_ok=True, gpu_ok=True)
    assert mode == 'qwen'
    assert notes == []


def test_mode_qwen_polish_switches_to_hybrid():
    # Polish is supported by Qwen but not by its aligner -> hybrid
    mode, notes = qe.choose_asr_mode('qwen', 'pl', qwen_ok=True, gpu_ok=True)
    assert mode == 'hybrid'
    assert any('hybrid' in n.lower() for n in notes)


def test_mode_qwen_unsupported_language_uses_whisper():
    # Ukrainian is not among Qwen's 30 languages
    mode, notes = qe.choose_asr_mode('qwen', 'uk', qwen_ok=True, gpu_ok=True)
    assert mode == 'whisper'
    assert any('does not support' in n for n in notes)


def test_mode_auto_lang_detection_defers_to_qwen():
    mode, _ = qe.choose_asr_mode('auto', 'auto', qwen_ok=True, gpu_ok=True)
    assert mode == 'qwen'


def test_mode_hybrid_explicit():
    mode, _ = qe.choose_asr_mode('hybrid', 'pl', qwen_ok=True, gpu_ok=True)
    assert mode == 'hybrid'


def test_mode_unknown_engine_falls_back_to_whisper():
    mode, notes = qe.choose_asr_mode('nonsense', 'pl', qwen_ok=True, gpu_ok=True)
    assert mode == 'whisper'
    assert notes  # explains the fallback


# ---------------------------------------------------------------------------
# Language code <-> Qwen canonical name mapping
# ---------------------------------------------------------------------------

def test_qwen_lang_name():
    assert qe.qwen_lang_name('ja') == 'Japanese'
    assert qe.qwen_lang_name('pl') == 'Polish'
    assert qe.qwen_lang_name('uk') is None  # not supported by Qwen


def test_code_from_qwen_language_single():
    assert qe.code_from_qwen_language('Japanese') == 'ja'


def test_code_from_qwen_language_merged_takes_first():
    # Multilingual audio: Qwen reports e.g. "Japanese,English"
    assert qe.code_from_qwen_language('Japanese,English') == 'ja'


def test_code_from_qwen_language_empty_or_unknown():
    assert qe.code_from_qwen_language('') is None
    assert qe.code_from_qwen_language(None) is None
    assert qe.code_from_qwen_language('Klingon') is None


def test_aligner_langs_are_a_subset_of_qwen_langs():
    assert qe.ALIGNER_LANGS <= set(qe.QWEN_LANGUAGES)
    assert 'ja' in qe.ALIGNER_LANGS
    assert 'pl' not in qe.ALIGNER_LANGS  # the whole reason hybrid mode exists


# ---------------------------------------------------------------------------
# group_align_items — word-level aligner output -> subtitle segments
# ---------------------------------------------------------------------------

def _word(text, start, end):
    return {'text': text, 'start_time': start, 'end_time': end}


def test_group_splits_on_long_pause():
    items = [
        _word('Hello', 0.0, 0.4),
        _word('world', 0.5, 0.9),
        _word('Again', 3.0, 3.4),  # 2.1 s pause -> new segment
    ]
    segs = qe.group_align_items(items, 'en')
    assert len(segs) == 2
    assert segs[0]['text'] == 'Hello world'
    assert segs[1]['text'] == 'Again'
    assert segs[0]['start'] == 0.0
    assert segs[0]['end'] == 0.9


def test_group_splits_after_sentence_end():
    items = [
        _word('Done.', 0.0, 0.4),
        _word('Next', 0.5, 0.9),
    ]
    segs = qe.group_align_items(items, 'en')
    assert [s['text'] for s in segs] == ['Done.', 'Next']


def test_group_respects_max_duration():
    # One word per second for 20 s with no pauses or punctuation
    items = [_word(f'w{i}', float(i), i + 0.9) for i in range(20)]
    segs = qe.group_align_items(items, 'en', max_dur=7.0)
    assert len(segs) >= 2
    assert all(s['end'] - s['start'] <= 7.0 + 1e-6 for s in segs)


def test_group_cjk_joins_without_spaces():
    items = [
        _word('こん', 0.0, 0.3),
        _word('にちは', 0.3, 0.8),
    ]
    segs = qe.group_align_items(items, 'ja')
    assert segs[0]['text'] == 'こんにちは'


def test_group_skips_empty_items_and_fixes_reversed_times():
    items = [
        _word('  ', 0.0, 0.1),          # whitespace only -> dropped
        _word('ok', 1.0, 0.5),          # end < start -> clamped, no crash
    ]
    segs = qe.group_align_items(items, 'en')
    assert len(segs) == 1
    assert segs[0]['end'] >= segs[0]['start']


def test_group_accepts_attribute_items():
    class Item:
        def __init__(self, text, start_time, end_time):
            self.text = text
            self.start_time = start_time
            self.end_time = end_time

    segs = qe.group_align_items([Item('Hi', 0.0, 0.2)], 'en')
    assert segs == [{'start': 0.0, 'end': 0.2, 'text': 'Hi'}]


# ---------------------------------------------------------------------------
# Text similarity (hybrid-mode cross-check)
# ---------------------------------------------------------------------------

def test_similarity_ignores_case_punctuation_and_spacing():
    assert qe._similarity('Hello, world!', 'hello world') == 1.0


def test_similarity_detects_differences():
    assert qe._similarity('completely different', 'zupełnie inaczej') < 0.6


def test_similarity_of_two_empty_texts_is_full():
    assert qe._similarity('', '  ') == 1.0


# ---------------------------------------------------------------------------
# AlignerUnsupported carries the detected language
# ---------------------------------------------------------------------------

def test_aligner_unsupported_exposes_lang_code():
    err = qe.AlignerUnsupported('pl')
    assert err.lang_code == 'pl'
    assert isinstance(err, qe.QwenEngineError)
