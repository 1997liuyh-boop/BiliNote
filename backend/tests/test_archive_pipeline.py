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
            if payload.get("event") != "failed":
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
    assert sum(path.endswith("/notify") for _, path in calls) == 2


def test_incomplete_hermes_output_does_not_publish(pipeline):
    library, worker, dav, job, calls, organized = pipeline
    organized["notes"] = []
    worker.tick()
    worker.tick()
    assert library.list_jobs()[0]["status"] == "FAILED"
    assert "BiliNote/总览.md" not in dav.files
    assert sum(path.endswith("/notify") for _, path in calls) == 1
    assert worker.library.list_jobs()[0]["failureNotification"]["status"] == "SENT"


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
    assert sum(path.endswith("/notify") for _, path in calls) == 1
    assert worker.library.list_jobs()[0]["failureNotification"]["status"] == "SENT"


def test_unknown_notification_keeps_published_notes_and_does_not_repeat_publish(pipeline, monkeypatch):
    library, worker, dav, job, calls, _ = pipeline
    original = worker.bridge
    notify_calls = []
    def bridge(method, path, payload=None):
        if path.endswith("/notify"):
            notify_calls.append(path)
            return {"status": "UNKNOWN", "error": "请先确认 QQ 是否收到"}
        return original(method, path, payload)
    monkeypatch.setattr(worker, "bridge", bridge)
    for _ in range(4):
        worker.tick()
    state = library.list_jobs()[0]
    assert state["stage"] == "NOTIFY" and state["status"] == "FAILED"
    assert state["notification"] == "UNKNOWN"
    assert library.detail("note-a")["archiveStatus"] == "ARCHIVED"
    files = dict(dav.files)
    before = list(calls)
    library.retry_job(job["id"])
    worker.tick()
    assert dav.files == files and calls == before
    assert len(notify_calls) == 2
    assert library.list_jobs()[0]["notification"] == "UNKNOWN"


@pytest.mark.parametrize("stage", ["UPLOAD", "HERMES", "PUBLISH"])
def test_archive_failure_sends_separate_event_and_keeps_failed_status(pipeline, monkeypatch, stage):
    library, worker, dav, job, calls, _ = pipeline
    original = worker.bridge
    sent = []
    def bridge(method, path, payload=None):
        if path.endswith("/notify"):
            sent.append(payload)
            return {"status": "SENT"}
        if stage == "HERMES" and method == "GET":
            return {"status": "FAILED", "error": "模型整理失败"}
        return original(method, path, payload)
    monkeypatch.setattr(worker, "bridge", bridge)
    if stage == "UPLOAD":
        monkeypatch.setattr(dav, "put", lambda *args, **kwargs: (_ for _ in ()).throw(ConnectionError("private-secret")))
    elif stage == "PUBLISH":
        monkeypatch.setattr(worker, "publish", lambda *args: (_ for _ in ()).throw(ValueError("文件冲突")))
    for _ in range(5):
        worker.tick()
    state = library.list_jobs()[0]
    assert state["status"] == "FAILED" and state["stage"] == stage
    assert state["failureNotification"]["status"] == "SENT"
    assert len(sent) == 1 and sent[0]["event"] == "failed" and sent[0]["attempt"] == 0
    assert "入库失败" in sent[0]["message"] and job["id"] in sent[0]["message"]
    assert "private-secret" not in sent[0]["message"]
    assert library.detail("note-a")["archiveStatus"] == "UNARCHIVED"


@pytest.mark.parametrize("status", ["FAILED", "UNKNOWN", "SENDING"])
def test_failed_notification_does_not_hide_archive_error_or_loop(pipeline, monkeypatch, status):
    library, worker, _, _, _, organized = pipeline
    original = worker.bridge
    sent = []
    def bridge(method, path, payload=None):
        if path.endswith("/notify"):
            sent.append(payload)
            return {"status": status}
        return original(method, path, payload)
    monkeypatch.setattr(worker, "bridge", bridge)
    organized["notes"] = []
    for _ in range(5):
        worker.tick()
    state = library.list_jobs()[0]
    assert state["status"] == "FAILED" and state["error"]
    assert state["failureNotification"]["status"] == status
    assert len(sent) == 1


def test_pending_failure_notification_resumes_after_restart(pipeline, monkeypatch):
    library, worker, _, job, _, _ = pipeline
    worker.update(job["id"], status="FAILED", error="整理失败", result={"failureNotification": {
        "status": "PENDING", "attempt": 0, "stage": "HERMES", "reason": "整理失败"}})
    restarted = ArchiveWorker(library)
    calls = []
    monkeypatch.setattr(restarted, "bridge", lambda method, path, payload: calls.append(payload) or {"status": "SENT"})
    restarted.tick()
    restarted.tick()
    assert len(calls) == 1 and calls[0]["event"] == "failed"
    assert library.list_jobs()[0]["status"] == "FAILED"


