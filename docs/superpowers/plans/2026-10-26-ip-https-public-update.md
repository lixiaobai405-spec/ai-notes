# 公网IP HTTPS与版本更新实施计划

> **For agentic workers:** 使用 subagent-driven-development 或 executing-plans，按任务清单推进。用户随后明确授权Task 2和Task 3，以及Task 4兼容工具的本地实现/隔离验证；未授权提交、推送、证书申请、云安全组修改、服务器写入、真实OSS读写或部署。

**Goal:** 在不购买或绑定域名的前提下，使现有服务通过浏览器公开可信的`https://47.111.12.85`访问，并安全发布本地待交付功能。

**Architecture:** 升级现有前端Caddy，继续一个入口提供SPA和同源API；显式申请Let’s Encrypt短期IP证书，持久化证书与ACME账户。开发HTTP配置保留；生产使用独立Compose覆盖文件，准确替换公网端口。数据库镜像、命名卷和私密凭据保留，通过独立迁移步骤更新schema。

**Tech Stack:** Ubuntu 24.04、Docker Compose 5.6.0（现网）、Caddy 2.11.7、Let’s Encrypt ACME `shortlived`、FastAPI、PostgreSQL、GitHub Actions与现有ACR。

状态：**本地配置与隔离验证已完成；真实签发、发布和迁移尚未执行**。现状历史见[服务器核对](../../task-evidence/2026-10-26-https-server-audit.md)，本轮实现见[本地验证](../../task-evidence/2026-10-26-ip-https-local.md)。用户已明确短期不绑定域名，不再把域名作为前置条件。

## Task 1：锁定现状与方案

- [x] 核对现网代码、镜像revision/数据库实际digest、配置差异、卷名、健康和端口。
- [x] 核对GitHub main为`587ba362bf95b2158c772bfda0cc54fcf7a6fe8d`且该提交CI成功；区分本地尚未提交功能。
- [x] 核对当前schema `c2e41a9f3b67`与本地head `2ef16e7ca892`的差异。
- [x] 确认公开可信IP证书可行、准确寿命160小时；不沿用“IP只能自签”这一过时结论。

选择比较：

| 方案 | 取舍 |
|---|---|
| **推荐：升级现有前端Caddy，单一入口** | 本轮前端本就要发布；无需新常驻服务、额外证书客户端或二层代理信任配置。需验证新版Caddy的上传/流式行为及证书存储 |
| 独立edge Caddy代理现有web | TLS可单独回退，但增加一个镜像、代理层和真实IP/HTTPS协议转发链；现有HTTP web仍要维护 |
| 保留旧Caddy，额外Certbot | 官方支持IP签发，但webroot需Certbot>=5.4，并增加续期任务和成功后reload钩子；当前运维面更大 |

没有选择自签证书作为正式浏览器入口，也不要求用户购买域名。公开可信IP证书需要持续控制该公网IP；IP变化时重新签发并更新Origin。证书到期前必须自动完成续期，失败时不能无声退回HTTP或内部证书。

## Task 2：本地实现HTTPS部署配置

**计划修改/新增：** `frontend/Dockerfile`、`deploy/Caddyfile.https`、`compose.https.yaml`、`deploy/server.env.example`、`tests/test_https_deployment.py`、CI配置检查及部署文档。保留`frontend/Caddyfile`的本地HTTP用途，不修改已有应用API、会话设计或私密环境文件。

- [x] 前端基础镜像固定为`FROM caddy:2.11.7-alpine`，本机镜像构建已验证；ACR发布仍未执行，不在ECS现场构建。
- [x] 新增生产Caddy配置，保留SPA fallback、现有安全响应头与同源API；默认不公开交互文档。以下配置已在禁网络容器真实adapt/validate；临时CA运行验证不代表真实ACME签发：

```caddyfile
{
  default_sni {$SITE_IP}
}

https://{$SITE_IP} {
  tls {
    issuer acme {
      dir {$ACME_DIRECTORY}
      profile shortlived
      disable_tlsalpn_challenge
    }
  }
  handle /api/* {
    reverse_proxy api:8000
  }
  @apiDocs path /docs /docs/* /redoc /redoc/* /openapi.json
  handle @apiDocs {
    respond 404
  }
  handle {
    root * /srv
    try_files {path} /index.html
    file_server
  }
  header {
    X-Content-Type-Options nosniff
    X-Frame-Options DENY
    Referrer-Policy same-origin
  }
}
```

