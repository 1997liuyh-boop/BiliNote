"""只读取 B 站多 P 目录，不下载视频，也不执行页面脚本。"""
import json
import re
from urllib.parse import parse_qs, urlsplit

import requests

from app.services.cookie_manager import CookieConfigManager

MAX_EPISODES = 500
MAX_RESPONSE_BYTES = 8 * 1024 * 1024
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"


class CollectionUnavailable(ValueError):
    pass


class PageOutOfRange(ValueError):
    pass


def parse_video_url(value):
    value = str(value).strip().replace("\\&", "&")
    if "://" not in value:
        value = "https://" + value
    url = urlsplit(value)
    if (url.scheme not in ("http", "https") or url.hostname not in
            ("www.bilibili.com", "bilibili.com", "m.bilibili.com") or
            url.username or url.password or url.port not in (None, 80, 443)):
        raise ValueError("请使用 bilibili.com/video/BV… 格式的完整视频链接")
    match = re.fullmatch(r"/video/(BV[a-zA-Z0-9]{10})/?", url.path)
    if not match:
        raise ValueError("请使用 bilibili.com/video/BV… 格式的完整视频链接")
    pages = parse_qs(url.query, keep_blank_values=True).get("p", ["1"])
    if len(pages) != 1 or not pages[0].isdigit() or int(pages[0]) < 1:
        raise ValueError("分集序号 p 必须是正整数")
    return match.group(1), int(pages[0])


def read_public_resource(url, headers, params=None):
    # 地址只能由服务端常量构造，禁止跳转，Cookie 不会发往用户输入的主机。
    with requests.get(url, params=params, headers=headers, timeout=(5, 15),
                      allow_redirects=False, stream=True) as response:
        if response.status_code != 200:
            raise CollectionUnavailable("B 站暂时拒绝访问，请稍后重试或检查 B 站 Cookie 配置")
        chunks, size = [], 0
        for chunk in response.iter_content(65536):
            size += len(chunk)
            if size > MAX_RESPONSE_BYTES:
                raise CollectionUnavailable("视频目录响应过大，已停止读取")
            chunks.append(chunk)
        return b"".join(chunks).decode("utf-8")


def parse_collection(data, bvid, current_page, source):
    if not isinstance(data, dict) or data.get("bvid") != bvid:
        raise CollectionUnavailable("视频目录与请求的视频不一致")
    pages = data.get("pages")
    count = data.get("videos")
    if not isinstance(pages, list) or not pages or type(count) is not int or count != len(pages):
        raise CollectionUnavailable("未能取得完整分集目录，请稍后重试")
    if count > MAX_EPISODES:
        raise CollectionUnavailable(f"选集超过 {MAX_EPISODES} 集，暂不支持整套读取")
    episodes, seen = [], set()
    for page in pages:
        if not isinstance(page, dict):
            raise CollectionUnavailable("分集目录格式异常")
        number, cid = page.get("page"), page.get("cid")
        title, duration = page.get("part"), page.get("duration")
        if (type(number) is not int or type(cid) is not int or cid <= 0 or cid in seen or
                not isinstance(title, str) or not title.strip() or
                type(duration) is not int or duration < 0):
            raise CollectionUnavailable("分集目录格式异常")
        seen.add(cid)
        episodes.append({"page": number, "cid": cid, "title": title.strip(), "duration": duration,
                         "url": f"https://www.bilibili.com/video/{bvid}/?p={number}"})
    episodes.sort(key=lambda episode: episode["page"])
    if [episode["page"] for episode in episodes] != list(range(1, count + 1)):
        raise CollectionUnavailable("分集目录不完整或包含重复序号")
    if current_page > count:
        raise PageOutOfRange(f"该视频只有 {count} 集，链接中的 p={current_page} 超出范围")
    return {"bvid": bvid, "title": str(data.get("title") or bvid), "kind": "multipart",
            "currentPage": current_page, "total": count, "episodes": episodes, "source": source}


def get_collection(video_url):
    bvid, current_page = parse_video_url(video_url)
    headers = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
    cookie = CookieConfigManager().get("bilibili")
    if cookie:
        headers["Cookie"] = cookie
    try:
        payload = json.loads(read_public_resource("https://api.bilibili.com/x/web-interface/view",
                                                  headers, {"bvid": bvid}))
        if payload.get("code") == 0:
            return parse_collection(payload.get("data"), bvid, current_page, "api")
    except PageOutOfRange:
        raise
    except (requests.RequestException, ValueError, UnicodeError, AttributeError):
        pass
    try:
        text = read_public_resource(f"https://www.bilibili.com/video/{bvid}/?p={current_page}", headers)
        marker = re.search(r"window\.__INITIAL_STATE__\s*=\s*", text)
        if not marker:
            raise CollectionUnavailable("网页未提供可读取的分集目录")
        state, _ = json.JSONDecoder().raw_decode(text[marker.end():])
        return parse_collection(state.get("videoData"), bvid, current_page, "page")
    except (CollectionUnavailable, PageOutOfRange):
        raise
    except (requests.RequestException, ValueError, UnicodeError, AttributeError) as exc:
        raise CollectionUnavailable("无法获取完整选集，请稍后重试或在设置中检查 B 站 Cookie") from exc
