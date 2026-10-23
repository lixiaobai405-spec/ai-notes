# 知页 AI 笔记 Web

M1 开发中，版本 0.1.0。手机和电脑共用 React 前端，后端为 FastAPI，数据保存至 PostgreSQL，后台任务由独立 Worker 执行。

当前完成度与剩余工作统一见 [M1 状态总表](docs/M1开发与云端验收计划.md#m1-状态总表)。旧版服务器部署和手动恢复已有交接验收；本轮新功能尚未发布到服务器，完整 M1 仍待收尾。

## 当前可用功能

- 邀请码注册、登录、退出、一次性密码恢复。
- 管理员创建和停用邀请码，管理成员状态，查看操作审计。
- 数据库会话、Argon2id 密码哈希、CSRF 和 Origin 校验、持久化登录限流。
- 私人资源权限检查；管理员不绕过所有者权限。
- 文件夹、笔记本、标签与私人笔记，自动保存、版本冲突提示、历史版本和回收站。
- Tiptap 富文本、公式、代码块、Markdown 导入与本机草稿恢复。
- 图片笔记批量预览、排序、上传、后台生成阅读图和缩略图、失败重试与缩放阅读器。
- PDF 导入为多页图片笔记，保留原件、显示后台逐页进度，支持断点重试与原件下载；默认 50 MiB / 200 页。
- 私有 OSS 官方 SDK 适配器与权限校验后的图片代理；开发环境可使用本地私有存储。
- 管理员在本机后端统一配置 DeepSeek Key，所有登录成员共用；受控能力表与管理员手动连接测试，网页不显示或收集个人 Key。
- AI 文字与 PDF 选页 / 整本对话，支持 Markdown 和数学公式显示；普通图片支持单张整图或一个矩形选区提问，选区由后端裁剪后发送。
- 成功回答可选标题和笔记本保存为可编辑笔记，保留格式、问题、模型与独立来源引用；图片来源保留原图和选区位置，重复保存不覆盖编辑或重建回收站笔记。
- 当前账号完整 ZIP 导出，包含回收站、历史版本、原图 / PDF、变体及 AI 对话来源；附离线完整性校验器，默认 512 MiB 上限。支持命令行恢复到独立空库 / 空目录，见 [完整导出格式](docs/完整导出格式.md) 和 [账号归档恢复演练](docs/账号归档恢复演练.md)。另有 [整站备份与隔离恢复](docs/整站备份与隔离恢复.md) 工具，已通过本机三账号 PostgreSQL / 文件演练；真实 OSS 云端验收按用户决定暂缓。网页合并导入与定时备份仍待完成。
- 持久任务队列、去重、租约续期、过期回收、退避重试和失败任务人工重试。
- 事务 Outbox 和幂等站内通知投递；可在网页发起后台自检并查看真实结果。
- Alembic 初始迁移、OpenAPI 与生成的 TypeScript 类型、依赖锁文件、Docker Compose 和 GitHub Actions CI。

当前暂用“私有 OSS + 后端图片代理”，无需 OSS 图片自定义域名。配置见 [OSS私有存储与图片代理配置](docs/OSS私有存储与图片代理配置.md)。ECS 已更换为 Ubuntu 24.04，采用 GitHub 获取代码、GitHub Actions 构建、杭州 ACR 个人版分发镜像和 Compose 私有开发联调，步骤见 [Ubuntu服务器部署与运维](docs/Ubuntu服务器部署与运维.md)；进度见 [M1开发与云端验收计划](docs/M1开发与云端验收计划.md)。真实 OSS SDK 联调与本机网页上传 → Worker 生成阅读图/缩略图 → 后端代理阅读已通过。维护者交接确认旧版 ECS 部署、持久化和手动备份恢复已有验收；本轮新功能未执行云端更新，完整 M1 仍待收尾。共享模型配置已开放，启用方式见 [共享模型与DeepSeek配置](docs/共享模型与DeepSeek配置.md)；AI 文字对话已开放，使用方式见 [AI文字对话与运行恢复](docs/AI文字对话与运行恢复.md)；PDF 选页与整本提问已开放，见 [PDF对话说明](docs/PDF选页与整本AI对话.md)。回答保存功能已在本地实现，使用与边界见 [AI回答保存为笔记](docs/AI回答保存为笔记.md)，保存基础流程的本机实测已通过。保存直接使用已有回答，不再调用模型；聊天与新保存笔记支持受控 Markdown 和数学公式排版。新增普通图片整图 / 矩形选区提问的用法与限制见同一文档；本机桌面 / 手机尺寸浏览器与真实整图、裁片调用已通过。OCR、复习与社区仍未开放，笔记功能不依赖模型 Key。

## 本地启动

安装 Docker Desktop 或 Docker Engine 与 Compose 插件。整个代码目录解压为 `ai-notes`，在该目录运行：

```bash
python scripts/init_env.py
docker compose up --build -d
docker compose exec api python -m app.cli create-admin --username admin
```

Windows 可将首条命令替换为 `py scripts/init_env.py`。初始化脚本只使用 Python 标准库，生成随机数据库密码，不输出密码。管理员命令交互输入两次密码，没有内置默认账户，也不会开放网页管理员注册。

访问 http://localhost:8080 登录；API 文档位于 http://localhost:8080/docs。管理员创建邀请码后及时复制；好友使用邀请码在注册页面创建账户。每个密码至少 10 位。

网页支持同一浏览器不同标签页分别登录不同账号：刷新保留本页登录，新开或复制标签页需单独登录，退出只影响当前会话。使用及更新说明见 [标签页独立登录](docs/标签页独立登录.md)。

运行一次后台自检，状态依次显示等待、执行、完成；完成后在首页显示通知。自检持续约 3 秒，页面定期刷新状态。若任务一直等待，检查 Worker 日志。

```bash
docker compose ps
docker compose logs --tail=100 worker
docker compose logs --tail=100 api
docker compose stop worker
docker compose start worker
docker compose down
```

正常 down 保留命名数据库卷与本地图片卷；`docker compose down -v` 会删除这些数据。不要对有重要内容的环境使用该命令。

## 开发启动

后端使用 Python 3.12 或以上，前端使用 Node.js 22。先启动 PostgreSQL，创建专用开发数据库，配置 DATABASE_URL。

当前本机已准备 `compose.dev.yaml` 持久化开发库，端口 55433、数据库 `notes_dev`，API/Worker 沿用项目虚拟环境。私密配置已在 Git 忽略路径中生成；管理员创建、三个进程启动、停止与恢复见 [本地开发启动与停止](docs/本地开发启动与停止.md)。它与临时回归测试库分开保存数据。

```bash
cd backend
uv sync --locked
export DATABASE_URL='postgresql+psycopg://notes:你的密码@localhost:5432/notes'
export APP_ORIGIN='http://localhost:5173'
uv run alembic upgrade head
uv run python -m app.cli create-admin --username admin
uv run uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log
```

另开两个终端，在 backend 运行 `uv run python -m app.worker`，在 frontend 运行：

```bash
npm ci
npm run dev
```

访问 http://localhost:5173。Vite 将 /api 请求代理到 8000，保留浏览器 Origin。PowerShell 环境变量语法是 `$env:DATABASE_URL='...'` 和 `$env:APP_ORIGIN='...'`。

没有 uv 时，可以创建 Python 虚拟环境后用 `pip install -r requirements-dev.lock`，通过虚拟环境执行 alembic、python 和 uvicorn。

## 原生 PostgreSQL 测试

测试会清空业务表，只能使用隔离测试库。以下 Compose 测试库使用临时内存卷，不与应用数据卷共享。

```bash
docker compose -f compose.test.yaml up -d --wait
cd backend
export TEST_DATABASE_URL='postgresql+psycopg://notes:notes@localhost:55432/notes_test'
export DATABASE_URL="$TEST_DATABASE_URL"
export APP_ORIGIN='http://testserver'
uv run alembic upgrade head
uv run alembic check
uv run pytest -q
```

测试包含最后一个邀请码名额的并发注册，以及多个 Worker 的同时领取。CI 使用真实 PostgreSQL 16 执行全部测试。测试后回到根目录运行 `docker compose -f compose.test.yaml down`。

接口修改后重新生成契约：

```bash
cd backend
uv run python -m scripts.export_openapi
cd ../frontend
npm run types
npm run build
```

生成的 backend/openapi.json 和 frontend/src/api.generated.ts 需要一起提交。CI 会检查差异，防止接口和前端类型脱节。

## 目录与扩展入口

| 目录 | 内容 |
|---|---|
| backend/app/modules/auth | 账户、会话、邀请码、恢复与管理员接口 |
| backend/app/modules/jobs | 后台任务、通知与队列服务 |
| backend/app/modules/notes | 笔记组织、正文、版本、回收站 |
| backend/app/modules/files | 私有存储、上传会话、图片处理与代理 |
| backend/app/modules/ai | 共享模型、文字 / PDF / 图片选区对话、回答保存与来源引用 |
| backend/app/shared | 配置、事务、模型、错误、安全与权限 |
| backend/app/worker.py | 独立后台任务入口 |
| backend/alembic | 可审查、可执行的数据库迁移 |
| backend/tests | 身份、权限、并发、租约与投递测试 |
| frontend/src | 账户、空间概览、管理与任务页面 |
| .github/workflows | 原生 PostgreSQL、构建与契约检查 |
| scripts | 本地配置初始化 |

notes 与 files 模块均检查私人所有者，不把 admin 作为读取私人内容的通行证。后台新处理器必须以 lease 验证的事务写入业务副作用；对非幂等外部 API 的未知结果，另行设计 unknown 状态，不能套用当前自检任务的自动重试策略。

Outbox 将业务变更和事件保存在同一个数据库事务，通知投递与 delivered_at 也在同一事务中完成。本期仅使用站内通知列表轮询，实时 WebSocket 在后续阶段加入。

## 配置与运维边界

| 配置 | 默认与说明 |
|---|---|
| APP_ORIGIN | 必须匹配浏览器访问来源，本地 Compose 为 http://localhost:8080 |
| COOKIE_SECURE | 本地 HTTP 为 false；正式 HTTPS 环境为 true |
| APP_ENV | 本地 development；生产必须同时配置 HTTPS Origin 和 Secure Cookie |
| SESSION_DAYS | 会话 7 天，退出和密码恢复立即撤销相应会话 |
| RATE_LIMIT_REQUESTS / RATE_LIMIT_SECONDS | 每个来源 IP 的认证接口限额，默认每 300 秒 10 次 |
| JOB_LEASE_SECONDS | 默认 30 秒；Worker 按租约续期，旧执行者不能完成新租约任务 |
| DB_POOL_SIZE / DB_MAX_OVERFLOW | 每个进程默认 5 / 5，按总数据库连接预算调整 |
| STORAGE_BACKEND / STORAGE_ROOT | 开发默认 local；API 与 Worker 使用同一目录，Compose 配置共享 filesdata 卷 |
| OSS_REGION / OSS_BUCKET / OSS_ENDPOINT / OSS_PREFIX | 当前杭州私有 Bucket；默认区域 HTTPS Endpoint，专用 ai-notes/ 前缀 |
| OSS_ACCESS_KEY_ID / OSS_ACCESS_KEY_SECRET / OSS_SECURITY_TOKEN | 仅注入服务端；长期 AccessKey 不填 security token；真实值禁止提交 |
| DEEPSEEK_API_KEY | 仅后端持有的共享 DeepSeek Key，所有登录成员共用；留空时模型服务不可用，笔记功能继续正常 |
| PDF_MAX_BYTES / PDF_MAX_PAGES / PDF_PAGE_TIMEOUT_SECONDS | PDF 默认 50 MiB、200 页、每次打开/逐页渲染 60 秒；详细说明见 [PDF 导入配置](docs/PDF笔记导入与逐页转换.md) |

`compose.yaml` 是本地构建开发配置；`compose.server.yaml` 是从 ACR 拉取镜像的 Ubuntu 私有开发联调配置，使用独立 `.env.server`。仓库默认均只绑定本机 8080，数据库和 API 无公网映射；交接记录中的服务器工作副本曾单独调整 web 端口绑定，后续更新须先核对并保留已有配置。旧版备份恢复有历史验收，新增数据、正式域名、HTTPS 与生产配置另行验收。

账户恢复由管理员核实身份后生成 30 分钟一次性凭证；新凭证替代旧凭证，成功重置后撤销全部旧会话。管理员自身只能在服务器执行 `python -m app.cli reset-admin --username admin` 恢复。

标准错误响应包含 code、message、request_id。日志不记录 Cookie、密码、邀请码、重置凭证、请求正文或签名 URL。停用用户阻止新访问并撤销会话；不把停用误当成删除用户数据。

## 当前验证范围

以下是各阶段当时的验证记录。后续完成情况与本轮审计结论统一见 [M1 计划](docs/M1开发与云端验收计划.md)，不按旧段落的“尚未”重新判定当前状态。

2026-10-20 可靠性收尾：修复跨标签退出后旧内容 / 草稿重现、用户名 pattern，以及普通图片部分变体失败的残留清理。后端全量 278 项、前端 39 项、配置 6 项与生产构建通过；真实本机验证退出、迟到响应、换账号及原有离线 / 冲突 / Markdown 流程，无新增模型调用。存储或数据库状态未知时保守留存对象，强杀与统一引用 GC 仍需后续；完整导出、新版备份恢复、真实手机与云端完整闭环尚未完成。

2026-10-20 普通图片阶段：后端全量 265 项、前端 11 项、配置 6 项与生产构建通过，无新增数据库迁移。使用两张生成的 1200×800 测试图，现有共享模型整图调用 1 次（返回用量 800 tokens）、右半 600×800 裁片调用 1 次（620 tokens）；整图识别左右两侧，裁片只读取右侧。桌面 1440×1100 / 手机尺寸 390×844 验证反向画框、百分比调整、预览、引用、100% / 125% 缩放及平移后的选区位置、保存编辑与刷新；重排后仍打开同一图片并提示历史版本，后续复验没有新增调用。浏览器无应用异常、HTTP 失败或 Key 泄漏，既有登录 pattern / favicon 提示单独识别。每次选择一张已处理普通图片，选区至少 8×8 像素，阅读图和输出裁片各不超过 8 MiB；每篇最多 200 张普通图片。不支持多图同时提问、多个选区或 PDF 页面框选。共享 Key 仍仅在本机后端私密配置中；真实手机触控、云端部署与完整 M1 继续待验收。

2026-10-20 排版阶段：后端全量 204 项、前端 8 项、配置 6 项和生产构建通过。独立 Playwright + Edge 在桌面 1440×1100、手机尺寸 390×844 验证流式原文转终态排版、保存、编辑、刷新、JSON/Markdown 导出与 PDF 原页；使用合成回答，新增模型调用为零。无新应用错误，既有登录 pattern/favicon 提示单独识别。复杂内容回退原文，表格与远程图片嵌入尚未加入。

2026-10-20 回答保存阶段：开发库备份及增量迁移、Alembic check、后端全量 182 项、前端 5 项、配置 6 项和生产构建通过。桌面与手机尺寸完成保存、丢响应后重试、编辑与来源原页回看；新增模型调用为零。见 [本阶段记录](docs/task-evidence/2026-10-20-save-ai-answer.md)。完整 M1 与云端部署仍未完成。

2026-10-16 PDF 导入：后端全量 110 项、配置 6 项、自动保存回归与前端构建通过。真实本机网页上传中文三页 PDF、Worker 渲染、刷新继续、原页序/缩放、原件下载比对和加密文件提示均通过。使用见 [PDF笔记导入与逐页转换](docs/PDF笔记导入与逐页转换.md)，证据见 [本次验收](docs/task-evidence/2026-10-16-pdf-import.md)；真实 OSS PDF 和 ECS 部署未验收。

2026-10-16：按用户要求改为本机后端统一持有共享 DeepSeek Key；隔离 PostgreSQL 后端回归 94 项、配置脚本测试 6 项、自动保存回归与前端构建通过。桌面和手机尺寸检查通过，真实官方模型列表连接成功，浏览器未持有 Key。聊天生成仍未实现。证据见 [共享 Key 验收记录](docs/task-evidence/2026-10-16-shared-deepseek.md)。

2026-10-14：隔离原生 PostgreSQL 后端 68 项测试通过，Alembic 升级与 check 通过；自动保存延迟请求回归 1 项通过；前端 TypeScript 与生产构建通过。OSS 测试使用替身客户端，不能作为真实 Bucket 验收。富文本编辑器的独立构建块仍有体积提示。

首次真实 OSS 联调因 RAM 用户与 Bucket 分属不同账号收到 `403 AccessDenied`；在 Bucket 所属账号重建受限用户、授权并替换私密配置后，77 字节测试图的上传、大小检查、读取比对、删除与删除后不存在检查全部通过。随后两张生成图片的真实网页上传、Worker 派生图、代理阅读、缩放、页序调整与刷新保留通过；测试笔记保留，6 个对象数据库记录合计 55021 字节。Ubuntu ECS 应用部署、手机真实触控、模型调用、备份恢复均未验收；这些事项按 M1 计划继续。Docker 图片共享卷配置已补齐，但这轮尚未完成整套应用容器构建运行验证。