显式ACME issuer避免IP默认走内部CA；`default_sni`覆盖Docker端口映射下客户端不发送SNI（服务器名称指示）的情况；固定HTTP-01验证需要公网TCP 80。不额外配置代理缓存、请求缓冲或缩短上传/SSE时间限制。

- [x] 新增Compose覆盖文件，本地Compose 5.5.1实际解析通过；CI已接入但尚未推送运行。`!override`避免默认端口合并后继续暴露旧8080：

```yaml
services:
  db:
    image: ${POSTGRES_IMAGE:?pin the existing verified database image}
  web:
    ports: !override
      - "80:80"
      - "443:443"
    environment:
      SITE_IP: ${SITE_IP:?set the public IPv4 address}
      ACME_DIRECTORY: ${ACME_DIRECTORY:?set the ACME directory}
    volumes:
      - ./deploy/Caddyfile.https:/etc/caddy/Caddyfile:ro
      - caddy_data:/data
      - caddy_config:/config
volumes:
  caddy_data:
  caddy_config:
```

目标Compose项目名仍为`ai-notes-server`，数据库卷仍是`ai-notes-server_pgdata`。覆盖文件锁定现有DB镜像，不把应用更新顺带变成数据库镜像更新。容器80/443映射到宿主机标准端口；API/DB/Worker不新增公网映射，不把Docker socket挂入应用。HTTP/3的UDP 443本次不作为依赖。

- [x] 示例仅增加注释中的非敏感变量，原HTTP默认值保持；未来私密配置的精确变更如下：

```dotenv
SITE_IP=47.111.12.85
APP_ENV=production
APP_ORIGIN=https://47.111.12.85
COOKIE_SECURE=true
ACME_DIRECTORY=https://acme-v02.api.letsencrypt.org/directory
POSTGRES_IMAGE=crpi-b9j6kbtluavj6v3w.cn-hangzhou.personal.cr.aliyuncs.com/ai-notes/postgres@sha256:03808cfa25a692ac94353f108736b5d4c0d812c1bdcba774d70d63388c0335c8
```

未来仅在服务器保留已有数据库密码、连接串、OSS、模型配置，增加上述非敏感键并更新目标`RELEASE_TAG`。不得重跑初始化脚本或把当前秘密复制到示例。`STORAGE_BACKEND=oss`已满足生产存储要求，实际权限仍需单独验收。

## Task 3：本地隔离验证与发布门禁

- [x] 使用仓库外假环境文件真实解析Compose；6项测试确认仅80/443、服务不额外公开、DB镜像/卷保留、证书卷独立、web无秘密。
- [x] 禁外网容器实际验证Caddy版本和生产配置adapt/validate，确认唯一LE/shortlived、IP/default_sni；未签发真实证书。
- [x] 临时CA/模拟上游验证无SNI IP TLS、SPA/资源、Origin/Bearer/CSRF头透传、伪造转发头清理、20/50MiB合成数据完整传输、64秒GET SSE及重连；应用HTTPS认证/CSRF/Secure Cookie另有隔离数据库8项回归。临时根不安装系统。此处不声称真实账号经代理全链路、有效图片/PDF入库或公网限流实测。
- [x] 后端560、前端80、根工具298项（292通过/6平台跳过）、镜像构建与Alembic check通过，既有契约/依赖按指纹保持；隔离工具安全回归另10项。CI配置已接入但远端未运行。
- [ ] 审阅最终diff并获授权后才提交推送；逐项暂存选定功能和部署文件，不包含`AGENTS.md`、密钥、真实环境、证书/ACME账户、备份、测试输出或临时脚本。
- [ ] 等目标提交Project checks通过，再获授权触发现有ACR发布；记录同一提交、完整RELEASE_TAG与三份镜像digest。当前`587ba36…`的绿色CI不能覆盖未提交HTTPS和前几轮功能。
- [ ] 发布前写出回退收据并演练：目前**没有已验收的独立HTTPS前端回退镜像**。已知旧组合是下表所列HTTP版本；若维护窗口批准包含回退该组合，必须连同旧入口/Origin和旧schema一致恢复，不只切前端。若未批准HTTP回退且新镜像本身失败，保持维护状态，不能承诺存在可直接切换的旧HTTPS入口。

