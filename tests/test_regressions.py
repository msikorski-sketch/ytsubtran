"""Regression coverage for downloads, translation failures and insert editing.

Remote services are replaced at their boundary. Media tests use real ffmpeg;
the packaging test builds a wheel and imports it outside the source checkout.
"""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import types
import zipfile

import pytest

import inserts
import organize
import youtube_downloader as yt


FIRST_ID = 'AAAAAAAAAAA'
SECOND_ID = 'BBBBBBBBBBB'
URL = f'https://www.youtube.com/watch?v={SECOND_ID}'


@pytest.fixture
def local_downloads(monkeypatch):
    """Simulate yt-dlp's template and existing-file behavior, without a network."""
    calls = []

    def run(command):
        calls.append(command)
        video_id = yt.video_id_from_url(command[-1])
        template = command[command.index('-o') + 1]
        filename = template.replace('%(title)s', 'Same title').replace('%(id)s', video_id)
        path = Path(filename.replace('%(ext)s', 'mp4'))
        if path.exists():
            message = f'{path} has already been downloaded'
        else:
            path.write_text(video_id)
            message = f'Downloaded {path}'
        print_path = command[command.index('--print-to-file') + 2]
        Path(print_path).write_text(str(path) + '\n')
        return 0, message

    monkeypatch.setattr(yt, 'run_ytdlp', run)
    monkeypatch.setattr(yt, 'ensure_ytdlp', lambda: True)
    monkeypatch.setattr(yt, 'check_ffmpeg_installed', lambda: True)
    monkeypatch.setattr(yt, 'remux_for_resolve', lambda p: p)
    monkeypatch.setattr(yt, 'media_height', lambda p: 1080)
    return calls


def test_same_title_downloads_keep_separate_content_and_ids(tmp_path, local_downloads):
    for video_id in (FIRST_ID, SECOND_ID):
        yt.download_youtube(f'https://youtube.com/watch?v={video_id}', output_dir=str(tmp_path))
    index = yt.load_download_index(str(tmp_path))
    first = tmp_path / index[f'{FIRST_ID}|mp4|full']['file']
    second = tmp_path / index[f'{SECOND_ID}|mp4|full']['file']
    assert first != second
    assert first.read_text() == FIRST_ID
    assert second.read_text() == SECOND_ID
    yt.download_youtube(URL, output_dir=str(tmp_path))
    assert len(local_downloads) == 2  # the genuine duplicate still skips


def test_fragment_templates_separate_both_video_and_range(tmp_path, local_downloads):
    for video_id, start, end in [(FIRST_ID, 5, 12), (FIRST_ID, 20, 30), (SECOND_ID, 5, 12)]:
        yt.download_youtube(f'https://youtube.com/watch?v={video_id}', output_dir=str(tmp_path),
                            section_args=yt.build_section_args(start, end))
    assert len(list(tmp_path.glob('*.mp4'))) == 3
    assert (tmp_path / f'Same title [{FIRST_ID}] [5-12s].mp4').read_text() == FIRST_ID
    assert (tmp_path / f'Same title [{SECOND_ID}] [5-12s].mp4').read_text() == SECOND_ID


def test_legacy_title_collision_is_rejected_before_postprocessing(tmp_path, monkeypatch, local_downloads):
    wrong = tmp_path / 'Same title.mp4'
    wrong.write_bytes(b'old video')
    monkeypatch.setattr(yt, 'attempt_all_strategies', lambda *a: (
        True, str(wrong), 'has already been downloaded'))
    monkeypatch.setattr(yt, '_post_process_download', lambda *a, **kw: pytest.fail('touched unrelated file'))
    monkeypatch.setattr(yt, 'probe_video_duration', lambda *a: 3.)
    monkeypatch.setattr(inserts, 'ffprobe_duration', lambda p: 3.)
    assert yt.existing_file_matches_video(str(wrong), URL) is False
    yt.download_youtube(URL, output_dir=str(tmp_path))
    assert wrong.read_bytes() == b'old video'
    assert yt.load_download_index(str(tmp_path)) == {}


