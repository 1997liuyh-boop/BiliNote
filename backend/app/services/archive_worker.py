import copy
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
from app.services.obsidian import category_path, document, wikilink
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

    def notify_failure(self, job_id, snapshot, result):
        failure = result["failureNotification"]
        label = snapshot["category"]["name"] if snapshot.get("category") else "所选笔记"
        titles = "、".join(note["audioMeta"]["title"][:80] for note in snapshot["notes"][:3])
        stage_label = {"UPLOAD": "上传文件", "HERMES": "Hermes 整理", "PUBLISH": "写入 Obsidian"}.get(failure["stage"], failure["stage"])
        message = (f"BiliNote 入库失败：{label}，共 {len(snapshot['notes'])} 条笔记。\n"
                   f"笔记：{titles}\n失败阶段：{stage_label}\n原因：{failure['reason'][:500]}\n"
                   f"已校验入库：{len(result.get('publishedNoteIds', []))}/{len(snapshot['notes'])} 条。\n"
                   f"任务：{job_id}\n请在生成历史的入库任务中处理原因后重试；本次入库未全部完成。")
        try:
            state = self.bridge("POST", f"/jobs/{job_id}/notify", {
                "message": message, "event": "failed", "attempt": failure["attempt"]})
            status = state.get("status", "UNKNOWN")
            if status not in ("SENT", "FAILED", "UNKNOWN", "SENDING"):
                status = "UNKNOWN"
        except Exception:
            # 桥接请求可能已经送出，保留未知状态，不能自动重复发送。
            status = "UNKNOWN"
            logger.warning("入库失败通知 %s 无法确认送达", job_id)
        self.update(job_id, result={**result, "failureNotification": {**failure, "status": status}})

    def tick(self):
        if not all(os.getenv(key) for key in ("ARCHIVE_BRIDGE_URL", "ARCHIVE_BRIDGE_TOKEN", "WEBDAV_URL", "WEBDAV_USERNAME", "WEBDAV_PASSWORD")):
            return
        with self.library.sessions() as db:
            # 先补发已经持久化但尚未开始发送的失败事件，重启后仍可恢复。
            pending = next((job for job in db.scalars(select(ArchiveJob).where(ArchiveJob.status == "FAILED"))
                            if job.result.get("failureNotification", {}).get("status") == "PENDING"), None)
            pending_data = (pending.id, pending.snapshot, pending.result) if pending else None
        if pending_data:
            self.notify_failure(*pending_data)
            return
        with self.library.sessions() as db:
            job = db.scalar(select(ArchiveJob).where(ArchiveJob.status.in_(["QUEUED", "RUNNING"]))
                            .order_by(ArchiveJob.created_at).limit(1))
            if not job:
                return
            job_id, stage, snapshot, result, attempt = job.id, job.stage, job.snapshot, job.result, job.attempts
        self.update(job_id, status="RUNNING")
        dav = None
        try:
            dav = WebDAV(os.environ["WEBDAV_URL"], os.environ["WEBDAV_USERNAME"], os.environ["WEBDAV_PASSWORD"])
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
                self.library.prepare_archive(job_id, state["result"])
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
            if stage != "NOTIFY":
                result = {**result, "failureNotification": {"status": "PENDING", "attempt": attempt,
                          "stage": stage, "reason": message}}
                self.update(job_id, status="FAILED", error=message, result=result)
                self.notify_failure(job_id, snapshot, result)
            else:
                # 文件已发布成功，仅完成通知失败时不能发送“入库失败”。
                self.update(job_id, status="FAILED", error=message)
            logger.warning("入库任务 %s 在 %s 阶段失败：%s", job_id, stage, type(exc).__name__)
        finally:
            if dav is not None:
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
        committed_index = copy.deepcopy(index)
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
        overview_path = "BiliNote/总览.md"
        existing_overview, _ = dav.get(overview_path)
        section = "<!-- bilinote-overview:start -->\n" + overview + "\n<!-- bilinote-overview:end -->"
        overview_content = document({"tags": ["bilinote", "总览"], "bilinote_managed": True}, section)
        if existing_overview is not None:
            text = existing_overview.decode()
            pattern = r"<!-- bilinote-overview:start -->.*?<!-- bilinote-overview:end -->"
            blocks = list(re.finditer(pattern, text, flags=re.DOTALL))
            if blocks:
                if (len(blocks) != 1 or text.count("<!-- bilinote-overview:start -->") != 1
                        or text.count("<!-- bilinote-overview:end -->") != 1
                        or (sha(blocks[0].group().encode()) != index.get("overviewSectionHash")
                            and blocks[0].group() != section)):
                    raise ValueError("总览的自动索引区域已被修改，已保留原文件；请处理该区域冲突后重试")
                overview_content = text[:blocks[0].start()] + section + text[blocks[0].end():]
            elif "<!-- bilinote-overview:" in text:
                raise ValueError("总览的自动索引区域标记不完整，已保留原文件；请处理冲突后重试")
            elif sha(existing_overview) != index["files"].get(overview_path):
                # 旧总览已被编辑时只追加自动区域，不覆盖原文或用户维护的目录。
                overview_content = text + "\n\n" + section + "\n"
        files[overview_path] = overview_content
        prepared = {}
        # 先检查所有文档冲突，避免写完正文后才发现后续文件无法更新。
        for path, content in files.items():
            content = self.attachments(dav, job_id, path, content).encode()
            previous, etag = dav.get(path)
            expected = index["files"].get(path)
            if path == overview_path and previous != existing_overview:
                raise ValueError("总览在发布期间发生变化，请重试")
            if path != overview_path and previous is not None and previous != content and (not expected or sha(previous) != expected):
                raise ValueError(f"{path} 已被手动修改，已保留原文件；请处理冲突后重试")
            if previous != content and previous is not None and not etag:
                raise ValueError("WebDAV 未提供 ETag，无法安全更新已有文件")
            prepared[path] = (content, previous, etag)
        for path, (content, previous, etag) in prepared.items():
            if previous != content:
                dav.put(path, content, etag=etag, new=previous is None)
            verification, _ = dav.get(path)
            if verification != content:
                raise ValueError("WebDAV 文件回读校验失败")
            sources = [n for n in snapshot["notes"] if result["paths"][n["id"]] == path]
            # 持久化索引只登记已回读成功的正文，避免部分失败时列出尚未写入的笔记。
            committed_index["files"][path] = sha(content)
            for source in sources:
                committed_index["notes"][source["id"]] = index["notes"][source["id"]]
            for category_id, category in index["categories"].items():
                if category["path"] == path:
                    committed_index["categories"][category_id] = category
            if path == overview_path:
                committed_index["overviewSectionHash"] = sha(section.encode())
            dav.put(INDEX_PATH, json.dumps(committed_index, ensure_ascii=False).encode(), etag=index_etag, new=raw is None)
            raw, index_etag = dav.get(INDEX_PATH)
            # 每篇正文回读及索引提交成功后立即记录，后续失败不抹掉已经入库的事实。
            if sources:
                published = set(result.get("publishedNoteIds", []))
                with self.library.sessions.begin() as db:
                    for source in sources:
                        note = db.get(LibraryNote, source["id"])
                        if note and not note.deleted:
                            note.archived_hash = source["fingerprint"]
                            note.archived_path = path
                            published.add(note.id)
                    result["publishedNoteIds"] = sorted(published)
                    db.get(ArchiveJob, job_id).result = dict(result)
        with self.library.sessions.begin() as db:
            category = snapshot.get("category")
            if category:
                row = db.get(LibraryCategory, category["id"])
                if row:
                    row.archived_hash = category["fingerprint"]
                    row.archived_path = category_path(category)


archive_worker = ArchiveWorker()