def test_failed_then_successful_archive_can_send_both_events(pipeline, monkeypatch):
    library, worker, _, job, _, organized = pipeline
    original = worker.bridge
    notes = organized["notes"]
    organized["notes"] = []
    sent = []
    def bridge(method, path, payload=None):
        if path.endswith("/notify"):
            sent.append(payload)
        return original(method, path, payload)
    monkeypatch.setattr(worker, "bridge", bridge)
    worker.tick()
    worker.tick()
    library.retry_job(job["id"])
    organized["notes"] = notes
    for _ in range(4):
        worker.tick()
    assert library.list_jobs()[0]["status"] == "COMPLETED"
    assert [payload.get("event", "completed") for payload in sent] == ["failed", "completed"]


def test_notify_stage_failure_does_not_emit_archive_failed_event(pipeline, monkeypatch):
    library, worker, _, _, _, _ = pipeline
    original = worker.bridge
    sent = []
    def bridge(method, path, payload=None):
        if path.endswith("/notify"):
            sent.append(payload)
            return {"status": "FAILED"}
        return original(method, path, payload)
    monkeypatch.setattr(worker, "bridge", bridge)
    for _ in range(5):
        worker.tick()
    assert len(sent) == 1 and sent[0].get("event", "completed") == "completed"
    assert library.list_jobs()[0]["failureNotification"] is None
    assert library.detail("note-a")["archiveStatus"] == "ARCHIVED"


def test_failure_bridge_connection_error_keeps_archive_failure(pipeline, monkeypatch):
    library, worker, _, _, _, organized = pipeline
    original = worker.bridge
    calls = []
    def bridge(method, path, payload=None):
        if path.endswith("/notify"):
            calls.append(payload)
            raise ConnectionError("敏感的第三方响应")
        return original(method, path, payload)
    monkeypatch.setattr(worker, "bridge", bridge)
    organized["notes"] = []
    for _ in range(4):
        worker.tick()
    job = library.list_jobs()[0]
    assert job["status"] == "FAILED" and job["failureNotification"]["status"] == "UNKNOWN"
    assert "敏感的第三方响应" not in json.dumps(job, ensure_ascii=False)
    assert len(calls) == 1


def test_archive_retry_failure_uses_new_notification_attempt(pipeline, monkeypatch):
    library, worker, _, job, _, organized = pipeline
    original = worker.bridge
    attempts = []
    def bridge(method, path, payload=None):
        if path.endswith("/notify"):
            attempts.append(payload["attempt"])
        return original(method, path, payload)
    monkeypatch.setattr(worker, "bridge", bridge)
    organized["notes"] = []
    worker.tick()
    worker.tick()
    library.retry_job(job["id"])
    worker.tick()
    worker.tick()
    assert attempts == [0, 1]
    assert library.list_jobs()[0]["status"] == "FAILED"


def test_modified_legacy_overview_preserves_user_text_across_updates(pipeline):
    library, worker, dav, _, calls, organized = pipeline
    original = "---\ntags: [私人目录]\n---\n\n# 手工总览\n\n保留这一段。\n".encode()
    dav.files["BiliNote/总览.md"] = original
    for _ in range(4):
        worker.tick()
    assert library.list_jobs()[0]["status"] == "COMPLETED"
    assert dav.files["BiliNote/总览.md"].startswith(original)
    dav.files["BiliNote/总览.md"] += "\n末尾手工备注\n".encode()
    library.import_legacy([{"id": "note-b", "status": "SUCCESS", "markdown": "B正文", "audioMeta": {"title": "B"}}])
    library.create_job(note_ids=["note-b"])
    organized["notes"] = [{"id": "note-b", "body": "B整理", "tags": [], "related_ids": []}]
    for _ in range(4):
        worker.tick()
    text = dav.files["BiliNote/总览.md"].decode()
    assert text.startswith(original.decode()) and text.endswith("末尾手工备注\n")
    assert text.count("<!-- bilinote-overview:start -->") == 1 and "note-b" in text
    assert library.list_jobs()[0]["status"] == "COMPLETED"
    assert sum(path.endswith("/notify") for _, path in calls) == 2


@pytest.mark.parametrize("change", ["body", "marker"])
def test_edited_managed_overview_blocks_all_new_documents(pipeline, change):
    library, worker, dav, _, _, organized = pipeline
    for _ in range(4):
        worker.tick()
    path = "BiliNote/总览.md"
    dav.files[path] = dav.files[path].replace(
        "## 最近入库".encode() if change == "body" else b"<!-- bilinote-overview:end -->", "用户修改".encode())
    library.import_legacy([{"id": "note-b", "status": "SUCCESS", "markdown": "B正文", "audioMeta": {"title": "B"}}])
    library.create_job(note_ids=["note-b"])
    organized["notes"] = [{"id": "note-b", "body": "B整理", "tags": [], "related_ids": []}]
    before = {key: value for key, value in dav.files.items() if key.endswith(".md")}
    for _ in range(3):
        worker.tick()
    assert library.list_jobs()[0]["status"] == "FAILED"
    assert {key: value for key, value in dav.files.items() if key.endswith(".md")} == before
    assert library.detail("note-b")["archiveStatus"] == "UNARCHIVED"