def test_ambiguous_legacy_index_is_not_accepted(tmp_path):
    path = tmp_path / 'Same title.mp4'
    path.write_bytes(b'one video')
    index = {f'{video_id}|mp4|full': {'file': path.name, 'size': path.stat().st_size,
                                    'video_id': video_id}
             for video_id in (FIRST_ID, SECOND_ID)}
    yt.save_download_index(str(tmp_path), index)
    assert yt.check_already_downloaded(URL, str(tmp_path), 'mp4')[0] is None


def test_fallback_does_not_pick_unrelated_file(tmp_path, monkeypatch):
    wrong = tmp_path / 'Unrelated.mp4'
    wrong.write_bytes(b'not the requested film')
    monkeypatch.setattr(yt, 'run_ytdlp', lambda command: (0, 'no completed file'))
    success, path, _ = yt.try_download(URL, 'best', 'test',
        str(tmp_path/'%(title)s [%(id)s].%(ext)s'), [], str(tmp_path), 'mp4')
    assert success is False
    assert path is None


def test_fallback_finds_matching_existing_file_despite_new_unrelated_file(tmp_path):
    matching = tmp_path/f'Same title [{SECOND_ID}].mp4'
    matching.touch()
    (tmp_path/'Unrelated.mp4').touch()
    assert yt.detect_downloaded_file({matching.name}, str(tmp_path), 'mp4', SECOND_ID) == matching.name


def test_existing_id_file_with_wrong_length_is_not_postprocessed(tmp_path, monkeypatch, local_downloads):
    wrong = tmp_path/f'Same title [{SECOND_ID}].mp4'
    wrong.write_bytes(b'incomplete video')
    monkeypatch.setattr(yt, 'attempt_all_strategies', lambda *a: (
        True, str(wrong), 'has already been downloaded'))
    monkeypatch.setattr(yt, 'probe_video_duration', lambda *a: 300.)
    monkeypatch.setattr(inserts, 'ffprobe_duration', lambda p: 10.)
    monkeypatch.setattr(yt, '_post_process_download', lambda *a, **kw: pytest.fail('processed incomplete video'))
    yt.download_youtube(URL, output_dir=str(tmp_path))
    assert yt.load_download_index(str(tmp_path)) == {}


@pytest.mark.parametrize('cached', [False, True])
def test_fragment_disables_sponsorblock_for_fresh_and_cached_downloads(
        tmp_path, monkeypatch, local_downloads, cached):
    section = yt.build_section_args(100, 200)
    path = tmp_path / f'Same title [{SECOND_ID}] [100-200s].mp4'
    path.write_bytes(b'fragment')
    if cached:
        yt.record_download(str(tmp_path), yt.download_key(SECOND_ID, 'mp4', section),
                           SECOND_ID, URL, str(path), 'mp4', section)
    else:
        monkeypatch.setattr(yt, 'attempt_all_strategies', lambda *a: (True, str(path), 'downloaded'))
    monkeypatch.setattr(inserts, 'fetch_sponsorblock_segments', lambda *a: pytest.fail('full-video timestamps used'))
    monkeypatch.setattr(inserts, 'ffprobe_duration', lambda p: 100.)
    monkeypatch.setattr(inserts, 'detect_inserts_heuristic', lambda *a, **kw: [(10., 20., 'local')])
    cut_ranges = []
    monkeypatch.setattr(inserts, 'cut_segments', lambda p, ranges, out: cut_ranges.extend(ranges))
    yt.download_youtube(URL, output_dir=str(tmp_path), section_args=section,
                        inserts_opts={'do_cut': True, 'assume_yes': True})
    assert cut_ranges == [(10., 20., 'local')]


def test_full_video_keeps_sponsorblock(monkeypatch):
    monkeypatch.setattr(inserts, 'fetch_sponsorblock_segments',
                        lambda video_id, categories: [(10., 20., video_id)])
    assert inserts.find_inserts('unused', url=URL) == ([(10., 20., SECOND_ID)], 'sponsorblock')


@pytest.fixture
def gemini(monkeypatch):
    def install(responses):
        replies = iter(responses)
        calls = []

        def generate(**kwargs):
            calls.append(kwargs)
            response = next(replies)
            if isinstance(response, Exception):
                raise response
            return types.SimpleNamespace(text=json.dumps(response))

        google = types.ModuleType('google')
        genai = types.ModuleType('google.genai')
        genai.Client = lambda **kw: types.SimpleNamespace(
            models=types.SimpleNamespace(generate_content=generate))
        genai.types = types.SimpleNamespace(GenerateContentConfig=lambda **kw: kw)
        google.genai = genai
        monkeypatch.setitem(sys.modules, 'google', google)
        monkeypatch.setitem(sys.modules, 'google.genai', genai)
        monkeypatch.setattr(inserts, 'get_gemini_key', lambda **kw: 'test-key')
        return calls
    return install


