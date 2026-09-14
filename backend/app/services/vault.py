"""只读查看 Vault，并将可确认的分类关系补齐到笔记库。"""

import os
import threading
import uuid
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

import yaml
from sqlalchemy import select

from app.db.models.library import ArchiveJob, LibraryCategory, LibraryNote
from app.services.library import library, note_fingerprint, now, valid_id
from app.services.webdav_archive import WebDAV

MAX_FILES = 3000
MAX_DIRECTORIES = 500
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_BYTES = 32 * 1024 * 1024


class LocalVault:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("配置的 Obsidian Vault 不是目录")

    def resolve(self, path):
        parts = PurePosixPath(path).parts
        if path.startswith("/") or "\\" in path or ".." in parts:
            raise ValueError("Vault 路径无效")
        current = self.root
        for part in parts:
            current = current / part
            if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
                raise ValueError("不读取 Vault 中的符号链接")
        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ValueError("Vault 路径越界")
        return resolved

    def list_directory(self, path=""):
        entries = []
        for item in self.resolve(path).iterdir():
            if item.is_symlink() or (hasattr(item, "is_junction") and item.is_junction()):
                continue
            if item.is_dir() or item.is_file():
                entries.append({"path": item.relative_to(self.root).as_posix(), "directory": item.is_dir()})
        return entries

    def read_limited(self, path, limit):
        with self.resolve(path).open("rb") as stream:
            content = stream.read(limit + 1)
        if len(content) > limit:
            raise ValueError("Vault 中存在过大的文件，请缩小同步范围")
        return content

    def close(self):
        pass


@contextmanager
def connect_vault():
    local = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    if local:
        source = LocalVault(local)
    else:
        values = [os.getenv(key, "") for key in ("WEBDAV_URL", "WEBDAV_USERNAME", "WEBDAV_PASSWORD")]
        if not all(values):
            raise ValueError("请配置 OBSIDIAN_VAULT_PATH，或配置指向 Vault 根目录的 WebDAV 连接")
        source = WebDAV(*values)
    try:
        yield source
    finally:
        source.close()


def scan_paths(source):
    pending = [""]
    visited = set()
    files = []
    while pending:
        directory = pending.pop()
        if directory in visited:
            continue
        visited.add(directory)
        if len(visited) > MAX_DIRECTORIES:
            raise ValueError("Vault 目录超过 500 个，请配置较小的 Vault 根目录后重试")
        for entry in source.list_directory(directory):
            path = entry["path"]
            if any(part.startswith(".") for part in PurePosixPath(path).parts):
                continue
            if entry["directory"]:
                if len(PurePosixPath(path).parts) > 30:
                    raise ValueError("Vault 目录层级超过 30 层")
                pending.append(path)
            elif path.lower().endswith(".md"):
                files.append(path)
                if len(files) > MAX_FILES:
                    raise ValueError("Vault Markdown 超过 3000 篇，请配置较小的 Vault 根目录后重试")
    return sorted(set(files), key=str.casefold)


def read_properties(content):
    text = content.decode("utf-8-sig").replace("\r\n", "\n")
    if not text.startswith("---\n"):
        return {}, text
    end = text.find("\n---\n", 4)
    if end < 0 or end > 65536:
        raise ValueError("Markdown 属性区缺少结束标记或超过 64 KB")
    properties = yaml.safe_load(text[4:end])
    if not isinstance(properties, dict):
        raise ValueError("Markdown 属性区必须是键值对象")
    return properties, text[end + 5:]


