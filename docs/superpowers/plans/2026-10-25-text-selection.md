# 富文本选段提问与来源定位实施计划

> **For agentic workers:** 使用 subagent-driven-development 分工实施并独立审查。当前用户授权开发，不包含 commit、push 或部署。

**Goal:** 从已保存富文本选择连续文字，提问、保存并回看原版本高亮来源。

**Architecture:** 基于历史 NoteVersion 的规范文本与码点选区冻结来源；后端统一校验，前端编辑器映射选区，独立阅读器避免覆盖草稿。沿用已有 JSONB，不新增表。

**Tech Stack:** FastAPI、SQLAlchemy/PostgreSQL、React、Tiptap、TypeScript、pytest、Node tests、Playwright。

## Task 1：上下文与设计

- [x] 阅读现有编辑器、聊天、来源和导出/恢复链路，确认历史版本与草稿屏障。
- [x] 写入已获用户继续开发授权的设计，明确 Unicode、版本、权限和模型上下文边界。

## Task 2：后端来源与文字运行

- [x] 新增 text_schemas.py / text_context.py，实现规范全文、选区严格校验和原版本读取。
- [x] 在 schemas.py / source_context.py / chat_service.py / chat_router.py 接入 text，区分文字和视觉执行路径，保留旧 hash。
- [x] saved_notes.py 加入文字来源；新增 test_text_selection.py 验证真实 API 与 MockTransport 模型输入。
- [x] 运行隔离测试，命令：`backend/.venv/Scripts/python.exe -m pytest -q tests/test_text_selection.py`，预期所有新测试通过。

## Task 3：编辑器选段入口

- [x] 新建 frontend/src/features/notes/text-selection.ts 与 tests/text-selection.test.mjs，映射规范码点与 ProseMirror UTF-16 位置；覆盖 emoji、重复文字、跨段落/列表/公式。
- [x] 修改 RichEditor.tsx、NoteDetail.tsx、NotesWorkspace.tsx、App.tsx，仅已保存内容可携带版本进入新对话，沿用会话屏障。

## Task 4：AI 与来源阅读器

- [x] 新增 TextSources.tsx；AiChat.tsx 接入初始选段、新对话、请求幂等 payload、历史恢复与来源入口；更新 SavedAnswerSource.tsx 和来源类型缩窄。
- [x] 来源按原版本全文 mark 高亮并自动滚动，提示当前版本变化，提供打开当前笔记按钮。
- [x] `npm run types`、`node --test tests/*.test.mjs`、`npm run build` 全部通过。

## Task 5：归档与回归

- [x] 修改 scripts/verify_export.py、scripts/restore_export.py、导出序列化，加入文字来源历史正文引用校验，保留旧格式兼容。
- [x] 新增真实导出/离线验证/隔离空库恢复测试；运行后端全量与根工具测试，检查 Alembic 无新增迁移。

## Task 6：浏览器、审查与收尾

- [x] 桌面/手机尺寸验证选段、保存前阻止、提问、原文定位、变更提示、保存编辑刷新，复验 PDF/图片既有模式；使用模拟模型，不访问付费接口。
- [x] 独立 spec/quality 审查并修复阻断问题。
- [x] 更新 README、M1 计划、功能说明和阶段证据，检查前两轮改动保持、无敏感/临时文件、关闭临时服务。

最终验证：后端488/488、前端80/80、根工具108通过/4平台跳过；契约/构建/Alembic检查通过，桌面/手机尺寸与迟到导航浏览器复验通过。真实模型/公网未调用，临时服务关闭。详见docs/task-evidence/2026-10-25-text-selection.md。
