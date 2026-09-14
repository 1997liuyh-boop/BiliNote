import json
from urllib.parse import quote

import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.engine import Base
from app.db.models.library import ArchiveJob
from app.services.library import LibraryService, now
from app.services.obsidian import document, note_path
from app.services.vault import LocalVault, VaultService
from app.services.webdav_archive import WebDAV


@pytest.fixture
def setup(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    library = LibraryService(sessionmaker(bind=engine, autoflush=False), tmp_path / 'notes')
    library.import_legacy([{"id": "note-a", "status": "SUCCESS", "markdown": "正文", "audioMeta": {"title": "标题"}}])
    root = tmp_path / 'Obsidian Vault'
    root.mkdir()
    monkeypatch.setenv('OBSIDIAN_VAULT_PATH', str(root))
    return library, VaultService(library), root


def write_note(root, path='BiliNote/笔记/标题.md', **properties):
    target = root / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(document({"id": "bilinote-note-a", "title": "标题", **properties}, '# 标题\n\n正文'), encoding='utf-8')
    return target


def test_tree_only_markdown_and_no_hidden_directories(setup):
    _, vault, root = setup
    write_note(root, '知识/中文 空格.MD')
    write_note(root, '.obsidian/hidden.md')
    write_note(root, '.bilinote-inbox/job/note.md')
    (root / '附件.png').write_bytes(b'png')
    (root / '空目录').mkdir()
    tree = vault.tree()
    assert tree['paths'] == ['知识/中文 空格.MD']
    assert tree['fileCount'] == tree['folderCount'] == 1
    assert str(root) not in json.dumps(tree)


def test_directory_sync_is_idempotent_and_does_not_write_vault(setup):
    library, vault, root = setup
    path = write_note(root, '知识/人工智能/改名.md', bilinote_revision=library.detail('note-a')['revision'])
    original = path.read_bytes()
    report = vault.sync()
    assert report['categorized'] == 1 and report['archived'] == 1
    assert library.list_categories()[0]['name'] == '知识/人工智能'
    assert library.detail('note-a')['archiveStatus'] == 'ARCHIVED'
    assert library.detail('note-a')['archivePath'] == '知识/人工智能/改名.md'
    assert note_path(library.detail('note-a')) == '知识/人工智能/改名.md'
    again = vault.sync()
    assert again['categorized'] == again['archived'] == 0
    assert again['unchanged'] == 1
    assert path.read_bytes() == original


def test_restore_legacy_category_from_category_page(setup):
    library, vault, root = setup
    write_note(root, category_id='category-a')
    target = root / 'BiliNote/分类/知识.md'
    target.parent.mkdir()
    target.write_text(document({'id': 'bilinote-category-category-a'}, '# 知识管理\n'), encoding='utf-8')
    report = vault.sync()
    assert report['categorized'] == 1 and report['unverified'] == 1
    assert library.detail('note-a')['categoryId'] == 'category-a'
    assert library.detail('note-a')['archiveStatus'] == 'OUTDATED'
    assert library.list_categories()[0]['name'] == '知识管理'


def test_restore_from_frontmatter_and_completed_job_revision(setup):
    library, vault, root = setup
    write_note(root, category_id='cat-a', category_name='编程')
    with library.sessions.begin() as db:
        db.add(ArchiveJob(id='job-a', fingerprint='unique', scope='notes', status='COMPLETED',
            snapshot={'notes': [library.detail('note-a')]}, result={'paths': {'note-a': 'BiliNote/笔记/标题.md'}},
            created_at=now(), updated_at=now()))
    vault.sync()
    assert library.detail('note-a')['archiveStatus'] == 'ARCHIVED'
    assert library.detail('note-a')['categoryId'] == 'cat-a'


def test_no_category_in_default_folder_stays_uncategorized(setup):
    library, vault, root = setup
    write_note(root, bilinote_revision=library.detail('note-a')['revision'])
    report = vault.sync()
    assert report['categorized'] == 0
    assert library.detail('note-a')['categoryId'] is None
    assert library.detail('note-a')['archiveStatus'] == 'ARCHIVED'


def test_existing_category_is_not_overwritten(setup):
    library, vault, root = setup
    category = library.create_category('原分类')
    library.assign(['note-a'], category['id'])
    write_note(root, '新分类/标题.md')
    report = vault.sync()
    assert len(report['conflicts']) == 1
    assert library.detail('note-a')['categoryId'] == category['id']
    assert len(library.list_categories()) == 1


def test_duplicate_id_and_same_title_without_id_are_not_guessed(setup):
    library, vault, root = setup
    write_note(root, 'A/标题.md')
    write_note(root, 'B/标题.md')
    (root / '标题.md').write_text('# 标题', encoding='utf-8')
    report = vault.sync()
    assert len(report['conflicts']) == 1 and report['skipped'] == 1
    assert library.detail('note-a')['categoryId'] is None
    assert library.detail('note-a')['archivePath'] == ''


@pytest.mark.parametrize('properties', [{'category_id': ['bad']}, {'category_id': {}},
    {'category_id': 'x' * 37}, {'category_id': 'missing'}, {'category_name': ['bad']}])
def test_invalid_or_unresolved_category_is_reported(setup, properties):
    library, vault, root = setup
    write_note(root, **properties)
    assert len(vault.sync()['conflicts']) == 1
    assert library.detail('note-a')['categoryId'] is None


def test_old_revision_is_not_marked_current(setup):
    library, vault, root = setup
    write_note(root, '知识/标题.md', bilinote_revision='old-revision')
    assert vault.sync()['unverified'] == 1
    assert library.detail('note-a')['archiveStatus'] == 'OUTDATED'


def test_connection_failure_does_not_partially_apply(setup, monkeypatch):
    library, vault, root = setup
    write_note(root, 'A/标题.md')
    write_note(root, 'Z/another.md')
    original = LocalVault.read_limited
    def fail(self, path, limit):
        if path.startswith('Z/'):
            raise OSError('connection failed')
        return original(self, path, limit)
    monkeypatch.setattr(LocalVault, 'read_limited', fail)
    with pytest.raises(OSError):
        vault.sync()
    assert library.detail('note-a')['categoryId'] is None
    assert library.list_categories() == []


def test_pending_archive_blocks_sync(setup):
    library, vault, root = setup
    write_note(root, '知识/标题.md')
    library.create_job(note_ids=['note-a'])
    with pytest.raises(ValueError, match='进行中'):
        vault.sync()
    assert library.detail('note-a')['categoryId'] is None


def test_deleted_note_is_not_restored(setup):
    library, vault, root = setup
    write_note(root, '知识/标题.md')
    library.delete_note('note-a')
    assert vault.sync()['matched'] == 0
    assert library.list_notes()['total'] == 0


def test_local_source_rejects_parent_escape(setup):
    _, _, root = setup
    with pytest.raises(ValueError):
        LocalVault(root).read_limited('../test.db', 1024)


def test_scan_limit_is_not_silently_truncated(setup, monkeypatch):
    _, vault, root = setup
    write_note(root)
    monkeypatch.setattr('app.services.vault.MAX_FILES', 0)
    with pytest.raises(ValueError, match='3000'):
        vault.tree()


def dav_response(href, collection=False, status='200 OK'):
    kind = '<d:collection/>' if collection else ''
    return f'<d:response><d:href>{href}</d:href><d:propstat><d:prop><d:resourcetype>{kind}</d:resourcetype></d:prop><d:status>HTTP/1.1 {status}</d:status></d:propstat></d:response>'


def test_webdav_reads_depth_one_and_rejects_external_and_traversal():
    name = quote('中文 空格.md')
    body = '<d:multistatus xmlns:d="DAV:">' + ''.join([
        dav_response('/vault/', True), dav_response('/vault/' + name),
        dav_response('/vault/folder/', True), dav_response('/vault/folder/nested.md'),
        dav_response('https://evil.example/vault/secret.md'), dav_response('/other/wrong.md'),
        dav_response('/vault/%2e%2e/escape.md'), dav_response('/vault/bad%5cname.md'),
    ]) + '</d:multistatus>'
    def handler(request):
        assert request.method == 'PROPFIND' and request.headers['Depth'] == '1'
        return httpx.Response(207, content=body.encode())
    dav = WebDAV('https://dav.example/vault', 'user', 'secret')
    dav.client.close()
    dav.client = httpx.Client(transport=httpx.MockTransport(handler))
    try:
        assert dav.list_directory() == [
            {'path': '中文 空格.md', 'directory': False}, {'path': 'folder', 'directory': True}]
    finally:
        dav.close()


@pytest.mark.parametrize('body', ['<!DOCTYPE a><d:multistatus xmlns:d="DAV:"/>', '<broken', '<wrong/>',
    '<d:multistatus xmlns:d="DAV:">' + dav_response('/vault/secret.md', status='403 Forbidden') + '</d:multistatus>'])
def test_webdav_invalid_listing_does_not_look_empty(body):
    dav = WebDAV('https://dav.example/vault', 'user', 'secret')
    dav.client.close()
    dav.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(207, content=body)))
    try:
        with pytest.raises(ValueError):
            dav.list_directory()
    finally:
        dav.close()


