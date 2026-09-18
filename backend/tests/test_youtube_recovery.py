"""YouTube 字幕兼容、媒体缓存和错误脱敏的回归测试。"""
import ast
import importlib.util
import json
import logging
import sys
import types
from dataclasses import asdict
from pathlib import Path
from typing import List, Optional, Union
from unittest.mock import Mock, patch

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load_file(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, {name: module}):
        spec.loader.exec_module(module)
    return module


audio_module = load_file('recovery_audio', 'app/models/audio_model.py')
transcript_module = load_file('recovery_transcript', 'app/models/transcriber_model.py')
errors = load_file('recovery_errors', 'app/downloaders/youtube_errors.py')
AudioDownloadResult = audio_module.AudioDownloadResult
TranscriptResult = transcript_module.TranscriptResult
TranscriptSegment = transcript_module.TranscriptSegment


@pytest.fixture
def subtitle_module():
    api = types.ModuleType('youtube_transcript_api')
    api.YouTubeTranscriptApi = Mock()
    proxy = types.ModuleType('app.services.proxy_config_manager')
    proxy.ProxyConfigManager = Mock()
    logger = types.ModuleType('app.utils.logger')
    logger.get_logger = logging.getLogger
    with patch.dict(sys.modules, {
        'youtube_transcript_api': api,
        'app.models.transcriber_model': transcript_module,
        'app.services.proxy_config_manager': proxy,
        'app.utils.logger': logger,
    }):
        yield load_file('recovery_subtitle', 'app/downloaders/youtube_subtitle.py')


@pytest.mark.parametrize('snippet', [
    {'text': ' real text ', 'start': 12.5, 'duration': 3},
    types.SimpleNamespace(text=' real text ', start=12.5, duration=3),
])
def test_subtitle_reads_text_and_timestamps(subtitle_module, snippet):
    track = types.SimpleNamespace(language='English', language_code='en', is_generated=False,
                                  fetch=lambda: [snippet])
    tracks = Mock()
    tracks.__iter__ = Mock(return_value=iter([track]))
    tracks.find_manually_created_transcript.return_value = track
    fetcher = subtitle_module.YouTubeSubtitleFetcher.__new__(subtitle_module.YouTubeSubtitleFetcher)
    fetcher._api = Mock()
    fetcher._api.list.return_value = tracks
    result = fetcher.fetch_subtitles('N7Hkqznvc7I')
    assert result.full_text == 'real text'
    assert (result.segments[0].start, result.segments[0].end) == (12.5, 15.5)


@pytest.mark.parametrize('snippet', [
    types.SimpleNamespace(text='', start=0, duration=1),
    types.SimpleNamespace(text='x', start=float('nan'), duration=1),
    {'text': 'x', 'start': -1, 'duration': 1},
    {'text': 'x', 'start': 'bad', 'duration': 1},
    {'text': None, 'start': 0, 'duration': 1},
])
def test_invalid_subtitle_is_not_used(subtitle_module, snippet):
    track = types.SimpleNamespace(language='English', language_code='en', is_generated=False,
                                  fetch=lambda: [snippet])
    tracks = Mock()
    tracks.__iter__ = Mock(return_value=iter([track]))
    tracks.find_manually_created_transcript.return_value = track
    fetcher = subtitle_module.YouTubeSubtitleFetcher.__new__(subtitle_module.YouTubeSubtitleFetcher)
    fetcher._api = Mock()
    fetcher._api.list.return_value = tracks
    assert fetcher.fetch_subtitles('N7Hkqznvc7I') is None


@pytest.fixture
def generator():
    # 提取真实方法测试，避免加载语音模型、数据库和其他平台的运行时依赖。
    tree = ast.parse((ROOT / 'app/services/note.py').read_text(encoding='utf-8'))
    cls = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'NoteGenerator')
    method = next(node for node in cls.body if isinstance(node, ast.FunctionDef) and node.name == '_download_media')
    namespace = dict(globals(), Downloader=object, HttpUrl=str, DownloadQuality=str, TaskStatus=str,
                     extract_video_id=lambda url, platform: 'N7Hkqznvc7I' if platform == 'youtube' else None,
                     logger=logging.getLogger('recovery'), youtube_download_error=errors.youtube_download_error)
    exec(compile(ast.Module(body=[method], type_ignores=[]), '<actual-download-media>', 'exec'), namespace)
    instance = types.SimpleNamespace(_update_status=Mock(), _handle_exception=Mock())
    instance.download = types.MethodType(namespace['_download_media'], instance)
    return instance


