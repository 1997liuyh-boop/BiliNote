import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def bridge(monkeypatch):
    monkeypatch.setenv("ARCHIVE_NOTIFY_TRANSPORT", "cli")
    monkeypatch.setenv("ARCHIVE_BRIDGE_TOKEN", "test-token")
    monkeypatch.setenv("ARCHIVE_QQ_TARGET", "qqbot:test")
    spec = importlib.util.spec_from_file_location("archive_bridge", Path(__file__).parents[2] / "deploy/archive_bridge.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_invalid_cached_output_is_regenerated(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, 'model_connection', lambda: ('example-model', {}))
    (tmp_path / "note.json").write_text('{"body":"","tags":[]}', encoding="utf-8")
    calls = []
    def run(*args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout='{"body":"有效正文","tags":[],"related_ids":[]}')
    monkeypatch.setattr(bridge.subprocess, "run", run)
    value = bridge.ask(tmp_path, "note", "整理资料", lambda value: bridge.validate_note(value, {"a"}))
    assert value["body"] == "有效正文" and len(calls) == 1
    assert '--safe-mode' in calls[0][0]
    assert Path(calls[0][0][1]).name == 'hermes_safe_chat.py'


def test_model_connection_keeps_selected_provider_in_safe_mode(bridge, monkeypatch):
    # 模拟容器中另一个默认地址，避免子进程重新读取配置后串用模型连接。
    monkeypatch.setenv('CUSTOM_BASE_URL', 'https://other.example/v1')
    monkeypatch.setenv('PYTHON_DOTENV_DISABLED', '0')
    monkeypatch.setitem(sys.modules, 'hermes_cli.config', SimpleNamespace(
        load_config=lambda: {'model': {'provider': 'selected', 'default': 'configured-model', 'api_key': 'configured-key'}}))
    def resolve(requested, explicit_api_key):
        assert requested == 'selected'
        assert explicit_api_key == 'configured-key'
        return {'api_mode': 'chat_completions', 'api_key': 'private-test-key',
                'base_url': 'https://selected.example/v1'}
    monkeypatch.setitem(sys.modules, 'hermes_cli.runtime_provider', SimpleNamespace(resolve_runtime_provider=resolve))
    model, env = bridge.model_connection()
    assert model == 'configured-model'
    assert env['CUSTOM_BASE_URL'] == env['OPENAI_BASE_URL'] == 'https://selected.example/v1'
    assert env['OPENAI_API_KEY'] == 'private-test-key'
    assert env['PYTHON_DOTENV_DISABLED'] == '1'


@pytest.mark.parametrize("value,expected", [({"success": True}, "SENT"), ({"skipped": True}, "FAILED"), ({"error": "failed"}, "FAILED")])
def test_notification_requires_confirmed_delivery(bridge, value, expected):
    result = SimpleNamespace(returncode=0, stdout=json.dumps(value).encode())
    assert bridge.delivery_result(result)["status"] == expected


@pytest.mark.parametrize('same_connection', [True, False])
def test_safe_chat_checks_connection_before_calling_hermes(tmp_path, monkeypatch, same_connection):
    spec = importlib.util.spec_from_file_location('hermes_safe_chat', Path(__file__).parents[2] / 'deploy/hermes_safe_chat.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    prompt = tmp_path / 'prompt.txt'
    prompt.write_text('仅整理示例资料', encoding='utf-8')
    monkeypatch.setattr(sys, 'argv', ['hermes_safe_chat.py', '--safe-mode', '-m', 'configured-model', '--query-file', str(prompt)])
    monkeypatch.setenv('ARCHIVE_MODEL_API_KEY', 'configured-key')
    monkeypatch.setenv('CUSTOM_BASE_URL', 'https://selected.example/v1')
    for name in ('HERMES_SAFE_MODE', 'HERMES_IGNORE_USER_CONFIG', 'HERMES_IGNORE_RULES', 'PYTHON_DOTENV_DISABLED'):
        monkeypatch.setenv(name, '0')
    calls = []
    def resolve(**kwargs):
        assert module.os.environ['HERMES_SAFE_MODE'] == '1'
        return {'base_url': kwargs['explicit_base_url'],
                'api_key': kwargs['explicit_api_key'] if same_connection else 'wrong-key'}
    monkeypatch.setitem(sys.modules, 'hermes_cli.runtime_provider', SimpleNamespace(resolve_runtime_provider=resolve))
    monkeypatch.setitem(sys.modules, 'cli', SimpleNamespace(main=lambda **kwargs: calls.append(kwargs)))
    if same_connection:
        module.main()
        assert len(calls) == 1
        assert calls[0]['api_key'] == 'configured-key'
        assert calls[0]['toolsets'] == 'none'
        assert calls[0]['ignore_user_config'] and calls[0]['ignore_rules']
        assert module.os.environ['PYTHON_DOTENV_DISABLED'] == '1'
    else:
        with pytest.raises(ValueError, match='模型连接不一致'):
            module.main()
        assert not calls


@pytest.fixture
def webhook(bridge, tmp_path, monkeypatch):
    monkeypatch.setenv("ARCHIVE_NOTIFY_TRANSPORT", "webhook")
    monkeypatch.setenv("ARCHIVE_HERMES_WEBHOOK_URL", "http://127.0.0.1:8644/webhooks/bilinote-archive")
    monkeypatch.setenv("ARCHIVE_HERMES_WEBHOOK_SECRET", "unit-test-signing-secret")
    monkeypatch.setattr(bridge, "STATE", tmp_path)
    monkeypatch.setattr(bridge, "TARGET", "")
    job_id = "a10d6d0d-8fb7-447e-ae1e-7992b63c2670"
    bridge.save(bridge.folder(job_id) / "state.json", {"status": "COMPLETED"})
    return bridge, job_id


@pytest.mark.parametrize("status,value,expected", [
    (200, {"status": "delivered", "route": "bilinote-archive", "target": "qqbot", "delivery_id": "test-id"}, "SENT"),
    (200, {"status": "delivered", "route": "wrong", "target": "qqbot", "delivery_id": "test-id"}, "UNKNOWN"),
    (200, {"status": "delivered", "route": "bilinote-archive", "target": "log", "delivery_id": "test-id"}, "UNKNOWN"),
    (200, {"status": "delivered", "route": "bilinote-archive", "target": "qqbot", "delivery_id": "wrong"}, "UNKNOWN"),
    (200, {"status": "delivered"}, "UNKNOWN"),
    (200, {"status": "duplicate"}, "UNKNOWN"),
    (200, {"status": "ignored"}, "FAILED"),
    (200, [], "UNKNOWN"),
    (202, {"status": "accepted"}, "UNKNOWN"),
    (302, {}, "UNKNOWN"),
    (401, {"error": "private upstream details"}, "FAILED"),
    (404, {}, "FAILED"),
    (413, {}, "FAILED"),
    (429, {}, "FAILED"),
    (500, {}, "UNKNOWN"),
    (502, {"status": "error", "error": "Delivery failed"}, "UNKNOWN"),
])
def test_webhook_requires_matching_delivery_receipt(bridge, status, value, expected):
    result = bridge.webhook_result(status, json.dumps(value).encode(), "bilinote-archive", "test-id")
    assert result["status"] == expected
    assert "private upstream details" not in json.dumps(result)


@pytest.mark.parametrize("raw", [b"invalid JSON", b"\xff", b" " * 16385])
def test_webhook_invalid_response_is_unknown(bridge, raw):
    assert bridge.webhook_result(200, raw, "bilinote-archive", "test-id")["status"] == "UNKNOWN"


@pytest.mark.parametrize("url", [
    "http://public.example/webhooks/notify", "file:///webhooks/notify",
    "http://user:password@127.0.0.1/webhooks/notify", "http://127.0.0.1/webhooks/notify?secret=x",
    "http://127.0.0.1/webhooks/notify#fragment", "http://127.0.0.1/other",
    "http://127.0.0.1:0/webhooks/notify", "http://127.0.0.1:bad/webhooks/notify",
])
def test_webhook_invalid_config_does_not_claim_notification(webhook, monkeypatch, url):
    bridge, job_id = webhook
    monkeypatch.setenv("ARCHIVE_HERMES_WEBHOOK_URL", url)
    with pytest.raises(ValueError):
        bridge.notify(job_id, "入库完成")
    assert not (bridge.folder(job_id) / "notification.json").exists()


@pytest.mark.parametrize("secret", ["", " ", "INSECURE_NO_AUTH"])
def test_webhook_requires_secret(webhook, monkeypatch, secret):
    bridge, _ = webhook
    monkeypatch.setenv("ARCHIVE_HERMES_WEBHOOK_SECRET", secret)
    with pytest.raises(ValueError, match="密钥"):
        bridge.notification_config()


def test_webhook_signs_exact_utf8_body_and_checks_receipt(webhook, monkeypatch):
    import hashlib
    import hmac
    bridge, job_id = webhook
    calls = []
    class Connection:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("127.0.0.1", 8644, 10)
        def connect(self):
            pass
        def request(self, method, path, body, headers):
            calls.append((body, headers))
            assert method == "POST" and path == "/webhooks/bilinote-archive"
            assert headers["X-Request-ID"] == "bilinote-archive-" + job_id
            expected = hmac.new(b"unit-test-signing-secret", headers["X-Webhook-Timestamp"].encode() + b"." + body, hashlib.sha256).hexdigest()
            assert hmac.compare_digest(headers["X-Webhook-Signature-V2"], expected)
            payload = json.loads(body)
            assert payload["event_type"] == "bilinote.archive.completed"
            assert payload["message"] == "入库完成，标题 {test}；$(不执行)"
            assert payload["job_id"] == job_id
            assert payload["delivery_id"] == headers["X-Request-ID"]
        def getresponse(self):
            def read(limit):
                assert limit == 16385
                return json.dumps({"status": "delivered", "route": "bilinote-archive", "target": "qqbot",
                                   "delivery_id": "bilinote-archive-" + job_id}).encode()
            return SimpleNamespace(status=200, read=read)
        def close(self):
            pass
    monkeypatch.setattr(bridge.http.client, "HTTPConnection", Connection)
    monkeypatch.setattr(bridge.subprocess, "run", lambda *a, **kw: pytest.fail("Webhook 不得调用 CLI"))
    result = bridge.notify(job_id, "入库完成，标题 {test}；$(不执行)")
    assert result == {"status": "SENT", "transport": "webhook"}
    assert bridge.notify(job_id, "重复请求") == result
    assert len(calls) == 1


@pytest.mark.parametrize("phase,expected", [("connect", "FAILED"), ("request", "UNKNOWN"), ("read", "UNKNOWN")])
def test_webhook_network_failures_are_classified_safely(webhook, monkeypatch, phase, expected):
    bridge, job_id = webhook
    calls = []
    class Connection:
        def __init__(self, *args, **kwargs):
            calls.append(1)
        def connect(self):
            if phase == "connect":
                raise ConnectionRefusedError("private connection details")
        def request(self, *args, **kwargs):
            if phase == "request":
                raise TimeoutError("private request details")
        def getresponse(self):
            raise TimeoutError("private response details")
        def close(self):
            pass
    monkeypatch.setattr(bridge.http.client, "HTTPConnection", Connection)
    result = bridge.notify(job_id, "已入库")
    assert result["status"] == expected
    assert "private" not in json.dumps(result)
    bridge.notify(job_id, "已入库")
    assert len(calls) == (2 if expected == "FAILED" else 1)


@pytest.mark.parametrize("status", ["SENT", "UNKNOWN", "SENDING"])
def test_persisted_notification_blocks_resend_after_reload(webhook, monkeypatch, status):
    bridge, job_id = webhook
    bridge.save(bridge.folder(job_id) / "notification.json", {"status": status})
    monkeypatch.setattr(bridge, "send_webhook", lambda *args: pytest.fail("不能重复发送"))
    # 从磁盘读取状态，不依赖 Hermes 一小时内存缓存，也兼容旧 CLI 记录。
    assert bridge.notify(job_id, "已入库")["status"] == status


def test_failed_webhook_retries_keep_original_message(webhook, monkeypatch):
    bridge, job_id = webhook
    calls = []
    def send(key, message, *args):
        calls.append((key, message))
        return {"status": "FAILED" if len(calls) == 1 else "SENT"}
    monkeypatch.setattr(bridge, "send_webhook", send)
    assert bridge.notify(job_id, "第一次原文")["status"] == "FAILED"
    assert bridge.notify(job_id, "第二次修改")["status"] == "SENT"
    assert calls == [(job_id, "第一次原文")] * 2


def test_webhook_cannot_notify_unfinished_job(webhook):
    bridge, job_id = webhook
    bridge.save(bridge.folder(job_id) / "state.json", {"status": "RUNNING"})
    with pytest.raises(ValueError, match="整理尚未完成"):
        bridge.notify(job_id, "完成")


def test_webhook_concurrent_calls_claim_once(webhook, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    bridge, job_id = webhook
    entered, release = Event(), Event()
    calls = []
    def send(*args):
        calls.append(1)
        entered.set()
        assert release.wait(5)
        return {"status": "SENT"}
    monkeypatch.setattr(bridge, "send_webhook", send)
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(bridge.notify, job_id, "已入库")
        try:
            assert entered.wait(5)
            assert bridge.notify(job_id, "已入库")["status"] == "SENDING"
        finally:
            release.set()
        assert future.result()["status"] == "SENT"
    assert len(calls) == 1


def test_cli_notification_remains_available(webhook, monkeypatch):
    bridge, job_id = webhook
    monkeypatch.setenv("ARCHIVE_NOTIFY_TRANSPORT", "cli")
    monkeypatch.setattr(bridge, "TARGET", "qqbot:confirmed-test-target")
    def run(args, **kwargs):
        assert args[args.index("--to") + 1] == "qqbot:confirmed-test-target"
        assert "send" in args and "--json" in args
        return SimpleNamespace(returncode=0, stdout=b'{"success":true}')
    monkeypatch.setattr(bridge.subprocess, "run", run)
    assert bridge.notify(job_id, "已入库") == {"status": "SENT", "transport": "cli"}


@pytest.mark.parametrize("event", ["completed", "failed"])
@pytest.mark.parametrize("status,expected", [(200, "SENT"), (302, "UNKNOWN")])
def test_webhook_local_http_roundtrip(webhook, monkeypatch, status, expected, event):
    import hashlib
    import hmac
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    from threading import Thread
    bridge, job_id = webhook
    received = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass
        def do_POST(self):
            body = self.rfile.read(int(self.headers["Content-Length"]))
            timestamp = self.headers["X-Webhook-Timestamp"]
            signature = hmac.new(b"unit-test-signing-secret", timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
            received.append((self.path, json.loads(body), hmac.compare_digest(signature, self.headers["X-Webhook-Signature-V2"])))
            data = json.dumps({"status": "delivered", "route": "bilinote-archive", "target": "qqbot",
                               "delivery_id": self.headers["X-Request-ID"]}).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Location", "/must-not-follow")
            self.end_headers()
            self.wfile.write(data)
    # 仅本机模拟接收端，不调用 Hermes、不触发 QQ 或模型。
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setenv("ARCHIVE_HERMES_WEBHOOK_URL", f"http://127.0.0.1:{server.server_port}/webhooks/bilinote-archive")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:1")
    try:
        assert bridge.notify(job_id, "本机回归：通知", event, 2)["status"] == expected
    finally:
        server.shutdown()
        server.server_close()
        thread.join(5)
    assert len(received) == 1
    assert received[0][0] == "/webhooks/bilinote-archive"
    assert received[0][1]["message"] == "本机回归：通知"
    assert received[0][1]["event_type"] == f"bilinote.archive.{event}"
    assert received[0][1]["delivery_id"] == f"bilinote-archive-{job_id}" + ("-failed-2" if event == "failed" else "")
    assert received[0][2] is True


@pytest.mark.parametrize("bridge_status", [None, "RUNNING", "FAILED", "COMPLETED"])
def test_failure_notification_is_allowed_at_any_archive_stage(webhook, monkeypatch, bridge_status):
    bridge, job_id = webhook
    state_path = bridge.folder(job_id) / "state.json"
    if bridge_status is None:
        state_path.unlink()
    else:
        bridge.save(state_path, {"status": bridge_status})
    sent = []
    monkeypatch.setattr(bridge, "send_webhook", lambda *args: sent.append(args) or {"status": "SENT"})
    assert bridge.notify(job_id, "入库失败", "failed", 0)["status"] == "SENT"
    assert bridge.notify(job_id, "重复失败", "failed", 0)["status"] == "SENT"
    assert len(sent) == 1 and sent[0][4:] == ("failed", 0)
    assert not (bridge.folder(job_id) / "notification.json").exists()
    bridge.save(state_path, {"status": "COMPLETED"})
    assert bridge.notify(job_id, "入库完成")["status"] == "SENT"
    assert len(sent) == 2


def test_failure_notifications_are_independent_per_attempt(webhook, monkeypatch):
    bridge, job_id = webhook
    sent = []
    monkeypatch.setattr(bridge, "send_webhook", lambda *args: sent.append(args) or {"status": "SENT"})
    bridge.notify(job_id, "第一次入库失败", "failed", 0)
    bridge.notify(job_id, "第二次入库失败", "failed", 1)
    bridge.notify(job_id, "重复调用", "failed", 1)
    assert len(sent) == 2
    assert [args[-1] for args in sent] == [0, 1]


@pytest.mark.parametrize("status", ["SENT", "UNKNOWN", "SENDING"])
def test_persisted_failure_notification_prevents_resend(webhook, monkeypatch, status):
    bridge, job_id = webhook
    bridge.save(bridge.folder(job_id) / "failure-notification-0.json", {"status": status})
    monkeypatch.setattr(bridge, "send_webhook", lambda *args: pytest.fail("失败通知不能重复发送"))
    assert bridge.notify(job_id, "入库失败", "failed", 0)["status"] == status


@pytest.mark.parametrize("event,attempt", [("unknown", 0), ("failed", -1), ("failed", "../x"), ("failed", True)])
def test_notification_rejects_invalid_event_or_attempt(webhook, event, attempt):
    bridge, job_id = webhook
    with pytest.raises(ValueError, match="通知事件"):
        bridge.notify(job_id, "入库失败", event, attempt)


def test_failure_notification_before_manifest_exists(webhook, monkeypatch):
    import uuid
    bridge, _ = webhook
    job_id = str(uuid.uuid4())
    monkeypatch.setattr(bridge, "send_webhook", lambda *args: {"status": "SENT"})
    assert bridge.notify(job_id, "上传失败", "failed")["status"] == "SENT"
    assert (bridge.folder(job_id) / "failure-notification-0.json").is_file()


def test_unknown_optional_links_are_discarded_without_losing_valid_links(bridge):
    value = {"body": "完整正文", "tags": [], "related_ids": ["a", "unknown", "a", "b"]}
    bridge.validate_note(value, {"a", "b"})
    assert value["related_ids"] == ["a", "b"]


@pytest.mark.parametrize("related", ["a", [1], [None]])
def test_malformed_related_links_still_fail(bridge, related):
    with pytest.raises(ValueError, match="关联笔记格式"):
        bridge.validate_note({"body": "正文", "related_ids": related}, {"a"})


@pytest.mark.parametrize("name", [None, "", "   ", "x" * 101, "分类\n名称"])
def test_auto_category_requires_valid_name(bridge, name):
    with pytest.raises(ValueError, match="自动分类名称"):
        bridge.validate_note({"body": "正文", "category_name": name}, set(), True)


def test_auto_category_is_trimmed_and_optional_for_normal_archive(bridge):
    value = {"body": "正文", "category_name": "  电商运营  "}
    bridge.validate_note(value, set(), True)
    assert value["category_name"] == "电商运营"
    bridge.validate_note({"body": "正文"}, set())


def test_organize_requests_category_only_for_unassigned_notes(bridge, tmp_path, monkeypatch):
    monkeypatch.setattr(bridge, "VAULT", tmp_path)
    monkeypatch.setattr(bridge, "folder", lambda _: tmp_path / "job")
    note = {"id": "a", "audioMeta": {"title": "测试"}, "formData": {}, "markdown": [{"content": "正文"}]}
    monkeypatch.setattr(bridge, "load", lambda _: {
        "autoClassify": True, "categories": [{"id": "commerce", "name": "电商运营"}],
        "notes": [note, {**note, "id": "b", "categoryId": "existing"}]})
    prompts = []
    def ask(folder, name, prompt, validate):
        prompts.append(prompt)
        value = {"body": "整理正文", "tags": [], "related_ids": ["a", "invented"]}
        if name == "note-0":
            value["category_name"] = "电商运营"
        validate(value)
        return value
    monkeypatch.setattr(bridge, "ask", ask)
    result = bridge.organize("job")
    assert "category_name" in prompts[0] and "电商运营" in prompts[0]
    assert "category_name" not in prompts[1]
    assert result["notes"][0]["category_name"] == "电商运营"
    assert all(note["related_ids"] == ["a"] for note in result["notes"])
