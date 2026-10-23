# Ubuntu 服务器开发联调与运维

适用范围：Ubuntu 24.04、2 vCPU/4 GiB ECS、杭州 ACR 个人版、Compose、私有 OSS。个人版仅用于开发测试。本手册当前提供 SSH 隧道下的私有联调，不表示公网生产上线或完整 M1 验收通过。

Windows 本机继续使用原有开发方式。下文除明确标为 Windows 的命令外，均在 Ubuntu 的 root SSH 会话执行。命令失败时停在该步骤，保留错误输出，不继续迁移或启动新版本。

## 已完成与未完成

2026-10-20 状态校正：依据维护者 2026-10-15《开发协作交接》第 3 节，旧版镜像发布、Ubuntu 部署、OSS 图片链路、重启持久化、跨账号隔离及手动备份 / 隔离恢复已有历史验收。本轮没有查询现网，也未发布新增 AI / PDF 功能；当前状态统一见 [M1 计划](M1开发与云端验收计划.md)。下文是操作参考，不能因再次阅读而重复初始化现有服务器。

- Ubuntu、Docker、Compose 已由用户实机核实。
- `/opt/ai-notes` 已通过 GitHub SSH 443 和只读部署密钥克隆。
- ACR 杭州个人版及 `ai-notes/backend`、`ai-notes/frontend`、`ai-notes/postgres` 三个私有仓库已创建。
- 用户报告 ACR **公网**域名登录成功；**内网**域名曾返回认证失败，当前配置使用公网域名。公网同地域拉取速度仍需镜像发布后实测。
- 最后交接记录的运行提交为 `1948ce2393ac47b9f6cfa278befcd8e25ec22ed5`，不是本轮新增功能；当前运行版本须在实际更新前另行核对。
- 待执行的是新增功能的发布与云端恢复验收、定时备份、外网访问及域名 HTTPS。原维护者 `.local/ops/` 的备份脚本未随 Fork 提供；本轮新增的 [整站备份与隔离恢复工具](整站备份与隔离恢复.md) 已通过本机多账号演练，真实 OSS 与服务器连接方式按用户决定待商榷。下文原有数据库命令不等于完整 OSS 对象导出。
- 仓库 web 默认只绑定回环。交接记录中的服务器工作副本曾调整为 `0.0.0.0:8080:80`；执行更新前须核对并保留已有未提交配置，不据此认定公网已验收。

## 一、GitHub 配置镜像发布凭据

在仓库 Settings → Secrets and variables → Actions 配置：

| 类型 | 名称 | 值 |
|---|---|---|
| Repository variable | `ACR_REGISTRY` | ACR 控制台显示的公网实例域名，不含协议、命名空间或路径 |
| Repository secret | `ACR_USERNAME` | ACR 访问凭证页显示的完整登录名 |
| Repository secret | `ACR_PASSWORD` | 在 ACR 设置的固定密码 |

Secret 值由用户直接填写到 GitHub；不发聊天、截图，不加入 Git。工作流只使用这份镜像仓库凭据，不需要 OSS Key、阿里云 AccessKey 或服务器 SSH 私钥。首次联调可沿用已验证的 ACR 账号凭据；后续正式方案应使用与操作范围匹配的独立授权。

当前公网域名：`crpi-b9j6kbtluavj6v3w.cn-hangzhou.personal.cr.aliyuncs.com`。以控制台实际复制值为准。公网与内网域名的 Docker 登录凭据分别保存；当前不要仅把域名改成 `-vpc` 后假定可以使用。

代码更新通过 `git pull --ff-only`；它同步代码与部署文件。镜像需要通过下面的发布流程单独构建和推送。

## 二、发布三份镜像

1. 将经过审查的代码推送到 GitHub `main`，等待 Project checks 通过。
2. 仓库 Actions → **Publish development images to ACR** → **Run workflow**，分支选择 `main`。
3. 工作流再次验证同一提交，再构建 Linux amd64 后端和前端，复制项目当前使用的官方 `postgres:16.8-alpine` 到 ACR。
4. 完成后复制 Summary 中的 **Commit** 和 **RELEASE_TAG**。发布标签格式为 `sha-完整提交号-运行号-重跑号`，三份镜像共用标签。Summary 也记录镜像 digest 和 PostgreSQL 上游来源。
5. 只有整次发布任务成功，三仓库对应标签都存在，才执行部署。某个仓库推送失败时不使用该次未完成发布。

发布工作流仅手动执行，不连接 ECS、不修改服务器数据库。它沿用当前已验证的 PostgreSQL/Caddy 基础版本；正式生产前还需独立核对补丁版本与安全更新，不将这轮开发联调配置宣称为生产加固完成。

## 三、首次获取发布与创建私密配置

```bash
cd /opt/ai-notes
git status --short --branch
git pull --ff-only
git log -1 --oneline
```

若已有未提交修改，先分析来源，不执行 reset 或覆盖。核对 GitHub 成功发布的 Commit 与服务器当前提交一致。若 main 在构建期间又有新提交，先为最新提交重新完成发布，保持代码和应用镜像对应。

