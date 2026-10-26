# 2026-10-26 纯公网IP HTTPS本地配置与隔离验证

用户明确授权：按推荐方案实现本地纯IP HTTPS配置并完成隔离验证，暂时不修改服务器或发布。本轮全程没有SSH、访问或修改公网应用、真实ACME签发、模型/OSS调用、提交推送或发布。公网情况仍以[上一轮只读盘点](2026-10-26-https-server-audit.md)为历史依据，没有把它当作本轮重新核验结果。

## 交付

- 前端Caddy固定升级到`2.11.7-alpine`，原HTTP Caddyfile/基础Compose保留。
- 新增`deploy/Caddyfile.https`：显式IP站点、LE ACME issuer、`shortlived`、`default_sni`和HTTP-01；原API/SPA/安全响应头保留，文档入口返回404。
- 新增`compose.https.yaml`：`!override`完全替换8080为TCP80/443，API/DB不公开；指定原DB镜像，保留项目和原pgdata，证书/data/config独立持久卷；web仅接收两个非敏感变量。
- 环境示例仅加注释，不改变HTTP默认值。实际生产三项模式/Origin/Secure配置仍需未来维护窗口核对，未生成或覆盖私密配置。
- `scripts/verify_https_isolated.py`及白名单夹具提供可复现的本机Docker验证；CI在既有检查后构建本机镜像、显式运行安全单测和隔离验证，**远端CI未推送或运行**。
- 新增真实应用的HTTPS Origin、Secure Cookie和标签页Bearer/CSRF回归；不改应用API、schema、模型配置或依赖。

## 测试结果

| 验证 | 本次结果 |
|---|---|
| 部署RED/GREEN | HTTPS未实现时出现11个预期断言失败，HTTP基线通过；完成后6个真实Compose解析测试通过 |
| Compose | 客户端5.5.1；假环境位于仓库外，原回环8080/受控公网8080两种配置都准确替换；DB镜像/卷、秘密隔离、必需变量缺失/空值均覆盖 |
| Caddy生产配置 | 真实2.11.7执行adapt/validate，`network none`，确认唯一LE/shortlived、IP匹配与default_sni；没有启动生产issuer或签发证书 |
| HTTPS应用新增回归 | 8项通过：HTTP旧源/其他源拒绝、Bearer/CSRF、独立退出、Secure/SameSite/HttpOnly兼容Cookie、production接受IP HTTPS且拒绝不安全配置 |
| 全后端回归 | **560 passed**，246.83秒；隔离notes_test和真实pg_dump/pg_restore，Alembic check通过、无新迁移；1条既有Starlette/httpx弃用警告 |
| 前端 | **80/80通过**；新前端Docker镜像内TypeScript和Vite生产构建通过 |
| 根工具 | **298项：292通过、6个平台跳过**，16.478秒 |
| 隔离工具安全单测 | **10/10通过**，CI显式收集；覆盖远程Docker拒绝、优先级/端点固定、未知卷拒绝/imageID固定、证书替换不改HTTP、清理前所有权复验 |
| 双审查 | 配置规格与最终质量审查通过；已修复Docker上下文优先级问题，并补测试发现的能力/权限和Caddy JSON兼容处理 |

构建产物均仅保存在本机，没有推送：

- Caddy上游镜像：`caddy:2.11.7-alpine@sha256:eb4d35b7452d1ab6171fbdbe8d9275b2301dad6dad92115d91e19fb9b24d510f`。
- 当前完整前端镜像：`ai-notes-ip-https-local:verified`，ID `sha256:b142b319dc0a53044b3adbdd518e10966a1fefd6c32c0194bd69da368d32bb68`。
- CI式Python镜像：按当前backend Dockerfile/锁文件另行本机构建`ai-notes-ip-https-python:verified`，ID `sha256:0f1e3767673337f4f73539199b28e5ab06f03927c389fb323b4446ed7c7061e2`；只为夹具提供Python/OpenSSL，不启动应用。

