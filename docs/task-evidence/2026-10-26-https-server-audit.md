# 2026-10-26 公网入口与HTTPS准备核对

范围：用户要求先检查域名和服务器配置、准备HTTPS及公网更新方案；随后明确**没有域名，短期不绑定域名，只有服务器**。本轮执行只读SSH、端口连通与健康GET、GitHub记录和官方文档核对，只新增计划/证据文档并更新M1入口；没有申请证书、改安全组、安装软件、修改服务器文件、重启服务、备份迁移或发布。

## 实际服务器快照

采样：2026-10-26 03:05:54 UTC（北京时间11:05:54）。连接沿用既有密钥，严格校验已保存SSH主机指纹；没有输出私钥、密码或完整环境配置。

| 项目 | 本次读取得到的结果 |
|---|---|
| ECS / 代码路径 | `47.111.12.85`，`/opt/ai-notes` |
| 系统 / 引擎 | Linux `6.8.0-139-generic`；Docker `29.9.1`，Compose `5.6.0`；项目既有Ubuntu 24.04部署 |
| 服务器代码 | `main`，`cb60fee9a61c603e9eaee4ec62efb2c325573c63` |
| 服务器工作区 | 仅 `compose.server.yaml` 已修改；web映射由仓库 `127.0.0.1:8080:80` 改为 `0.0.0.0:8080:80` |
| 现有入口 | `http://47.111.12.85:8080`；没有发现已配置的网站域名 |
| TLS / 反向代理 | 宿主机没有nginx/caddy/certbot；web镜像内部Caddy **2.9.1**，HTTP配置，未挂载证书持久卷；未发现宿主机公开证书 |
| 监听 / 外部连通 | SSH读取主机22、8080监听，80/443没有监听；本机绕过代理的TCP探测8080可连接，80/443超时。超时不能确定是哪一条安全组规则，云安全组尚未查看 |
| 主机防火墙 | `ufw` inactive；不能据此认定阿里云安全组或Docker入口已正确配置 |
| 运行模式 | 仅投影非敏感配置：`APP_ENV=development`，`APP_ORIGIN=http://47.111.12.85:8080`，`COOKIE_SECURE=false`，`STORAGE_BACKEND=oss`；配置文件权限0600 |
| 应用版本 | web/API/Worker镜像revision均为`cb60fee…`；发布标签 `sha-cb60fee9a61c603e9eaee4ec62efb2c325573c63-39941913800-1` |
| 容器状态 | db/API healthy，Worker/web running；宿主机GET `/api/v1/health/live`、`/api/v1/health/ready` 均200、`status=ok`；本轮未运行写入自检或处理任务 |
| 数据库镜像 | 实际仍为 `sha-a981deec9d6ebaf13f7c8b4c6c360d5a77e37963-39912109727-1`，与配置中当前应用发布标签不同；更新不能只按环境变量推断实际镜像 |
| 数据库镜像digest | `crpi-b9j6kbtluavj6v3w.cn-hangzhou.personal.cr.aliyuncs.com/ai-notes/postgres@sha256:03808cfa25a692ac94353f108736b5d4c0d812c1bdcba774d70d63388c0335c8` |
| 数据卷 / 结构 | `ai-notes-server_pgdata`；只读SELECT得到Alembic **`c2e41a9f3b67`**、public普通表22张（含迁移表），数据库约8.93MiB |
| 资源 | 根盘40G、可用33G、使用率12%；可用内存约2.66GiB；只是采样值，不是负载测试 |
| 历史备份 | `.backups`存在历史目录；最新完成更新目录`update-cb60fee-20261023T105653Z-wEP4Kr`含63284字节`database.dump`、哈希、部署收据、旧配置等。只核对文件名/大小，未读取秘密配置或证明归档可恢复，亦未证明包含OSS对象 |

首次数据库元数据查询的SQL shell引号错误导致后续SELECT未完成；已改为固定参数安全引用，并在只读连接中重跑，表中数据库元数据来自退出0的结果。没有执行任何DDL/DML或迁移。

## 本地与GitHub

