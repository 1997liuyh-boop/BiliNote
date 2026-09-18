"""
通过 youtube-transcript-api 获取 YouTube 字幕。
优先人工字幕，其次自动生成字幕。不依赖 yt_dlp，无需下载任何文件。
"""

import math
from typing import Optional, List

from youtube_transcript_api import YouTubeTranscriptApi

from app.models.transcriber_model import TranscriptResult, TranscriptSegment
from app.services.proxy_config_manager import ProxyConfigManager
from app.utils.logger import get_logger

logger = get_logger(__name__)


class YouTubeSubtitleFetcher:
    """通过 youtube-transcript-api 获取 YouTube 字幕。"""

    def __init__(self):
        # 配了全局代理就给 youtube-transcript-api 套一个带 proxies 的 requests.Session，
        # 否则国内拉字幕同样会超时。代理未配置时退回默认无代理客户端。
        proxy = ProxyConfigManager().get_proxy_url()
        if proxy:
            try:
                import requests
                session = requests.Session()
                session.proxies = {"http": proxy, "https": proxy}
                self._api = YouTubeTranscriptApi(http_client=session)
                logger.info("YouTube 字幕使用已配置的代理")
            except Exception as e:
                logger.warning("为字幕客户端配置代理失败：%s", type(e).__name__)
                self._api = YouTubeTranscriptApi()
        else:
            self._api = YouTubeTranscriptApi()

    def fetch_subtitles(
        self,
        video_id: str,
        langs: Optional[List[str]] = None,
    ) -> Optional[TranscriptResult]:
        if langs is None:
            langs = ["zh-Hans", "zh", "zh-CN", "zh-TW", "en", "en-US", "ja"]

        try:
            # 1. 列出所有可用字幕
            transcript_list = self._api.list(video_id)

            available = []
            for t in transcript_list:
                available.append(
                    f"{t.language_code}({'auto' if t.is_generated else 'manual'})"
                )
            logger.info(f"可用字幕轨道: {', '.join(available)}")

            # 2. 按优先级查找：先人工字幕，再自动字幕
            transcript = None
            try:
                transcript = transcript_list.find_manually_created_transcript(langs)
                logger.info(f"选中人工字幕: {transcript.language_code} ({transcript.language})")
            except Exception:
                try:
                    transcript = transcript_list.find_generated_transcript(langs)
                    logger.info(f"选中自动字幕: {transcript.language_code} ({transcript.language})")
                except Exception:
                    # 都没匹配，取第一个可用的
                    for t in transcript_list:
                        transcript = t
                        source = "auto" if t.is_generated else "manual"
                        logger.info(f"使用首个可用字幕: {t.language_code} ({source})")
                        break

            if not transcript:
                logger.info(f"YouTube 视频 {video_id} 没有任何可用字幕")
                return None

            # 3. 获取字幕内容
            fetched = transcript.fetch()
            segments = []
            for snippet in fetched:
                # 新版 API 返回数据对象，旧版返回字典；不能把对象描述当成字幕。
                get = snippet.get if isinstance(snippet, dict) else lambda key, default: getattr(snippet, key, default)
                text = get("text", "")
                if not isinstance(text, str) or not text.strip():
                    continue
                try:
                    start = float(get("start", 0))
                    duration = float(get("duration", 0))
                except (TypeError, ValueError):
                    continue
                if not all(math.isfinite(value) and value >= 0 for value in (start, duration, start + duration)):
                    continue
                segments.append(TranscriptSegment(start=start, end=start + duration, text=text.strip()))

            if not segments:
                logger.warning(f"YouTube 字幕内容为空: {video_id}")
                return None

            full_text = " ".join(seg.text for seg in segments)
            logger.info(f"成功获取 YouTube 字幕，共 {len(segments)} 段")

            return TranscriptResult(
                language=transcript.language_code,
                full_text=full_text,
                segments=segments,
                raw={
                    "source": "youtube_transcript_api",
                    "language": transcript.language,
                    "language_code": transcript.language_code,
                    "is_generated": transcript.is_generated,
                },
            )

        except Exception as e:
            logger.warning("YouTube 字幕获取失败：%s", type(e).__name__)
            return None
