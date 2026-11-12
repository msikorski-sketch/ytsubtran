"""Reusable ASR and standalone subtitle translation without model/API downloads."""
import json
import os
from pathlib import Path
import sys
import types

import pytest

import subtitle_workflow as sw
import youtube_downloader as yt


SRT = '7\n00:00:01,001 --> 00:00:02,999\nHello\nworld\n\n8\n00:00:03,500 --> 00:00:04,000\nGoodbye\n'
VTT = ('WEBVTT\nX-TIMESTAMP-MAP=LOCAL:00:00:00.000,MPEGTS:900000\n\n'
       'STYLE\n::cue { color: lime; }\n\nREGION\nid:main\n\nNOTE reviewer\nKeep this note.\n\n'
       'speaker-one\n00:01.001 --> 00:02.999 line:90% align:start\n<v Alice>Hello</v>\n\n'
       '00:00:03.500 --> 00:00:04.000\nGoodbye\n')


def test_srt_preserves_ids_exact_timestamps_and_multiline_payload(tmp_path):
    path = tmp_path/'source.srt'
    path.write_bytes(b'\xef\xbb\xbf' + SRT.replace('\n', '\r\n').encode())
    doc = sw.read_subtitles(path)
    assert doc.texts == ['Hello\nworld', 'Goodbye']
    output = doc.render(['Cześć\nświecie', 'Do widzenia'])
    assert '7\n00:00:01,001 --> 00:00:02,999\nCześć\nświecie' in output
    assert '8\n00:00:03,500 --> 00:00:04,000\nDo widzenia' in output
    assert '00:00:01.001 --> 00:00:02.999' in doc.render_vtt(['Hello', 'Goodbye'])


def test_vtt_preserves_metadata_ids_settings_and_inline_markup(tmp_path):
    path = tmp_path/'source.vtt'
    path.write_text(VTT)
    doc = sw.read_subtitles(path)
    assert doc.texts == ['<v Alice>Hello</v>', 'Goodbye']
    output = doc.render(['<v Alice>Cześć</v>', 'Do widzenia'])
    assert output == VTT.replace('Hello', 'Cześć').replace('Goodbye', 'Do widzenia')
    assert doc.render_vtt(['<v Alice>Cześć</v>', 'Do widzenia']) == output


@pytest.mark.parametrize('name,content', [
    ('a.srt', ''), ('a.srt', 'invalid'), ('a.srt', '1\n00:01 --> 00:02\nHello'),
    ('a.srt', '1\n00:00:03,000 --> 00:00:02,000\nHello'),
    ('a.srt', '1\n00:00:01,000 --> 00:00:02,000'),
    ('a.srt', '1\n00:60:01,000 --> 00:60:02,000\nHello'),
    ('a.vtt', '00:01.000 --> 00:02.000\nHello'),
    ('a.vtt', 'WEBVTT\n\nNOTE only a note'),
    ('a.vtt', 'WEBVTT\n\n00:01,000 --> 00:02,000\nHello'),
    ('a.vtt', 'WEBVTT\n00:01.000 --> 00:02.000\nLost first cue\n\n00:03.000 --> 00:04.000\nSecond'),
    ('a.txt', SRT),
])
def test_invalid_subtitles_are_rejected(tmp_path, name, content):
    path = tmp_path/name
    path.write_text(content)
    with pytest.raises(ValueError):
        sw.read_subtitles(path)


def test_render_rejects_missing_or_empty_translations(tmp_path):
    path = tmp_path/'source.srt'
    path.write_text(SRT)
    doc = sw.read_subtitles(path)
    for translated in (['one'], ['', 'two']):
        with pytest.raises(ValueError):
            doc.render(translated)