使用发布 Summary 中的真实标签替换下面的示例参数：

```bash
python3 scripts/init_server_env.py \
  --registry crpi-b9j6kbtluavj6v3w.cn-hangzhou.personal.cr.aliyuncs.com \
  --release-tag '从成功发布的Summary复制RELEASE_TAG值'
```

脚本只使用标准库，以 0600 权限排他创建 `.env.server`，自动生成数据库密码。重复执行不会读取或覆盖已有配置。配置已被 Git 忽略，不应上传。

用服务器编辑器打开 `.env.server`，自行填写 `OSS_ACCESS_KEY_ID`、`OSS_ACCESS_KEY_SECRET`；长期 AccessKey 的 `OSS_SECURITY_TOKEN` 保持空。沿用 Bucket 所属账号的受限 OSS 用户。不要把整个文件输出到终端截图或聊天。必要时先使用本机安全传输方式保存备份；不复制真实值到示例文件。

当前固定：`APP_ENV=development`、`APP_ORIGIN=http://127.0.0.1:8080`、`COOKIE_SECURE=false`、`STORAGE_BACKEND=oss`。这与 SSH 隧道访问方式配套。正式 HTTPS 部署仍须按应用现有生产校验单独配置。

定义当前 SSH 会话内的 Compose 命令数组，然后只做不输出解析内容的检查：

```bash
DC=(docker compose --env-file .env.server -f compose.server.yaml)
"${DC[@]}" --profile tools config --quiet
```

不要使用普通 `docker compose config` 输出整份配置，它包含解析后的数据库密码和 OSS 凭据。

## 四、首次拉取和启动

服务器先使用 ACR 控制台原始公网 `docker login` 命令交互登录，密码不放在命令行中。已成功登录且凭据未变时可沿用。

```bash
"${DC[@]}" --profile tools pull
```

此时三个镜像均从 ACR 获取，不在服务器构建，不访问 Docker Hub。拉取成功后再启动数据库：

```bash
"${DC[@]}" up -d --wait db
```

数据库健康后显式执行一次迁移；失败就停在这里：

```bash
"${DC[@]}" run --rm migrate
```

迁移成功后启动长期服务：

```bash
"${DC[@]}" up -d --wait api worker web
"${DC[@]}" ps
curl --fail --silent --show-error http://127.0.0.1:8080/api/v1/health/ready
"${DC[@]}" exec api python -m app.cli create-admin --username admin
```

管理员命令交互输入密码；服务器数据库是独立的新数据库，本机开发管理员不会自动迁移过去。Worker 显示 running 只能证明进程运行，必须再通过网页后台自检和图片处理结果核验任务执行。

## 五、Windows 本机通过 SSH 隧道访问

在 Windows **另一个本机 PowerShell 标签页**执行，保留普通服务器会话：

```powershell
ssh -N -L 127.0.0.1:8080:127.0.0.1:8080 -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -i "C:\Users\32159\Downloads\ai_note的key\_ai-notes-server.pem" root@47.111.12.85
```

该窗口不显示命令提示符属于隧道运行中的正常状态，保持打开。浏览器访问 **http://127.0.0.1:8080**，与 APP_ORIGIN 完全匹配；关闭此 SSH 窗口会断开隧道。若本机 8080 已被占用，先检查占用情况，不能只改浏览器端口而漏改 APP_ORIGIN。

仓库默认的隧道联调配置不向公网映射数据库、API 或网页端口；服务器已有 web 端口差异须按顶部记录核对。本节隧道步骤不要求新增 ECS 入站规则，也不代替域名、备案或公开 HTTPS 验收。

## 六、后续更新与备份

每次只使用成功发布的标签，维护窗口内用户暂停编辑。以下流程保留 `.env.server` 中现有数据库密码。

1. 在 GitHub 确认目标提交及其发布已成功，记录目标 Commit 和 RELEASE_TAG。服务器执行 `git status --short --branch`，确认没有未处理修改，先保留当前代码以生成旧版本备份。
2. 停止写入进程，创建私密备份。下面的子 shell 发生错误会停止，不会继续打印成功信息：

```bash
bash <<'BASH'
set -euo pipefail
cd /opt/ai-notes
umask 077
DC=(docker compose --env-file .env.server -f compose.server.yaml)
"${DC[@]}" stop api worker
backup_dir=".backups/$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$backup_dir"
cp .env.server "$backup_dir/server.env"
git rev-parse HEAD > "$backup_dir/checkout-commit.txt"
"${DC[@]}" config --images > "$backup_dir/images.txt"
"${DC[@]}" images --format json > "$backup_dir/running-images.json"
"${DC[@]}" exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$backup_dir/database.dump"
test -s "$backup_dir/database.dump"
"${DC[@]}" exec -T db pg_restore --list < "$backup_dir/database.dump" > /dev/null
printf 'Backup prepared: %s\n' "$backup_dir"
BASH
```