| 已知旧组合 | 精确恢复依据 |
|---|---|
| API / Worker / SPA | revision `cb60fee9a61c603e9eaee4ec62efb2c325573c63`，标签 `sha-cb60fee9a61c603e9eaee4ec62efb2c325573c63-39941913800-1`；实际image ID见现状证据 |
| 入口 | 原前端镜像包含Caddy 2.9.1，原Compose映射`0.0.0.0:8080:80`；不是HTTPS回退产物 |
| 配置 | 本次维护前私密副本，原`APP_ENV=development`、`APP_ORIGIN=http://47.111.12.85:8080`、`COOKIE_SECURE=false`；恢复时保留原密码/OSS配置 |
| 数据库 | 本次维护前`c2e41a9f3b67`数据库恢复点、原数据库镜像digest与`ai-notes-server_pgdata`身份；不能用10月23日旧数据冒充本次快照 |
| 对象 | 同一个维护恢复点的OSS对象副本/经演练的版本读取，不能只有对象清单 |

窗口批准时一起明确“失败且尚未开放新写入，是否允许恢复上述旧HTTP组合”，避免故障时临时猜测。恢复已产生新写入的数据库仍须另行说明数据损失并确认。

## Task 4：旧schema备份与恢复门禁

**真实恢复点必须在公网迁移之前完成。** 整站工具现已增加已知旧版本的显式严格配置档，默认仍拒绝旧表集合/head不匹配；不绕过校验，也不先迁移源库以适配备份。见[兼容操作说明](../../公网旧版数据库备份与恢复.md)与[本地隔离证据](../../task-evidence/2026-10-26-legacy-site-backup.md)。

- [x] 本地实现并隔离验证兼容`c2e41a9f3b67`的备份路径：显式`--schema-revision`、冻结22表/161列结构、同一只读快照的`pg_dump -Fc`及指纹、旧文件/变体对象字节与哈希。Local与真实OSS适配器的Fake客户端通过；只保存清单不算完整备份。
- [x] 本地新空子库与独立对象目录恢复，核对全部原始行指纹、所有者、密码、历史/回收站、页面与对象，再归一化目标运行态；CLI及拒绝/损坏路径覆盖。此项不代表真实云端数据或真实OSS恢复。
- [x] 在本地隔离恢复副本真实演练四段迁移：`c2e41a9f3b67 → d89f74a2136c → 16bd16083e39 → 63b9d8343e02 → 2ef16e7ca892`，旧业务列和对象保持、新表初始为空，源仍c2。验证与生产数据规模不同。
- [ ] 在获准维护窗口使用现网当次DB与真实OSS对象副本完成独立目标恢复，重新核对schema、所有者、笔记/页面、对象及完整迁移；记录实际恢复用时与快照时点。不能用2026-10-23旧备份或本地合成数据代表今天恢复点。真实OSS权限、读写/费用及新桶恢复范围须执行前说明并确认。
- [ ] 发布前新建当天私密恢复点，维护窗口停止写入进程；保存旧Compose原文件/patch、提交号、实际镜像ID/digest、当前非敏感配置摘要，并在私密位置保存环境配置副本。原数据库密码与OSS配置不改变，所有数据文件0600、目录0700。

下列仅是未来维护窗口中的**数据库部分**命令；备份目标动态生成且不覆盖已有目录，失败时停止后续发布。它不能替代OSS导出或恢复演练：

```bash
set -euo pipefail
cd /opt/ai-notes
umask 077
OLD_DC=(docker compose --env-file .env.server -f compose.server.yaml)
"${OLD_DC[@]}" stop api worker
backup_dir=$(mktemp -d /opt/ai-notes/.backups/ip-https-XXXXXXXX)
chmod 700 "$backup_dir"
cp .env.server "$backup_dir/server.env"
cp compose.server.yaml "$backup_dir/compose.server.yaml"
git diff --binary -- compose.server.yaml > "$backup_dir/compose-local.patch"
git rev-parse HEAD > "$backup_dir/checkout-commit.txt"
"${OLD_DC[@]}" images --format json > "$backup_dir/running-images.json"
"${OLD_DC[@]}" exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_dir/database.dump"
test -s "$backup_dir/database.dump"
"${OLD_DC[@]}" exec -T db pg_restore --list < "$backup_dir/database.dump" > /dev/null
sha256sum "$backup_dir/database.dump" > "$backup_dir/database.dump.sha256"
chmod 600 "$backup_dir"/*
```

