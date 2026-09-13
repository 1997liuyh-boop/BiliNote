from pathlib import PurePosixPath
from urllib.parse import quote, urlparse

import httpx


class WebDAV:
    def __init__(self, url, username, password):
        if urlparse(url).scheme not in ("http", "https"):
            raise ValueError("WebDAV 地址必须使用 HTTP 或 HTTPS")
        self.root = url.rstrip("/") + "/"
        self.client = httpx.Client(auth=(username, password), timeout=60, follow_redirects=False, trust_env=False)

    def url(self, path):
        parts = PurePosixPath(path).parts
        if not parts or path.startswith("/") or ".." in parts or "\\" in path:
            raise ValueError("WebDAV 路径无效")
        return self.root + "/".join(quote(part, safe="") for part in parts)

    def mkdirs(self, path):
        parts = PurePosixPath(path).parent.parts
        for index in range(1, len(parts) + 1):
            response = self.client.request("MKCOL", self.url("/".join(parts[:index])))
            if response.status_code not in (201, 405):
                response.raise_for_status()

    def get(self, path):
        response = self.client.get(self.url(path))
        if response.status_code == 404:
            return None, None
        response.raise_for_status()
        return response.content, response.headers.get("etag")

    def put(self, path, content, etag=None, new=False):
        self.mkdirs(path)
        headers = {"If-None-Match": "*"} if new else ({"If-Match": etag} if etag else {})
        response = self.client.put(self.url(path), content=content, headers=headers)
        if response.status_code == 412:
            raise ValueError("文件已被其他设备修改，请检查冲突后重试")
        response.raise_for_status()

    def close(self):
        self.client.close()