def finalize(tmp_path, **kwargs):
    return yt._finalize_subtitle_files(str(tmp_path/'video.mp4'),
        [{'start': 0., 'end': 1., 'text': 'Hello'}], 'Hello', 'en', 'pl',
        str(tmp_path), False, kwargs.pop('burn', False), kwargs.pop('embed', False), **kwargs)


def test_gemini_quota_error_uses_google_fallback(tmp_path, monkeypatch, gemini):
    gemini([RuntimeError('quota exceeded')])
    calls = []
    monkeypatch.setattr(yt, 'translate_texts', lambda *a: calls.append(a) or ['Cześć'])
    assert finalize(tmp_path, translate_engine='gemini') is True
    assert len(calls) == 1
    assert 'Cześć' in (tmp_path/'video_PL.srt').read_text()


def test_gemini_bad_key_retries_once(monkeypatch, gemini):
    calls = gemini([RuntimeError('API_KEY_INVALID'), ['Cześć']])
    prompts, cleared = [], []
    monkeypatch.setattr(inserts, 'get_gemini_key', lambda **kw: prompts.append(kw) or 'test-key')
    monkeypatch.setattr(inserts, 'clear_gemini_key', lambda: cleared.append(True))
    assert yt.translate_texts_gemini(['Hello'], 'pl', 'en') == ['Cześć']
    assert len(calls) == 2
    assert cleared == [True]
    assert prompts == [{}, {'force_prompt': True}]


def test_gemini_repeated_bad_key_returns_fallback_signal(monkeypatch, gemini):
    calls = gemini([RuntimeError('API_KEY_INVALID'), RuntimeError('API_KEY_INVALID')])
    monkeypatch.setattr(inserts, 'clear_gemini_key', lambda: None)
    assert yt.translate_texts_gemini(['Hello'], 'pl', 'en') is None
    assert len(calls) == 2


def test_gemini_empty_translation_falls_back(gemini):
    gemini([['']])
    assert yt.translate_texts_gemini(['Hello'], 'pl', 'en') is None


def test_gemini_client_initialization_failure_falls_back(monkeypatch, gemini):
    gemini([])
    def fail(**kwargs):
        raise RuntimeError('client unavailable')
    monkeypatch.setattr(sys.modules['google.genai'], 'Client', fail)
    assert yt.translate_texts_gemini(['Hello'], 'pl', 'en') is None


@pytest.fixture
def google_translator(monkeypatch):
    def install(translate):
        module = types.ModuleType('deep_translator')
        module.GoogleTranslator = lambda **kw: types.SimpleNamespace(translate=translate)
        monkeypatch.setitem(sys.modules, 'deep_translator', module)
    return install


@pytest.mark.parametrize('result', [None, '', '   '])
def test_google_empty_results_are_failures(google_translator, result):
    google_translator(lambda text: result)
    with pytest.raises(yt.TranslationError, match='1/1 segments failed'):
        yt.translate_texts(['Hello'], 'pl', 'en')


def test_google_partial_failure_reports_count_and_position(google_translator):
    def translate(text):
        if text == 'fails':
            raise ConnectionError('offline')
        return 'Cześć'
    google_translator(translate)
    with pytest.raises(yt.TranslationError, match=r'1/3.*positions: 2'):
        yt.translate_texts(['Hello', 'fails', 'World'], 'pl', 'en')


def test_google_preserves_order_and_accepts_unchanged_names(google_translator):
    google_translator(lambda text: {'Hello': 'Cześć', 'Alice': 'Alice'}[text])
    assert yt.translate_texts(['Hello', ' ', 'Alice'], 'pl', 'en') == ['Cześć', ' ', 'Alice']