def test_markup_tokens_protect_tags_timestamps_and_ass_overrides():
    texts = ['<v Alice><i>Hello</i> <00:01.500>world</v>', r'{\an8}Goodbye']
    masked, replacements = sw.protect_markup(texts)
    assert all('<' not in text and '{' not in text for text in masked)
    translated = [t.replace('Hello', 'Cześć').replace('world', 'świecie').replace('Goodbye', 'Do widzenia')
                  for t in masked]
    assert sw.restore_markup(translated, replacements) == [
        '<v Alice><i>Cześć</i> <00:01.500>świecie</v>', r'{\an8}Do widzenia']
    token = replacements[0][0][0]
    with pytest.raises(ValueError, match='formatting tokens'):
        sw.restore_markup([translated[0].replace(token, ''), translated[1]], replacements)
    with pytest.raises(ValueError, match='formatting tokens'):
        sw.restore_markup([translated[0] + token, translated[1]], replacements)


@pytest.fixture
def fake_asr(monkeypatch):
    calls = []
    def transcribe(media, model, language, prompt):
        calls.append((media, model, language, prompt))
        return ([{'start': 0., 'end': 1., 'text': 'Hello'}], 'Hello', 'en')
    qe = types.SimpleNamespace(check_qwen_installed=lambda: (False, None),
                               cuda_available=lambda: False,
                               choose_asr_mode=lambda *a: ('whisper', []),
                               DEFAULT_QWEN_MODEL='default-qwen')
    monkeypatch.setattr(yt, '_import_qwen_engine', lambda: qe)
    monkeypatch.setattr(yt, '_transcribe_with_whisper', transcribe)
    monkeypatch.setattr(yt, 'check_ffmpeg_installed', lambda: True)
    monkeypatch.setattr(yt, 'check_translator_installed', lambda: True)
    monkeypatch.setattr(sw, 'asr_versions', lambda: {'test-asr': '1'})
    return calls


def test_completed_transcription_reuses_without_ffmpeg_or_asr(tmp_path, monkeypatch, fake_asr):
    media = tmp_path/'video.mp4'
    media.write_bytes(b'media')
    assert yt.generate_subtitles(str(media), source_lang='auto')
    cache = Path(sw.transcription_path(str(media)))
    assert json.loads(cache.read_text())['language'] == 'en'
    monkeypatch.setattr(yt, 'check_ffmpeg_installed', lambda: pytest.fail('ffmpeg checked for cache hit'))
    monkeypatch.setattr(yt, '_import_qwen_engine', lambda: pytest.fail('ASR imported for cache hit'))
    (tmp_path/'video_EN.srt').unlink()
    assert yt.generate_subtitles(str(media), source_lang='auto', also_vtt=True)
    assert len(fake_asr) == 1
    assert (tmp_path/'video_EN.srt').exists()
    assert (tmp_path/'video_EN.vtt').exists()


def test_translation_retry_and_new_target_language_reuse_transcription(tmp_path, monkeypatch, fake_asr):
    media = tmp_path/'video.mp4'
    media.write_bytes(b'media')
    def offline(*args):
        raise yt.TranslationError('offline')
    monkeypatch.setattr(yt, 'translate_texts', offline)
    assert not yt.generate_subtitles(str(media), source_lang='auto', translate_to='pl')
    assert Path(sw.transcription_path(str(media))).exists()
    assert (tmp_path/'video_EN.srt').exists()
    assert not (tmp_path/'video_PL.srt').exists()
    monkeypatch.setattr(yt, 'translate_texts', lambda texts, target, source: [target + ' translated'])
    for target in ('pl', 'de'):
        assert yt.generate_subtitles(str(media), source_lang='auto', translate_to=target)
        assert target + ' translated' in (tmp_path/f'video_{target.upper()}.srt').read_text()
    assert len(fake_asr) == 1


@pytest.mark.parametrize('changes', [
    {'model_size': 'small'}, {'source_lang': 'pl'}, {'initial_prompt': 'Alice'},
    {'engine': 'hybrid'}, {'qwen_model': 'another-qwen'}, {'force_transcribe': True},
])
def test_asr_option_changes_invalidate_saved_transcription(tmp_path, fake_asr, changes):
    media = tmp_path/'video.mp4'
    media.write_bytes(b'media')
    assert yt.generate_subtitles(str(media), source_lang='auto')
    options = {'source_lang': 'auto', **changes}
    assert yt.generate_subtitles(str(media), **options)
    assert len(fake_asr) == 2


