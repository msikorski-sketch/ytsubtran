"""Subtitle documents and reusable transcription state; standard library only."""
from dataclasses import dataclass
from importlib.metadata import PackageNotFoundError, version
import json
import math
import os
import re
import tempfile


CACHE_VERSION = 1
_STAMP = r'(?:\d{2,}:)?[0-5]\d:[0-5]\d[.,]\d{3}'
_TIMING = re.compile(rf'^(?P<start>{_STAMP})[ \t]+-->[ \t]+(?P<end>{_STAMP})(?:[ \t].*)?$')
_MARKUP = re.compile(r'<[^>\n]+>|\{\\[^}\n]*\}')


def atomic_write_text(path, text):
    """Replace a complete file, never expose a truncated checkpoint/output."""
    path = os.path.abspath(path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.ytsubtran-', dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.remove(temporary)


def media_identity(path):
    stat = os.stat(path)
    return {'path': os.path.realpath(path), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}


def asr_versions():
    """Check installed distributions without importing models or probing CUDA."""
    versions = {}
    for package in ('openai-whisper', 'qwen-asr'):
        try:
            versions[package] = version(package)
        except PackageNotFoundError:
            versions[package] = None
    return versions


def transcription_path(media, output_dir=None):
    folder = output_dir or os.path.dirname(os.path.abspath(media))
    return os.path.join(folder, os.path.basename(media) + '.transcription.json')


def validate_transcription(segments, full_text, language):
    if not isinstance(segments, list) or not isinstance(full_text, str):
        raise ValueError('Invalid transcription payload')
    if not isinstance(language, str) or not re.fullmatch(r'[a-zA-Z][a-zA-Z0-9-]{0,34}', language):
        raise ValueError('Invalid transcription language')
    clean = []
    for segment in segments:
        if not isinstance(segment, dict) or not isinstance(segment.get('text'), str):
            raise ValueError('Invalid transcription segment')
        start, end = segment.get('start'), segment.get('end')
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, (float, int)) or not isinstance(end, (float, int))
                or not math.isfinite(start) or not math.isfinite(end) or start < 0 or end < start):
            raise ValueError('Invalid transcription timing')
        clean.append({'start': float(start), 'end': float(end), 'text': segment['text']})
    return clean, full_text, language


def load_transcription(path, key):
    try:
        with open(path, encoding='utf-8') as stream:
            data = json.load(stream)
        if not isinstance(data, dict) or data.get('version') != CACHE_VERSION or data.get('key') != key:
            return None
        return validate_transcription(data['segments'], data['full_text'], data['language'])
    except (OSError, ValueError, KeyError, TypeError):
        return None


def save_transcription(path, key, segments, full_text, language, actual_engine):
    segments, full_text, language = validate_transcription(segments, full_text, language)
    data = {'version': CACHE_VERSION, 'key': key, 'segments': segments,
            'full_text': full_text, 'language': language, 'actual_engine': actual_engine}
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n')


def _milliseconds(stamp):
    parts = stamp.replace(',', '.').split(':')
    seconds, millis = parts[-1].split('.')
    minutes = int(parts[-2])
    hours = int(parts[0]) if len(parts) == 3 else 0
    return ((hours * 60 + minutes) * 60 + int(seconds)) * 1000 + int(millis)


@dataclass
class Cue:
    block: int
    timing_line: int
    text: str
    start: str
    end: str


@dataclass
class SubtitleDocument:
    format: str
    blocks: list
    cues: list

    @property
    def texts(self):
        return [cue.text for cue in self.cues]

    def render(self, texts):
        if len(texts) != len(self.cues):
            raise ValueError('Translation count does not match subtitle cues')
        blocks = [list(block) for block in self.blocks]
        for cue, text in zip(self.cues, texts):
            if not isinstance(text, str) or not text.strip():
                raise ValueError('Translation contains an empty cue')
            # A blank line terminates a cue in both formats.
            lines = [line for line in text.strip().splitlines() if line.strip()]
            blocks[cue.block] = blocks[cue.block][:cue.timing_line + 1] + lines
        return '\n\n'.join('\n'.join(block) for block in blocks) + '\n'

    def render_vtt(self, texts):
        if self.format == 'vtt':
            return self.render(texts)
        if len(texts) != len(self.cues):
            raise ValueError('Translation count does not match subtitle cues')
        blocks = ['WEBVTT']
        for cue, text in zip(self.cues, texts):
            timing = f'{cue.start.replace(",", ".")} --> {cue.end.replace(",", ".")}'
            blocks.append(timing + '\n' + '\n'.join(line for line in text.strip().splitlines() if line.strip()))
        return '\n\n'.join(blocks) + '\n'


