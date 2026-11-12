"""Local diagnostics and credential setup must not contact APIs or expose keys."""
from importlib.metadata import PackageNotFoundError
import json
import os
import subprocess
import sys
import types

import pytest

import diagnostics as diag
import inserts
import subtitle_workflow as sw
import youtube_downloader as yt


@pytest.fixture(autouse=True)
def no_environment_keys(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY', raising=False)
    monkeypatch.delenv('GOOGLE_API_KEY', raising=False)


def test_missing_distribution_does_not_import_models(monkeypatch):
    def absent(name):
        raise PackageNotFoundError(name)
    monkeypatch.setattr(diag, 'version', absent)
    monkeypatch.setattr(diag.importlib, 'import_module', lambda *a: pytest.fail('unexpected import'))
    assert diag.package_status('openai-whisper', 'whisper') == {'status': 'missing', 'version': None}


def test_broken_import_reports_actual_error_and_captures_import_noise(monkeypatch, capsys):
    monkeypatch.setattr(diag, 'version', lambda name: '1.2.3')
    def broken(name):
        print('import warning')
        raise RuntimeError('shared library could not be loaded')
    monkeypatch.setattr(diag.importlib, 'import_module', broken)
    result = diag.package_status('torch', 'torch')
    assert result['status'] == 'error'
    assert result['error'] == 'RuntimeError: shared library could not be loaded'
    assert result['messages'] == 'import warning'
    assert not capsys.readouterr().out


def test_cuda_probe_does_not_load_models(monkeypatch):
    fake = types.SimpleNamespace(version=types.SimpleNamespace(cuda='12.6'),
        cuda=types.SimpleNamespace(is_available=lambda: True, get_device_name=lambda i: 'Test GPU'))
    monkeypatch.setattr(diag.importlib, 'import_module', lambda name: fake)
    assert diag.cuda_status({'status': 'ok'}) == {'available': True, 'build': '12.6', 'device': 'Test GPU'}
    assert diag.cuda_status({'status': 'missing'})['available'] is False


def test_cuda_failure_is_reported_without_crashing(monkeypatch):
    def broken(*args):
        raise RuntimeError('driver unavailable')
    monkeypatch.setattr(diag.importlib, 'import_module', broken)
    assert diag.cuda_status({'status': 'ok'}) == {'available': False, 'reason': 'RuntimeError: driver unavailable'}


def test_tool_probe_preserves_full_failure_output(monkeypatch):
    monkeypatch.setattr(diag.shutil, 'which', lambda name: '/bin/' + name)
    monkeypatch.setattr(diag.subprocess, 'run', lambda *a, **kw: types.SimpleNamespace(
        returncode=1, stdout='', stderr='warning first\nactual decoder error'))
    result = diag.tool_status('ffmpeg')
    assert result['status'] == 'error'
    assert 'actual decoder error' in result['error']


def test_gpu_timeout_is_reported(monkeypatch):
    monkeypatch.setattr(diag.shutil, 'which', lambda name: '/bin/nvidia-smi')
    def timeout(*args, **kwargs):
        raise subprocess.TimeoutExpired('nvidia-smi', 10)
    monkeypatch.setattr(diag.subprocess, 'run', timeout)
    assert diag.gpu_status()['status'] == 'error'


def test_unrelated_parent_repository_is_not_reported_as_application_revision(tmp_path, monkeypatch):
    calls = []
    def git(*args, **kwargs):
        calls.append(args)
        return types.SimpleNamespace(returncode=0, stdout=str(tmp_path.parent))
    monkeypatch.setattr(diag.subprocess, 'run', git)
    assert diag.git_revision(tmp_path) is None
    assert len(calls) == 1


def test_local_changes_are_reported_without_exposing_filenames(tmp_path, monkeypatch):
    monkeypatch.setattr(diag.subprocess, 'run', lambda *a, **kw: types.SimpleNamespace(
        returncode=0, stdout=' M private-filename\n'))
    assert diag.git_dirty(tmp_path) is True


@pytest.mark.parametrize('kind', ['config', 'GEMINI_API_KEY', 'GOOGLE_API_KEY'])
def test_key_presence_never_returns_secret_value(tmp_path, monkeypatch, kind):
    path = tmp_path/'config.json'
    secret = 'private-key-do-not-print'
    if kind == 'config':
        path.write_text(json.dumps({'gemini_api_key': secret, 'unrelated': 'private setting'}))
    else:
        monkeypatch.setenv(kind, secret)
    result = diag.key_status(path)
    assert result['configured'] is True
    assert secret not in json.dumps(result)
    assert 'private setting' not in json.dumps(result)


def test_corrupt_config_is_reported_without_contents(tmp_path):
    path = tmp_path/'config.json'
    path.write_text('{private-secret')
    result = diag.key_status(path)
    assert result['configured'] is False
    assert 'error' in result
    assert 'private-secret' not in json.dumps(result)


def test_build_report_is_read_only_and_reports_actual_module_path(tmp_path, monkeypatch, capsys):
    module = tmp_path/'youtube_downloader.py'
    module.touch()
    config = tmp_path/'config.json'
    secret = 'private-key-do-not-print'
    config.write_text(json.dumps({'gemini_api_key': secret}))
    before = config.read_bytes(), config.stat().st_mtime_ns
    monkeypatch.setattr(diag, 'package_status', lambda *a: {'status': 'missing', 'version': None})
    monkeypatch.setattr(diag, 'tool_status', lambda *a: {'status': 'missing', 'path': None})
    monkeypatch.setattr(diag, 'gpu_status', lambda: {'status': 'missing'})
    monkeypatch.setattr(diag, 'git_revision', lambda folder: 'abc123')
    monkeypatch.setattr(diag.Path, 'home', lambda: tmp_path)
    report = diag.build_report(module, config)
    assert report['application']['module'] == str(module)
    assert report['application']['revision'] == 'abc123'
    assert report['network_tested'] is False
    assert report['gemini_key']['configured'] is True
    diag.print_report(report)
    assert secret not in capsys.readouterr().out
    assert secret not in json.dumps(report)
    assert (config.read_bytes(), config.stat().st_mtime_ns) == before


def test_cli_doctor_json_needs_no_input_and_does_not_run_media_setup(tmp_path, monkeypatch, capsys):
    report = {'application': {'module': yt.__file__}, 'network_tested': False}
    monkeypatch.setattr(diag, 'build_report', lambda module, config: report)
    monkeypatch.setattr(yt, 'prompt_output_dir', lambda: pytest.fail('prompted for output'))
    monkeypatch.setattr(yt, 'generate_subtitles', lambda *a, **kw: pytest.fail('started ASR'))
    monkeypatch.setattr(sys, 'argv', ['ytsubtran', '--doctor', '--json'])
    yt.main()
    assert json.loads(capsys.readouterr().out) == report


@pytest.mark.parametrize('arguments', [['--json'], ['--doctor', '--file', 'x.mp4'],
    ['--doctor', '--set-gemini-key'], ['--doctor', '--set-output-dir', 'x']])
def test_doctor_rejects_mutating_or_unrelated_actions(monkeypatch, arguments):
    monkeypatch.setattr(sys, 'argv', ['ytsubtran'] + arguments)
    with pytest.raises(SystemExit) as error:
        yt.main()
    assert error.value.code == 2


def test_hidden_key_setup_is_atomic_private_and_preserves_other_settings(tmp_path, monkeypatch, capsys):
    config = tmp_path/'config.json'
    config.write_text(json.dumps({'default_output_dir': '/saved/output'}))
    monkeypatch.setattr(inserts, 'CONFIG_PATH', str(config))
    monkeypatch.setattr(yt, 'APP_CONFIG_PATH', str(config))
    monkeypatch.setattr(inserts.sys, 'stdin', types.SimpleNamespace(isatty=lambda: True))
    secret = 'private-key-do-not-print'
    monkeypatch.setattr(inserts.getpass, 'getpass', lambda prompt: secret)
    monkeypatch.setattr(sys, 'argv', ['ytsubtran', '--set-gemini-key'])
    yt.main()
    stored = json.loads(config.read_text())
    assert stored == {'default_output_dir': '/saved/output', 'gemini_api_key': secret}
    if os.name != 'nt':
        assert config.stat().st_mode & 0o777 == 0o600
    assert secret not in capsys.readouterr().out


def test_key_write_failure_is_not_reported_as_success(tmp_path, monkeypatch, capsys):
    config = tmp_path/'config.json'
    config.write_text('{}')
    monkeypatch.setattr(inserts, 'CONFIG_PATH', str(config))
    monkeypatch.setattr(yt, 'APP_CONFIG_PATH', str(config))
    monkeypatch.setattr(inserts.sys, 'stdin', types.SimpleNamespace(isatty=lambda: True))
    monkeypatch.setattr(inserts.getpass, 'getpass', lambda prompt: 'not-saved-key')
    def fail(*args):
        raise OSError('disk unavailable')
    monkeypatch.setattr(sw, 'atomic_write_text', fail)
    monkeypatch.setattr(sys, 'argv', ['ytsubtran', '--set-gemini-key'])
    with pytest.raises(SystemExit) as error:
        yt.main()
    assert error.value.code == 1
    assert json.loads(config.read_text()) == {}
    assert 'Gemini key saved' not in capsys.readouterr().out


def test_noninteractive_key_setup_exits_without_changing_config(tmp_path, monkeypatch):
    config = tmp_path/'config.json'
    config.write_text('{}')
    monkeypatch.setattr(inserts, 'CONFIG_PATH', str(config))
    monkeypatch.setattr(yt, 'APP_CONFIG_PATH', str(config))
    monkeypatch.setattr(inserts.sys, 'stdin', types.SimpleNamespace(isatty=lambda: False))
    monkeypatch.setattr(inserts.getpass, 'getpass', lambda *a: pytest.fail('prompted without terminal'))
    monkeypatch.setattr(sys, 'argv', ['ytsubtran', '--set-gemini-key'])
    with pytest.raises(SystemExit) as error:
        yt.main()
    assert error.value.code == 1
    assert json.loads(config.read_text()) == {}
