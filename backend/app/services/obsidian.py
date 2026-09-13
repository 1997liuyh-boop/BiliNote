import json
import re
from pathlib import PurePosixPath

import yaml

from app.services.library import now


def safe_name(value):
    value = re.sub(r'[\\/:*?"<>|#\[\]\r\n\x00-\x1f]', '-', value).strip(' .-')
    return value[:65] or "未命名"


def note_path(note):
    if note.get("archivePath", "").startswith("BiliNote/笔记/"):
        return note["archivePath"]
    return f"BiliNote/笔记/{safe_name(note['audioMeta']['title'])}--{note['id']}.md"


def category_path(category):
    if category.get("archivePath", "").startswith("BiliNote/分类/"):
        return category["archivePath"]
    return f"BiliNote/分类/{safe_name(category['name'])}--{category['id']}.md"


def wikilink(path, title):
    return f"[[{path.removesuffix('.md')}|{safe_name(title)}]]"


def clean_tags(tags):
    if not isinstance(tags, list):
        raise ValueError("Hermes 返回的标签格式错误")
    result = ["bilinote"]
    for tag in tags[:12]:
        if not isinstance(tag, str):
            raise ValueError("标签必须是文本")
        value = re.sub(r"[^\w/\-\u4e00-\u9fff]", "-", tag.lstrip("#"), flags=re.UNICODE).strip("-/")[:80]
        if value and not value.isdigit() and value not in result:
            result.append(value)
    return result


def document(properties, body):
    return "---\n" + yaml.safe_dump(properties, allow_unicode=True, sort_keys=False) + "---\n\n" + body.rstrip() + "\n"


def validate_links(body, allowed):
    for target in re.findall(r"(?<!!)\[\[([^\]]+)\]\]", body):
        path = target.split("|", 1)[0].split("#", 1)[0].removesuffix(".md")
        if path and path not in allowed:
            raise ValueError("Hermes 正文引用了不存在的笔记，请重试整理")


def render_archive(snapshot, organized):
    notes = {note["id"]: note for note in snapshot["notes"]}
    values = organized.get("notes")
    if not isinstance(values, list) or len(values) != len(notes) or {v.get("id") for v in values} != set(notes):
        raise ValueError("整理结果没有完整覆盖本次入库范围")
    files = {}
    paths = {}
    stamp = now()
    allowed = {note_path(note).removesuffix(".md") for note in notes.values()}
    allowed.add("BiliNote/总览")
    for value in values:
        note = notes[value["id"]]
        body = value.get("body")
        if not isinstance(body, str) or not body.strip():
            raise ValueError("整理结果正文为空")
        validate_links(body, allowed)
        path = note_path(note)
        paths[note["id"]] = path
        links = [f"返回：{wikilink('BiliNote/总览.md', '笔记总览')}"]
        if note.get("categoryId"):
            category = {"id": note["categoryId"], "name": note.get("categoryName") or "分类", "archivePath": note.get("categoryPath", "")}
            links.insert(0, f"所属分类：{wikilink(category_path(category), category['name'])}")
        related = value.get("related_ids") or []
        if not isinstance(related, list) or any(item not in notes for item in related):
            raise ValueError("整理结果引用了本次范围以外的笔记")
        related_links = ["- " + wikilink(note_path(notes[key]), notes[key]["audioMeta"]["title"])
                         for key in dict.fromkeys(related) if key != note["id"]]
        source = note["formData"].get("video_url", "")
        properties = {"id": f"bilinote-{note['id']}", "title": note["audioMeta"]["title"],
                      "aliases": [note["audioMeta"]["title"]], "tags": clean_tags(value.get("tags", [])),
                      "category_id": note.get("categoryId"), "source": source,
                      "created": note["createdAt"], "updated": stamp, "bilinote_managed": True}
        source_body = note["markdown"][0]["content"]
        content = f"# {note['audioMeta']['title']}\n\n" + "\n\n".join(links) + f"\n\n{body}\n"
        if related_links:
            content += "\n## 关联笔记\n\n" + "\n".join(related_links) + "\n"
        # 保留原始生成笔记，便于追溯、核对和保留截图。
        content += "\n## 原始笔记\n\n" + source_body
        files[path] = document(properties, content)
    category = snapshot.get("category")
    if category:
        summary = organized.get("category_summary")
        if not isinstance(summary, str) or not summary.strip():
            raise ValueError("整类入库缺少分类综合归纳")
        validate_links(summary, allowed)
        order = organized.get("reading_order") or list(notes)
        if not isinstance(order, list) or len(order) != len(notes) or set(order) != set(notes):
            raise ValueError("分类阅读顺序必须包含每条来源笔记且不能重复")
        content = f"# {category['name']}\n\n" + wikilink("BiliNote/总览.md", "返回总览") + "\n\n" + summary
        content += "\n\n## 来源与阅读顺序\n\n" + "\n".join(
            f"{index + 1}. {wikilink(paths[key], notes[key]['audioMeta']['title'])}" for index, key in enumerate(order))
        files[category_path(category)] = document({"id": f"bilinote-category-{category['id']}",
            "tags": ["bilinote", "分类"], "updated": stamp, "bilinote_managed": True}, content)
    for path, content in files.items():
        if PurePosixPath(path).is_absolute() or ".." in PurePosixPath(path).parts:
            raise ValueError("输出文件路径无效")
        properties = yaml.safe_load(content.partition("\n---\n")[0].removeprefix("---\n"))
        if not isinstance(properties, dict) or not isinstance(properties.get("tags"), list):
            raise ValueError("Obsidian 属性校验失败")
    return files, paths
