# PDF 单页框选提问与来源定位

日期：2026-10-25（Asia/Shanghai）。代码基线 `587ba36`。用户明确批准单页单矩形、服务端仅发送裁片、来源原页高亮定位、保存/归档保留选区和旧模式兼容。上一项 T14 未提交内容保留；本次不提交、推送、部署或调用真实付费模型。

## 当前实现

- PDF 资料新增 `mode=region`，仅允许一个原 PDF 页码和一个有限、非空、有界矩形；详情给出授权 file_id，preview 给出实际裁片尺寸。原 `pages/all`、缺 kind 的历史来源及旧请求 hash 保持兼容。
- 后端复用有界 JPEG 读取与内存裁片，创建前和执行前验证来源；仅发送裁片，不附带整页或创建裁片存储对象。8 MiB、2400² 像素和最小 8×8 的限制保留。
- 来源以稳定 page_id、原 PDF 页码和 region 贯穿历史、正文说明、独立保存 provenance、ZIP、离线校验及新版恢复工具。新增 region 归档需新版恢复器，旧无 region 归档仍可恢复。
- 前端共享普通图片的框选控件，支持反向拖框、百分比、清除、加载/预览身份门禁、历史页身份和版本核对。来源阅读器高亮选区，初次自动居中/放大，支持定位按钮、缩放平移及整页重置。
- 无新依赖或数据库迁移。无效/空对象/非对象的来源 JSON 返回受控 409，后台不会降级为无来源文字请求而调用模型。

## 自动验证

| 检查 | 结果 | 边界 |
|---|---|---|
| 后端完整 pytest | 439 passed | 独立 PostgreSQL `127.0.0.1:55432/notes_test`、临时本地对象存储与模拟 provider；包含真实整站/账号恢复工具测试 |
| Alembic check | No new upgrade operations detected | 本阶段无 schema 变更，未迁移日常开发库 |
| OpenAPI / TypeScript / Vite | 通过 | 两个生成契约同步，生产构建通过 |
| 前端 Node tests | 70 passed | 含新增 PDF choice 5 组、定位数学 4 组及原会话/上传/SSE 屏障 |
| 根工具 unittest | 112 项：108 通过、4 跳过 | Windows 下 T14 的四项 POSIX 执行测试按平台跳过，其前项 Linux 证据保持独立 |
| 浏览器桌面 / 手机尺寸 | 1440×1100、390×844 均通过 | 前端 Vite＋合成 API；不等于真实手机触控或前后端真实网页联调 |
| 独立规格审查 | PASS | 独立重跑纯 Node 14 项，通过；未占用测试数据库 |
| 独立质量审查 | PASS | 核对裁片、权限、坏来源、旧 hash、归档、旧预览门禁与定位几何；纯 Node 14 项通过 |
| 上一项 T14 保留检查 | 13 个独立文件 SHA-256 一致 | README/M1 进度入口在保留原内容的基础上补充本阶段状态 |

后端完整回归有一条现有 Starlette TestClient/httpx 弃用警告；未为本功能更换依赖。

执行命令：backend 使用 `.venv/Scripts/python.exe -m alembic check`、`-m scripts.export_openapi`、`-m pytest -q`，显式指定独立 `TEST_DATABASE_URL`、`STORAGE_BACKEND=local`、空共享 Key 以及测试容器 ID；frontend 使用 `npm run types`、`npm run build`；根目录运行 Node tests 和 `python -m unittest discover -s tests -p 'test_*.py'`。

## 关键行为证据

- 新契约先在旧实现 RED；实现后有界/单页/权限/版本专项 GREEN。
- MockTransport 解码真实发送的 base64 JPEG：100×80 左右双色整页中，实际只收到选定蓝色 50×80 裁片；preview 无模型调用和对象写入。
- 旧恢复器拒绝合法 PDF region、离线验证未拒绝模式/坐标冲突的问题先 RED，再补齐验证及恢复。真实 API 创建 region 运行→保存→ZIP→离线 verify→独立空库恢复→重新读取裁片链路通过。
- 损坏来源历史 GET 原先 500，包括空对象、数组和字符串；回归先 RED，修复后运行/对话/事件/来源返回受控 409，worker 模型调用为零。来源删除后既有回答的保留语义未改。
- 定位数学先以缺少 helper 确认 RED，再验证带留白图的选择中心、极小框 600% 上限、整页、无效几何和横竖 viewport 变化。

## 浏览器验证范围

Browser plugin not available，使用已有缓存 Playwright，无新浏览器依赖。前端位于 `http://localhost:5174`，所有 `/api/v1/` 请求由合成路由处理，禁止外部网络；模型/业务数据均未接入。

两个 viewport 均通过：反向拖框→百分比改为 x=.5/y=.25/width=.5/height=.5→300×450 尺寸预览→请求携带原第 2 页与选区→点击回答引用→原页选区居中（中心误差小于 2 px）→放大/平移→重新定位→100% 重置→保存回答→编辑正文→刷新独立来源保留。追加指定页/整本 PDF 与普通图片整图/局部预览回归。没有非预期控制台/页面异常、框架覆盖层、横向溢出或未处理 API 路由。

脚本、`result.json`、`pdf-region-1440.png`、`pdf-region-390.png` 保存在仓库外 Codex 可视化目录的 `pdf-region-qa/`。每个 viewport 仅创建一个合成运行，其他旧模式只核验预览，真实付费模型调用为零。

真实后端网页预览启动命令被自动审批拦截，工具仅返回 `blocked by policy`；该命令没有执行。采用前端模拟 API 继续检查交互。后端的真实数据库、裁片字节与恢复行为由上述独立测试验证，不能将二者合称真实网页端到端验收。

## 收尾与未验证项

- 临时前端、独立测试数据库已停止，5174/8001/55432 关闭；日常 5173/8000/55433 保持关闭，`ai-notes-dev_dev_pgdata` 保留，公网未访问。
- 未使用真实模型、OSS、真实手机、ECS 或新版本云端发布；这些验收不从本机替身结果推定通过。
- 同一个对象键及大小被外部原地覆盖属于既有身份校验边界，本轮不新增内容哈希或历史对象复制。
- PDF 多页多框、原生文本选择及富文本选段来源继续后续任务。本轮所有改动保留未提交，AGENTS.md 与真实凭据未纳入交付。
