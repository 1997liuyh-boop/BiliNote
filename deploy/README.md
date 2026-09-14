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

## 查看 Vault 目录与反向同步

生成历史顶部新增 **Obsidian Vault** 面板。展开即可查看服务器上实际存在的 Markdown 目录树，支持折叠、文件名/目录路径搜索和手动刷新；笔记卡片同时展示已记录的 Vault 相对路径。仅显示 `.md` 文件及其祖先目录，不显示隐藏目录、附件和空目录。

连接方式（二选一）：

- 默认复用已有 WebDAV 配置，`WEBDAV_URL` 必须指向 Vault 根目录（本次服务器即 `/opt/data/obsidian-vault` 对应的 WebDAV 根）。读取目录需要 WebDAV 支持 `PROPFIND`，只读查看与同步不依赖 Hermes。
- 设置后端环境变量 `OBSIDIAN_VAULT_PATH` 可直接读取服务器目录，优先于 WebDAV。Docker 部署应将宿主机 Vault 以只读方式挂载，例如 `/opt/data/obsidian-vault:/vault:ro`，并在后端容器中设置 `OBSIDIAN_VAULT_PATH=/vault`。不要填浏览器所在电脑的路径。已有 WebDAV 入库写入配置不受此变量影响，两种连接应指向同一个 Vault。

### 为什么入库了还是未归类？

“归类”对应 BiliNote 数据库的分类成员关系，“入库”对应导出文件与版本；二者独立。原流程只从 BiliNote 向 Vault 发布，不会读取在 Obsidian 中移动目录或修改分类所产生的变化。把文件写到默认 `BiliNote/笔记` 目录本身不等于归类。

更新服务后，展开面板并点击 **同步归类与入库状态**，确认后执行一次只读核对：

1. 用 YAML 属性 `id: bilinote-<笔记 ID>` 精确匹配现有且未删除的 BiliNote 笔记。没有稳定 ID、仅标题相同的文件不会自动匹配，也不会把其他个人 Markdown 导入历史。
2. 自定义目录中的笔记按完整相对父目录补齐分类，例如 `知识/人工智能`；默认根目录、`BiliNote`、`BiliNote/笔记` 不作为分类。默认目录中的文件读取 `category_id`、`category_name`，旧文件可从同 ID 的分类入口一级标题恢复名称。
3. 只为未归类笔记补齐成员关系。已有分类不一致、分类名称冲突或一个笔记 ID 有多个副本时跳过，并在同步报告列出原因，交给用户手动确认。不会取消已有分类、恢复已删除笔记或移动/修改/删除 Vault 文件。
4. 更新实际入库文件路径；新导出文件包含 `bilinote_revision` 与 `category_name`，旧文件可用已完成入库任务中的对应路径和版本核对。无法确认当前版本的文件显示“待更新”，不会仅因文件存在就冒充当前版本已入库。
5. 文件已移动时，后续入库沿用该路径，不生成另一个默认路径副本；已有人工编辑仍由原来的入库冲突校验保护。遇到冲突先备份并核对文件，勿直接覆盖。

同步是手动补齐，不是持续双向镜像。进行中的入库任务会阻止同步。读取中断或权限错误会在写入分类事务前终止，不应用半次结果。文件解析异常会单独报告，其余可确认笔记继续同步。

安全边界：不读取本地符号链接或目录联接；不向前端暴露服务器绝对路径或 WebDAV 凭据。每次扫描最多 3000 篇 Markdown、500 个目录、30 层目录；同步每文件最多 2 MB、总读取最多 32 MB，超限会明确报错，不返回截断结果。大型 Vault 可将连接根目录限定到相应笔记子库。

接口：`GET /api/library/vault/tree`、`POST /api/library/vault/sync`，沿用现有共享笔记库访问控制。新增内容无需数据库迁移；需要更新前后端，旧线上页面不会自动获得此面板。

### B 站多 P 选集预览与批量生成