def test_webdav_read_size_limit():
    dav = WebDAV('https://dav.example/vault', 'user', 'secret')
    dav.client.close()
    dav.client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'12345')))
    try:
        with pytest.raises(ValueError, match='过大'):
            dav.read_limited('large.md', 4)
    finally:
        dav.close()

@pytest.mark.parametrize('method,path', [('GET', '/library/vault/tree'), ('POST', '/library/vault/sync')])
def test_vault_routes_have_consistent_response(setup, monkeypatch, method, path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import library as routes
    _, vault, root = setup
    write_note(root, '知识/标题.md')
    monkeypatch.setattr(routes, 'vault', vault)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.request(method, path)
    assert response.status_code == 200
    assert response.json()['code'] == 0
    assert str(root) not in response.text


def test_vault_route_hides_connection_details(setup, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from app.routers import library as routes
    _, vault, _ = setup
    def fail():
        raise httpx.ConnectError('https://user:secret@private-server/vault')
    monkeypatch.setattr(vault, 'tree', fail)
    monkeypatch.setattr(routes, 'vault', vault)
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as client:
        response = client.get('/library/vault/tree')
    assert response.status_code == 503
    assert 'secret' not in response.text and 'private-server' not in response.text


def test_note_path_rejects_unsafe_synced_paths():
    for path in ('../secret.md', '/outside.md', '.obsidian/config.md', 'C:/outside.md', 'a\\b.md'):
        assert note_path({'archivePath': path, 'audioMeta': {'title': '标题'}, 'id': 'note-a'}).startswith('BiliNote/笔记/')
