import os
from typing import Literal

import httpx

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.services.library import library
from app.services.vault import vault
from app.utils.response import ResponseWrapper as R

router = APIRouter(prefix="/library", tags=["笔记库"])


class CategoryRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class MembershipRequest(BaseModel):
    note_ids: list[str] = Field(min_length=1, max_length=500)
    category_id: str | None = None


class ImportRequest(BaseModel):
    tasks: list[dict] = Field(max_length=20)


class ArchiveRequest(BaseModel):
    note_ids: list[str] = Field(default_factory=list, max_length=500)
    category_id: str | None = None
    auto_classify: bool = False


def perform(action, *args, **kwargs):
    try:
        return R.success(action(*args, **kwargs))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.get("/notes")
def list_notes(category: str | None = None, search: str = "",
               sort: Literal["created", "name"] = "created", direction: Literal["asc", "desc"] = "desc",
               offset: int = Query(0, ge=0), limit: int = Query(100, ge=1, le=200)):
    return perform(library.list_notes, category, search, sort, direction, offset, limit)


@router.get("/notes/{note_id}")
def get_note(note_id: str):
    return perform(library.detail, note_id)


@router.delete("/notes/{note_id}")
def delete_note(note_id: str):
    return perform(library.delete_note, note_id)


@router.post("/import")
def import_notes(data: ImportRequest):
    return perform(library.import_legacy, data.tasks)


@router.get("/categories")
def list_categories():
    return perform(library.list_categories)


@router.post("/categories")
def create_category(data: CategoryRequest):
    return perform(library.create_category, data.name)


@router.patch("/categories/{category_id}")
def rename_category(category_id: str, data: CategoryRequest):
    return perform(library.rename_category, category_id, data.name)


@router.delete("/categories/{category_id}")
def delete_category(category_id: str):
    return perform(library.delete_category, category_id)


@router.post("/membership")
def assign_notes(data: MembershipRequest):
    return perform(library.assign, data.note_ids, data.category_id)


@router.get("/archive/config")
def archive_config():
    required = ("ARCHIVE_BRIDGE_URL", "ARCHIVE_BRIDGE_TOKEN", "WEBDAV_URL", "WEBDAV_USERNAME", "WEBDAV_PASSWORD")
    missing = [key for key in required if not os.getenv(key)]
    return R.success({"ready": not missing, "message": "" if not missing else "管理员尚未配置 WebDAV 与 Hermes 入库连接"})


@router.get("/archive/jobs")
def list_archive_jobs():
    return perform(library.list_jobs)


@router.post("/archive/jobs")
def create_archive_job(data: ArchiveRequest):
    if not all(os.getenv(key) for key in ("ARCHIVE_BRIDGE_URL", "ARCHIVE_BRIDGE_TOKEN", "WEBDAV_URL", "WEBDAV_USERNAME", "WEBDAV_PASSWORD")):
        raise HTTPException(status_code=503, detail="请先配置 WebDAV 与 Hermes 入库连接")
    if data.category_id and data.note_ids:
        raise HTTPException(status_code=400, detail="整类入库与多选入库不能同时指定")
    return perform(library.create_job, data.note_ids, data.category_id, data.auto_classify)


@router.post("/archive/jobs/{job_id}/retry")
def retry_archive_job(job_id: str):
    return perform(library.retry_job, job_id)


def perform_vault(action):
    try:
        return perform(action)
    except (OSError, httpx.HTTPError) as exc:
        raise HTTPException(status_code=503, detail="无法读取服务器 Vault，请检查目录挂载、WebDAV 连接和读取权限") from exc


@router.get("/vault/tree")
def vault_tree():
    return perform_vault(vault.tree)


@router.post("/vault/sync")
def sync_vault():
    return perform_vault(vault.sync)