- 新建笔记时，在视频链接下点击「查看视频选集」。支持 `bilibili.com/video/BV…/?p=N` 完整链接；不支持 b23 短链或跨 BV 的 UP 主合集。
- 只读返回同一 BV 的完整分 P 目录、标题、时长和当前集。默认仅勾选链接中的当前集，可全选或清空；预览不会下载视频或调用转写/模型。
- 点击「生成所选 N 集笔记」后还需确认。所有分集采用确认时的模型和笔记设置；每集独立入库，按目录顺序逐集执行，一集异常不会阻断后续任务。每集可能产生转写与模型调用费用。
- 目录优先读取 B 站视频信息接口，接口风控时尝试公开网页中的 JSON 元数据，不执行网页脚本。读取失败会报错，不把残缺目录当成完整目录；最多支持 500 集。
- 批次提交使用 UUID 标识，同一标识及参数重试复用原有任务。断网或超时后，优先使用「重试同一批次」，不要刷新后立即新建重复任务；刷新页面后应先核对生成历史。
- **队列为服务进程内队列，不是可持久化调度器。** 关闭浏览器不会取消已接收任务，但服务重启不会自动恢复尚未执行的分集。重启后请在历史中核对并重试未完成项；删除尚未执行的笔记会跳过该项。
- 视频、音频及字幕缓存按 `BV号_p序号` 隔离，避免批量生成时不同分集复用错误的字幕或截图。历史标题带 `P序号 + 分集标题`，保留单集重试时的标题。

新增接口（沿用现有 `/api` 前缀）：

- `GET /api/video/collection?video_url=...`：只读目录预览。
- `POST /api/generate_collection`：原有生成参数加 `request_id`（UUID）、`pages`（正整数数组）。服务端重新校验目录及所选分集，不接收客户端传入的分集标题、下载地址或预取字幕。

本地回归测试（在 `backend` 目录）：

```powershell
..\.venv\Scripts\python.exe -m pytest tests/test_bilibili_collection.py tests/test_bilibili_collection_cache.py tests/test_library.py tests/test_archive_pipeline.py tests/test_archive_bridge.py tests/test_vault.py tests/test_url_normalize.py tests/test_video_url_support.py -q
```

2026-09-14 只读实测：`BV1DfrdByE2H/?p=38` 共 43 集，P38 为「5-文件建议」（764 秒），无需 Cookie 的网页回退成功。批量生成测试使用模拟执行器，没有实际生成整套课程，也未部署。

## 2026-09-14 腾讯云增量发布记录

本节仅适用于本次功能更新，优先于上文首次部署的回退说明。不要重新运行 `prepare_server.py`，也不要停止已有入库适配器。

- 服务器：`43.173.85.103`（硅谷轻量应用服务器），项目目录 `/opt/bilinote`。
- 旧镜像：`bilinote-local:20260913-history-v2`。
- 新镜像：`bilinote-local:20260914-vault-collection`，基于旧镜像复制本次后端改动及前端构建产物，没有重新安装模型依赖。
- 发布目录：`/opt/bilinote/releases/20260914-vault-collection`。
- 备份目录：`/opt/bilinote/backups/20260914-vault-collection`；包含 SQLite 一致性备份、生成结果、Compose、配置、下载器覆盖文件。切换前另存 `bili_note.pre-switch.db` 与 `pre-switch-state.json`。
- 发布包 SHA256：`9102f56f5396d941efec86bed830e53b50524d0b6d801a1fce64c697dcfa3aa6`。
- 前端主包：`index-BbcNRCA9.js`。

本次上线：长标题悬停提示、Vault Markdown 树及手动反向同步、B 站同 BV 多 P 选集预览与选择、已入库/未入库独立筛选。

### 生产定制与发布范围

生产下载器使用 `configured_youtube_dl` 每次重新读取 Cookie，与本地下载器基线不同。本次从原生产覆盖文件复制后，仅合并分集缓存补丁，保留三个动态 Cookie 调用点。Compose 只更换镜像及 B 站下载器只读挂载源：

`/opt/bilinote/releases/20260914-vault-collection/backend/app/downloaders/bilibili_downloader.py`

其他环境变量、网络、端口、卷及覆盖文件保持一致。仅更新 Compose 的 `app` 服务，未重启 `hermes`、`obsidian-webdav`、`gateway`，未修改 Vault 文件。后续更新不要直接用本地下载器覆盖这个生产定制文件。

### 已完成验收与限制

