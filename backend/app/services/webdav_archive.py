from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlparse
from xml.etree import ElementTree

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

    def list_directory(self, path=""):
        # 使用单层查询，兼容不允许 Depth: infinity 的 WebDAV 服务。
        url = self.url(path).rstrip("/") + "/" if path else self.root
        response = self.client.request("PROPFIND", url, headers={"Depth": "1", "Content-Type": "application/xml"},
            content=b'<d:propfind xmlns:d="DAV:"><d:prop><d:resourcetype/></d:prop></d:propfind>')
        response.raise_for_status()
        if response.status_code != 207 or len(response.content) > 4 * 1024 * 1024:
            raise ValueError("WebDAV 目录响应无效或过大")
        if b"<!DOCTYPE" in response.content.upper() or b"<!ENTITY" in response.content.upper():
            raise ValueError("WebDAV 目录响应包含不支持的 XML 声明")
        try:
            root = ElementTree.fromstring(response.content)
        except ElementTree.ParseError as exc:
            raise ValueError("WebDAV 目录响应不是有效 XML") from exc
        if root.tag != "{DAV:}multistatus":
            raise ValueError("WebDAV 目录响应缺少 multistatus")
        base = urlparse(self.root)
        base_path = unquote(base.path).rstrip("/") + "/"
        entries = {}
        for item in root.findall("{DAV:}response"):
            href = urlparse(item.findtext("{DAV:}href", ""))
            if href.netloc and (href.netloc != base.netloc or href.scheme != base.scheme):
                continue
            full_path = unquote(href.path)
            if not full_path.startswith(base_path):
                continue
            relative = full_path[len(base_path):].rstrip("/")
            parts = relative.split("/")
            if not relative or any(part in ("", ".", "..") for part in parts) or "\\" in relative:
                continue
            parent = relative.rpartition("/")[0]
            if parent != path:
                continue
            prop = next((value.find("{DAV:}prop") for value in item.findall("{DAV:}propstat")
                         if " 200 " in value.findtext("{DAV:}status", "")), None)
            if prop is None:
                raise ValueError("WebDAV 无法读取部分目录属性，请检查读取权限")
            entries[relative] = {"path": relative, "directory": prop.find("{DAV:}resourcetype/{DAV:}collection") is not None}
        return list(entries.values())

    def read_limited(self, path, limit):
        # 限制同步读取量，避免大文件耗尽内存。
        with self.client.stream("GET", self.url(path)) as response:
            response.raise_for_status()
            content = bytearray()
            for chunk in response.iter_bytes():
                content.extend(chunk)
                if len(content) > limit:
                    raise ValueError("Vault 中存在过大的文件，请缩小同步范围")
            return bytes(content)

    def put(self, path, content, etag=None, new=False):
        self.mkdirs(path)
        headers = {"If-None-Match": "*"} if new else ({"If-Match": etag} if etag else {})
        response = self.client.put(self.url(path), content=content, headers=headers)
        if response.status_code == 412:
            raise ValueError("文件已被其他设备修改，请检查冲突后重试")
        response.raise_for_status()

    def close(self):
        self.client.close()
