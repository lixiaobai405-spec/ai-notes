# M1 最新进度整合

日期：2026-10-23（Asia/Shanghai）。整合本机 `b5252fb` 与上游 `cb60fee`，用于统一提交 PDF / AI、回答保存、账号及整站导出恢复工具，以及最新会话修复。未合并上游 PR、部署服务器、修改业务数据库或调用真实模型 / OSS。

## 整合规则

- 采用上游每个标签页独立的 `sessionStorage` / Bearer 会话；刷新保留，新文档导航清除复制的凭据。仅广播随机 scope 以识别复制标签，不广播账号或凭据，也不传播全局退出。
- 保留当前标签的代次屏障：API、SSE、上传、文件 / ZIP 下载及草稿拒绝旧会话的结果。旧 401 或退出响应不得清除新登录。
- 私有图片与 PDF 原件使用认证读取后生成 Blob；页面链接不再依赖共享 Cookie 取原件。上传继续使用 XHR 保留真实进度，固定当前标签 Bearer / CSRF；同源 XHR 可能携带旧 Cookie，服务端显式 Bearer 优先且验证失败绝不回落 Cookie。
- 保留旧 Cookie 客户端的 `/auth/csrf` 恢复能力；新的标签客户端直接使用登录返回的 CSRF，不调用该 Cookie 引导接口。
- 新 PDF / AI 路径统一使用上游 UUID 兼容函数，保持公网 HTTP 的可用性。
- 修复迁移 `63b9d8343e02` 的降级顺序：先删除 CHECK 再删除其引用的列。CI 在空测试库执行从 head 降至 `c2e41a9f3b67` 再升级 head；这不是生产有数据环境的回滚验收。

## 验证和证据边界

本机 TypeScript / Vite 构建与 61 项前端回归通过；离线备份 / 恢复 / 配置工具 unittest 共 56 项，55 通过、1 项因 Windows 符号链接权限跳过。另一次独立复核的 Node 微任务交错探针确认，旧登录不能覆盖新账号凭据。PostgreSQL、迁移降升、契约与镜像构建结果以本 PR 的 Project checks 为准。

本机 Docker Desktop 启动因 `dockerInference` 运行时监听文件错误而失败，因此本轮没有在本机重新运行 PostgreSQL 全量测试、真实 API 浏览器流程或迁移降升。没有为此重置 Docker、清除容器 / 卷或改用业务库。后端新增两项混合 Cookie / Bearer 的真实 PostgreSQL 集成回归，纳入 CI，覆盖 PDF、AI 来源 / 保存及多账号 ZIP 内容隔离。

Browser plugin not available。浏览器冒烟使用本机缓存的 Playwright / Edge、独立前端预览和模拟 API，检查多标签登录 / 刷新 / 独立退出、PDF 上传 / 原件、AI SSE 与 ZIP 下载，以及桌面 / 手机尺寸、控制台和页面错误。模拟 API 证明前端交互与授权传递，不替代真实 PostgreSQL / 云端验收；最终运行结果及截图路径在 PR 描述中记录。

共享模型 Key 仍只在本机后端的私密运行配置，不进入本工作树、Git 或镜像。真实云端新版本部署、OSS / 隔离恢复、域名 HTTPS、真实手机跨设备与定时备份仍待后续协商；PDF 页内框选、富文本选段来源和统一引用 GC 也仍待开发。
