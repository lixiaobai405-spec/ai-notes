# PDF Region Questions Implementation Plan

> **For agentic workers:** Use isolated implementation ownership and independent specification/code reviews. Follow subagent-driven-development for delegated tasks. Do not commit/push/deploy. Keep previous T14 changes intact and work in the user's default main checkout.

**Goal:** PDF 单页矩形提问只发送裁片，回答和保存笔记能定位原页选区。

**Architecture:** 扩展现有 PDF JSON 来源，复用图片裁片和框选控件；稳定 page_id 与归一化 region 贯穿运行、保存与归档。来源阅读器以显示图几何计算居中缩放，重用既有授权读取与会话屏障。

**Tech Stack:** FastAPI/Pydantic/SQLAlchemy、Pillow、PostgreSQL、React/TypeScript、现有 Node tests、隔离 Playwright；不加依赖或迁移。

## Task 1: Backend contracts and bounded crop

Files: `backend/app/modules/ai/{schemas,image_context,pdf_context,pdf_chat,chat_service,saved_notes}.py`，必要的小型共享读取器；`backend/tests/test_pdf_region.py`。

- [x] 先写单页/单框 schema、越界/NaN/不合法字段、preview 与裁片实际字节、身份/权限等测试，记录 RED。
- [x] `PdfInput` 增加 region 模式与坐标；PDF notebook page 增加 file_id；source page 增加 region；preview 增加可空尺寸。

```json
{"note_id":"UUID","revision":1,"mode":"region","page_numbers":[2],"region":{"x":0.5,"y":0.0,"width":0.5,"height":1.0}}
```

- [x] preview/freeze 校验并冻结 PDF 原页身份与框；worker 只发送服务端裁片，指令和历史范围明确仅选区；兼容旧缺 kind 来源与无 region 的请求 hash。
- [x] 保存的来源和正文保留选区说明。运行 `backend/.venv/Scripts/python.exe -m pytest -q tests/test_pdf_region.py tests/test_pdf_chat.py tests/test_image_context.py tests/test_image_chat.py tests/test_saved_ai_notes.py`（backend 目录，显式独立 TEST_DATABASE_URL），确认 GREEN。

## Task 2: Archive compatibility

Files: `backend/app/modules/exports/service.py`、`scripts/{verify_export,restore_export}.py`、`backend/tests/{test_exports,test_archive_restore}.py`。

- [x] 新 PDF region 导出、离线校验、恢复 round trip；拒绝无框/多页/越界归档，旧 PDF 无 region 继续接受。
- [x] JSON 来源新可选字段不静默丢失；记录新归档需新版恢复脚本。完整工具与后端回归由根 Agent 统一串行执行，避免测试库并发截表。

## Task 3: PDF picker and saved sources

Files: `frontend/src/features/ai/{AiChat,PdfSources,ImageSources,SavedAnswerSource}.tsx`、共享 RegionSelection、`pdf-choice.ts` 及前端测试。

- [x] 用纯函数测试选择/模式切换/历史恢复，覆盖单页 region、加载与非空门禁；先确认 RED。
- [x] 提取可复用的 RegionCanvas/RegionFields，PDF 新增单页模式与原页选择；复用 PrivateImage 授权，不新建公共图像 URL。
- [x] 构造 pdf 请求，保留 page_numbers/region/sourceRevision；切换笔记/页/模式时清掉不对应选区，来源身份未加载前禁止发送；历史 region 保留稳定页身份并提示版本改变。
- [x] 运行与保存来源统一显示选区模式/比例，复用 SourceReader。TypeScript 检查等待统一生成契约后执行。

## Task 4: Source viewport location

Files: `frontend/src/features/files/{ImageReader.tsx,reader-view.ts}`、`frontend/tests/reader-view.test.mjs`。

- [x] 对留白、矩形角落、极小框/整图和缩放限制写定位数学测试，记录 RED。
- [x] 以原图实际显示尺寸及 viewport 计算 view：`scale=clamp(min(0.8*viewportW/regionW,0.8*viewportH/regionH),1,6)`，`translation=scale*(viewportCenter-regionCenter)`。
- [x] 首次来源图加载后自动定位一次；显示“定位到选区”，重置保持整页，普通无选区阅读保持原行为。ResizeObserver 更新几何但不持续覆盖用户平移。

## Task 5: Integration, browser and evidence

- [x] 同步 `backend/openapi.json` 与 `frontend/src/api.generated.ts`，前端全量 Node tests、TypeScript/Vite build、后端完整隔离回归、Alembic check 和根工具回归。
- [x] 浏览器在隔离服务/合成数据上验证桌面与手机尺寸框选→预览→模拟回答→来源定位→保存/编辑刷新，不调用真实模型。记录后端真实集成与浏览器模拟的边界。
- [x] 独立规格审查及质量审查，修复重要问题并复核。
- [x] 更新 `docs/PDF选页与整本AI对话.md`、`docs/AI回答保存为笔记.md`、README、M1 状态和 task-evidence；检查 T14 文件哈希与保留改动，AGENTS.md/密钥不纳入交付。
- [x] 停止此次启动的隔离服务，确认日常开发库/公网未改；保持未提交，交付结果与验证边界。
