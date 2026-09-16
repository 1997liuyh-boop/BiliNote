import json
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.engine import Base
from app.services.library import LibraryService


@pytest.fixture
def library(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    return LibraryService(sessionmaker(bind=engine, autoflush=False), tmp_path / "notes")


def legacy(task_id="note-a", content="第一版", created="2026-09-01T00:00:00Z"):
    return {"id": task_id, "status": "SUCCESS", "createdAt": created,
            "audioMeta": {"title": "中文笔记", "platform": "bilibili"},
            "formData": {"video_url": "https://www.bilibili.com/video/BV123"},
            "markdown": content}


def test_other_client_reads_server_history_and_versions(library):
    library.import_legacy([legacy()])
    other = LibraryService(library.sessions, library.output_dir)
    assert other.list_notes()["items"][0]["id"] == "note-a"
    assert other.detail("note-a")["markdown"][0]["content"] == "第一版"


def test_import_is_idempotent_and_preserves_versions(library):
    library.import_legacy([legacy(), legacy(content="第二版", created="2026-09-02T00:00:00Z")])
    library.import_legacy([legacy()])
    assert len(library.detail("note-a")["markdown"]) == 2
    library.delete_note("note-a")
    library.import_legacy([legacy()])
    assert library.list_notes()["total"] == 0


def test_category_changes_only_membership(library):
    library.import_legacy([legacy()])
    category = library.create_category("知识管理")
    library.assign(["note-a"], category["id"])
    assert library.list_notes(category=category["id"])["total"] == 1
    assert library.list_notes(category="uncategorized")["total"] == 0
    assert library.list_jobs() == []
    library.delete_category(category["id"])
    assert library.list_notes(category="uncategorized")["total"] == 1


def test_restore_files_ignores_caches_and_broken_json(library):
    library.output_dir.mkdir()
    (library.output_dir / "note-a.json").write_text(json.dumps({
        "markdown": "服务端笔记", "audio_meta": {"title": "视频A"}}), encoding="utf-8")
    (library.output_dir / "note-a_audio.json").write_text("{}", encoding="utf-8")
    (library.output_dir / "broken.json").write_text("{", encoding="utf-8")
    library.refresh_files(force=True)
    assert library.list_notes()["total"] == 1
    assert library.detail("note-a")["markdown"][0]["content"] == "服务端笔记"


def test_category_archive_snapshots_all_members_and_deduplicates(library):
    library.import_legacy([legacy(), legacy("note-b")])
    category = library.create_category("合集")
    library.assign(["note-a", "note-b"], category["id"])
    first = library.create_job(category_id=category["id"])
    assert len(first["snapshot"]["notes"]) == 2
    assert library.create_job(category_id=category["id"])["id"] == first["id"]


def test_archive_rejects_pending_member_and_invalid_path(library):
    library.save_pending("pending", {"video_url": "https://example.com"})
    with pytest.raises(ValueError):
        library.create_job(note_ids=["pending"])
    with pytest.raises(ValueError):
        library.detail("../secrets")


def test_duplicate_versions_in_one_import(library):
    note = legacy()
    note["markdown"] = [{"content": "重复正文"}, {"content": "重复正文"}]
    library.import_legacy([note])
    assert len(library.detail("note-a")["markdown"]) == 1


def test_browser_import_completes_existing_server_metadata(library):
    library.save_result("note-a", {"markdown": "第一版", "audio_meta": {"title": "中文笔记"}})
    note = legacy()
    note["formData"].update({"style": "学术", "model_name": "示例模型"})
    library.import_legacy([note])
    restored = library.detail("note-a")
    assert restored["formData"]["video_url"] == note["formData"]["video_url"]
    assert restored["markdown"][0]["style"] == "学术"
    assert restored["createdAt"].startswith("2026-09-01")


def test_renamed_category_creates_new_note_archive_snapshot(library):
    library.import_legacy([legacy()])
    category = library.create_category("旧名称")
    library.assign(["note-a"], category["id"])
    first = library.create_job(note_ids=["note-a"])
    library.rename_category(category["id"], "新名称")
    second = library.create_job(note_ids=["note-a"])
    assert first["id"] != second["id"]
    assert second["snapshot"]["notes"][0]["categoryName"] == "新名称"


def test_bad_version_shape_is_isolated_during_scan(library):
    library.output_dir.mkdir()
    (library.output_dir / "bad.json").write_text('{"markdown":["bad"]}', encoding="utf-8")
    (library.output_dir / "good.json").write_text('{"markdown":"good"}', encoding="utf-8")
    library.refresh_files(force=True)
    assert library.list_notes()["total"] == 1


def test_regenerated_previous_content_is_current(library):
    for content, date in [("A", "2026-09-01"), ("B", "2026-09-02"), ("A", "2026-09-03")]:
        library.save_result("note-a", {"markdown": content}, created_at=date)
    library.import_legacy([legacy(content="B", created="2026-09-02")])
    assert library.detail("note-a")["markdown"][0]["content"] == "A"
    assert len(library.detail("note-a")["markdown"]) == 2
    assert library.create_job(note_ids=["note-a"])["snapshot"]["notes"][0]["markdown"][0]["content"] == "A"


def test_migrated_title_is_searchable(library):
    library.save_result("note-a", {"markdown": "第一版"})
    library.import_legacy([legacy()])
    assert library.list_notes(search="中文笔记")["total"] == 1


def test_browser_clock_cannot_override_server_current_version(library):
    library.import_legacy([legacy(content="旧正文", created="2030-01-01")])
    library.save_result("note-a", {"markdown": "当前正文"}, created_at="2026-09-13")
    library.import_legacy([legacy(content="旧正文", created="2030-01-01")])
    assert library.detail("note-a")["markdown"][0]["content"] == "当前正文"


@pytest.mark.parametrize("status,stage", [("QUEUED", "UPLOAD"), ("RUNNING", "HERMES"),
                                         ("FAILED", "PUBLISH"), ("FAILED", "NOTIFY"), ("COMPLETED", "NOTIFY")])
def test_history_includes_latest_archive_job(library, status, stage):
    from app.db.models.library import ArchiveJob
    library.import_legacy([legacy()])
    job = library.create_job(note_ids=["note-a"])
    with library.sessions.begin() as db:
        row = db.get(ArchiveJob, job["id"])
        row.status, row.stage = status, stage
    expected = {"id": job["id"], "status": status, "stage": stage}
    assert library.list_notes()["items"][0]["archiveJob"] == expected
    assert library.detail("note-a")["archiveJob"] == expected


def test_archive_filter_not_limited_to_recent_100_jobs(library):
    from app.db.models.library import ArchiveJob
    library.import_legacy([legacy()])
    job = library.create_job(note_ids=["note-a"])
    with library.sessions.begin() as db:
        db.get(ArchiveJob, job["id"]).status = "FAILED"
        for index in range(101):
            db.add(ArchiveJob(id=f"other-{index}", fingerprint=str(index), scope="notes", status="COMPLETED",
                             snapshot={"notes": [{"id": "other", "audioMeta": {"title": "其他笔记"}}]},
                             created_at="2099-01-01", updated_at="2099-01-01"))
    assert len(library.list_jobs()) == 100
    assert library.list_notes()["items"][0]["archiveJob"]["id"] == job["id"]


def test_newer_archive_success_supersedes_old_failure(library):
    from app.db.models.library import ArchiveJob
    library.import_legacy([legacy()])
    first = library.create_job(note_ids=["note-a"])
    library.save_result("note-a", {"markdown": "新版"})
    second = library.create_job(note_ids=["note-a"])
    with library.sessions.begin() as db:
        old = db.get(ArchiveJob, first["id"])
        old.status, old.created_at = "FAILED", "2026-01-01"
        new = db.get(ArchiveJob, second["id"])
        new.status, new.stage, new.created_at = "COMPLETED", "NOTIFY", "2026-01-02"
    assert library.list_notes()["items"][0]["archiveJob"]["id"] == second["id"]
