from pathlib import Path

from app.downloaders import bilibili_downloader as module
from app.utils.url_parser import bilibili_cache_id

BVID = 'BV1DfrdByE2H'


def test_cache_key_isolates_all_pages():
    assert bilibili_cache_id(f'https://www.bilibili.com/video/{BVID}') == f'{BVID}_p1'
    assert len({bilibili_cache_id(f'https://www.bilibili.com/video/{BVID}/?p={page}') for page in range(1, 44)}) == 43


def test_media_and_subtitle_caches_never_reuse_another_episode(tmp_path, monkeypatch):
    options = []
    class FakeYdl:
        def __init__(self, opts):
            self.opts = opts
            options.append(opts)
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def extract_info(self, url, download):
            assert self.opts['noplaylist'] is True
            assert download is True
            if self.opts.get('writesubtitles'):
                target = Path(self.opts['outtmpl'].replace('%(ext)s', 'zh.srt'))
                target.write_text('1\n00:00:00,000 --> 00:00:01,000\n' + url + '\n\n', encoding='utf-8')
                return {'requested_subtitles': {'zh': {'ext': 'srt'}}}
            ext = 'mp4' if self.opts.get('merge_output_format') else 'mp3'
            Path(self.opts['outtmpl'].replace('%(ext)s', ext)).write_text(url, encoding='utf-8')
            return {'id': BVID, 'title': '课程', 'duration': 1}
    monkeypatch.setattr(module.yt_dlp, 'YoutubeDL', FakeYdl)
    monkeypatch.setattr(module.BilibiliSubtitleFetcher, 'fetch_subtitles', lambda *args: None)
    downloader = module.BilibiliDownloader.__new__(module.BilibiliDownloader)
    downloader._cookiefile = None
    # 旧版本的 BV 缓存不能误命中任意分集。
    (tmp_path / f'{BVID}.mp4').write_text('旧版缓存', encoding='utf-8')
    for page in (1, 38):
        url = f'https://www.bilibili.com/video/{BVID}/?p={page}'
        audio = downloader.download(url, str(tmp_path))
        assert Path(audio.file_path).name == f'{BVID}_p{page}.mp3'
        video = downloader.download_video(url, str(tmp_path))
        assert Path(video).name == f'{BVID}_p{page}.mp4'
        assert Path(video).read_text(encoding='utf-8') == url
        subtitle = downloader.download_subtitles(url, str(tmp_path), ['zh'])
        assert url in subtitle.full_text
        assert downloader.download_video(url, str(tmp_path)) == video
    assert len(options) == 6
    assert len({opts['outtmpl'] for opts in options}) == 2
