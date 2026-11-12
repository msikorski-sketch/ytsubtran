"""Read-only local diagnostics. No model downloads or external API requests."""
import contextlib
import importlib
from importlib.metadata import PackageNotFoundError, version
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


def package_status(distribution, module):
    result = {'status': 'missing', 'version': None}
    try:
        result['version'] = version(distribution)
    except PackageNotFoundError:
        return result
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            imported = importlib.import_module(module)
        result.update(status='ok', path=getattr(imported, '__file__', None))
    except Exception as error:
        result.update(status='error', error=f'{type(error).__name__}: {error}')
    if log.getvalue().strip():
        result['messages'] = log.getvalue().strip()
    return result


def tool_status(name):
    path = shutil.which(name)
    if not path:
        return {'status': 'missing', 'path': None}
    try:
        result = subprocess.run([path, '-version'], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=10)
        if result.returncode:
            return {'status': 'error', 'path': path, 'error': result.stderr or result.stdout}
        return {'status': 'ok', 'path': path, 'version': result.stdout.splitlines()[0]}
    except (OSError, subprocess.TimeoutExpired, IndexError) as error:
        return {'status': 'error', 'path': path, 'error': str(error)}


def cuda_status(torch_status):
    if torch_status['status'] != 'ok':
        return {'available': False, 'reason': 'PyTorch is missing or could not be imported'}
    log = io.StringIO()
    try:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            torch = importlib.import_module('torch')
            result = {'available': bool(torch.cuda.is_available()), 'build': torch.version.cuda}
            if result['available']:
                result['device'] = torch.cuda.get_device_name(0)
            else:
                result['reason'] = 'CUDA is not available to this Python process; CPU remains possible'
        if log.getvalue().strip():
            result['messages'] = log.getvalue().strip()
        return result
    except Exception as error:
        return {'available': False, 'reason': f'{type(error).__name__}: {error}'}


def gpu_status():
    path = shutil.which('nvidia-smi')
    if not path:
        return {'status': 'missing'}
    try:
        result = subprocess.run([path, '--query-gpu=name,memory.total,memory.free,driver_version',
                                 '--format=csv,noheader'], capture_output=True, text=True,
                                encoding='utf-8', errors='replace', timeout=10)
        if result.returncode:
            return {'status': 'error', 'error': (result.stderr or result.stdout).strip()}
        return {'status': 'ok', 'devices': result.stdout.strip().splitlines()}
    except (OSError, subprocess.TimeoutExpired) as error:
        return {'status': 'error', 'error': str(error)}


def git_revision(folder):
    try:
        root = subprocess.run(['git', '-C', str(folder), 'rev-parse', '--show-toplevel'],
                              capture_output=True, text=True, timeout=5)
        if root.returncode or Path(root.stdout.strip()).resolve() != Path(folder).resolve():
            return None
        result = subprocess.run(['git', '-C', str(folder), 'rev-parse', 'HEAD'],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip() if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def key_status(config_path):
    """Return presence/source only. Never return credential values."""
    for name in ('GEMINI_API_KEY', 'GOOGLE_API_KEY'):
        if os.environ.get(name, '').strip():
            return {'configured': True, 'source': name}
    try:
        with open(config_path, encoding='utf-8') as stream:
            config = json.load(stream)
        configured = isinstance(config, dict) and isinstance(config.get('gemini_api_key'), str) \
            and bool(config['gemini_api_key'].strip())
        return {'configured': configured, 'source': 'saved config' if configured else None}
    except FileNotFoundError:
        return {'configured': False, 'source': None}
    except (OSError, ValueError) as error:
        return {'configured': False, 'source': None, 'error': f'Cannot read config: {type(error).__name__}'}


def git_dirty(folder):
    try:
        result = subprocess.run(['git', '-C', str(folder), 'status', '--porcelain'],
                                capture_output=True, text=True, timeout=5)
        return bool(result.stdout.strip()) if result.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def build_report(module_path, config_path):
    folder = Path(module_path).resolve().parent
    packages = {label: package_status(dist, module) for label, dist, module in (
        ('torch', 'torch', 'torch'), ('whisper', 'openai-whisper', 'whisper'),
        ('qwen', 'qwen-asr', 'qwen_asr'), ('google_translate', 'deep-translator', 'deep_translator'),
        ('gemini', 'google-genai', 'google.genai'), ('yt_dlp', 'yt-dlp', 'yt_dlp'))}
    try:
        app_version = version('ytsubtran')
    except PackageNotFoundError:
        app_version = 'source checkout (not installed)'
    cache_root = Path(os.environ.get('XDG_CACHE_HOME', str(Path.home()/'.cache')))/'whisper'
    try:
        models = sorted(p.name for p in cache_root.glob('*.pt') if p.is_file())
    except OSError:
        models = []
    try:
        free = shutil.disk_usage(folder).free
    except OSError:
        free = None
    revision = git_revision(folder)
    return {'version': 1, 'application': {'version': app_version, 'module': str(Path(module_path).resolve()),
                                         'revision': revision, 'dirty': git_dirty(folder) if revision else None,
                                         'python': sys.executable,
                                         'python_version': sys.version.split()[0]},
            'packages': packages, 'tools': {name: tool_status(name) for name in ('ffmpeg', 'ffprobe')},
            'cuda': cuda_status(packages['torch']), 'nvidia': gpu_status(),
            'gemini_key': key_status(config_path), 'whisper_cached_models': models,
            'disk_free_bytes': free, 'network_tested': False}


def print_report(report):
    app = report['application']
    print(f"ytsubtran {app['version']} — local diagnostics")
    print(f"Module: {app['module']}")
    print(f"Commit: {app['revision'] or 'not a repository checkout'}"
          + (' (working tree has local changes)' if app.get('dirty') else ''))
    print(f"Python: {app['python']} ({app['python_version']})")
    for label, component in {**report['tools'], **report['packages']}.items():
        print(f"{label}: {component['status']}" + (f" ({component['version']})" if component.get('version') else ''))
        if component.get('error'):
            print(f"  {component['error']}")
        if component.get('messages'):
            print(f"  {component['messages']}")
    cuda = report['cuda']
    print('CUDA: ' + (cuda.get('device', 'available') if cuda['available'] else cuda.get('reason', 'unavailable')))
    if cuda.get('messages'):
        print(cuda['messages'])
    nvidia = report['nvidia']
    print('NVIDIA driver: ' + ('; '.join(nvidia['devices']) if nvidia['status'] == 'ok' else nvidia['status']))
    if nvidia.get('error'):
        print(nvidia['error'])
    print('Gemini key: ' + ('configured (' + report['gemini_key']['source'] + ')' if report['gemini_key']['configured']
                            else 'not configured — use ytsubtran --set-gemini-key'))
    if report['gemini_key'].get('error'):
        print(report['gemini_key']['error'])
    print('Cached Whisper models: ' + (', '.join(report['whisper_cached_models']) or 'none'))
    if report['disk_free_bytes'] is not None:
        print(f"Free disk space: {report['disk_free_bytes']/1024**3:.2f} GiB")
    print('Network/API access: not tested. No models were downloaded and no API requests were sent.')