def test_media_change_invalidates_state_even_at_same_size(tmp_path, fake_asr):
    media = tmp_path/'video.mp4'
    media.write_bytes(b'first')
    assert yt.generate_subtitles(str(media))
    before = media.stat()
    media.write_bytes(b'other')
    os.utime(media, ns=(before.st_atime_ns, before.st_mtime_ns + 1_000_000_000))
    assert yt.generate_subtitles(str(media))
    assert len(fake_asr) == 2


def test_state_path_separates_media_containers_and_uses_output_folder(tmp_path, fake_asr):
    output = tmp_path/'results'
    for name in ('same.mp4', 'same.wav'):
        media = tmp_path/name
        media.write_bytes(name.encode())
        assert yt.generate_subtitles(str(media), output_dir=str(output))
        assert (output/(name + '.transcription.json')).exists()
    assert len(fake_asr) == 2


def test_cache_hit_reports_export_error_without_traceback(tmp_path, monkeypatch, fake_asr, capsys):
    media = tmp_path/'video.mp4'
    media.write_bytes(b'media')
    assert yt.generate_subtitles(str(media))
    def fail(*args):
        raise OSError('output unavailable')
    monkeypatch.setattr(yt, 'write_srt', fail)
    assert not yt.generate_subtitles(str(media))
    assert len(fake_asr) == 1
    assert 'Could not export subtitles from saved transcription' in capsys.readouterr().out


def test_failed_forced_asr_preserves_previous_completed_state(tmp_path, monkeypatch, fake_asr):
    media = tmp_path/'video.mp4'
    media.write_bytes(b'media')
    assert yt.generate_subtitles(str(media))
    cache = Path(sw.transcription_path(str(media)))
    previous = cache.read_bytes()
    monkeypatch.setattr(yt, '_transcribe_with_whisper', lambda *args: None)
    assert not yt.generate_subtitles(str(media), force_transcribe=True)
    assert cache.read_bytes() == previous
    assert yt.generate_subtitles(str(media))


def test_library_version_change_invalidates_state(tmp_path, monkeypatch, fake_asr):
    media = tmp_path/'video.mp4'
    media.write_bytes(b'media')
    assert yt.generate_subtitles(str(media))
    monkeypatch.setattr(sw, 'asr_versions', lambda: {'test-asr': '2'})
    assert yt.generate_subtitles(str(media))
    assert len(fake_asr) == 2


@pytest.mark.parametrize('damage', ['truncated', 'version', 'nan', 'language', 'text'])
def test_invalid_state_is_ignored_and_regenerated(tmp_path, fake_asr, damage):
    media = tmp_path/'video.mp4'
    media.write_bytes(b'media')
    assert yt.generate_subtitles(str(media))
    cache = Path(sw.transcription_path(str(media)))
    data = json.loads(cache.read_text())
    if damage == 'truncated':
        cache.write_text('{"segments":')
    else:
        if damage == 'version':
            data['version'] = -1
        elif damage == 'nan':
            data['segments'][0]['end'] = float('nan')
        elif damage == 'language':
            data['language'] = '../../escape'
        else:
            data['segments'][0]['text'] = None
        cache.write_text(json.dumps(data))
    assert yt.generate_subtitles(str(media))
    assert len(fake_asr) == 2
    assert json.loads(cache.read_text())['version'] == sw.CACHE_VERSION


def test_cache_atomic_write_failure_preserves_previous_file(tmp_path, monkeypatch):
    path = tmp_path/'state.json'
    path.write_text('previous state')
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(sw.os, 'replace', fail)
    with pytest.raises(OSError):
        sw.save_transcription(path, {}, [{'start': 0, 'end': 1, 'text': 'Hi'}], 'Hi', 'en', 'whisper')
    assert path.read_text() == 'previous state'
    assert list(tmp_path.iterdir()) == [path]