def test_partial_publish_keeps_receipt_and_retry_notifies_once(pipeline, monkeypatch):
    library, worker, dav, job, calls, _ = pipeline
    original_put, original_bridge = dav.put, worker.bridge
    notifications = []
    def put(path, *args, **kwargs):
        if path == "BiliNote/总览.md":
            raise ConnectionError("模拟总览写入中断")
        return original_put(path, *args, **kwargs)
    def bridge(method, path, payload=None):
        if path.endswith("/notify"):
            notifications.append(payload)
        return original_bridge(method, path, payload)
    monkeypatch.setattr(dav, "put", put)
    monkeypatch.setattr(worker, "bridge", bridge)
    for _ in range(3):
        worker.tick()
    state = library.list_jobs()[0]
    assert state["status"] == "FAILED" and state["publishedCount"] == 1
    assert library.detail("note-a")["archiveStatus"] == "ARCHIVED"
    assert library.detail("note-a")["archiveJob"]["published"] is True
    assert "已校验入库：1/1" in notifications[0]["message"]
    path = library.detail("note-a")["archivePath"]
    content = dav.files[path]
    monkeypatch.setattr(dav, "put", original_put)
    library.retry_job(job["id"])
    worker.tick()
    worker.tick()
    assert library.list_jobs()[0]["status"] == "COMPLETED" and dav.files[path] == content
    assert len(notifications) == 2 and notifications[1].get("event") != "failed"
    assert sum(path == "/jobs" for _, path in calls) == 1


def test_legacy_written_note_without_database_receipt_recovers(pipeline):
    from app.services.archive_worker import INDEX_PATH
    library, worker, dav, job, calls, _ = pipeline
    worker.tick()
    worker.tick()
    with library.sessions.begin() as db:
        row = db.get(ArchiveJob, job["id"])
        path = row.result["paths"]["note-a"]
        content = row.result["files"][path].encode()
        row.status = "FAILED"
        row.error = "总览已被手动修改"
    dav.files[path] = content
    dav.files[INDEX_PATH] = json.dumps({"files": {path: sha(content), "BiliNote/总览.md": "old"}, "notes": {}, "categories": {}}).encode()
    original = "# 用户整理过的总览\n\n不能覆盖。\n".encode()
    dav.files["BiliNote/总览.md"] = original
    assert library.detail("note-a")["archiveStatus"] == "UNARCHIVED"
    library.retry_job(job["id"])
    worker.tick()
    worker.tick()
    assert library.detail("note-a")["archiveStatus"] == "ARCHIVED"
    assert library.list_jobs()[0]["status"] == "COMPLETED" and dav.files[path] == content
    assert dav.files["BiliNote/总览.md"].startswith(original)
    assert sum(path.endswith("/notify") for _, path in calls) == 1


def test_later_document_conflict_is_found_before_first_write(pipeline):
    library, worker, dav, job, _, _ = pipeline
    worker.tick()
    worker.tick()
    with library.sessions() as db:
        result = db.get(ArchiveJob, job["id"]).result
    paths = list(result["files"])
    dav.files[paths[-1]] = "用户自建同名分类文件".encode()
    worker.tick()
    assert library.list_jobs()[0]["status"] == "FAILED" and paths[0] not in dav.files


def test_auto_classification_reuses_category_and_preserves_assigned(pipeline):
    library, _, _, job, _, _ = pipeline
    for key in ["note-b", "note-c"]:
        library.import_legacy([{"id": key, "status": "SUCCESS", "markdown": "电商正文", "audioMeta": {"title": key}}])
    auto_job = library.create_job(note_ids=["note-a", "note-b", "note-c"], auto_classify=True)
    values = [{"id": key, "body": "整理正文", "tags": [], "related_ids": [], "category_name": "电商运营"}
              for key in ["note-a", "note-b", "note-c"]]
    library.prepare_archive(auto_job["id"], {"notes": values})
    assert library.detail("note-a")["categoryId"] == job["category_id"]
    assert library.detail("note-b")["categoryId"] == library.detail("note-c")["categoryId"]
    assert len([c for c in library.list_categories() if c["name"] == "电商运营"]) == 1
    library.import_legacy([{"id": "note-d", "status": "SUCCESS", "markdown": "电商正文", "audioMeta": {"title": "D"}}])
    next_job = library.create_job(note_ids=["note-d"], auto_classify=True)
    library.prepare_archive(next_job["id"], {"notes": [{**values[0], "id": "note-d"}]})
    assert library.detail("note-d")["categoryId"] == library.detail("note-b")["categoryId"]