- 无生产数据卷、无网络的隔离容器检查通过：Python 语法、新路由和前端产物、P1/P38 音视频与字幕缓存隔离。检查挂载了原生产 `download_cookie.py` 只读辅助模块，模拟下载未访问 B 站或调用模型。
- 新版容器健康状态为 `healthy`；内部前端 HTTP 200 且引用新主包。
- SQLite 完整性检查通过。保留 51 条未删除笔记、1 个分类、2 条入库任务、51 个版本；笔记表总共 52 行，其中包含既有删除记录。
- 历史 API 返回 51 条；Vault 树只读接口返回 31 篇 Markdown、9 个目录。
- 选集只读接口实测 `BV1DfrdByE2H` 共 43 集，当前 P38。
- 公网 `https://note.no-ask.site/` 返回 HTTP 401，原有身份认证仍生效。内置浏览器导航遭客户端拦截，未完成公网浏览器页面验收；未绕过认证或调整访问权限。
- 未执行 Vault 同步、未批量生成视频、未测试真实模型生成。历史 Markdown 中间状态全部对应更新时间更晚的已成功主任务，未清理或改写这些状态文件。

### 本次更新的回退

先确认没有正在生成的视频或入库任务。保留回退前的最新数据备份，然后恢复旧 Compose 并仅重建 BiliNote：

```sh
sudo cp -p /opt/bilinote/backups/20260914-vault-collection/compose.yaml /opt/bilinote/compose.yaml
sudo docker compose --project-directory /opt/bilinote -f /opt/bilinote/compose.yaml up -d --no-deps --pull never app
```

旧镜像与旧下载器覆盖文件均已保留。不要还原数据库覆盖发布后新增笔记，不要执行 `down -v`，不要停止入库适配器。选集队列仍为进程内顺序执行，重启不会自动恢复未完成队列。


## Hermes Webhook 完成通知（可选启用）

此改造只替换完成通知的发送方式，**不改变 Hermes 整理、WebDAV 发布、回读校验或入库状态逻辑**。原有服务器未配置时继续使用 CLI；代码更新本身不代表生产 Webhook 已启用。

### 配置与兼容性

- 参考官方文档：https://hermes-agent.nousresearch.com/docs/user-guide/messaging/webhooks
- 适配器环境变量样例：`deploy/bridge-webhook.env.example`。需要合并到 `/opt/hermes/data/bilinote-archive/bridge.env`，不是 BiliNote 应用的 `.env`。
- Hermes 路由样例：`deploy/hermes-webhook.example.yaml`。将 `platforms.webhook` 合并到现有配置，保留所有模型和其他消息平台设置。
- `ARCHIVE_NOTIFY_TRANSPORT=webhook` 明确选择新方式；`cli` 为兼容默认值。Webhook 失败不会自动切换 CLI，避免一次任务两次通知。
- `ARCHIVE_HERMES_WEBHOOK_URL` 必须为 `/webhooks/<route-name>` 地址，不支持查询参数、URL 内嵌凭据、重定向或 profile 前缀。HTTP 仅接受回环地址及内部服务名 `hermes`；其他主机要求 HTTPS，使用系统证书验证。
- `ARCHIVE_HERMES_WEBHOOK_SECRET` 与该路由的 `secret` 完全一致，在服务器生成独立随机密钥，不复用入库桥接 Token。禁止空密钥和 `INSECURE_NO_AUTH`，不要写进 Git、命令输出或共享日志。
- 路由必须设置 `deliver_only: true`、`deliver: qqbot`、`prompt: "{message}"`，不要添加 `skills`、`script` 或 `cron_job`。QQ 目标固定在服务器配置中，不从请求载荷动态选择。
- `deliver_extra.chat_id` 应使用现有 QQ 适配器确认过的会话 ID，不能直接复制完整的 `ARCHIVE_QQ_TARGET=qqbot:...` CLI 目标字符串。沿用同一接收会话，不切换到未确认的默认 home channel。

当前桥接服务通过 `docker exec` 在 Hermes 容器内运行，所以建议 Webhook **仅监听 `127.0.0.1:8644`**；无需宿主机端口映射、腾讯云防火墙规则或公网反向代理。若已有 Webhook 接收其他业务，先检查监听与路由配置，不要直接替换或缩小原服务的监听范围。

### 请求与状态处理