class VaultService:
    def __init__(self, service=library):
        self.library = service
        self.sync_lock = threading.Lock()

    def tree(self):
        with connect_vault() as source:
            paths = scan_paths(source)
        folders = {str(parent) for path in paths for parent in PurePosixPath(path).parents if str(parent) != "."}
        return {"name": "Obsidian Vault", "paths": paths, "fileCount": len(paths),
                "folderCount": len(folders), "scannedAt": now()}

    def sync(self):
        if not self.sync_lock.acquire(blocking=False):
            raise ValueError("已有 Vault 同步正在进行，请稍后刷新")
        try:
            return self.reconcile()
        finally:
            self.sync_lock.release()

    def reconcile(self):
        self.library.refresh_files(force=True)
        report = {"matched": 0, "categorized": 0, "archived": 0, "unverified": 0, "unchanged": 0,
                  "skipped": 0, "conflicts": [], "scannedAt": now()}
        records = defaultdict(list)
        category_names = defaultdict(set)
        with self.library.sessions() as db:
            known_ids = set(db.scalars(select(LibraryNote.id).where(LibraryNote.deleted.is_(False))))
        with connect_vault() as source:
            paths = scan_paths(source)
            total_bytes = 0
            for path in paths:
                raw = source.read_limited(path, MAX_FILE_BYTES)
                total_bytes += len(raw)
                if total_bytes > MAX_TOTAL_BYTES:
                    raise ValueError("本次同步读取超过 32 MB，未修改分类；请缩小 Vault 范围")
                try:
                    properties, body = read_properties(raw)
                except (ValueError, UnicodeError, yaml.YAMLError):
                    report["conflicts"].append({"path": path, "reason": "属性区无法解析，已跳过"})
                    continue
                identity = properties.get("id", "")
                if not isinstance(identity, str):
                    report["skipped"] += 1
                    continue
                if identity.startswith("bilinote-category-"):
                    heading = next((line[2:].strip() for line in body.splitlines() if line.startswith("# ")), "")
                    if heading:
                        category_names[identity.removeprefix("bilinote-category-")].add(heading)
                    continue
                note_id = identity.removeprefix("bilinote-")
                if not identity.startswith("bilinote-") or note_id not in known_ids:
                    report["skipped"] += 1
                    continue
                records[note_id].append((path, properties))
        # 所有远端读取成功后再开启事务，断线或权限错误不会留下半次同步。
        with self.library.sessions.begin() as db:
            active = db.scalar(select(ArchiveJob.id).where(ArchiveJob.status.in_(["QUEUED", "RUNNING"])).limit(1))
            if active:
                raise ValueError("存在进行中的入库任务，请等待完成后再同步 Vault")
            categories = {category.id: category for category in db.scalars(select(LibraryCategory))}
            by_name = {category.name: category for category in categories.values()}
            revisions = defaultdict(set)
            for job in db.scalars(select(ArchiveJob).where(ArchiveJob.status == "COMPLETED")):
                for note in job.snapshot.get("notes", []):
                    revisions[(note["id"], (job.result.get("paths") or {}).get(note["id"]))].add(note.get("revision"))
            for note_id, entries in records.items():
                path, properties = entries[0]
                if len(entries) != 1:
                    report["conflicts"].append({"path": path, "reason": "同一笔记 ID 对应多个文件，请先处理重复副本"})
                    continue
                note = db.get(LibraryNote, note_id)
                if not note or note.deleted:
                    continue
                report["matched"] += 1
                category_id = properties.get("category_id")
                name = properties.get("category_name") or ""
                parent = str(PurePosixPath(path).parent)
                # 移入自定义目录视为目录归类；默认输出目录本身不是分类。
                if parent not in (".", "BiliNote", "BiliNote/笔记"):
                    name, category_id = parent, None
                if category_id is not None and category_id != "":
                    try:
                        valid_id(category_id)
                        if len(category_id) > 36:
                            raise ValueError("分类 ID 过长")
                    except ValueError:
                        report["conflicts"].append({"path": path, "reason": "分类 ID 无效"})
                        continue
                    candidates = category_names.get(category_id, set())
                    if len(candidates) > 1:
                        report["conflicts"].append({"path": path, "reason": "Vault 中同一分类 ID 存在多个名称"})
                        continue
                    name = next(iter(candidates), name)
                category = categories.get(category_id)
                if category and name and category.name != name:
                    report["conflicts"].append({"path": path, "reason": "Vault 与 BiliNote 的分类名称不一致，未自动改名"})
                    continue
                name = name or (category.name if category else "")
                if not isinstance(name, str) or len(name.strip()) > 100 or (category_id and not name.strip()):
                    report["conflicts"].append({"path": path, "reason": "无法确认分类名称，请保留分类入口或 category_name 属性"})
                    continue
                name = name.strip()
                category = category or by_name.get(name)
                if note.category_id and (not category or note.category_id != category.id):
                    report["conflicts"].append({"path": path, "reason": "已有分类与 Vault 不一致，请手动确认；未覆盖已有分类"})
                    continue
                changed = False
                if not note.category_id and name:
                    if category is None:
                        category = LibraryCategory(id=category_id or str(uuid.uuid4()), name=name, created_at=now(), updated_at=now())
                        db.add(category)
                        db.flush()
                        categories[category.id] = category
                        by_name[category.name] = category
                    note.category_id = category.id
                    note.payload = {**note.payload, "categoryName": category.name}
                    report["categorized"] += 1
                    changed = True
                revision = properties.get("bilinote_revision")
                known_revision = note.content_hash in revisions.get((note_id, path), set())
                # 仅在正文版本有凭据时确认已入库；旧文件无法确认则标记待更新。
                verified = bool((revision == note.content_hash if revision else known_revision)
                                and note.content_hash and properties.get("title") == note.title)
                archive_hash = note_fingerprint(note) if verified else "vault-unverified"
                if note.archived_path != path or note.archived_hash != archive_hash:
                    note.archived_path, note.archived_hash = path, archive_hash
                    changed = True
                    report["archived"] += 1
                if not verified:
                    report["unverified"] += 1
                if changed:
                    note.updated_at = now()
                else:
                    report["unchanged"] += 1
        return report


vault = VaultService()