def test_auto_classification_has_separate_fingerprint(pipeline):
    library, _, _, job, _, _ = pipeline
    ordinary = library.create_job(note_ids=["note-a"])
    auto = library.create_job(note_ids=["note-a"], auto_classify=True)
    assert ordinary["id"] != auto["id"]
    assert library.create_job(note_ids=["note-a"], auto_classify=True)["id"] == auto["id"]
    with pytest.raises(ValueError, match="整类入库"):
        library.create_job(category_id=job["category_id"], auto_classify=True)


@pytest.mark.parametrize("failure", ["missing", "invalid", "render", "concurrent"])
def test_auto_classification_rolls_back_and_never_overwrites_user_change(pipeline, failure):
    library, _, _, existing, _, _ = pipeline
    library.import_legacy([{"id": "note-b", "status": "SUCCESS", "markdown": "电商正文", "audioMeta": {"title": "B"}}])
    job = library.create_job(note_ids=["note-b"], auto_classify=True)
    note = {"id": "note-b", "body": "正文", "tags": [], "related_ids": [], "category_name": "新主题"}
    if failure == "missing":
        del note["category_name"]
    elif failure == "invalid":
        note["category_name"] = "分类\n换行"
    elif failure == "render":
        note["body"] = "[[不存在的链接]]"
    else:
        library.assign(["note-b"], existing["category_id"])
    with pytest.raises(ValueError):
        library.prepare_archive(job["id"], {"notes": [note]})
    expected = existing["category_id"] if failure == "concurrent" else None
    assert library.detail("note-b")["categoryId"] == expected
    assert not any(c["name"] == "新主题" for c in library.list_categories())
    with library.sessions() as db:
        assert db.get(ArchiveJob, job["id"]).stage == "UPLOAD"


def test_partial_index_excludes_unwritten_notes(pipeline, monkeypatch):
    from app.services.archive_worker import INDEX_PATH
    library, worker, dav, first, _, _ = pipeline
    with library.sessions.begin() as db:
        db.get(ArchiveJob, first["id"]).status = "COMPLETED"
    library.import_legacy([{"id": "note-b", "status": "SUCCESS", "markdown": "B正文", "audioMeta": {"title": "B"}}])
    job = library.create_job(note_ids=["note-a", "note-b"])
    library.prepare_archive(job["id"], {"notes": [{"id": key, "body": "正文", "tags": [], "related_ids": []}
                                                   for key in ["note-a", "note-b"]]})
    with library.sessions() as db:
        failed_path = db.get(ArchiveJob, job["id"]).result["paths"]["note-b"]
    original = dav.put
    def put(path, *args, **kwargs):
        if path == failed_path:
            raise ConnectionError("模拟第二篇写入失败")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(dav, "put", put)
    worker.tick()
    index = json.loads(dav.files[INDEX_PATH])
    assert "note-a" in index["notes"] and "note-b" not in index["notes"]
    assert library.detail("note-a")["archiveStatus"] == "ARCHIVED"
    assert library.detail("note-b")["archiveStatus"] == "UNARCHIVED"
    assert library.list_jobs()[0]["publishedCount"] == 1


def test_archive_route_passes_auto_classification_option(pipeline, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import library as routes
    library, _, _, _, _, _ = pipeline
    monkeypatch.setattr(routes, "library", library)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.post("/library/archive/jobs", json={"note_ids": ["note-a"], "auto_classify": True})
    assert response.status_code == 200
    snapshot = response.json()["data"]["snapshot"]
    assert snapshot["autoClassify"] is True
    assert snapshot["categories"][0]["name"] == "人工智能"


def test_overview_written_before_index_failure_can_be_retried_safely(pipeline, monkeypatch):
    from app.services.archive_worker import INDEX_PATH
    library, worker, dav, job, _, _ = pipeline
    original_put = dav.put
    def put(path, *args, **kwargs):
        if path == INDEX_PATH and "BiliNote/总览.md" in dav.files:
            raise ConnectionError("模拟总览成功后索引提交中断")
        return original_put(path, *args, **kwargs)
    monkeypatch.setattr(dav, "put", put)
    for _ in range(3):
        worker.tick()
    assert library.list_jobs()[0]["status"] == "FAILED"
    overview = dav.files["BiliNote/总览.md"]
    monkeypatch.setattr(dav, "put", original_put)
    library.retry_job(job["id"])
    worker.tick()
    worker.tick()
    assert library.list_jobs()[0]["status"] == "COMPLETED"
    assert dav.files["BiliNote/总览.md"] == overview