- 本地`main`与远端`main`均为`587ba362bf95b2158c772bfda0cc54fcf7a6fe8d`；前几轮PDF框选、选段、备份、GC、监测仍在未提交工作区。
- 已通过GitHub只读核对：该提交的[Project checks成功](https://github.com/lixiaobai405-spec/ai-notes/actions/runs/39947723668)。这不包含本地未提交功能，也不包含本次计划中的HTTPS实现。
- 最新已有成功镜像发布仍对应现网`cb60fee…`：[发布运行39941913800](https://github.com/lixiaobai405-spec/ai-notes/actions/runs/39941913800)。本轮没有触发CI或发布。
- 本地迁移head为`2ef16e7ca892`，从现网经过四次迁移：`c2e41a9f3b67 → d89f74a2136c → 16bd16083e39 → 63b9d8343e02 → 2ef16e7ca892`。涉及PDF、AI对话/步骤、来源页码和回答保存，不能只换镜像而跳过迁移。
- 新版`site_backup.py`严格校验当前表集合与当前head，不能直接作为现网旧schema的迁移前备份工具，也不能先迁移再补备份。历史pg_dump不等于当前完整DB+OSS恢复点。
- 本轮开始时在仓库外保存98个现有改动文件的SHA-256基线；原有代码、契约与未跟踪功能文件保持原样。

## 方案验证

- 本轮只新增此证据和实施计划，修改M1进度入口；98文件基线仅该进度文档变化，未改前后端、部署配置、依赖或已有工具。
- 本地文档链接、代码围栏、凭据模式与`git diff --check`通过；`AGENTS.md`仍忽略且未跟踪，暂存区为空。
- 两个独立审查分别核对部署/数据门禁与IP证书方案。修正迁移失败后可能继续启动的脚本问题、SSE实际GET方法、旧Caddy镜像无法直接当IP HTTPS回退产物的歧义。
- 在无网络、只读挂载、无Docker socket的Linux容器中，以假`docker`实际运行文档中的迁移脚本：配置失败、迁移失败、全部成功三种场景均通过；前两种均未执行应用启动命令。此验证不使用真实数据库或服务。
- 未运行Caddy 2.11.7或Compose HTTPS候选配置，未做签发/续期。应用代码未变，本轮没有重跑上一轮552项后端/前端测试，不把历史通过当新HTTPS通过。

## 纯IP HTTPS可行性

Let’s Encrypt已公开提供IPv4/IPv6地址证书，强制`shortlived`，有效期**160小时**。本项目无需先购买域名，但需要新客户端、自动续期、持久证书存储和真实外部验证。[正式开放公告](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)

当前Caddy 2.9.1不直接满足所选IP签发方案。建议升级现有前端至固定`caddy:2.11.7-alpine`，显式指定Let’s Encrypt ACME issuer、`profile shortlived`、`default_sni 47.111.12.85`，沿用单一同源代理。该版本有正式发布及Docker官方标签；配置语法、签发和续期仍须下一阶段验证，**本轮没有运行新版镜像或签发证书**。[Caddy稳定版本](https://github.com/caddyserver/caddy/releases/tag/v2.11.7)、[Docker官方标签](https://github.com/docker-library/official-images/blob/master/library/caddy)、[TLS配置](https://caddyserver.com/docs/caddyfile/directives/tls#acme)、[无SNI集成测试](https://github.com/caddyserver/caddy/blob/v2.11.7/caddytest/integration/sni_test.go)

入口从HTTP/IP:8080切换为HTTPS/IP:443仍属于不同Origin（浏览器源），需要重新登录；旧入口本地未同步草稿不会自动迁入。须先同步/保留草稿，再进入维护窗口。

## 交付与待办

方案及后续门禁见[IP HTTPS与公网更新计划](../superpowers/plans/2026-10-26-ip-https-public-update.md)。待完成：本地HTTPS模板与隔离验证、兼容旧schema的完整备份和恢复演练、新提交CI/镜像、80/443安全组与ACME外部验证、维护窗口迁移切换、真实自动续期及实机功能验收。没有域名不作为本方案阻塞项。
