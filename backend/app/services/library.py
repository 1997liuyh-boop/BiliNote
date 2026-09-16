import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db.engine import SessionLocal
from app.db.models.library import ArchiveJob, LibraryCategory, LibraryNote, LibraryVersion

logger = logging.getLogger(__name__)
ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")


def now():
    return datetime.now(timezone.utc).isoformat()


def timestamp(value=None):
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=parsed.tzinfo or timezone.utc).astimezone(timezone.utc).isoformat()
    except (ValueError, TypeError):
        return now()


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def valid_id(value):
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise ValueError("无效的笔记 ID")
    return value


def note_fingerprint(note):
    return digest([note.id, note.content_hash, note.category_id, note.title, note.payload.get("categoryName", "")])


def job_dict(job):
    return {"id": job.id, "scope": job.scope, "category_id": job.category_id,
            "status": job.status, "stage": job.stage, "error": job.error,
            "notification": job.notification, "failureNotification": job.result.get("failureNotification"),
            "createdAt": job.created_at,
            "updatedAt": job.updated_at, "snapshot": job.snapshot, "result": job.result}


class LibraryService:
    def __init__(self, sessions=SessionLocal, output_dir=None):
        self.sessions = sessions
        self.output_dir = Path(output_dir or os.getenv("NOTE_OUTPUT_DIR", "note_results"))
        self.scan_lock = threading.RLock()
        self.last_scan = 0

    def require_note(self, db, note_id):
        note = db.get(LibraryNote, valid_id(note_id))
        if note is None or note.deleted:
            raise ValueError("笔记不存在或已删除")
        return note

    def versions(self, db, note_id):
        return db.scalars(select(LibraryVersion).where(LibraryVersion.note_id == note_id)
                          .order_by(LibraryVersion.created_at.desc(), LibraryVersion.id.desc())).all()

    def add_versions(self, db, note, markdown, created_at, activate=False):
        values = markdown if isinstance(markdown, list) else [{"content": markdown}]
        active_id = ""
        for version in values:
            if not isinstance(version, dict):
                raise ValueError("笔记版本格式不正确")
            content = version.get("content", "")
            if not isinstance(content, str) or not content.strip():
                continue
            # 相同内容重复导入不产生新版本，旧浏览器的不同版本全部保留。
            version_id = digest([note.id, content])
            active_id = active_id or version_id
            existing = db.get(LibraryVersion, version_id)
            form = note.payload.get("formData", {})
            if existing:
                existing.style = existing.style or version.get("style") or form.get("style") or ""
                existing.model_name = existing.model_name or version.get("model_name") or form.get("model_name") or ""
                if activate or version.get("created_at"):
                    existing.created_at = max(existing.created_at, timestamp(version.get("created_at") or created_at))
                continue
            db.add(LibraryVersion(id=version_id, note_id=note.id, content=content,
                                 created_at=timestamp(version.get("created_at") or created_at),
                                 style=version.get("style") or form.get("style") or "",
                                 model_name=version.get("model_name") or form.get("model_name") or ""))
            db.flush()
        db.flush()
        versions = self.versions(db, note.id)
        if activate and active_id:
            # 当前结果由服务器明确指定，不受其他电脑的时钟偏差影响。
            note.payload = {**note.payload, "currentVersion": active_id}
        note.content_hash = note.payload.get("currentVersion") or (versions[0].id if versions else "")

    def save_pending(self, task_id, form):
        valid_id(task_id)
        with self.sessions.begin() as db:
            note = db.get(LibraryNote, task_id)
            if note and note.deleted:
                raise ValueError("已删除的任务不能重试")
            if note is None:
                note = LibraryNote(id=task_id, created_at=now(), updated_at=now(), payload={})
                db.add(note)
            previous = note.payload.get("formData", {})
            if previous.get("collection_title"):
                form = {**form, "collection_title": previous["collection_title"]}
            note.payload = {**note.payload, "formData": form, "platform": form.get("platform", "")}
            note.status = "PENDING"
            note.updated_at = now()

    def save_collection_pending(self, request_id, form, episodes, collection_title):
        """整批原子入库；同一提交标识重试只返回原任务，不重复生成。"""
        namespace = uuid.UUID(request_id)
        fingerprint = digest([form, [episode["url"] for episode in episodes]])
        items = [{"task_id": str(uuid.uuid5(namespace, episode["url"])),
                  "page": episode["page"], "title": f"{collection_title} · P{episode['page']} {episode['title']}",
                  "url": episode["url"]} for episode in episodes]
        with self.scan_lock:
            for attempt in range(2):
                try:
                    with self.sessions.begin() as db:
                        existing = db.scalars(select(LibraryNote).where(
                            LibraryNote.payload["collectionRequestId"].as_string() == request_id)).all()
                        if existing:
                            if ({note.id for note in existing} != {item["task_id"] for item in items} or
                                    any(note.deleted or note.payload.get("collectionFingerprint") != fingerprint for note in existing)):
                                raise ValueError("该批次已提交，但参数已变化或笔记已删除；请核对历史后重新提交")
                            return items, False
                        for item in items:
                            db.add(LibraryNote(id=item["task_id"], title=item["title"], status="PENDING",
                                created_at=now(), updated_at=now(), payload={
                                    "platform": "bilibili", "collectionFingerprint": fingerprint, "collectionRequestId": request_id,
                                    "audioMeta": {"title": item["title"], "platform": "bilibili"},
                                    "formData": {**form, "video_url": item["url"],
                                                 "collection_request_id": request_id,
                                                 "collection_title": item["title"]}}))
                        db.flush()
                    return items, True
                except IntegrityError:
                    # 多进程同时提交时，由数据库主键兜底，重读已经提交的批次。
                    if attempt:
                        raise

    def save_result(self, task_id, result, created_at=None, stamp=""):
        valid_id(task_id)
        with self.sessions.begin() as db:
            note = db.get(LibraryNote, task_id)
            if note and note.deleted:
                return
            if note is None:
                note = LibraryNote(id=task_id, created_at=timestamp(created_at), updated_at=now(), payload={})
                db.add(note)
            audio = result.get("audio_meta") or result.get("audioMeta") or {}
            form = note.payload.get("formData", {})
            if form.get("collection_title"):
                audio = {**audio, "title": form["collection_title"]}
            # 老文件没有生成参数时，从笔记中的来源链接补回可用的信息。
            markdown = result.get("markdown", "")
            source = re.search(r"来源链接[：:]\s*(https?://\S+)", markdown) if isinstance(markdown, str) else None
            if not form.get("video_url") and source:
                form = {**form, "video_url": source.group(1)}
            note.payload = {**note.payload, "audioMeta": audio, "formData": form,
                            "platform": audio.get("platform") or form.get("platform", ""),
                            "transcript": result.get("transcript") or {}}
            note.title = audio.get("title") or note.title or "未命名笔记"
            self.add_versions(db, note, markdown, created_at or now(), activate=True)
            note.status = "SUCCESS" if note.content_hash else "SAVING"
            note.updated_at = now()
            note.file_stamp = stamp

    def import_legacy(self, tasks):
        imported = 0
        with self.sessions.begin() as db:
            for task in tasks:
                task_id = valid_id(task.get("id"))
                note = db.get(LibraryNote, task_id)
                if note and note.deleted:
                    continue
                if note is None:
                    payload = {key: task.get(key) or {} for key in ("audioMeta", "formData", "transcript")}
                    payload["platform"] = task.get("platform") or payload["audioMeta"].get("platform", "")
                    note = LibraryNote(id=task_id, created_at=timestamp(task.get("createdAt")),
                                       updated_at=now(), payload=payload,
                                       title=payload["audioMeta"].get("title") or "未命名笔记",
                                       status=task.get("status", "PENDING"))
                    db.add(note)
                else:
                    payload = dict(note.payload)
                    for key in ("audioMeta", "formData", "transcript"):
                        current = payload.get(key) or {}
                        incoming = task.get(key) or {}
                        if not isinstance(incoming, dict):
                            raise ValueError("旧历史元数据格式不正确")
                        payload[key] = {**incoming, **{k: v for k, v in current.items() if v not in (None, "", [], {})}}
                    note.payload = payload
                    note.title = payload["audioMeta"].get("title") or note.title or "未命名笔记"
                    if task.get("createdAt"):
                        note.created_at = min(note.created_at, timestamp(task["createdAt"]))
                self.add_versions(db, note, task.get("markdown", ""), task.get("createdAt"))
                if note.content_hash and note.status not in ("PENDING", "PARSING", "DOWNLOADING", "TRANSCRIBING", "SUMMARIZING"):
                    note.status = "SUCCESS"
                imported += 1
        return {"imported": imported}

    def refresh_files(self, force=False):
        with self.scan_lock:
            if not force and time.monotonic() - self.last_scan < 5:
                return
            self.last_scan = time.monotonic()
            if not self.output_dir.exists():
                return
            for path in self.output_dir.glob("*.json"):
                task_id = path.stem
                if not ID_PATTERN.fullmatch(task_id) or task_id.endswith(("_audio", "_transcript")):
                    continue
                try:
                    stat = path.stat()
                    stamp = f"{stat.st_mtime_ns}:{stat.st_size}"
                    with self.sessions() as db:
                        note = db.get(LibraryNote, task_id)
                        if note and (note.deleted or note.file_stamp == stamp):
                            continue
                    data = json.loads(path.read_text(encoding="utf-8"))
                    if not isinstance(data, dict) or not data.get("markdown"):
                        continue
                    self.save_result(task_id, data, datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(), stamp)
                except (ValueError, OSError, TypeError, AttributeError):
                    logger.warning("跳过无法读取的历史文件：%s", path.name)
            # 状态文件用于跨设备展示正在生成的任务，不把缺结果文件的 SUCCESS 当作完成。
            for path in self.output_dir.glob("*.status.json"):
                task_id = path.name.removesuffix(".status.json")
                if not ID_PATTERN.fullmatch(task_id):
                    continue
                try:
                    status = json.loads(path.read_text(encoding="utf-8")).get("status", "PENDING")
                    with self.sessions.begin() as db:
                        note = db.get(LibraryNote, task_id)
                        if note and not note.deleted and note.status != status:
                            note.status = "SAVING" if status == "SUCCESS" and not note.content_hash else status
                except (ValueError, OSError, AttributeError):
                    logger.warning("跳过无法读取的状态文件：%s", path.name)

    def latest_archive_jobs(self, db, note_ids):
        # 从全部任务中按创建时间选取每篇笔记的最新任务，不受“最近任务”展示上限影响。
        remaining, latest = set(note_ids), {}
        if not remaining:
            return latest
        rows = db.execute(select(ArchiveJob.id, ArchiveJob.status, ArchiveJob.stage, ArchiveJob.snapshot)
                          .order_by(ArchiveJob.created_at.desc(), ArchiveJob.id.desc())
                          .execution_options(yield_per=100))
        try:
            for row in rows:
                for note in row.snapshot.get("notes", []):
                    if note["id"] in remaining:
                        latest[note["id"]] = {"id": row.id, "status": row.status, "stage": row.stage}
                        remaining.remove(note["id"])
                if not remaining:
                    break
        finally:
            rows.close()
        return latest

    def serialize(self, db, note, full=False, archive_job=None):
        payload = note.payload
        audio = payload.get("audioMeta") or {}
        archive_status = "UNARCHIVED"
        if note.archived_hash:
            archive_status = "ARCHIVED" if note.archived_hash == note_fingerprint(note) else "OUTDATED"
        data = {"id": note.id, "status": note.status, "createdAt": note.created_at,
                "updatedAt": note.updated_at, "categoryId": note.category_id,
                "archiveStatus": archive_status, "archivePath": note.archived_path, "archiveJob": archive_job,
                "revision": note.content_hash, "platform": payload.get("platform", ""),
                "audioMeta": {**{key: audio.get(key, "") for key in ("cover_url", "platform", "video_id", "file_path")},
                              "title": note.title, "duration": audio.get("duration", 0), "raw_info": None},
                "formData": payload.get("formData") or {}, "markdown": [],
                "transcript": {"full_text": "", "language": "", "segments": [], "raw": None}}
        if full:
            data["audioMeta"] = {**data["audioMeta"], **audio}
            data["transcript"] = {**data["transcript"], **(payload.get("transcript") or {})}
            versions = sorted(self.versions(db, note.id), key=lambda v: v.id != note.content_hash)
            data["markdown"] = [{"ver_id": v.id, "content": v.content, "created_at": v.created_at,
                                 "style": v.style, "model_name": v.model_name} for v in versions]
        return data

    def list_notes(self, category=None, search="", sort="created", direction="desc", offset=0, limit=100):
        self.refresh_files()
        with self.sessions() as db:
            query = select(LibraryNote).where(LibraryNote.deleted.is_(False))
            if category == "uncategorized":
                query = query.where(LibraryNote.category_id.is_(None))
            elif category == "categorized":
                query = query.where(LibraryNote.category_id.is_not(None))
            elif category:
                query = query.where(LibraryNote.category_id == category)
            if search:
                query = query.where(LibraryNote.title.contains(search, autoescape=True))
            total = db.scalar(select(func.count()).select_from(query.subquery()))
            order = LibraryNote.title if sort == "name" else LibraryNote.created_at
            query = query.order_by(order.asc() if direction == "asc" else order.desc(), LibraryNote.id)
            notes = list(db.scalars(query.offset(offset).limit(limit)))
            jobs = self.latest_archive_jobs(db, [note.id for note in notes])
            return {"items": [self.serialize(db, n, archive_job=jobs.get(n.id)) for n in notes], "total": total}

    def detail(self, note_id):
        self.refresh_files()
        with self.sessions() as db:
            return self.serialize(db, self.require_note(db, note_id), full=True,
                                  archive_job=self.latest_archive_jobs(db, [note_id]).get(note_id))

    def delete_note(self, note_id):
        with self.sessions.begin() as db:
            self.require_note(db, note_id).deleted = True

    def create_category(self, name):
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("分类名称需要 1 至 100 个字符")
        try:
            with self.sessions.begin() as db:
                category = LibraryCategory(id=str(uuid.uuid4()), name=name, created_at=now(), updated_at=now())
                db.add(category)
                db.flush()
                return {"id": category.id, "name": category.name}
        except IntegrityError:
            raise ValueError("该分类名称已存在") from None

    def rename_category(self, category_id, name):
        name = name.strip()
        if not name or len(name) > 100:
            raise ValueError("分类名称需要 1 至 100 个字符")
        try:
            with self.sessions.begin() as db:
                category = db.get(LibraryCategory, category_id)
                if not category:
                    raise ValueError("分类不存在")
                category.name = name
                category.updated_at = now()
                category.archived_hash = ""
                for note in db.scalars(select(LibraryNote).where(LibraryNote.category_id == category_id)):
                    note.payload = {**note.payload, "categoryName": name}
        except IntegrityError:
            raise ValueError("该分类名称已存在") from None

    def delete_category(self, category_id):
        with self.sessions.begin() as db:
            category = db.get(LibraryCategory, category_id)
            if not category:
                raise ValueError("分类不存在")
            for note in db.scalars(select(LibraryNote).where(LibraryNote.category_id == category_id)):
                note.category_id = None
                note.payload = {**note.payload, "categoryName": ""}
            db.delete(category)

    def assign(self, note_ids, category_id):
        with self.sessions.begin() as db:
            category = db.get(LibraryCategory, category_id) if category_id else None
            if category_id and not category:
                raise ValueError("分类不存在")
            notes = [self.require_note(db, note_id) for note_id in set(note_ids)]
            for note in notes:
                note.category_id = category_id
                note.payload = {**note.payload, "categoryName": category.name if category else ""}
                note.updated_at = now()

    def category_fingerprint(self, category, notes):
        return digest([category.id, category.name, sorted(note_fingerprint(n) for n in notes)])

    def list_categories(self):
        with self.sessions() as db:
            result = []
            for category in db.scalars(select(LibraryCategory).order_by(LibraryCategory.name)):
                notes = db.scalars(select(LibraryNote).where(LibraryNote.category_id == category.id,
                                                           LibraryNote.deleted.is_(False))).all()
                state = "UNARCHIVED"
                if category.archived_path:
                    state = "ARCHIVED" if category.archived_hash == self.category_fingerprint(category, notes) else "OUTDATED"
                result.append({"id": category.id, "name": category.name, "count": len(notes),
                               "createdAt": category.created_at, "archiveStatus": state,
                               "archivePath": category.archived_path})
            return result

    def create_job(self, note_ids=None, category_id=None):
        self.refresh_files(force=True)
        with self.sessions.begin() as db:
            category = db.get(LibraryCategory, category_id) if category_id else None
            if category_id and not category:
                raise ValueError("分类不存在")
            notes = (db.scalars(select(LibraryNote).where(LibraryNote.category_id == category_id,
                                                        LibraryNote.deleted.is_(False))).all() if category else
                     [self.require_note(db, value) for value in sorted(set(note_ids or []))])
            if not notes:
                raise ValueError("请先选择笔记，空分类不能入库")
            if any(n.status != "SUCCESS" or not n.content_hash for n in notes):
                raise ValueError("所选范围包含尚未生成成功的笔记，请完成生成后再入库")
            snapshot_notes = []
            for note in notes:
                value = self.serialize(db, note, full=True)
                value["markdown"] = value["markdown"][:1]
                value["transcript"] = {}
                value["fingerprint"] = note_fingerprint(note)
                assigned = db.get(LibraryCategory, note.category_id) if note.category_id else None
                value["categoryName"] = assigned.name if assigned else ""
                value["categoryPath"] = assigned.archived_path if assigned else ""
                snapshot_notes.append(value)
            snapshot = {"notes": snapshot_notes, "category": None}
            if category:
                snapshot["category"] = {"id": category.id, "name": category.name,
                                        "archivePath": category.archived_path,
                                        "fingerprint": self.category_fingerprint(category, notes)}
            fingerprint = digest([category_id, snapshot["category"]["fingerprint"] if category else None,
                                  sorted(n["fingerprint"] for n in snapshot_notes)])
            existing = db.scalar(select(ArchiveJob).where(ArchiveJob.fingerprint == fingerprint))
            if existing:
                return job_dict(existing)
            job = ArchiveJob(id=str(uuid.uuid4()), fingerprint=fingerprint, category_id=category_id,
                             scope="category" if category else "notes", snapshot=snapshot,
                             created_at=now(), updated_at=now())
            try:
                with db.begin_nested():
                    db.add(job)
                    db.flush()
            except IntegrityError:
                existing = db.scalar(select(ArchiveJob).where(ArchiveJob.fingerprint == fingerprint))
                if existing is None:
                    raise
                return job_dict(existing)
            return job_dict(job)

    def list_jobs(self):
        with self.sessions() as db:
            jobs = db.scalars(select(ArchiveJob).order_by(ArchiveJob.created_at.desc()).limit(100))
            return [{**job_dict(job), "result": {"overview": job.result.get("overview", "")}, "snapshot": {"category": job.snapshot.get("category"),
                     "notes": [{"id": n["id"], "title": n["audioMeta"]["title"]} for n in job.snapshot["notes"]]}}
                    for job in jobs]

    def retry_job(self, job_id):
        with self.sessions.begin() as db:
            job = db.get(ArchiveJob, job_id)
            if not job or job.status != "FAILED":
                raise ValueError("只有失败的任务可以重试")
            job.status = "QUEUED"
            job.error = ""
            if job.stage == "HERMES":
                job.stage = "UPLOAD"
            job.attempts += 1
            job.updated_at = now()


library = LibraryService()
