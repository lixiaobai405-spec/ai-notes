# IP 地址 HTTPS 本地配置与隔离验证

本文说明仓库内的 IP HTTPS 配置和本机复现入口。本地配置及隔离工具已完成；实际结果、版本与限制见[阶段证据](task-evidence/2026-10-26-ip-https-local.md)。GitHub 上新增的 CI（持续集成）门禁尚未推送或运行，不能用本机通过代替远端结果。

所有命令在 Windows PowerShell 7、本项目根目录执行。本文不包含服务器部署操作；后续上线、备份、迁移、正式证书申请和回退，统一遵循[公网 IP HTTPS 与版本更新实施计划](superpowers/plans/2026-10-26-ip-https-public-update.md)，并按计划取得对应授权。

## 配置组成

前端镜像使用 `caddy:2.11.7-alpine`。现有 `frontend/Caddyfile` 与 `compose.server.yaml` 保留原 HTTP 行为；HTTPS 通过额外的 `deploy/Caddyfile.https` 和 `compose.https.yaml` 启用，不改变默认开发入口。

| 配置 | 行为与原因 |
|---|---|
| `compose.https.yaml` 的 `ports: !override` | 完整替换原端口，只映射 TCP 80 和 443；原 `127.0.0.1:8080:80` 或受控的 `0.0.0.0:8080:80` 差异均不会继续合并暴露 |
| `POSTGRES_IMAGE` | 必填，使用现有数据库镜像已核实的完整 digest（内容摘要）；应用更新时保留原数据库镜像，不随 `RELEASE_TAG` 改变 |
| 项目与业务卷 | 保留项目名 `ai-notes-server`、原 `pgdata` 及其实际身份 `ai-notes-server_pgdata` |
| web 环境变量 | 只显式传入 `SITE_IP`、`ACME_DIRECTORY` 两个非敏感值，不把整份服务器环境文件交给 web |
| Caddy 配置挂载 | `deploy/Caddyfile.https` 以只读方式挂载到 `/etc/caddy/Caddyfile` |
| 证书存储 | `caddy_data:/data` 和 `caddy_config:/config` 为独立持久卷，与数据库卷分开；更换应用容器时保留证书和 ACME 账户数据 |
| 内部服务 | API、数据库、worker、迁移服务和 Caddy 管理端口 2019 均不增加宿主机公开端口 |

Caddy 继续以一个入口提供 SPA（单页应用）和同源 `/api/*` 代理，保留 `/srv` 静态文件、`index.html` fallback（回退）和现有安全响应头。HTTPS 配置中的 `/docs`、`/docs/*`、`/redoc`、`/redoc/*`、`/openapi.json` 默认返回 404。

全局 `default_sni {$SITE_IP}` 用于客户端不发送 SNI（服务器名称指示）的 IP TLS 握手。站点明确使用 `https://{$SITE_IP}` 和 ACME issuer（签发器），指定 `profile shortlived`、关闭 TLS-ALPN-01 验证，使用 HTTP-01 路径完成验证。

## 未来生产配置的一致性

`deploy/server.env.example` 只追加注释说明，原有 HTTP 默认值不变。后续获准上线时，应在私密配置中核实以下非敏感项；以下为结构示例，不是可直接运行的环境文件：

```dotenv
SITE_IP=<受控的公网IPv4地址>
ACME_DIRECTORY=https://acme-v02.api.letsencrypt.org/directory
POSTGRES_IMAGE=<原数据库镜像仓库>@sha256:<已经核实的原镜像digest>
APP_ENV=production
APP_ORIGIN=https://<与SITE_IP相同的公网IPv4地址>
COOKIE_SECURE=true
```

`APP_ENV=production`、HTTPS `APP_ORIGIN` 和 `COOKIE_SECURE=true` 必须一起匹配实际入口。只更换端口或证书配置，不能代替应用运行模式、Origin（源）与安全 Cookie 的一致性检查。原数据库密码、`DATABASE_URL`、OSS 和模型凭据继续保留在私密配置中，不写进示例、命令输出或验证记录。

Let’s Encrypt 的短期 IP 证书有效期为 160 小时。首次签发和后续自动续期都依赖持续控制该公网 IP、公共 TCP 80 可达，以及可写且持久的 Caddy 存储。TCP 443 用于 HTTPS 业务访问。容器启动或首次签发成功均不足以证明续期可靠；真实签发、外网可达、浏览器公开信任和自动续期需在后续授权阶段分别验收。

## 本机复现

先在本项目根目录打开 PowerShell。配置解析测试只需要 Docker Compose 客户端；构建与隔离运行另外需要本机 Docker daemon（守护进程）。Python 使用现有 `backend/.venv`，不使用全局 Python。

