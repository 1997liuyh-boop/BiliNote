# 分类与 Obsidian 入库部署

历史以服务端 SQLite 和结果文件为准，浏览器原有 IndexedDB 首次连接新版时自动补入旧版本。分类只保存成员关系；手动入库才创建持久化整理任务。

## 本次服务器

- BiliNote 原镜像：`bilinote-local:20260909-download-fix`，保留所有原有下载器和转写器挂载。
- WebDAV 根目录：`/opt/data/obsidian-vault`。
- Hermes 通过已有容器执行文档整理，QQ 接收账号由用户发送标记消息确认。
- 私有网络 `bilinote_archive` 仅连接三项服务，适配器没有对外发布端口。

`prepare_server.py` 仅适用于本次已核对的服务器路径。先备份 SQLite、历史 JSON、Compose 和配置，再准备私有连接；密钥仅保存在服务器，不写入 Git。

## 工作流程

1. 分类：创建分类、移动成员、取消归类、改名或解散，仅更新历史组织关系。
2. 手动入库：上传此次版本的固定快照至 `.bilinote-inbox`。
3. Hermes：逐条整理正文、标签和关联；整类任务另外归纳全部来源并给出阅读顺序。
4. 发布：写入 `BiliNote/笔记`、`BiliNote/分类`、`BiliNote/附件` 和 `BiliNote/总览.md`，回读校验后更新入库状态。
5. 通知：使用已确认的 QQ 会话发送完成提醒；失败记录在任务中，重试通知不会重新整理正文。

Obsidian 无需额外插件。Markdown 包含 YAML 标签、稳定 ID、来源、时间、分类入口、双向关联和原始笔记。文件名包含完整来源 ID，改名后沿用既有路径以保留外部链接。

## 运行与恢复

- 后台会恢复排队和中断的任务。相同成员与版本重复点击不会重复入库。
- 分类或正文更新后显示“待更新”，手动入库后更新 Vault。
- 人工修改过的受管文件不自动覆盖。任务会报告具体冲突文件；备份并处理冲突后重试。
- QQ 发送状态不确定时保留 UNKNOWN，避免重复提醒。先确认手机是否收到，再由管理员检查适配器记录。
- 从历史移除笔记或解散分类不会删除已经入库的 Vault 文件。
- 老电脑需要在原来使用的浏览器打开一次新版网站，才能迁移仅存于该浏览器的历史版本。
- 当前是原项目的单实例共享笔记库，不是独立多用户笔记系统；沿用服务器现有访问控制。

服务管理：`systemctl status bilinote-archive-bridge`；连接配置为 `/opt/bilinote/archive.env` 与 `/opt/hermes/data/bilinote-archive/bridge.env`。请勿把这些文件的内容贴到公开日志。

适配器沿用 Hermes 已有的 OpenAI 兼容模型。整理子进程保留安全模式、禁用工具和用户规则；模型连接只在服务器内传递，并禁止子进程重新加载 `.env` 覆盖该连接。首次部署需要通过 `ARCHIVE_QQ_TARGET` 指定已确认的接收会话，后续沿用服务器私有配置。

回退时读取 `/opt/bilinote/archive-last-backup.txt` 指向的备份目录，将其 `bilinote-compose.yaml` 恢复到 `/opt/bilinote/compose.yaml`，重新启动原服务，并停止入库适配器。新增数据库表可保留，旧代码会忽略它们；不要直接覆盖数据库，以免丢失部署后新生成的笔记。

## 验证

自动检查覆盖跨设备历史、迁移去重、版本时钟偏差、归类不入库、完整分类快照、重试恢复、人工修改保护、Obsidian 属性和关联、QQ 实际送达判断。

2026-09-13 验收：29 项本地检查通过；26 项核心检查曾在生产镜像环境通过。两条示例笔记完成真实整类入库，WebDAV 四个 Markdown 文件回读校验一致，标签有效、双向链接均有目标；任务状态 COMPLETED，QQ 返回 SENT。网站保留“部署验收（可删除）”分类供查看，原有 23 条历史均已恢复。

前端采用现有 Tailwind 样式。可使用 `node node_modules/vite/bin/vite.js build --configLoader runner` 在受限 Windows 环境构建。项目全量 TypeScript 检查仍存在原有错误，不应把构建成功等同于全量类型检查通过。