通知载荷只有 `event_type=bilinote.archive.completed`、任务 ID、通知 ID、完成提醒文本，不附带笔记正文、Cookie 或模型密钥。使用 UTF-8 原始 JSON 字节签名：

- `X-Webhook-Signature-V2`：HMAC-SHA256(`<timestamp>.<raw_body>`) 的十六进制值。
- `X-Webhook-Timestamp`：Unix 秒时间戳；双方系统时钟应同步。
- `X-Request-ID`：`bilinote-archive-<job_id>`，重试保持一致。

新客户端不读取代理环境变量、不跟随重定向；连接和读操作设置超时，响应最多读取 16 KiB 加一个检测字节，不将第三方错误原文写入任务日志。

| 接收结果 | 本地通知状态 | 后续处理 |
| --- | --- | --- |
| HTTP 200，`status=delivered`，route、qqbot target 和 delivery_id 均匹配 | SENT | 持久化成功，后续请求不再发送；这不是用户已读回执 |
| 连接建立前失败；HTTP 400/401/403/404/405/413/415/422/429；`status=ignored` | FAILED | 排查服务、签名、过滤或限流后，从现有入库任务手动重试 |
| 请求开始后超时、断连；HTTP 202、502、其他未知响应；`duplicate` 或不匹配回执 | UNKNOWN | 不自动重发，不回退 CLI，先人工核对 QQ 收信和 Hermes 日志 |

Hermes 的 ID 去重缓存只有一小时，且上游在实际发送**之前**记录 ID；所以 `duplicate` 甚至可能对应之前失败的发送。502 也可能源于部分发送后异常，不能一律当作安全重试。BiliNote 持续保存 `notification.json` 和原始 `notification.txt`：同任务复用原文，SENT/SENDING/UNKNOWN 阻止再次发送，服务重启时 SENDING 仍转为 UNKNOWN。UNKNOWN 不能通过切换传输方式或删除记录来“修复”；先核实实际送达情况，再由管理员决定是否需要重新发送，避免破坏去重保护。

### 上线检查与回退

1. 核对生产 Hermes 已安装版本的 `gateway/platforms/webhook.py` 支持 `X-Webhook-Signature-V2`、`deliver_only`、同步 `delivered` 回执及 QQ 投递。只读取代码/版本，不调用通知接口；旧版本不自动降级到无时间戳签名，也不在此次改造中自动升级整个 Hermes。
2. 确认没有正在整理或通知的任务，备份桥接脚本、`bridge.env` 和 Hermes 配置；保留 jobs 目录和数据库，不覆盖历史状态。
3. 在服务器私下生成并填写路由密钥、已确认 QQ 会话 ID；合并配置并校验，保留其他路由。先使 Hermes Webhook 服务生效，再更新桥接脚本和环境。若启用平台需要重启 Hermes 网关，应安排短暂消息服务中断窗口。
4. 只读检查容器内 `GET http://127.0.0.1:8644/health`、监听范围及桥接 systemd 服务状态；health 成功并不证明 QQ 可送达。
5. 实际发送验收会向现有 QQ 会话发消息，应先确认再用一个真实已完成入库任务验证。不能仅凭 HTTP 200 宣布验收成功，应核对匹配回执、任务 SENT 和 QQ 收信。不批量重试历史任务。
6. 回退时先确认没有发送中的任务，再将 `ARCHIVE_NOTIFY_TRANSPORT` 改为 `cli`，保留原 `ARCHIVE_QQ_TARGET`，仅重启桥接服务；必要时恢复备份脚本。不要删除通知状态、还原旧数据库或停止整个入库系统。是否撤回 Hermes 新路由应单独核对，不影响其余消息服务。

本地测试仅使用模拟投递和本机 HTTP 接收端，不会调用真实 Hermes、QQ 或模型。覆盖签名原文字节、中文文本、严格回执校验、连接失败与发送中断、限流与 duplicate、并发去重、磁盘去重、重试复用原文、旧 CLI 兼容及通知失败不重复整理/发布。

2026-09-14 本次本地回归：Webhook、入库流水线、历史库、Vault 和分集相关测试共 **153 passed**（2 条依赖弃用警告）；未执行生产配置切换，也未发送真实 QQ 验收消息。
