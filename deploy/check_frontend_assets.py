"""只读验收首页引用的 JS/CSS，防止首页 200 掩盖静态资源故障。"""
import argparse
from html.parser import HTMLParser
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, build_opener, ProxyHandler, HTTPRedirectHandler
from uuid import uuid4


class EntryAssets(HTMLParser):
    def __init__(self):
        super().__init__()
        self.assets = set()

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script" and attrs.get("src"):
            self.assets.add((attrs["src"], "js"))
        if tag == "link" and attrs.get("href"):
            relations = attrs.get("rel", "").split()
            if "stylesheet" in relations:
                self.assets.add((attrs["href"], "css"))
            elif "modulepreload" in relations:
                self.assets.add((attrs["href"], "js"))


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # 重定向可能掩盖登录页或代理错误，验收时应直接报错。
        return None


def fetch(url):
    opener = build_opener(ProxyHandler({}), NoRedirect())
    request = Request(url, headers={"Cache-Control": "no-cache"})
    try:
        response = opener.open(request, timeout=15)
    except HTTPError as error:
        response = error
    with response:
        return response.code, response.headers.get_content_type(), response.read(8 * 1024 * 1024 + 1)


def check_asset(status, content_type, body, kind):
    expected = {"text/css"} if kind == "css" else {"application/javascript", "text/javascript"}
    if status != 200 or content_type not in expected:
        raise ValueError(f"资源响应错误：HTTP {status}, {content_type}")
    if not body or len(body) > 8 * 1024 * 1024:
        raise ValueError("资源为空或超过验收大小上限")
    prefix = body.lstrip()[:256].lower()
    if prefix.startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
        raise ValueError("资源正文实际为 HTML，不能仅修改 MIME 类型掩盖错误")


def verify(base_url):
    status, content_type, body = fetch(base_url)
    if status != 200 or content_type != "text/html":
        raise ValueError(f"首页响应错误：HTTP {status}, {content_type}")
    parser = EntryAssets()
    parser.feed(body.decode("utf-8"))
    if not parser.assets:
        raise ValueError("首页没有 JS/CSS 引用，可能访问到了错误页面")
    origin = urlsplit(base_url)
    for reference, kind in sorted(parser.assets):
        url = urljoin(base_url, reference)
        target = urlsplit(url)
        if (target.scheme, target.netloc) != (origin.scheme, origin.netloc):
            raise ValueError("验收不自动请求外部域名资源")
        check_asset(*fetch(url), kind)
        print(f"PASS {kind} {target.path}")
    # 随机不存在的资源必须为 404，不能以 200 返回首页。
    missing = urljoin(base_url, f"assets/missing-{uuid4().hex}.js")
    if fetch(missing)[0] != 404:
        raise ValueError("不存在的静态资源未返回 404")
    print(f"PASS {len(parser.assets)} entry assets; missing asset returns 404")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("base_url", help="站点入口 URL，例如 http://127.0.0.1:3015/")
    args = parser.parse_args()
    try:
        verify(args.base_url)
    except (ValueError, OSError) as error:
        parser.exit(1, f"FAIL {error}\n")