def test_cache_write_error_does_not_discard_successful_transcription(tmp_path, monkeypatch, fake_asr):
    media = tmp_path/'video.mp4'
    media.write_bytes(b'media')
    def fail(*args):
        raise OSError('read-only state folder')
    monkeypatch.setattr(sw, 'save_transcription', fail)
    assert yt.generate_subtitles(str(media))
    assert 'Hello' in (tmp_path/'video_EN.srt').read_text()


def test_media_changed_during_asr_is_not_cached_or_exported(tmp_path, monkeypatch, fake_asr):
    media = tmp_path/'video.mp4'
    media.write_bytes(b'media')
    def changing(*args):
        media.write_bytes(b'different recording')
        return ([{'start': 0., 'end': 1., 'text': 'Hi'}], 'Hi', 'en')
    monkeypatch.setattr(yt, '_transcribe_with_whisper', changing)
    assert not yt.generate_subtitles(str(media))
    assert not Path(sw.transcription_path(str(media))).exists()
    assert not (tmp_path/'video_EN.srt').exists()


def test_resolve_conversion_can_reuse_original_media_transcription(tmp_path, fake_asr):
    source = tmp_path/'video.mp4'
    converted = tmp_path/'video_resolve.mov'
    source.write_bytes(b'original')
    converted.write_bytes(b'converted first time')
    assert yt.generate_subtitles(str(converted), cache_source=str(source))
    converted.write_bytes(b'converted again')
    assert yt.generate_subtitles(str(converted), cache_source=str(source))
    assert len(fake_asr) == 1
    assert Path(sw.transcription_path(str(source))).exists()


