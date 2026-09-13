import hashlib
import json
import logging
import os
import re
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import select

from app.db.models.library import ArchiveJob, LibraryCategory, LibraryNote
from app.services.library import library, now
from app.services.obsidian import category_path, document, render_archive, wikilink
from app.services.webdav_archive import WebDAV

logger = logging.getLogger(__name__)
INDEX_PATH = "BiliNote/.bilinote-index.json"


def sha(content):
    return hashlib.sha256(content).hexdigest()


class ArchiveWorker:
    def __init__(self, service=library):
        self.library = service
        self.stopped = threading.Event()
        self.thread = None

    def start(self):
        if self.thread:
            return
        self.thread = threading.Thread(target=self.loop, name="bilinote-archive", daemon=True)
        self.thread.start()

    def stop(self):
        self.stopped.set()
        if self.thread:
            self.thread.join(timeout=3)

    def loop(self):
        # 文件锁保证多个后端进程只启动一个归档执行者。
        from filelock import FileLock, Timeout
        lock_path = self.library.output_dir / ".archive-worker.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            with FileLock(str(lock_path), timeout=0):
                while not self.stopped.is_set():
                    try:
                        self.tick()
                    except Exception:
                        logger.exception("归档工作进程异常")
                    self.stopped.wait(5)
        except Timeout:
            logger.info("已有归档工作进程运行")

    def update(self, job_id, **values):
        with self.library.sessions.begin() as db:
            job = db.get(ArchiveJob, job_id)
            for key, value in values.items():
                setattr(job, key, value)
            job.updated_at = now()

    def bridge(self, method, path, payload=None):
        url = os.environ["ARCHIVE_BRIDGE_URL"].rstrip("/") + path
        with httpx.Client(timeout=30, trust_env=False) as client:
            response = client.request(method, url, json=payload,
                headers={"Authorization": "Bearer " + os.environ["ARCHIVE_BRIDGE_TOKEN"]})
            response.raise_for_status()
            return response.json()

    def tick(self):
        if not all(os.getenv(key) for key in ("ARCHIVE_BRIDGE_URL", "ARCHIVE_BRIDGE_TOKEN", "WEBDAV_URL", "WEBDAV_USERNAME", "WEBDAV_PASSWORD")):
            return
        with self.library.sessions() as db:
            job = db.scalar(select(ArchiveJob).where(ArchiveJob.status.in_(["QUEUED", "RUNNING"]))
                            .order_by(ArchiveJob.created_at).limit(1))
            if not job:
                return
            job_id, stage, snapshot, result = job.id, job.stage, job.snapshot, job.result
        self.update(job_id, status="RUNNING")
        dav = WebDAV(os.environ["WEBDAV_URL"], os.environ["WEBDAV_USERNAME"], os.environ["WEBDAV_PASSWORD"])
        try:
            if stage == "UPLOAD":
                dav.put(f".bilinote-inbox/{job_id}/manifest.json", json.dumps(snapshot, ensure_ascii=False).encode())
                self.bridge("POST", "/jobs", {"id": job_id})
                self.update(job_id, stage="HERMES")
                return
            if stage == "HERMES":
                state = self.bridge("GET", f"/jobs/{job_id}")
                if state["status"] == "FAILED":
                    raise ValueError(state.get("error") or "Hermes 整理失败")
                if state["status"] != "COMPLETED":
                    return
                files, paths = render_archive(snapshot, state["result"])
                result = {"files": files, "paths": paths, "overview": "BiliNote/总览.md"}
                self.update(job_id, result=result, stage="PUBLISH")
                return
            if stage == "PUBLISH":
                self.publish(dav, job_id, snapshot, result)
                self.update(job_id, stage="NOTIFY")
                return
            if stage == "NOTIFY":
                label = snapshot["category"]["name"] if snapshot.get("category") else "所选笔记"
                message = f"BiliNote 已整理完毕：{label}，共 {len(snapshot['notes'])} 条笔记。\n已同步至 Obsidian：BiliNote/总览.md\n任务：{job_id}"
                state = self.bridge("POST", f"/jobs/{job_id}/notify", {"message": message})
                if state.get("status") != "SENT":
                    self.update(job_id, notification=state.get("status", "FAILED"))
                    raise ValueError(state.get("error") or "文件已入库，但 QQ 通知未送达")
                self.update(job_id, status="COMPLETED", notification="SENT")
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else f"{stage} 步骤连接失败，请检查服务后重试"
            self.update(job_id, status="FAILED", error=message)
            logger.warning("入库任务 %s 在 %s 阶段失败：%s", job_id, stage, type(exc).__name__)
        finally:
            dav.close()

    def attachments(self, dav, job_id, path, content):
        static_root = Path("static").resolve()
        pattern = r"!\[([^\]]*)\]\(([^\s)]+)(?:\s+\"[^\"]*\")?\)"
        def replace(match):
            url = urlparse(match.group(2))
            image_path = unquote(url.path)
            if not image_path.startswith("/static/"):
                return match.group(0)
            source = (static_root / image_path.removeprefix("/static/")).resolve()
            if not source.is_relative_to(static_root) or not source.is_file():
                raise ValueError("笔记引用的截图不存在，已暂停发布")
            data = source.read_bytes()
            target = f"BiliNote/附件/{sha(data)}{source.suffix.lower()}"
            current, _ = dav.get(target)
            if current is None:
                dav.put(target, data, new=True)
            elif sha(current) != sha(data):
                raise ValueError("附件校验失败")
            return f"![[{target}]]"
        return re.sub(pattern, replace, content)

    def publish(self, dav, job_id, snapshot, result):
        raw, index_etag = dav.get(INDEX_PATH)
        index = json.loads(raw) if raw else {"files": {}, "notes": {}, "categories": {}}
        files = dict(result["files"])
        for note in snapshot["notes"]:
            category_id = note.get("categoryId")
            index["notes"][note["id"]] = {"path": result["paths"][note["id"]], "title": note["audioMeta"]["title"],
                "category_id": category_id, "updated": now()}
            if category_id:
                category = {"id": category_id, "name": note["categoryName"], "archivePath": note.get("categoryPath", "")}
                index["categories"][category_id] = {"path": category_path(category), "name": category["name"]}
        if snapshot.get("category"):
            category = snapshot["category"]
            index["categories"][category["id"]] = {"path": category_path(category), "name": category["name"]}
        # 单条入库也创建分类入口，避免笔记链接指向不存在的页面。
        for category_id, category in index["categories"].items():
            members = [value for value in index["notes"].values() if value.get("category_id") == category_id]
            existing, _ = dav.get(category["path"])
            if category["path"] not in files:
                members_body = "\n".join("- " + wikilink(n["path"], n["title"]) for n in members)
                section = "<!-- bilinote-members:start -->\n## 已入库笔记\n\n" + members_body + "\n<!-- bilinote-members:end -->"
                if existing is None:
                    body = f"# {category['name']}\n\n" + wikilink("BiliNote/总览.md", "返回总览")
                    body += "\n\n尚未执行整类综合归纳。\n\n" + section
                    files[category["path"]] = document({"id": f"bilinote-category-{category_id}", "tags": ["bilinote", "分类"], "bilinote_managed": True}, body)
                elif index["files"].get(category["path"]) == sha(existing):
                    text = existing.decode()
                    pattern = r"<!-- bilinote-members:start -->.*?<!-- bilinote-members:end -->"
                    files[category["path"]] = re.sub(pattern, lambda _: section, text, flags=re.DOTALL) if re.search(pattern, text, re.DOTALL) else text.rstrip() + "\n\n" + section + "\n"
        overview = "# BiliNote 笔记总览\n\n## 分类\n\n"
        for category_id, category in sorted(index["categories"].items(), key=lambda item: item[1]["name"]):
            count = sum(n.get("category_id") == category_id for n in index["notes"].values())
            overview += f"- {wikilink(category['path'], category['name'])} · {count} 条\n"
        overview += "\n## 最近入库\n\n" + "\n".join("- " + wikilink(n["path"], n["title"])
            for n in sorted(index["notes"].values(), key=lambda n: n["updated"], reverse=True)[:50])
        overview += "\n\n## 未归类\n\n" + "\n".join("- " + wikilink(n["path"], n["title"])
            for n in index["notes"].values() if not n.get("category_id"))
        files["BiliNote/总览.md"] = document({"tags": ["bilinote", "总览"], "bilinote_managed": True}, overview)
        for path, content in files.items():
            content = self.attachments(dav, job_id, path, content).encode()
            previous, etag = dav.get(path)
            expected = index["files"].get(path)
            if previous is not None and previous != content and (not expected or sha(previous) != expected):
                raise ValueError(f"{path} 已被手动修改，已保留原文件；请处理冲突后重试")
            if previous != content:
                if previous is not None and not etag:
                    raise ValueError("WebDAV 未提供 ETag，无法安全更新已有文件")
                dav.put(path, content, etag=etag, new=previous is None)
            verification, _ = dav.get(path)
            if verification != content:
                raise ValueError("WebDAV 文件回读校验失败")
            index["files"][path] = sha(content)
            dav.put(INDEX_PATH, json.dumps(index, ensure_ascii=False).encode(), etag=index_etag, new=raw is None)
            raw, index_etag = dav.get(INDEX_PATH)
        with self.library.sessions.begin() as db:
            for source in snapshot["notes"]:
                note = db.get(LibraryNote, source["id"])
                if note and not note.deleted:
                    note.archived_hash = source["fingerprint"]
                    note.archived_path = result["paths"][note.id]
            category = snapshot.get("category")
            if category:
                row = db.get(LibraryCategory, category["id"])
                if row:
                    row.archived_hash = category["fingerprint"]
                    row.archived_path = category_path(category)


archive_worker = ArchiveWorker()
