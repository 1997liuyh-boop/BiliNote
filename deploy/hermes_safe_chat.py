"""使用 Hermes 官方 Python 入口，在安全模式下显式传入当前模型连接。"""
import argparse
import hmac
import os
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--query-file', required=True)
    parser.add_argument('-m', '--model', required=True)
    parser.add_argument('--safe-mode', action='store_true')
    args = parser.parse_args()
    if not args.safe_mode:
        parser.error('入库整理必须启用安全模式')
    # 与 Hermes --safe-mode 相同的三项限制，在导入 Hermes 前生效。
    os.environ.update(HERMES_SAFE_MODE='1', HERMES_IGNORE_USER_CONFIG='1',
                      HERMES_IGNORE_RULES='1', PYTHON_DOTENV_DISABLED='1')
    sys.path.insert(0, os.getenv('ARCHIVE_HERMES_ROOT', '/opt/hermes'))
    from hermes_cli.runtime_provider import resolve_runtime_provider
    from cli import main as hermes_main
    key = os.environ['ARCHIVE_MODEL_API_KEY']
    endpoint = os.environ['CUSTOM_BASE_URL']
    runtime = resolve_runtime_provider(requested='custom', explicit_api_key=key,
                                       explicit_base_url=endpoint)
    if (runtime['base_url'].rstrip('/') != endpoint.rstrip('/')
            or not hmac.compare_digest(runtime['api_key'], key)):
        raise ValueError('安全模式中的模型连接不一致，已停止整理')
    hermes_main(query=Path(args.query_file).read_text(encoding='utf-8'),
                model=args.model, provider='custom', api_key=key, base_url=endpoint,
                oneshot=True, quiet=True, toolsets='none', max_turns=2,
                ignore_user_config=True, ignore_rules=True)


if __name__ == '__main__':
    main()
