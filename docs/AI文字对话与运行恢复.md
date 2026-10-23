# AI 文字对话与运行恢复

2026-10-16：T12 第一阶段已开放独立文字对话。登录后进入侧栏“AI 对话”→“新建对话”→发送问题。所有成员共用本机后端的 DeepSeek Key，不需要个人配置；对话与运行记录按用户隔离，管理员也不能读取他人的对话。

## 当前行为

- 默认 DeepSeek Flash，可切换 DeepSeek V4 Pro；这一阶段只提交文字，关闭深度思考，每轮最多输出 4096 tokens。
- 回复分段显示；问题、部分回答、状态与服务商返回的用量保存在 PostgreSQL。刷新或切换页面后读取同一次运行，不重新调用模型。
- 已成功完成的轮次进入后续上下文；取消、失败、中断和截断的轮次仍显示在历史中，但不作为完整答案发给下一轮模型。
- “停止生成”立即保存取消状态，保留已经入库的部分回答。Worker 观察到取消后关闭上游连接；等待上游数据时受 20 秒读取超时约束。已经产生的费用不能撤销，服务商没返回用量时显示未知，不显示为零。
- 生成失败不会自动重发。用户可以输入新问题主动发起下一次运行；网络导致提交结果不明时，原内容重试使用同一 request_key，返回原记录。
- 回答当前以纯文本显示。后续已接入 PDF 选页、整本视觉提问与原页引用，见 [PDF对话说明](PDF选页与整本AI对话.md)；回答保存为笔记与 Markdown/公式渲染尚未接入。

## 持久化与去重

新增迁移 `16bd16083e39`，只增加 `ai_conversations` 和 `ai_runs`，不修改既有笔记数据。每个运行保存一轮用户问题和对应回答，同时冻结本次输入消息；不保存共享 Key。会话和运行均使用 owner_id 与 request_key 唯一约束，运行与会话还有同所有者的组合外键。

创建接口在调用模型前提交 queued 记录。Worker 用行锁和 skip_locked 领取，先提交 running 与租约，再向固定地址 `https://api.deepseek.com/chat/completions` 发一次 POST。生成期间每约 250 毫秒保存新进度并检查账户状态/取消；无数据库锁跨越网络等待。不走可自动重试的通用 jobs 状态机。

租约失效的 running 记录恢复为 interrupted，不会再次入队；失效执行者不能写回。数据库提交结果不明、上游断线或进程崩溃都不构成自动再发 POST 的依据。Worker 重启后会检查失效运行；在单 Worker 刚退出时，可能需要等待最多约 60 秒租约到期。排队记录尚未发出模型请求，可由 Worker 首次领取。

本阶段复用已有 Worker，生成与 PDF/图片处理串行执行；长任务会让后续任务等待。需要更高并发时另做容量设计，不宣称当前实现支持大量并行用户。Worker 每轮最长约 180 秒，连接超时 5 秒、单次读取超时 20 秒；无数据到达时按读取超时退出，持续有数据时检查总时限。

## 接口与 SSE

| 接口 | 用途 |
|---|---|
| GET/POST `/api/v1/ai/conversations` | 最近 100 段私人对话；幂等创建 |
| GET `/api/v1/ai/conversations/{id}` | 读取一段对话及全部轮次 |
| POST `/api/v1/ai/conversations/{id}/runs` | request_key、previous_run_id、受控 model 和 prompt 创建运行 |
| GET `/api/v1/ai/runs/{id}` | 查询已保存结果 |
| GET `/api/v1/ai/runs/{id}/events` | fetch 消费 SSE；只读取已有运行 |
| POST `/api/v1/ai/runs/{id}/cancel` | 幂等取消 |

写操作均验证登录、CSRF 和 Origin。用户行锁串行化同一账户的创建和取消；previous_run_id 防止旧窗口基于过时历史提交。读取 SSE 时释放初始数据库连接，每约 400 毫秒短查询进度，并重新验证登录会话与账户状态。

SSE 包含 meta（已有完整快照）、delta（Unicode 码点 offset 与新增文字）、state（状态与用量）、done（最终快照）、error（安全错误）。重连首先覆盖本地快照，不能把历史前缀再次拼接。前端支持跨网络分片的 UTF-8 与 CRLF；意外 EOF 明确提示“重新读取”，不会自动重新生成。

没有资料来源的文字会话不会伪造 citation。未来接入 PDF/笔记后再增加真实来源事件。

## 初始限制

- 单问题最多 8000 个字符；每段最多 100 轮；每位用户每小时最多 30 次创建运行，同时最多一条在途回复。
- 上下文按 UTF-8 总字节保守限制为 32000，不依赖不匹配的 tokenizer，也不自动截断前文；超限提示新建对话。
- 回答上限 64000 字符；服务商响应总量最多 2 MB，单行最多 64 KiB；异常响应、错误正文和任意服务商字段不直接返回网页。
- 模型地址和模型 ID 受控，不接受浏览器传入 Key、任意 URL、system 消息或工具。只有本机后端与 DeepSeek 官方服务在 HTTPS 请求中接触共享 Key。

网页目前列出最近 100 段对话，更早记录保留在数据库中；历史分页和会话删除不在本阶段。

协议依据：[DeepSeek Chat Completions](https://api-docs.deepseek.com/zh-cn/api/create-chat-completion/)、[Thinking Mode](https://api-docs.deepseek.com/guides/thinking_mode/)。实际验收见 [2026-10-16 文字对话记录](task-evidence/2026-10-16-ai-text-chat.md)。
