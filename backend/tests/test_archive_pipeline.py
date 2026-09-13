import json

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.engine import Base
from app.db.models.library import ArchiveJob
from app.services.library import LibraryService
from app.services.archive_worker import ArchiveWorker, sha
from app.services.obsidian import render_archive


class MemoryDAV:
    def __init__(self):
        self.files = {}

    def get(self, path):
        content = self.files.get(path)
        return content, sha(content) if content is not None else None

    def put(self, path, content, etag=None, new=False):
        old = self.files.get(path)
        if (new and old is not None) or (etag and (old is None or sha(old) != etag)):
            raise ValueError("文件冲突")
        self.files[path] = content

    def close(self):
        pass


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    library = LibraryService(sessionmaker(bind=engine, autoflush=False), tmp_path / "notes")
    library.import_legacy([{"id": "note-a", "status": "SUCCESS", "markdown": "原始笔记", "createdAt": "2026-09-01",
        "audioMeta": {"title": "人工智能 / 入门", "platform": "bilibili"}, "formData": {"video_url": "https://example.com/video"}}])
    category = library.create_category("人工智能")
    library.assign(["note-a"], category["id"])
    job = library.create_job(category_id=category["id"])
    dav = MemoryDAV()
    monkeypatch.setattr("app.services.archive_worker.WebDAV", lambda *args: dav)
    for key in ("ARCHIVE_BRIDGE_URL", "ARCHIVE_BRIDGE_TOKEN", "WEBDAV_URL", "WEBDAV_USERNAME", "WEBDAV_PASSWORD"):
        monkeypatch.setenv(key, "test")
    worker = ArchiveWorker(library)
    calls = []
    organized = {"notes": [{"id": "note-a", "body": "## 核心观点\n\n整理后的知识", "tags": ["AI/入门"], "related_ids": []}],
                 "category_summary": "## 分类归纳\n\n一条来源的综合说明", "reading_order": ["note-a"]}
    def bridge(method, path, payload=None):
        calls.append((method, path))
        if path.endswith("/notify"):
            assert "BiliNote/总览.md" in dav.files
            return {"status": "SENT"}
        return {"status": "COMPLETED", "result": organized} if method == "GET" else {"status": "QUEUED"}
    monkeypatch.setattr(worker, "bridge", bridge)
    return library, worker, dav, job, calls, organized


def test_full_pipeline_only_notifies_after_verified_publish(pipeline):
    library, worker, dav, job, calls, _ = pipeline
    worker.tick()
    assert list(dav.files) == [f".bilinote-inbox/{job['id']}/manifest.json"]
    assert not any(path.endswith("/notify") for _, path in calls)
    worker.tick()
    worker.tick()
    assert not any(path.endswith("/notify") for _, path in calls)
    worker.tick()
    assert library.list_jobs()[0]["status"] == "COMPLETED"
    assert library.detail("note-a")["archiveStatus"] == "ARCHIVED"
    assert library.list_categories()[0]["archiveStatus"] == "ARCHIVED"
    content = dav.files[library.detail("note-a")["archivePath"]].decode()
    assert yaml.safe_load(content.split("---", 2)[1])["tags"] == ["bilinote", "AI/入门"]
    assert "[[BiliNote/总览|笔记总览]]" in content and "原始笔记" in content


def test_user_edits_survive_rearchive(pipeline):
    library, worker, dav, job, calls, _ = pipeline
    for _ in range(4):
        worker.tick()
    path = library.detail("note-a")["archivePath"]
    dav.files[path] += "\n用户新增的内容".encode()
    library.save_result("note-a", {"markdown": "新版本"}, created_at="2030-01-01")
    library.create_job(note_ids=["note-a"])
    for _ in range(3):
        worker.tick()
    assert "用户新增的内容" in dav.files[path].decode()
    assert library.list_jobs()[0]["status"] == "FAILED"
    assert sum(path.endswith("/notify") for _, path in calls) == 1


def test_incomplete_hermes_output_does_not_publish(pipeline):
    library, worker, dav, job, calls, organized = pipeline
    organized["notes"] = []
    worker.tick()
    worker.tick()
    assert library.list_jobs()[0]["status"] == "FAILED"
    assert "BiliNote/总览.md" not in dav.files
    assert not any(path.endswith("/notify") for _, path in calls)


def test_notification_retry_does_not_repeat_hermes(pipeline, monkeypatch):
    library, worker, dav, job, calls, _ = pipeline
    original = worker.bridge
    def bridge(method, path, payload=None):
        return {"status": "FAILED"} if path.endswith("/notify") else original(method, path, payload)
    monkeypatch.setattr(worker, "bridge", bridge)
    for _ in range(4):
        worker.tick()
    assert library.list_jobs()[0]["stage"] == "NOTIFY"
    assert library.detail("note-a")["archiveStatus"] == "ARCHIVED"
    library.retry_job(job["id"])
    before = len(calls)
    monkeypatch.setattr(worker, "bridge", original)
    worker.tick()
    assert calls[before:] == [("POST", f"/jobs/{job['id']}/notify")]
    assert library.list_jobs()[0]["status"] == "COMPLETED"


def test_worker_restart_resumes_hermes_stage(pipeline, monkeypatch):
    library, worker, dav, job, calls, _ = pipeline
    worker.tick()
    restarted = ArchiveWorker(library)
    monkeypatch.setattr(restarted, "bridge", worker.bridge)
    for _ in range(3):
        restarted.tick()
    assert library.list_jobs()[0]["status"] == "COMPLETED"
    assert sum(path == "/jobs" for _, path in calls) == 1


def test_completed_category_is_still_deduplicated(pipeline):
    library, worker, dav, job, calls, _ = pipeline
    for _ in range(4):
        worker.tick()
    assert library.create_job(category_id=job["category_id"])["id"] == job["id"]


def test_individual_archive_updates_category_members(pipeline):
    library, worker, dav, job, calls, organized = pipeline
    for _ in range(4):
        worker.tick()
    library.import_legacy([{"id": "note-b", "status": "SUCCESS", "markdown": "B正文", "audioMeta": {"title": "B"}}])
    library.assign(["note-b"], job["category_id"])
    library.create_job(note_ids=["note-b"])
    organized["notes"] = [{"id": "note-b", "body": "B整理", "tags": [], "related_ids": []}]
    for _ in range(4):
        worker.tick()
    path = library.list_categories()[0]["archivePath"]
    assert "note-b" in dav.files[path].decode()
    assert "一条来源的综合说明" in dav.files[path].decode()


def test_hyphens_in_title_are_valid_yaml(pipeline):
    _, _, _, job, _, organized = pipeline
    job["snapshot"]["notes"][0]["audioMeta"]["title"] = "Title---suffix"
    files, _ = render_archive(job["snapshot"], organized)
    assert any("Title---suffix" in path for path in files)


def test_unknown_backlink_blocks_publication(pipeline):
    _, worker, dav, _, calls, organized = pipeline
    organized["notes"][0]["body"] = "[[missing-page]]"
    worker.tick()
    worker.tick()
    assert "BiliNote/总览.md" not in dav.files
    assert not any(path.endswith("/notify") for _, path in calls)
