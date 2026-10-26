# 富文本选段提问与来源定位

用户在上一轮确认继续开发 T13：选中文字提问、冻结原文与版本、点击回答或保存笔记的来源定位高亮，原文变化时明确提示。本轮按该范围实施，沿用 main 与现有未提交 PDF/T14 改动，不提交、不部署、不调用真实模型。

## 交互与边界

- 已保存的文字笔记中，鼠标或键盘选择一个连续片段，点击“就选段提问”；跨段落、列表、中文、emoji、换行和原子公式均按规范文本处理。草稿、保存中、失败或冲突时先保存，不将未保存内容绑定旧版本。
- 打开一段新 AI 对话，显示来源标题、版本和原文，输入问题后发送。仅发送所选文字及当前对话历史，不自动附带整篇笔记；现有仅文字、PDF 和普通图片模式保持可用。
- 每轮选段最多 8000 个 Unicode code point（码点），上下文沿用 32000 UTF-8 字节预算。空白、越界、拆开代理对、篡改 quote 或过期 revision 在模型调用前拒绝。
- 回答和保存笔记都有来源入口。阅读器按 note_id 与 revision 打开原笔记当时版本的全文，滚动定位并高亮原选段；当前笔记已变化时显示提示，并可单独打开当前原笔记。不会覆盖当前草稿或猜测重定位重复文本。
- 原笔记删除、所有权不可用、历史版本缺失或记录损坏时拒绝来源访问与后续模型调用。已生成回答和其来源摘要仍可读取。恢复笔记后原版本可重新访问。

## 数据和服务端事实

`RunCreate.text` 可选结构为 `{note_id, revision, start, end, quote}`。start/end 为规范全文中左闭右开的 Unicode 码点偏移。来源输出为 `{kind:'text', note_id, revision, title, start, end, quote}`，无文件或存储键。text/pdf/image 最多选一种。

规范全文从历史 `body_json` 派生，不能信任客户端上传的 `plain_text`：text 节点返回 text；inlineMath/blockMath 返回 latex；hardBreak 返回换行；doc/bulletList/orderedList/listItem/blockquote 的子节点用换行连接，其他节点直接连接。前端提供 JSON 到编辑器位置的映射，明确处理 JS UTF-16 与 Unicode 码点差异。

首次引用时核对当前已保存版本并精确比对原文。同一对话已成功且 note_id/revision/start/end/quote 完全相同的来源，可在重新校验后复用旧冻结版本；其他旧版本输入仍拒绝。后台调用前再次检查所有者、笔记未删除、原版本与 quote 完整；当前笔记后来修改不替换冻结内容。历史上下文保留原 quote，模型可以使用现有文字模型，不走页图处理器或页图进度。旧请求的缺省 text 字段从 hash 中省略，保留旧幂等键。

`GET /ai/runs/{run_id}/source-text` 返回 `{source, text, source_changed}`，只读且不调用模型。复用已有历史表与 JSONB 来源列，无 migration 或新依赖。

保存回答保留独立文字来源及原文摘要。ZIP 导出、离线验证、空库恢复保留相同 note_id/revision/start/end/quote，验证所引用历史正文一致；原有 PDF 和图片归档继续兼容。

## 验证与交付

后端隔离 PostgreSQL 与模拟 provider 验证：精确模型输入、边界/权限、版本变化、删除恢复、旧 hash、保存编辑和 ZIP 恢复。前端纯函数验证 Unicode 和编辑器位置映射，并做桌面/手机尺寸浏览器流程及既有 PDF/图片回归。生成 OpenAPI/TypeScript、生产构建和独立审查。所有运行与未执行事项写入 task-evidence；临时服务结束后关闭。