@pytest.fixture
def no_media(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail('Subtitle translation tried to use media/ASR/download setup')
    for name in ('generate_subtitles', '_import_qwen_engine', 'check_ffmpeg_installed',
                 'download_youtube', 'resolve_default_output_dir', 'prompt_output_dir'):
        monkeypatch.setattr(yt, name, forbidden)
    monkeypatch.setattr(yt, 'translate_texts', lambda texts, *a: [
        text.replace('Hello', 'Cześć').replace('world', 'świecie').replace('Goodbye', 'Do widzenia')
        for text in texts])


@pytest.mark.parametrize('fmt,content', [('srt', SRT), ('vtt', VTT)])
def test_standalone_translation_preserves_source_and_format(tmp_path, no_media, fmt, content):
    path = tmp_path/f'source.{fmt}'
    path.write_text(content)
    original = path.read_bytes()
    assert yt.translate_subtitle_file(str(path), 'pl')
    target = tmp_path/f'source_PL.{fmt}'
    assert target.read_text() == content.replace('Hello', 'Cześć').replace('world', 'świecie').replace('Goodbye', 'Do widzenia')
    assert path.read_bytes() == original


def test_subtitle_output_folder_and_optional_vtt_export(tmp_path, no_media):
    path = tmp_path/'source.srt'
    path.write_text(SRT)
    output = tmp_path/'new'/'folder'
    assert yt.translate_subtitle_file(str(path), 'pl', output_dir=str(output), also_vtt=True)
    assert (output/'source_PL.srt').exists()
    assert '00:00:01.001 --> 00:00:02.999' in (output/'source_PL.vtt').read_text()
    assert (output/'source_PL.txt').exists()
    assert not (tmp_path/'source_PL.srt').exists()


def test_subtitle_translation_failure_leaves_existing_outputs_unchanged(tmp_path, monkeypatch, no_media):
    path = tmp_path/'source.srt'
    path.write_text(SRT)
    target = tmp_path/'source_PL.srt'
    target.write_text('previous good translation')
    def fail(*args):
        raise yt.TranslationError('offline')
    monkeypatch.setattr(yt, 'translate_texts', fail)
    assert not yt.translate_subtitle_file(str(path), 'pl')
    assert target.read_text() == 'previous good translation'
    assert not (tmp_path/'source_PL.txt').exists()
    assert path.read_text() == SRT


def test_changed_markup_tokens_reject_translation_before_any_output(tmp_path, monkeypatch, no_media):
    path = tmp_path/'source.vtt'
    path.write_text(VTT)
    monkeypatch.setattr(yt, 'translate_texts', lambda texts, *a: ['Cześć', 'Do widzenia'])
    assert not yt.translate_subtitle_file(str(path), 'pl')
    assert not (tmp_path/'source_PL.vtt').exists()


@pytest.mark.parametrize('language', ['../pl', '', '/tmp/escape', None])
def test_invalid_target_code_is_rejected(tmp_path, no_media, language):
    path = tmp_path/'source.srt'
    path.write_text(SRT)
    assert not yt.translate_subtitle_file(str(path), language)
    assert list(tmp_path.iterdir()) == [path]


def test_subtitle_source_symlink_cannot_be_overwritten_by_target(tmp_path, no_media):
    path = tmp_path/'source.srt'
    path.write_text(SRT)
    target = tmp_path/'source_PL.srt'
    try:
        target.symlink_to(path)
    except (OSError, NotImplementedError):
        pytest.skip('Symlinks are not available for this user/filesystem')
    assert not yt.translate_subtitle_file(str(path), 'pl')
    assert path.read_text() == SRT


def test_rate_limit_error_identifies_reason_without_subtitle_text(monkeypatch):
    class TooManyRequests(Exception):
        pass
    def fail(text):
        raise TooManyRequests('private subtitle contents should not be printed')
    module = types.ModuleType('deep_translator')
    module.GoogleTranslator = lambda **kwargs: types.SimpleNamespace(translate=fail)
    monkeypatch.setitem(sys.modules, 'deep_translator', module)
    with pytest.raises(yt.TranslationError) as error:
        yt.translate_texts(['private source'], 'pl', 'en')
    assert 'TooManyRequests (1)' in str(error.value)
    assert 'try again later' in str(error.value)
    assert 'private' not in str(error.value)


@pytest.mark.parametrize('flag', ['--subtitle-file', '--file'])
def test_cli_translates_subtitles_without_media_setup(tmp_path, monkeypatch, no_media, flag):
    path = tmp_path/'source.srt'
    path.write_text(SRT)
    monkeypatch.setattr(sys, 'argv', ['ytsubtran', flag, str(path), '--translate-to', 'pl'])
    yt.main()
    assert (tmp_path/'source_PL.srt').exists()


@pytest.mark.parametrize('extra', [[], ['--translate-to', 'pl', '--burn'],
    ['--translate-to', 'pl', '--force-transcribe'], ['--translate-to', 'pl', '--resolve-linux'],
    ['--translate-to', 'pl', '--start', '1'], ['--translate-to', 'pl', '--watch-clipboard'],
    ['--translate-to', 'pl', '--engine', 'qwen'], ['--translate-to', 'pl', '--snap-cuts'],
    ['--translate-to', 'pl', '--clips-copy']])
def test_cli_rejects_missing_target_and_incompatible_media_flags(tmp_path, monkeypatch, no_media, extra):
    path = tmp_path/'source.srt'
    path.write_text(SRT)
    monkeypatch.setattr(sys, 'argv', ['ytsubtran', '--subtitle-file', str(path)] + extra)
    with pytest.raises(SystemExit) as error:
        yt.main()
    assert error.value.code == 2


def test_cli_failed_subtitle_translation_exits_nonzero(tmp_path, monkeypatch, no_media):
    monkeypatch.setattr(sys, 'argv', ['ytsubtran', '--subtitle-file', str(tmp_path/'missing.srt'), '--translate-to', 'pl'])
    with pytest.raises(SystemExit) as error:
        yt.main()
    assert error.value.code == 1


def test_force_transcribe_propagates_in_clipboard_download_options(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(yt, 'watch_clipboard', lambda **kw: seen.update(kw))
    monkeypatch.setattr(sys, 'argv', ['ytsubtran', '--watch-clipboard', '--force-transcribe', '--output-dir', str(tmp_path)])
    yt.main()
    assert seen['force_transcribe'] is True
    assert seen['generate_subs'] is True