def test_total_translation_failure_does_not_write_or_burn_target(
        tmp_path, monkeypatch, google_translator, capsys):
    def fail(text):
        raise ConnectionError('offline')
    google_translator(fail)
    target = tmp_path/'video_PL.srt'
    target.write_text('existing valid subtitles')
    monkeypatch.setattr(yt, 'burn_subtitles', lambda *a: pytest.fail('burned untranslated subtitles'))
    monkeypatch.setattr(yt, 'embed_subtitles', lambda *a: pytest.fail('embedded untranslated subtitles'))
    assert finalize(tmp_path, burn=True, embed=True) is False
    assert (tmp_path/'video_EN.srt').exists()
    assert target.read_text() == 'existing valid subtitles'
    assert not (tmp_path/'video_PL.txt').exists()
    output = capsys.readouterr().out
    assert '1/1 segments failed' in output
    assert 'SUBTITLES GENERATED SUCCESSFULLY' not in output


@pytest.mark.parametrize('order', [False, True])
def test_sidecars_follow_longest_video_stem(tmp_path, order):
    for name in ('video.mp4', 'video_part2.mp4', 'video_part2_EN.srt', 'video_EN.srt'):
        (tmp_path/name).write_text(name)
    analyses = [('video.mp4', {'course': 'Course', 'title': 'First', 'confidence': 1.}),
                ('video_part2.mp4', {'course': 'Course', 'title': 'Second', 'confidence': 1.})]
    if order:
        analyses.reverse()
    plan = organize.build_plan(str(tmp_path), analyses)
    sources = [src for item in plan for src, _ in item.sidecars]
    assert len(sources) == len(set(sources)) == 2
    assert organize.apply_plan(plan) == 2
    assert (tmp_path/'Course'/'First_EN.srt').read_text() == 'video_EN.srt'
    assert (tmp_path/'Course'/'Second_EN.srt').read_text() == 'video_part2_EN.srt'


def test_sidecar_of_unclassified_video_is_not_taken_by_shorter_prefix(tmp_path):
    for name in ('video.mp4', 'video_part2.mp4', 'video_part2_EN.srt'):
        (tmp_path/name).touch()
    plan = organize.build_plan(str(tmp_path), [
        ('video.mp4', {'course': 'Course', 'title': 'First', 'confidence': 1.})])
    assert plan[0].sidecars == []


def test_same_stem_containers_leave_ambiguous_sidecar_in_place(tmp_path):
    for name in ('video.mp4', 'video.mov', 'video_EN.srt'):
        (tmp_path/name).touch()
    assert organize.collect_sidecars(str(tmp_path), 'video.mp4') == []
    assert organize.collect_sidecars(str(tmp_path), 'video.mov') == []


@pytest.mark.parametrize('required,cuts,expected', [
    (True, [], []), (True, [8.], []), (True, [3.], [(3, 5, 'heuristic')]),
    (False, [], [(3, 5, 'heuristic')]),
])
def test_audio_jump_requires_scene_confirmation(monkeypatch, required, cuts, expected):
    monkeypatch.setattr(inserts, 'ffprobe_duration', lambda p: 10.)
    monkeypatch.setattr(inserts, 'loudness_timeline', lambda *a, **kw: (
        list(range(10)), [-20., -20., -20., -5., -5., -5., -20., -20., -20., -20.]))
    monkeypatch.setattr(inserts, 'scene_cut_times', lambda *a, **kw: cuts)
    assert inserts.detect_inserts_heuristic('unused', require_scene_cut=required) == expected


def make_video(path, audio, color='red'):
    if not (shutil.which('ffmpeg') and shutil.which('ffprobe')):
        pytest.skip('ffmpeg and ffprobe are needed for media integration tests')
    cmd = ['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi', '-i', f'color=c={color}:s=320x180:r=25:d=3']
    if audio:
        cmd += ['-f', 'lavfi', '-i', 'sine=frequency=440:duration=3']
    cmd += ['-c:v', 'libx264', '-pix_fmt', 'yuv420p']
    if audio:
        cmd += ['-c:a', 'aac', '-shortest']
    subprocess.run(cmd + [str(path)], check=True, capture_output=True)