备份失败时保留现场，按发布前记录恢复原API/Worker运行状态，不能继续迁移；本轮计划未执行上述停止或备份。完整恢复点未验证前，公网迁移保持未开始。

## Task 5：公网IP证书预检

- [ ] 确认公网IP稳定且由本机控制；在阿里云控制台核对/批准TCP 80和443入站、保持SSH原规则与API/DB不公开。本轮80/443外部超时，尚未查安全组，不能直接假定放通。
- [ ] 在不接触DB和原8080应用的独立临时TLS测试项目中，使用同版本Caddy、仅响应测试页、独立staging证书卷及`https://acme-staging-v02.api.letsencrypt.org/directory`，核对HTTP-01可达、证书IP SAN与无SNI握手。此步会真实联系CA，执行前取得授权。
- [ ] 测试CA证书不受浏览器公开信任；正式证书使用生产ACME目录及不同的生产证书卷，不能把staging卷挂进正式入口。停止测试项目释放80/443后再正式启动，不删除生产卷。
- [ ] 短证书续期依赖持续可达的HTTP-01和可写持久`/data`；应用镜像替换、重启与回滚均保留证书/ACME账户卷，避免重复开户/签发和限额问题。

## Task 6：批准维护窗口后的版本切换

- [ ] 先完成目标镜像预拉取与上述备份/恢复门禁。用户在旧入口确认所有草稿已同步或另存；HTTPS入口属于新Origin，所有标签需重新登录，未同步localStorage草稿不能自动迁入。
- [ ] 保留服务器当前Compose原文件/patch后，审阅并处理这一个受控端口差异，再同步到**已成功发布的精确提交**；不对dirty服务器直接pull，不执行reset/clean。不得覆盖未知差异；若出现新的差异先重新核对。
- [ ] 更新私密配置中目标RELEASE_TAG及Task 2列出的非敏感键，保持密码、OSS和模型配置；数据库实际镜像由POSTGRES_IMAGE固定，不执行数据库镜像升级。
- [ ] 使用以下唯一生产命令数组；先仅验证解析，禁止输出完整`config`或环境：

```bash
set -euo pipefail
cd /opt/ai-notes
DC=(docker compose --env-file .env.server -f compose.server.yaml -f compose.https.yaml)
"${DC[@]}" --profile tools config --quiet
"${DC[@]}" --profile tools pull api worker web migrate
```

- [ ] 备份成功、原API/Worker已停止且db仍健康后，显式迁移；任一步失败都不启动新写入进程：

```bash
(
  set -euo pipefail
  cd /opt/ai-notes
  DC=(docker compose --env-file .env.server -f compose.server.yaml -f compose.https.yaml)
  "${DC[@]}" --profile tools config --quiet
  if ! "${DC[@]}" run --rm --no-deps migrate; then
    printf '%s\n' 'Migration failed; application services remain stopped.' >&2
    exit 1
  fi
  "${DC[@]}" up -d --wait --no-deps api worker web
)
```

`--no-deps`防止应用更新隐式重建现有数据库；仍须人工先核对db健康、网络、镜像和原卷。新前端正式映射80/443，8080映射移除；安全组中原8080规则在外部HTTPS验证通过后再获准关闭。不能把`up --wait`等同于证书签发与业务验收通过。

- [ ] 从外部网络不跳过证书检查地验证入口和IP身份：

```powershell
# Windows外部客户端；不使用 -k
curl.exe --noproxy "*" --fail --silent --show-error https://47.111.12.85/api/v1/health/ready
curl.exe --noproxy "*" --silent --show-error --head http://47.111.12.85/
```

在有OpenSSL的独立客户端验证无SNI与IP SAN：

```bash
openssl s_client -connect 47.111.12.85:443 -noservername -verify_ip 47.111.12.85 -verify_return_error </dev/null
```

