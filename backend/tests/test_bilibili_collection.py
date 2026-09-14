import json
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.db.engine import Base
from app.routers import collection as routes
from app.services import bilibili_collection as service
from app.services.library import LibraryService

BVID = 'BV1DfrdByE2H'
URL = f'https://www.bilibili.com/video/{BVID}/?p=38'


def video_data():
    return {'bvid': BVID, 'title': '测试课程', 'videos': 43, 'pages': [
        {'page': page, 'cid': page + 100, 'part': '5-文件建议' if page == 38 else f'第{page}集', 'duration': 120}
        for page in range(1, 44)]}


def catalog():
    return service.parse_collection(video_data(), BVID, 38, 'api')


@pytest.fixture
def library(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'test.db'}", connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    return LibraryService(sessionmaker(bind=engine, autoflush=False), tmp_path / 'notes')


@pytest.fixture
def client(library, monkeypatch):
    monkeypatch.setattr(routes, 'library', library)
    monkeypatch.setattr(routes, 'get_collection', lambda url: catalog())
    monkeypatch.setattr(routes, 'transcriber_readiness', lambda: {'ready': True})
    app = FastAPI()
    app.include_router(routes.router)
    with TestClient(app) as test_client:
        yield test_client


def submission(**changes):
    return {'request_id': str(uuid4()), 'pages': [38, 1], 'video_url': URL,
            'platform': 'bilibili', 'quality': 'medium', 'model_name': 'test-model',
            'provider_id': 'test-provider', 'style': 'minimal', **changes}


def test_real_shape_retains_current_episode():
    result = catalog()
    assert result['total'] == 43
    assert result['currentPage'] == 38
    assert result['episodes'][37]['title'] == '5-文件建议'
    assert result['episodes'][37]['url'] == URL
    assert result['episodes'][0]['url'].endswith('?p=1')


def test_tracking_and_escaped_ampersands():
    assert service.parse_video_url(URL.replace('?p=38', '?vd_source=test\\&p=38')) == (BVID, 38)
    assert service.parse_video_url(f'bilibili.com/video/{BVID}') == (BVID, 1)


@pytest.mark.parametrize('url', [
    f'https://evil.test/video/{BVID}/?p=38', f'https://bilibili.com.evil.test/video/{BVID}',
    f'https://user@www.bilibili.com/video/{BVID}', f'ftp://www.bilibili.com/video/{BVID}',
    URL + '&p=2', URL.replace('p=38', 'p=0'), URL.replace('p=38', 'p=-1'),
    URL.replace('p=38', 'p=abc'), URL.replace('p=38', 'p='), f'https://www.bilibili.com:999/video/{BVID}',
])
def test_invalid_url_rejected_before_network(url, monkeypatch):
    monkeypatch.setattr(service, 'read_public_resource', lambda *args: pytest.fail('不应访问网络'))
    with pytest.raises(ValueError):
        service.get_collection(url)


@pytest.mark.parametrize('mutation', ['missing', 'duplicate', 'count', 'foreign', 'cid', 'limit'])
def test_incomplete_or_invalid_catalog_rejected(mutation):
    data = video_data()
    if mutation == 'missing': data['pages'].pop()
    if mutation == 'duplicate': data['pages'][1]['page'] = 1
    if mutation == 'count': data['videos'] = 42
    if mutation == 'foreign': data['bvid'] = 'BV0000000000'
    if mutation == 'cid': data['pages'][1]['cid'] = 101
    if mutation == 'limit':
        data['pages'] *= 12
        data['videos'] = len(data['pages'])
    with pytest.raises(service.CollectionUnavailable):
        service.parse_collection(data, BVID, 38, 'api')


def test_api_failure_falls_back_to_page_without_executing_scripts(monkeypatch):
    calls = []
    monkeypatch.setattr(service.CookieConfigManager, 'get', lambda *args: 'test-cookie')
    def read(url, headers, params=None):
        calls.append((url, headers, params))
        if len(calls) == 1: raise service.CollectionUnavailable('412')
        return '<script>window.__INITIAL_STATE__=' + json.dumps({'videoData': video_data()}) + ';throw new Error()</script>'
    monkeypatch.setattr(service, 'read_public_resource', read)
    result = service.get_collection(URL)
    assert result['total'] == 43 and result['source'] == 'page'
    assert calls[0][2] == {'bvid': BVID}
    assert calls[1][0] == URL
    assert calls[1][1]['Cookie'] == 'test-cookie'


def test_out_of_range_is_not_replaced_with_network_error(monkeypatch):
    monkeypatch.setattr(service.CookieConfigManager, 'get', lambda *args: None)
    monkeypatch.setattr(service, 'read_public_resource', lambda *args: json.dumps({'code': 0, 'data': video_data()}))
    with pytest.raises(service.PageOutOfRange, match='44'):
        service.get_collection(URL.replace('p=38', 'p=44'))


def test_network_error_hides_details(monkeypatch):
    monkeypatch.setattr(service.CookieConfigManager, 'get', lambda *args: None)
    def fail(*args): raise service.requests.ConnectionError('private-password')
    monkeypatch.setattr(service, 'read_public_resource', fail)
    with pytest.raises(service.CollectionUnavailable) as error:
        service.get_collection(URL)
    assert 'private-password' not in str(error.value)