@pytest.mark.parametrize('audio', [False, True])
def test_real_cut_keeps_duration_and_available_streams(tmp_path, audio):
    source = tmp_path/'source.mp4'
    output = tmp_path/'cut.mp4'
    make_video(source, audio)
    assert inserts.cut_segments(str(source), [(1., 2., 'insert')], str(output)) == str(output)
    assert inserts.ffprobe_duration(str(output)) == pytest.approx(2., abs=.15)
    probe = subprocess.run(['ffprobe', '-v', 'error', '-show_streams', '-of', 'json', str(output)],
                           text=True, capture_output=True, check=True)
    streams = json.loads(probe.stdout)['streams']
    assert any(s['codec_type'] == 'video' for s in streams)
    assert any(s['codec_type'] == 'audio' for s in streams) is audio


def test_real_ytdlp_does_not_skip_same_title_different_content(tmp_path, monkeypatch, local_downloads):
    from yt_dlp import YoutubeDL

    output = tmp_path/'output'
    output.mkdir()
    legacy = output/'Same title.mp4'
    incoming = tmp_path/'incoming.mp4'
    make_video(legacy, audio=False, color='red')
    make_video(incoming, audio=False, color='blue')
    original = legacy.read_bytes()

    def run_with_local_metadata(command):
        log = io.StringIO()
        template = command[command.index('-o') + 1]
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            with YoutubeDL({'outtmpl': template, 'enable_file_urls': True, 'noprogress': True}) as dl:
                dl.process_ie_result({'id': SECOND_ID, 'title': 'Same title', 'url': incoming.as_uri(),
                                      'ext': 'mp4', 'extractor_key': 'Test', 'webpage_url': URL}, download=True)
                return dl._download_retcode, log.getvalue()

    monkeypatch.setattr(yt, 'run_ytdlp', run_with_local_metadata)
    monkeypatch.setattr(yt, 'probe_video_duration', lambda *a: 3.)
    yt.download_youtube(URL, output_dir=str(output))
    entry = yt.load_download_index(str(output))[f'{SECOND_ID}|mp4|full']
    actual = output/entry['file']
    assert actual != legacy
    assert actual.read_bytes() == incoming.read_bytes()
    assert legacy.read_bytes() == original
    assert actual.read_bytes() != original


def test_built_wheel_imports_qwen_outside_checkout(tmp_path):
    root = Path(__file__).resolve().parents[1]
    source = tmp_path/'source'
    source.mkdir()
    for path in list(root.glob('*.py')) + [root/'pyproject.toml', root/'README.md', root/'LICENSE']:
        shutil.copy2(path, source/path.name)
    dist = tmp_path/'dist'
    build = subprocess.run([sys.executable, '-m', 'build', '--wheel', '--no-isolation',
                            '--outdir', str(dist), str(source)], text=True, capture_output=True)
    assert build.returncode == 0, build.stdout + build.stderr
    wheel, = dist.glob('*.whl')
    with zipfile.ZipFile(wheel) as archive:
        assert 'qwen_engine.py' in archive.namelist()
        assert 'subtitle_workflow.py' in archive.namelist()
    installed = tmp_path/'installed'
    install = subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--no-deps',
                              '--no-compile', '--target', str(installed), str(wheel)],
                             text=True, capture_output=True)
    assert install.returncode == 0, install.stdout + install.stderr
    code = (f'import sys; sys.path.insert(0, {str(installed)!r}); '
            'import youtube_downloader as y; '
            'q = y._import_qwen_engine(); '
            'assert q.choose_asr_mode("qwen", "ja", True, True)[0] == "qwen"; '
            'from importlib.metadata import distribution; '
            'entry, = [e for e in distribution("ytsubtran").entry_points if e.name == "ytsubtran"]; '
            'entry.load()()')
    imported = subprocess.run([sys.executable, '-I', '-c', code, '--help'], cwd=tmp_path,
                              text=True, capture_output=True)
    assert imported.returncode == 0, imported.stdout + imported.stderr
    assert '--engine' in imported.stdout
    assert '--subtitle-file' in imported.stdout and '--force-transcribe' in imported.stdout
    source_subtitles = tmp_path/'captions.srt'
    source_subtitles.write_text('1\n00:00:01,001 --> 00:00:02,999\nHello\n')
    translated = subprocess.run([sys.executable, '-I', '-c', code, '--subtitle-file', str(source_subtitles),
                                  '--source-lang', 'en', '--translate-to', 'en'], cwd=tmp_path,
                                 text=True, capture_output=True)
    assert translated.returncode == 0, translated.stdout + translated.stderr
    assert (tmp_path/'captions_EN.srt').read_text() == source_subtitles.read_text()