预期：公开CA链验证通过、证书包含IP SAN、HTTP正确跳转HTTPS、443健康200；不将浏览器“忽略证书错误”或curl `-k`结果当作通过。

## Task 7：验收、续期与回退

- [ ] 手机/电脑验证重新登录、两个标签账号独立、保存/刷新/历史、图片和PDF上传/来源定位、后台自检、下载导出及受控越权拒绝。AI SSE先用模拟模型验证；真实共享模型调用需单独授权费用。
- [ ] 验证伪造X-Forwarded-For/Proto不会进入可信客户端身份；API仍只允许内部网络，登录限流不会因代理配置把所有用户折叠为同一个IP。
- [ ] 新版整站备份工具在新schema上重新完整备份/隔离恢复；GC、备份timer、监测timer与通知渠道不随本次应用发布自动启用。
- [ ] 为160小时证书补外部到期检查和失联检测的具体实现与授权；建议剩余48小时警告、24小时严重，失败告警不能只依赖同机。确认Caddy存储持久、重启后不重签；至少观察一次真实自动续期和新序列号/有效期，再登记续期通过。仅首次签发成功不代表续期验收完成。
- [ ] 记录维护实际停机时间、备份时点、恢复演练耗时、切换提交/tag/digest和证书有效期，填写[M1实机与性能验收表](../../M1实机与性能验收记录.md)。

回退按实际阶段选择：

| 阶段 | 操作与边界 |
|---|---|
| 迁移前任一门禁失败 | 停止后续操作，保留现网代码/配置和db；若已暂停写入，使用旧Compose及实际旧镜像恢复原API/Worker。staging测试项目可停止，保留日志 |
| 迁移成功、新业务尚未重新开放 | 配置问题可恢复已验收的HTTPS配置，保留支持IP证书的Caddy版本与已验证前后端组合；新镜像本身失败时没有现成旧HTTPS镜像可切。若窗口已批准HTTP回退，恢复Task 3精确旧组合及维护前DB+对象一致副本；否则保持维护状态。不能把旧Caddy 2.9.1直接当作IP HTTPS回退目标，也不能假定旧代码兼容迁移后的schema |
| 新版本已产生业务写入 | 先暂停并保留当前新状态，说明恢复旧快照会损失哪些新增数据，再另行确认；不自动覆盖数据库或对象，不自动downgrade |
| 仅证书/续期异常 | 保留/data、原证书和ACME账户，查80可达/存储/CA错误；允许恢复已验证HTTPS配置，不自动退回公网明文。确需恢复原HTTP入口时作为明确临时回退另行确认，并同步旧Origin/development配置 |

所有阶段都禁止`down -v`、删除业务卷、覆盖已有秘密、强制Git回滚或清理OSS对象。整个切换保留`ai-notes-server_pgdata`；任何销毁/覆盖恢复需单独批准。

## 官方依据与当前限制

- [Let’s Encrypt公开IP与160小时证书](https://letsencrypt.org/2026/01/15/6day-and-ip-general-availability)、[IP验证类型](https://letsencrypt.org/2025/07/01/issuing-our-first-ip-address-certificate/)。
- [Caddy 2.11.7稳定发行](https://github.com/caddyserver/caddy/releases/tag/v2.11.7)、[Docker官方镜像标签](https://github.com/docker-library/official-images/blob/master/library/caddy)。该版修复此前POST流式响应/HTTP2问题，仍要测试本应用长流。
- [Caddy ACME/profile配置](https://caddyserver.com/docs/caddyfile/directives/tls#acme)、[default_sni](https://caddyserver.com/docs/caddyfile/options#default_sni)、[固定版本无SNI集成测试](https://github.com/caddyserver/caddy/blob/v2.11.7/caddytest/integration/sni_test.go)。
- [Certbot IP备选方案](https://letsencrypt.org/2026/03/11/shorter-certs-certbot/)。

本轮已完成明确授权的本地配置与隔离验证，未连接服务器，也未进行staging/生产签发、80/443安全组变更、真实备份、迁移或发布。上述未完成项保留复选框，后续完成后按证据逐项更新；本地临时CA与模拟API不能替代公网签发、续期或真实业务验收。
