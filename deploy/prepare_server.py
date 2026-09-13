"""备份现有部署并准备入库连接；仅用于已核对路径的此台服务器。"""
import json
import os
import secrets
import shutil
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import yaml

ROOT = Path('/opt/bilinote')
RELEASE = Path(__file__).resolve().parent.parent
BACKUP = ROOT / 'backups' / ('history-archive-' + datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S'))
BRIDGE = Path('/opt/hermes/data/bilinote-archive')
NETWORK = 'bilinote_archive'
TARGET = os.environ.get('ARCHIVE_QQ_TARGET', '')
if not TARGET and (BRIDGE / 'bridge.env').exists():
    TARGET = next((line.split('=', 1)[1] for line in (BRIDGE / 'bridge.env').read_text().splitlines()
                   if line.startswith('ARCHIVE_QQ_TARGET=')), '')
if not TARGET.startswith('qqbot:'):
    raise ValueError('首次部署请先设置已确认的 ARCHIVE_QQ_TARGET')


def run(args):
    return subprocess.check_output(args, text=True).strip()


def private(path, text):
    path.write_text(text, encoding='utf-8')
    path.chmod(0o600)


BACKUP.mkdir(parents=True, mode=0o700)
for service in ('bilinote', 'hermes', 'webdav'):
    shutil.copy2(f'/opt/{service}/compose.yaml', BACKUP / f'{service}-compose.yaml')
container = json.loads(run(['docker', 'inspect', 'bilinote']))[0]
private(BACKUP / 'image.txt', container['Config']['Image'] + '\n')
data = Path(run(['docker', 'volume', 'inspect', 'bilinote_data', '--format', '{{.Mountpoint}}']))
with sqlite3.connect(data / 'bili_note.db') as source, sqlite3.connect(BACKUP / 'bili_note.db') as target:
    source.backup(target)
shutil.copytree(data / 'note_results', BACKUP / 'note_results', dirs_exist_ok=True)
for name in ('config', 'overrides', 'supervisord.conf'):
    path = ROOT / name
    if path.is_dir():
        shutil.copytree(path, BACKUP / name)
    elif path.exists():
        shutil.copy2(path, BACKUP / name)
for volume in ('bilinote_config',):
    source = Path(run(['docker', 'volume', 'inspect', volume, '--format', '{{.Mountpoint}}']))
    shutil.copytree(source, BACKUP / volume)
private(ROOT / 'archive-last-backup.txt', str(BACKUP) + '\n')

BRIDGE.mkdir(parents=True, exist_ok=True)
token_path = BRIDGE / 'token'
token = token_path.read_text().strip() if token_path.exists() else secrets.token_urlsafe(48)
private(token_path, token)
creds = json.loads(Path('/opt/webdav/credentials.json').read_text())
dav_config = yaml.safe_load(Path('/opt/webdav/config.yaml').read_text())
dav_prefix = str(dav_config.get('path-prefix', '')).strip('/')
dav_root = 'http://obsidian-webdav:5000/' + (dav_prefix + '/' if dav_prefix else '') + 'obsidian-vault/'
env = {'ARCHIVE_BRIDGE_URL': 'http://hermes:8650', 'ARCHIVE_BRIDGE_TOKEN': token,
       'WEBDAV_URL': dav_root,
       'WEBDAV_USERNAME': creds['username'], 'WEBDAV_PASSWORD': creds['password']}
# Compose 的单引号值不插值，保留密码中的美元符号。
private(ROOT / 'archive.env', '\n'.join(k + "='" + v.replace("'", "\\'") + "'" for k, v in env.items()) + '\n')
bridge_env = {'ARCHIVE_BRIDGE_TOKEN': token,
              'ARCHIVE_QQ_TARGET': TARGET,
              'ARCHIVE_BRIDGE_STATE': '/opt/data/bilinote-archive/jobs',
              'ARCHIVE_VAULT_PATH': '/obsidian-vault', 'HERMES_HOME': '/opt/data'}
private(BRIDGE / 'bridge.env', '\n'.join(k + '=' + v for k, v in bridge_env.items()) + '\n')
for name in ('archive_bridge.py', 'hermes_safe_chat.py'):
    shutil.copy2(RELEASE / 'deploy' / name, BRIDGE / name)
# 执行用户与原 Hermes 容器一致，数据目录沿用现有属主。
owner = Path('/opt/hermes/data').stat()
os.chown(BRIDGE, owner.st_uid, owner.st_gid)
os.chown(BRIDGE / 'archive_bridge.py', owner.st_uid, owner.st_gid)

if NETWORK not in run(['docker', 'network', 'ls', '--format', '{{.Name}}']).splitlines():
    run(['docker', 'network', 'create', '--internal', NETWORK])
for name, key, container_name in [('bilinote', 'app', 'bilinote'), ('hermes', 'agent', 'hermes'), ('webdav', 'webdav', 'obsidian-webdav')]:
    path = Path(f'/opt/{name}/compose.yaml')
    config = yaml.safe_load(path.read_text())
    service = config['services'][key]
    networks = service.get('networks', ['default'])
    if isinstance(networks, list):
        service['networks'] = list(dict.fromkeys(networks + [NETWORK]))
    else:
        service['networks'] = {**networks, NETWORK: {}}
    config.setdefault('networks', {})[NETWORK] = {'external': True, 'name': NETWORK}
    config['networks'].setdefault('default', {})
    if name == 'bilinote':
        sources = service.get('env_file', [])
        if isinstance(sources, str):
            sources = [sources]
        service['env_file'] = sources + [str(ROOT / 'archive.env')] if str(ROOT / 'archive.env') not in sources else sources
    private(path, yaml.safe_dump(config, allow_unicode=True, sort_keys=False))
    connected = json.loads(run(['docker', 'inspect', container_name]))[0]['NetworkSettings']['Networks']
    if NETWORK not in connected:
        run(['docker', 'network', 'connect', NETWORK, container_name])

unit = '''[Unit]
Description=BiliNote Hermes archive bridge
After=docker.service
Requires=docker.service
StartLimitIntervalSec=0

[Service]
ExecStart=/usr/bin/docker exec --env-file /opt/hermes/data/bilinote-archive/bridge.env hermes /opt/hermes/.venv/bin/python /opt/data/bilinote-archive/archive_bridge.py
ExecStop=/usr/bin/docker exec --env-file /opt/hermes/data/bilinote-archive/bridge.env hermes /opt/hermes/.venv/bin/python /opt/data/bilinote-archive/archive_bridge.py --stop
Restart=always
RestartSec=10
TimeoutStopSec=10

[Install]
WantedBy=multi-user.target
'''
Path('/etc/systemd/system/bilinote-archive-bridge.service').write_text(unit)
run(['systemctl', 'daemon-reload'])
run(['systemctl', 'enable', '--now', 'bilinote-archive-bridge.service'])
print('PREPARED', BACKUP)