备份失败时保持服务暂停，先处理原因或按旧配置恢复服务。此备份包含秘密配置，必须以受限权限保存；数据库备份不是 OSS 文件备份。现阶段原图与历史版本继续保留在 OSS，完整导出及恢复闭环按 M1 待办验收。

3. 备份成功后执行 `git pull --ff-only`，核对服务器 Commit 与成功发布的 Commit 一致。若当前处于回退后的 detached HEAD，先在工作区干净时执行 `git switch main`，再拉取。随后在 `.env.server` **只更新 `RELEASE_TAG`** 为新成功发布标签，保留原有密码和 OSS 配置。重新定义 `DC` 后依次执行，各步骤成功再进入下一块：

```bash
DC=(docker compose --env-file .env.server -f compose.server.yaml)
"${DC[@]}" --profile tools config --quiet
"${DC[@]}" --profile tools pull
```

```bash
"${DC[@]}" run --rm migrate
```

```bash
"${DC[@]}" up -d --wait db api worker web
"${DC[@]}" ps
curl --fail --silent --show-error http://127.0.0.1:8080/api/v1/health/ready
```

4. 浏览器验证登录、笔记保存、历史版本；按明确授权的小范围测试验证 OSS 上传、Worker 派生图及代理读取。失败时保留日志和发布标签，按下一节回退，不自动重试有费用的外部调用。

数据库镜像与应用镜像共享发布标签，更新过程中 DB 服务可能因新标签重建，命名卷保留。本轮 PostgreSQL 版本固定为 16.8；跨主版本升级需要专门计划。更新后的 `.env.server` 不应修改原有 POSTGRES_PASSWORD，修改变量不会自动更新既有数据库卷内的角色密码。

## 七、回退与隔离恢复演练

若没有执行不兼容迁移，可先暂停 API/Worker，核对所选备份的 `checkout-commit.txt` 和 `server.env` 中的旧发布标签。工作区干净时用 `git switch --detach 旧提交号` 恢复对应部署文件，再把当前 `.env.server` 的 `RELEASE_TAG` 改回旧标签，拉取及启动旧镜像、执行健康和功能检查。不要覆盖其他私密配置或清除现有修改。下次更新先回到 main；旧镜像、旧标签与备份必须在新版本验收后再按保留策略清理。

**应用镜像回退不会撤销数据库迁移。** 迁移后应先判断旧代码是否兼容当前结构；不自动执行 Alembic downgrade。若需要数据库恢复，先保留当前数据库和 OSS 状态并确认维护窗口与新写入影响，不能直接把旧备份覆盖到仍在使用的业务库。

首次恢复演练使用全新的 Compose 项目和命名卷，保持业务环境运行。下面的 `database.dump` 路径由用户选择已有备份：

```bash
RESTORE_PROJECT="ai-notes-restore-$(date -u +%Y%m%d%H%M%S)"
RC=(docker compose -p "$RESTORE_PROJECT" --env-file .env.server -f compose.server.yaml)
"${RC[@]}" up -d --wait db
```

下一块仅在确认 `RESTORE_PROJECT` 是新建恢复项目后执行：

```bash
"${RC[@]}" exec -T db sh -c 'pg_restore --exit-on-error --no-owner -U "$POSTGRES_USER" -d "$POSTGRES_DB"' < .backups/所选备份目录/database.dump
"${RC[@]}" exec -T db sh -c 'psql -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB" -c "SELECT version_num FROM alembic_version; SELECT count(*) FROM users;"'
```

这只验证备份可读、迁移版本和基础数据恢复；笔记、页面、所有者关系及 OSS 对象完整性需进一步核对，不能据此宣称完整恢复验收。检查后可用 `"${RC[@]}" down` 停止恢复项目，保留其独立命名卷供审查。禁止对业务项目执行 `down -v`。

## 八、排查和当前边界

```bash
"${DC[@]}" ps
"${DC[@]}" logs --tail=100 api
"${DC[@]}" logs --tail=100 worker
"${DC[@]}" exec api python -m alembic current
```

提交问题前检查日志，去除真实凭据、用户内容和 Cookie；不要输出 `.env.server` 或 `/root/.docker/config.json`。没有 sudo 的普通用户访问 Docker socket 会获得等同 root 的主机控制能力，新增 Docker 用户组成员需单独审查。

当前没有自动升级、后台定时部署、云资源自动购买或自动清理旧备份。AI 模型功能与完整 M1 验收继续按照主计划推进。

## 依据

- [GitHub 部署密钥](https://docs.github.com/en/authentication/connecting-to-github-with-ssh/managing-deploy-keys)、[SSH 443 端口](https://docs.github.com/en/authentication/troubleshooting-ssh/using-ssh-over-the-https-port)。
- [ACR 个人版边界](https://help.aliyun.com/zh/acr/user-guide/create-a-container-registry-personal-edition-instance/)、[个人版推送拉取](https://help.aliyun.com/zh/acr/user-guide/use-a-container-registry-personal-edition-instance-to-push-and-pull-images)。
- [Compose profiles](https://docs.docker.com/compose/how-tos/profiles/)、[生产部署注意事项](https://docs.docker.com/compose/how-tos/production/)。
