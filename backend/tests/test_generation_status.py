"""历史失败提示只展示已脱敏内容，不修改生成状态。"""
import json
from pathlib import Path

import pytest

from app.downloaders.youtube_errors import youtube_download_error
from app.utils.generation_status import add_generation_errors


def write_status(root, message, status="FAILED"):
    path = root / "task-1.status.json"
    path.write_text(json.dumps({"status": status, "message": message}), encoding="utf-8")
    return path


def failed_note(**values):
    return {"id": "task-1", "status": "FAILED", "platform": "youtube", **values}


def test_known_message_and_list_leave_file_unchanged(tmp_path):
    message = youtube_download_error(RuntimeError("not a bot"))
    path = write_status(tmp_path, message)
    before = path.read_bytes()
    result = {"items": [failed_note()], "total": 1}
    assert add_generation_errors(result, tmp_path) is result
    assert result["items"][0]["errorMessage"] == message
    assert result["items"][0]["status"] == "FAILED"
    assert path.read_bytes() == before


def test_legacy_error_never_exposes_url_or_credentials(tmp_path):
    write_status(tmp_path, "not a bot https://user:secret@example.test Cookie=secret")
    note = add_generation_errors(failed_note(), tmp_path)
    assert "验证" in note["errorMessage"]
    assert "secret" not in note["errorMessage"]
    assert "example.test" not in note["errorMessage"]


@pytest.mark.parametrize("content", ["{", "[]", "null", "x" * 17000])
def test_invalid_status_is_generic(tmp_path, content):
    (tmp_path / "task-1.status.json").write_text(content, encoding="utf-8")
    assert add_generation_errors(failed_note(), tmp_path)["errorMessage"] == "生成失败，请检查任务日志后重试。"


def test_unknown_error_and_other_platform_are_generic(tmp_path):
    write_status(tmp_path, "api_key=secret")
    assert "secret" not in add_generation_errors(failed_note(), tmp_path)["errorMessage"]
    write_status(tmp_path, "not a bot")
    assert add_generation_errors(failed_note(platform="bilibili"), tmp_path)["errorMessage"] == "生成失败，请检查任务日志后重试。"


@pytest.mark.parametrize("note_id", ["../task-1", "x/../../task-1", "x" * 129])
def test_unsafe_id_does_not_read_files(tmp_path, monkeypatch, note_id):
    def reject_read(*args, **kwargs):
        pytest.fail("不应读取非法任务路径")
    monkeypatch.setattr(Path, "open", reject_read)
    assert "生成失败" in add_generation_errors(failed_note(id=note_id), tmp_path)["errorMessage"]


def test_missing_stale_and_success_status(tmp_path):
    assert "生成失败" in add_generation_errors(failed_note(), tmp_path)["errorMessage"]
    write_status(tmp_path, "not a bot", "PENDING")
    assert "验证" not in add_generation_errors(failed_note(), tmp_path)["errorMessage"]
    assert add_generation_errors(failed_note(status="SUCCESS", errorMessage="old"), tmp_path)["errorMessage"] == ""