def read_subtitles(path):
    """Read UTF-8 SRT/WebVTT, preserving IDs, timing lines and VTT metadata."""
    fmt = os.path.splitext(path)[1].lower().lstrip('.')
    if fmt not in ('srt', 'vtt'):
        raise ValueError('Expected an .srt or .vtt subtitle file')
    with open(path, encoding='utf-8-sig') as stream:
        text = stream.read().replace('\r\n', '\n').replace('\r', '\n')
    blocks = [block.splitlines() for block in re.split(r'\n[ \t]*\n', text.strip()) if block.strip()]
    if not blocks:
        raise ValueError('Subtitle file is empty')
    if fmt == 'vtt' and not re.fullmatch(r'WEBVTT(?:[ \t].*)?', blocks[0][0]):
        raise ValueError('WebVTT header is missing')
    if fmt == 'vtt' and any(_TIMING.fullmatch(line) for line in blocks[0][1:]):
        raise ValueError('A blank line is required between the WebVTT header and cues')
    cues = []
    for index, block in enumerate(blocks):
        if fmt == 'vtt' and (index == 0 or block[0] in ('STYLE', 'REGION')
                             or re.match(r'^NOTE(?:[ \t]|$)', block[0])):
            continue
        timing_line = 0 if _TIMING.fullmatch(block[0]) else 1
        match = _TIMING.fullmatch(block[timing_line]) if len(block) > timing_line else None
        if not match or (timing_line and fmt == 'srt' and not block[0].isdigit()):
            raise ValueError(f'Invalid subtitle timing in block {index + 1}')
        start, end = match['start'], match['end']
        if fmt == 'srt' and (start.count(':') != 2 or end.count(':') != 2):
            raise ValueError(f'SRT timestamps need hours in block {index + 1}')
        if fmt == 'vtt' and (',' in start or ',' in end):
            raise ValueError(f'WebVTT timestamps need decimal dots in block {index + 1}')
        if _milliseconds(end) < _milliseconds(start):
            raise ValueError(f'Subtitle ends before it starts in block {index + 1}')
        payload = '\n'.join(block[timing_line + 1:])
        if not payload.strip():
            raise ValueError(f'Empty subtitle cue in block {index + 1}')
        cues.append(Cue(index, timing_line, payload, start, end))
    if not cues:
        raise ValueError('No subtitle cues found')
    return SubtitleDocument(fmt, blocks, cues)


def protect_markup(texts):
    """Hide formatting/voice tags and inline timestamps from translators."""
    prefix = 'YTSUBTAG'
    while any(prefix in text for text in texts):
        prefix += 'X'
    replacements = []
    masked = []
    for text in texts:
        tags = []

        def replace(match):
            token = f'{prefix}{len(replacements):06d}N{len(tags):06d}TOKEN'
            tags.append((token, match.group()))
            return token

        masked.append(_MARKUP.sub(replace, text))
        replacements.append(tags)
    return masked, replacements


def restore_markup(texts, replacements):
    if len(texts) != len(replacements):
        raise ValueError('Translation count does not match subtitle cues')
    restored = []
    for text, tags in zip(texts, replacements):
        last = -1
        for token, original in tags:
            position = text.find(token)
            if text.count(token) != 1 or position <= last:
                raise ValueError('Translator changed subtitle formatting tokens; no output was written')
            last = position
        for token, original in tags:
            text = text.replace(token, original)
        restored.append(text)
    return restored
