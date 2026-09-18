"""将下载阶段错误转为不包含 Cookie、代理或媒体签名的提示。"""


import re


def youtube_download_error(error: Exception) -> str:
    text = re.sub(r"https?://\S+", "", str(error).lower()).replace("’", "'")
    if "not a bot" in text or "confirm you're" in text:
        return "获取 YouTube 内容失败：YouTube 要求登录并验证访问请求。当前服务器会话未通过验证，尚未进入 AI 生成。请检查下载器 Cookie 与服务器访问环境，验证通过后再重试。"
    if "429" in text or "too many requests" in text:
        return "获取 YouTube 内容失败：请求被限流（429）。请暂停重试，稍后检查服务器访问环境。"
    if any(value in text for value in ("private video", "members-only", "sign in to confirm your age", "unavailable")):
        return "获取 YouTube 内容失败：视频不可用或需要访问权限。请确认视频可访问，且配置的会话具备相应权限。"
    if any(value in text for value in ("po token", "po_token", "requested format", "javascript", "challenge", "nsig")):
        return "获取 YouTube 内容失败：没有可用媒体格式或播放器验证失败。请检查 yt-dlp、JavaScript 运行环境及日志中的验证要求。"
    if any(value in text for value in ("timed out", "timeout", "connection", "proxy")):
        return "获取 YouTube 内容失败：连接超时或网络不可达。请检查服务器网络和已配置的代理。"
    return "获取 YouTube 内容失败：未能读取视频内容。请检查服务器下载日志，解决访问问题后再重试。"
