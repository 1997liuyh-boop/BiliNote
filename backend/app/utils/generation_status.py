"""为历史列表添加脱敏的失败说明，不修改笔记或状态文件。"""
import json
import re

from app.downloaders.youtube_errors import youtube_download_error


def add_generation_errors(result, output_dir):
    notes = result.get("items", [result])
    for note in notes:
        note["errorMessage"] = ""
        if note.get("status") != "FAILED":
            continue
        note["errorMessage"] = "生成失败，请检查任务日志后重试。"
        note_id = str(note.get("id", ""))
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", note_id):
            continue
        try:
            with (output_dir / f"{note_id}.status.json").open(encoding="utf-8") as stream:
                status = json.loads(stream.read(16384))
            if not isinstance(status, dict) or status.get("status") != "FAILED":
                continue
            message = str(status.get("message", ""))
        except (OSError, ValueError):
            continue
        if note.get("platform") != "youtube":
            continue
        # 仅返回本项目定义的完整提示或已识别的下载错误，不输出原始异常。
        known = [youtube_download_error(RuntimeError(value)) for value in
                 ("not a bot", "429", "private video", "requested format", "timeout", "unknown")]
        if message in known:
            note["errorMessage"] = message
        elif any(value in message.lower() for value in
                 ("not a bot", "confirm you’re", "confirm you're", "requested format", "nsig", "po_token")):
            note["errorMessage"] = youtube_download_error(RuntimeError(message))
    return result
