import importlib.util
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest


@pytest.fixture
def bridge(monkeypatch):
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