## 真实Caddy隔离传输

作者运行通过后，主执行者独立复跑：**77.317秒，全部通过，cleanup passed**。使用当前新前端及已有Python缓存镜像；生产Caddyfile SHA-256为`231f6ce46df53865b0b228242b1d409159da8f4f3eecb965e23bca8b22e43c8e`。

最后安全补充后，使用本轮新构建Python镜像按CI方式再次实际运行：**77.165秒，status=ok、cleanup=passed**；SSE持续64.044秒，32心跳、最大间隔2.044秒；两种大小请求哈希与前次一致。此轮覆盖最终版本的本机端点、VOLUME预检及不可变镜像ID路径，仍未触发远端CI。

- TLS客户端只在进程内信任临时CA，以数字IP验证证书SAN且不发送SNI；正确IP握手通过、错误IP拒绝。
- HTTP跳转保留路径/查询；SPA深链与真实构建静态资源可读，文档404，安全头存在。
- 两组固定合成身份的Authorization/Origin/CSRF头完整透传；伪造X-Forwarded-For/Proto/Host被清理，真实客户端IP和HTTPS协议保留。
- 20MiB和50MiB合成请求逐字节SHA-256一致。此项验证代理传输，**不是有效图片/PDF的应用入库验收**。
- GET SSE持续**64.045秒**，首字节0.044秒，32个心跳，最大心跳间隔2.044秒，Last-Event-ID重连通过；模拟API不调用模型。
- 重启后测试证书指纹及私密/data标记保持，验证测试卷持久性；不是ACME自动续期证明。

运行时仅替换生产解析JSON的证书提供方式，删除自动签发并明确关闭证书自动化；完整HTTP app（路由、SNI、超时）逐项相等。上游和探测客户端在随机标记的Docker内部网络，不映射宿主机端口，不挂全仓库或Docker socket，不接业务数据库。临时根证书不安装到系统信任库，CA/私钥仅在测试卷内并随清理删除。

## 修正与安全边界

- 官方Caddy二进制带文件能力，仅`cap-drop ALL`会导致无法exec；Caddy进程只补`NET_BIND_SERVICE`，没有privileged。证书生成助手仅在禁网络容器以root写受控测试卷，api/probe仍用镜像默认非root用户。
- Docker的`DOCKER_CONTEXT`优先于`DOCKER_HOST`，已按实际顺序解析并拒绝远端SSH/TCP；验后固定本机endpoint，后续显式指定，清理也不会随上下文变化转向别处。
- 镜像预检只取ID/声明卷，拒绝未管理卷，后续使用固定imageID且`--pull never`；不会拉缺失镜像或遗留镜像自动创建的匿名卷。
- 资源随机标签隔离；创建超时后仍按该标签查找，删除前再次核对所有权，不用全局prune、Compose down或业务卷删除。
- stdout只含结果、固定诊断码及公开测试哈希；不输出真实env、凭据、PEM或原始请求。真实环境文件不加载，所有测试密钥均为临时合成值。

## 范围与后续

开工前记录106个已有文件的SHA-256基线。允许改动仅为本轮Dockerfile、CI、环境示例及README/M1/当前计划，原有业务代码、契约、依赖和先前未提交功能保持原样；新文件仅为HTTPS配置、测试、夹具、验证工具和文档。`AGENTS.md`仍忽略且未跟踪，暂存区为空。

测试数据库完成后停止，日常5173/8000/55433保持关闭；隔离容器/网络/卷按随机标签清理。镜像保留供复现，测试输出放仓库外。操作说明见[本地配置与隔离验证](../IP地址HTTPS本地配置与隔离验证.md)。

仍待授权与现场验收：旧schema完整DB+OSS备份/恢复、最终提交CI/ACR发布、80/443安全组、公开IP证书真实签发/信任/自动续期、浏览器HTTP2和真实手机/电脑完整业务链路。此次HTTP/1.1临时CA与模拟API验证不能代替这些结果，也不代表公网已启用HTTPS。