def invoke(generator, downloader, tmp_path, **overrides):
    args = dict(downloader=downloader, video_url='https://www.youtube.com/watch?v=N7Hkqznvc7I',
                quality='fast', audio_cache_file=tmp_path / 'task_audio.json', status_phase='DOWNLOADING',
                platform='youtube', output_path=str(tmp_path), screenshot=False,
                video_understanding=False, video_interval=0, grid_size=[], skip_download=True,
                transcript=TranscriptResult('en', 'real text', [TranscriptSegment(0, 12, 'real text')]))
    args.update(overrides)
    return generator.download(**args)


def test_metadata_failure_does_not_download_audio(generator, tmp_path):
    downloader = Mock()
    downloader.download.side_effect = RuntimeError('Sign in to confirm you are not a bot')
    result = invoke(generator, downloader, tmp_path)
    assert result.file_path == ''
    assert result.raw_info['metadata_only'] is True
    assert result.raw_info['metadata_unavailable'] is True
    assert result.duration == 12
    assert 'N7Hkqznvc7I' in result.title
    assert downloader.download.call_count == 1
    assert downloader.download.call_args.kwargs['skip_download'] is True
    downloader.download_video.assert_not_called()


def test_metadata_failure_without_transcript_cannot_succeed(generator, tmp_path):
    downloader = Mock()
    downloader.download.side_effect = RuntimeError('blocked')
    with pytest.raises(RuntimeError):
        invoke(generator, downloader, tmp_path, transcript=None)
    assert downloader.download.call_count == 1


@pytest.mark.parametrize('raw_info,path_exists', [({'metadata_only': True}, True), ({}, False)])
def test_metadata_or_missing_file_cache_cannot_skip_audio(generator, tmp_path, raw_info, path_exists):
    path = tmp_path / 'audio.m4a'
    if path_exists:
        path.write_bytes(b'data')
    cached = AudioDownloadResult(str(path), 'cached', 12, None, 'youtube', 'id', raw_info)
    (tmp_path / 'task_audio.json').write_text(json.dumps(asdict(cached)))
    downloader = Mock()
    downloader.download.return_value = AudioDownloadResult('new.m4a', 'new', 12, None, 'youtube', 'id', {})
    result = invoke(generator, downloader, tmp_path, skip_download=False, transcript=None)
    assert result.title == 'new'
    downloader.download.assert_called_once()


def test_valid_audio_cache_is_reused(generator, tmp_path):
    path = tmp_path / 'audio.m4a'
    path.write_bytes(b'data')
    cached = AudioDownloadResult(str(path), 'cached', 12, None, 'youtube', 'id', {})
    (tmp_path / 'task_audio.json').write_text(json.dumps(asdict(cached)))
    downloader = Mock()
    assert invoke(generator, downloader, tmp_path, skip_download=False).title == 'cached'
    downloader.download.assert_not_called()


def test_video_requirement_is_not_bypassed_by_cache(generator, tmp_path):
    cached = AudioDownloadResult('', 'cached', 12, None, 'youtube', 'id', {'metadata_only': True})
    (tmp_path / 'task_audio.json').write_text(json.dumps(asdict(cached)))
    downloader = Mock()
    downloader.download_video.side_effect = RuntimeError('not a bot Cookie=SECRET')
    with pytest.raises(RuntimeError, match='尚未进入 AI 生成') as error:
        invoke(generator, downloader, tmp_path, skip_download=False, screenshot=True)
    assert 'SECRET' not in str(error.value)
    downloader.download_video.assert_called_once()


def test_non_youtube_keeps_original_fallback(generator, tmp_path):
    downloader = Mock()
    downloader.download.side_effect = [RuntimeError('no metadata'), AudioDownloadResult('a', 'ok', 0, None, 'bilibili', 'id', {})]
    assert invoke(generator, downloader, tmp_path, platform='bilibili').title == 'ok'
    assert downloader.download.call_count == 2


@pytest.mark.parametrize('detail,expected', [
    ("Sign in to confirm you’re not a bot", '验证'), ('HTTP Error 429', '限流'),
    ('Requested format is not available', '媒体格式'), ('private video', '权限'),
    ('connection timed out', '网络'), ('unexpected', '下载日志'),
])
def test_errors_do_not_expose_credentials(detail, expected):
    message = errors.youtube_download_error(RuntimeError(detail + ' https://user:SECRET@proxy/?token=TOKEN'))
    assert expected in message
    assert 'SECRET' not in message and 'TOKEN' not in message