### 1. 解析实际 Compose 文件

```powershell
& .\backend\.venv\Scripts\python.exe .\tests\test_https_deployment.py -v
```

测试在仓库外的临时 project directory（项目目录）生成假 `.env.server`，显式指定该文件，并清理可能覆盖同名变量的继承环境。它通过真实 `docker compose config --format json` 解析仓库配置，不读取真实 `.env` 或 `.env.server`，不访问 daemon，也不创建容器。

预期检查涵盖原 HTTP 配置、两种旧端口差异的完整替换、数据库镜像 digest 和 `pgdata` 身份、独立证书卷、必填变量缺失或为空时拒绝，以及 web 不接收数据库、OSS 或模型密钥。只有实际输出 `OK` 且没有跳过测试，才可将该层登记为通过。

### 2. 构建本机前端镜像

```powershell
docker build --tag ai-notes-ip-https-local:verified ./frontend
docker build --tag ai-notes-ip-https-python:verified ./backend
```

镜像仅用于本地验证，不自动推送到镜像仓库。第二个镜像仅为验证工具提供Python/OpenSSL，不启动API或Worker。构建可能按现有Dockerfile获取锁定依赖；不进行ACME申请或访问模型、OSS服务。构建成功只证明镜像构建完成，仍需下面的运行验证。

### 3. 运行隔离验证工具

```powershell
& .\backend\.venv\Scripts\python.exe .\scripts\verify_https_isolated.py --frontend-image ai-notes-ip-https-local:verified --python-image ai-notes-ip-https-python:verified
```

也可通过`--python-image`复用已有、满足要求的Python/OpenSSL镜像。验证工具不自动pull（拉取）或build（构建）缺失镜像；需要的镜像须事先准备。

CLI（命令行工具）不加载真实环境文件。它只管理自己随机命名的测试资源，失败时报告固定错误码，并清理自己创建的资源，不清理现有应用、业务卷或其他测试项目。判定通过须同时看到JSON的`status=ok`及`cleanup=passed`；失败时保留固定错误码排查，不能仅根据进程启动判断通过。

工具按Docker实际优先级解析本机上下文，拒绝SSH/TCP远程引擎，并固定后续所有命令的本机端点。镜像须已缓存，运行时固定核验后的不可变image ID；声明未管理持久卷的镜像会被拒绝。仅白名单夹具和Caddy配置复制进随机标记卷，不挂载全仓库或Docker socket；测试项目不发布任何宿主机端口。

生产配置解析和测试运行中的Caddy进程只保留`NET_BIND_SERVICE`能力，以兼容官方二进制文件能力及80/443监听；没有使用privileged。临时证书生成助手以容器root写入受控测试卷，仍禁网络且删除其他能力；模拟API和探测客户端沿用Python镜像默认非root用户。

## 隔离验证能证明什么

生产 `deploy/Caddyfile.https` 的实际 `caddy adapt` / `caddy validate` 在 `--network none` 的容器中运行，使用测试值与临时存储。该层用于确认 Caddy 版本、配置语法、IP 匹配、ACME `shortlived` 配置和路由结构；网络关闭，因此不会完成真实证书申请。

运行测试另用临时 CA（证书颁发机构）、测试证书与模拟 API，上游和客户端都位于内部隔离网络，不发布主机端口。测试根证书只供测试客户端使用，不安装到系统信任库。测试过程不向 ACME 申请证书，不调用真实模型或 OSS。

临时 CA 的成功握手只证明本机 TLS 与代理行为。它不证明公网 IP 控制权、真实 HTTP-01 可达、Let’s Encrypt 正式签发、浏览器公开信任或续期成功，测试证书也不能用作公网验收证据。新增 CI 门禁同样需要实际执行结果；本页不声称 GitHub 检查已经通过。

传输测试使用真实前端镜像和HTTP/1.1客户端，运行时只替换证书提供方式并关闭自动签发，完整HTTP路由、SNI策略和超时保持与生产解析结果一致。上游为模拟API，因此20/50MiB哈希、鉴权头透传、64秒GET SSE与Last-Event-ID重连证明代理传输，不代表真实账号、数据库文件处理或模型生成全链路验收。真实应用的HTTPS Origin、Bearer/CSRF、Secure Cookie及独立退出在隔离PostgreSQL测试中单独验证。

纯工具安全回归可独立执行，CI也显式收集这一目录：

```powershell
& .\backend\.venv\Scripts\python.exe -m unittest discover -s tests/https_fixtures -p "test_*.py" -q
```
