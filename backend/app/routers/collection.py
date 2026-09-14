"""多 P 选集的只读预览和显式批量提交。"""
import logging
from uuid import UUID

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from pydantic import Field, StrictInt, model_validator

from app.models.video_request import VideoRequest
from app.services.bilibili_collection import CollectionUnavailable, MAX_EPISODES, get_collection, parse_video_url
from app.services.library import library
from app.utils.response import ResponseWrapper as R

router = APIRouter(tags=["视频选集"])
logger = logging.getLogger(__name__)


class CollectionRequest(VideoRequest):
    request_id: UUID
    pages: list[StrictInt] = Field(min_length=1, max_length=MAX_EPISODES)

    @model_validator(mode="before")
    @classmethod
    def normalize_url(cls, data):
        if isinstance(data, dict) and data.get("video_url"):
            bvid, page = parse_video_url(data["video_url"])
            return {**data, "video_url": f"https://www.bilibili.com/video/{bvid}/?p={page}"}
        return data

    @model_validator(mode="after")
    def validate_collection(self):
        if self.platform != "bilibili" or self.task_id or self.prefetched_transcript is not None:
            raise ValueError("批量生成仅支持新建 B 站分集任务，不能复用单集任务或字幕")
        if any(page < 1 for page in self.pages) or len(self.pages) != len(set(self.pages)):
            raise ValueError("请选择不重复的正整数分集序号")
        if not self.model_name.strip() or not self.provider_id.strip():
            raise ValueError("请选择模型和提供者")
        return self


def load_collection(video_url):
    try:
        return get_collection(video_url)
    except CollectionUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/video/collection")
def preview_collection(video_url: str = Query(min_length=1, max_length=2048)):
    return R.success(load_collection(video_url))


def transcriber_readiness():
    from app.services.transcriber_config_manager import TranscriberConfigManager
    return TranscriberConfigManager().is_model_ready()


def execute_episode(task_id, form):
    from app.routers.note import run_note_task
    run_note_task(task_id, form["video_url"], "bilibili", form["quality"],
                  form.get("link", False), form.get("screenshot", False), form["model_name"],
                  form["provider_id"], form.get("format"), form.get("style"), form.get("extras"),
                  form.get("video_understanding", False), form.get("video_interval", 0), form.get("grid_size", []))


def mark_episode_failed(task_id):
    from app.services.note import NoteGenerator
    from app.enmus.task_status_enums import TaskStatus
    NoteGenerator()._update_status(task_id, TaskStatus.FAILED, "选集任务执行异常，请在历史中重试该集")


def run_collection(items, form):
    # 整套按顺序提交给现有执行器，一集失败不阻断后续分集。
    for item in items:
        try:
            library.detail(item["task_id"])
        except ValueError:
            continue
        try:
            execute_episode(item["task_id"], {**form, "video_url": item["url"]})
        except Exception:
            logger.exception("选集任务执行失败：%s", item["task_id"])
            try:
                mark_episode_failed(item["task_id"])
            except Exception:
                logger.exception("无法写入选集失败状态：%s", item["task_id"])


@router.post("/generate_collection")
def generate_collection(data: CollectionRequest, background_tasks: BackgroundTasks):
    readiness = transcriber_readiness()
    if not readiness["ready"]:
        return R.error(msg=readiness["reason"], code=300102,
                       data={"reason": "transcriber_model_not_ready", "downloading": readiness["downloading"]})
    collection = load_collection(data.video_url)
    selected = set(data.pages)
    episodes = [episode for episode in collection["episodes"] if episode["page"] in selected]
    if len(episodes) != len(selected):
        raise HTTPException(status_code=400, detail="所选分集已不在目录中，请重新读取选集")
    form = data.model_dump(mode="json", exclude={"request_id", "pages", "task_id", "prefetched_transcript"})
    try:
        items, created = library.save_collection_pending(str(data.request_id), form, episodes, collection["title"])
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if created:
        background_tasks.add_task(run_collection, items, form)
    return R.success({"tasks": items, "created": created, "total": len(items)})