@pytest.mark.parametrize('status,size', [(302, 0), (200, service.MAX_RESPONSE_BYTES + 1)])
def test_redirect_and_oversized_response_rejected(monkeypatch, status, size):
    class Response:
        status_code = status
        def __enter__(self): return self
        def __exit__(self, *args): pass
        def iter_content(self, chunk_size): yield b'x' * size
    def get(*args, **kwargs):
        assert kwargs['allow_redirects'] is False
        assert kwargs['stream'] is True
        return Response()
    monkeypatch.setattr(service.requests, 'get', get)
    with pytest.raises(service.CollectionUnavailable):
        service.read_public_resource('https://www.bilibili.com', {})


def test_preview_does_not_create_or_execute_tasks(client, library, monkeypatch):
    monkeypatch.setattr(routes, 'execute_episode', lambda *args: pytest.fail('预览不能生成'))
    result = client.get('/video/collection', params={'video_url': URL})
    assert result.status_code == 200
    assert result.json()['data']['total'] == 43
    assert library.list_notes()['total'] == 0


def test_submission_is_ordered_atomic_and_idempotent(client, library, monkeypatch):
    executed = []
    monkeypatch.setattr(routes, 'execute_episode', lambda task_id, form: executed.append((task_id, form)))
    data = submission()
    first = client.post('/generate_collection', json=data)
    assert first.status_code == 200, first.text
    result = first.json()['data']
    assert result['created'] is True
    assert [item['page'] for item in result['tasks']] == [1, 38]
    assert [form['video_url'] for _, form in executed] == [catalog()['episodes'][0]['url'], URL]
    second = client.post('/generate_collection', json=data).json()['data']
    assert second['created'] is False
    assert second['tasks'] == result['tasks']
    assert len(executed) == 2
    assert library.list_notes()['total'] == 2
    for task_id, form in executed:
        assert form['model_name'] == 'test-model'
        assert 'task_id' not in form
        library.save_result(task_id, {'markdown': '测试正文', 'audio_meta': {'title': '普通标题'}})
        assert ' · P' in library.detail(task_id)['audioMeta']['title']
    changed = {**data, 'style': 'academic'}
    assert client.post('/generate_collection', json=changed).status_code == 409


@pytest.mark.parametrize('changes', [{'pages': []}, {'pages': [1, 1]}, {'pages': [0]}, {'pages': [True]},
    {'pages': [44]}, {'task_id': 'existing'}, {'platform': 'youtube'}, {'provider_id': ''},
    {'prefetched_transcript': {}}, {'video_url': 'https://evil.test/video/' + BVID}])
def test_invalid_batch_writes_nothing(client, library, changes):
    assert client.post('/generate_collection', json=submission(**changes)).status_code in (400, 422)
    assert library.list_notes()['total'] == 0


def test_readiness_gate_does_not_create_tasks(client, library, monkeypatch):
    monkeypatch.setattr(routes, 'transcriber_readiness', lambda: {'ready': False, 'reason': '请下载模型', 'downloading': False})
    assert client.post('/generate_collection', json=submission()).json()['code'] == 300102
    assert library.list_notes()['total'] == 0


def test_one_failure_does_not_block_next_episode(client, monkeypatch):
    executed, failed = [], []
    def execute(task_id, form):
        executed.append(task_id)
        if len(executed) == 1: raise RuntimeError('测试异常')
    monkeypatch.setattr(routes, 'execute_episode', execute)
    monkeypatch.setattr(routes, 'mark_episode_failed', failed.append)
    assert client.post('/generate_collection', json=submission()).status_code == 200
    assert len(executed) == 2 and failed == [executed[0]]


def test_deleted_queued_episode_is_skipped(library, monkeypatch):
    items, _ = library.save_collection_pending(str(uuid4()), {}, catalog()['episodes'][:2], '测试课程')
    library.delete_note(items[0]['task_id'])
    executed = []
    monkeypatch.setattr(routes, 'library', library)
    monkeypatch.setattr(routes, 'execute_episode', lambda task_id, form: executed.append(task_id))
    routes.run_collection(items, {})
    assert executed == [items[1]['task_id']]


def test_database_failure_rolls_back_whole_batch(library):
    engine = library.sessions.kw['bind']
    def fail(*args): raise RuntimeError('模拟写入失败')
    event.listen(engine, 'before_cursor_execute', fail)
    with pytest.raises(RuntimeError):
        library.save_collection_pending(str(uuid4()), {}, catalog()['episodes'][:2], '课程')
    event.remove(engine, 'before_cursor_execute', fail)
    assert library.list_notes()['total'] == 0

def test_single_episode_retry_preserves_batch_identity(library):
    request_id = str(uuid4())
    episodes = catalog()['episodes'][:2]
    items, _ = library.save_collection_pending(request_id, {}, episodes, '课程')
    library.save_pending(items[0]['task_id'], {'video_url': items[0]['url']})
    repeated, created = library.save_collection_pending(request_id, {}, episodes, '课程')
    assert repeated == items and created is False
    library.save_result(items[0]['task_id'], {'markdown': '正文', 'audio_meta': {'title': '通用标题'}})
    assert library.detail(items[0]['task_id'])['audioMeta']['title'] == items[0]['title']
