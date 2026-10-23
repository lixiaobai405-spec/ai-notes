# Ubuntu 服务器开发联调与更新计划

> 2026-10-20：本文保留 2026-10-15 准备阶段的任务记录。维护者交接已确认后续完成旧版镜像发布、服务器部署、OSS 图片链路、持久化及手动恢复；下文仍未勾选的历史项不代表从未执行。当前判断与剩余范围统一见 [M1 状态总表](M1开发与云端验收计划.md#m1-状态总表)。本轮不重新初始化服务器，也不将旧版验收计为新增 AI / PDF 的云端通过。

> **For agentic workers:** 按任务清单在当前会话逐项执行；沿用用户指定的 main 工作目录，保留现有未提交修改。提交、推送和真实服务器部署分别依据用户授权执行。

**Goal:** 通过 GitHub 获取代码，由 GitHub Actions 构建并推送杭州 ACR 个人版镜像，在 Ubuntu 24.04 上以 Compose 完成私有开发联调及可回退更新。

**Architecture:** API、Worker 和迁移共用 backend 镜像；frontend 镜像包含静态网页和 Caddy；官方 PostgreSQL 镜像复制到同一 ACR 命名空间。服务器只拉取 ACR 镜像，不在 2 vCPU/4 GiB ECS 上构建。当前网页绑定回环地址，通过 SSH 隧道访问；保持开发模式。免费个人版仅用于开发测试，正式公网生产部署单独验收。

**Tech Stack:** Ubuntu 24.04、Docker Engine/Compose、GitHub Actions、ACR 个人版、PostgreSQL 16、FastAPI、React/Caddy、私有 OSS。

## 已核实的资源

- [x] ECS `47.111.12.85` 已运行 Ubuntu 24.04，root 密钥登录成功。
- [x] Docker Engine 29.9.1、Compose v5.6.0，服务 active。
- [x] GitHub SSH 443 端口及只读部署密钥已通过；仓库位于 `/opt/ai-notes`，服务器目前为初始 M0 提交 `9e94e35`。
- [x] Docker Hub 连接超时；GHCR 入口可连接，实际测试镜像下载因速度慢被 120 秒 timeout 中止，不作为本轮主要分发入口。
- [x] 用户选择免费个人版开发联调；杭州 ACR 命名空间 `ai-notes`，三个私有仓库 `backend`、`frontend`、`postgres` 已创建。
- [x] 用户报告 ACR 公网地址登录 `Login Succeeded`；内网地址此前认证失败，暂不将内网访问记为通过。
- [x] 旧版 ACR 发布与实际部署已有后续交接记录；速度指标和内网认证不据此推定通过。

## Task 1：准备可审查的服务器联调配置

**Files:** 创建 `compose.server.yaml`、`deploy/server.env.example`、`scripts/init_server_env.py`、`tests/test_init_server_env.py`；补充 `.gitignore` 和两个 Docker build context 的秘密文件排除规则。

- [x] 创建独立 Compose 文件，只引用 ACR 三个仓库；数据库/API 不映射宿主机端口，web 仅绑定 `127.0.0.1:8080`。
- [x] 迁移定义为显式运行的一次性工具；为长期进程设置重启、日志轮转及适合 4 GiB ECS 的内存限额。
- [x] 标准库初始化脚本生成随机数据库密码，用排他创建模式写 `.env.server`、权限 0600；不覆盖已有文件、不输出凭据；OSS 密钥由用户在服务器自行填写。
- [x] 新建隔离单元检查：拒绝无效 registry/tag，已有配置不覆盖，密码随机且数据库 URL 一致；Compose 检查无构建、无 Docker Hub 镜像、端口与内存预算正确。6 项通过。

验证命令：

```powershell
backend/.venv/Scripts/python.exe -m unittest discover -s tests -p "test_*.py"
```

## Task 2：配置手动镜像发布工作流

**Files:** 修改 `.github/workflows/ci.yml`；创建 `.github/workflows/publish-acr.yml`。

- [x] 保留 push/PR 检查，增加 workflow_call，补充自动保存和部署配置检查。
- [x] 实现仅允许从 main 手动 workflow_dispatch 的发布配置；先复用完整 CI，再构建 linux/amd64 应用镜像及复制官方 PostgreSQL 镜像。真实云端运行尚未执行。
- [x] 配置仓库变量 `ACR_REGISTRY`、Secrets `ACR_USERNAME` / `ACR_PASSWORD`，通过 password-stdin 登录；不接触 OSS 密钥或服务器私钥。用户已自行添加三项，已核对名称。
- [x] 实现每次运行使用独立的提交号/运行号/重跑号标签；输出提交号、发布标签及三份镜像 digest，保留旧标签支持回退。
- [x] 本地 actionlint 及两份镜像 fresh build、隔离容器启动检查通过；未执行的真实 CI/推送保持未验收。

## Task 3：同步 Ubuntu 操作手册及历史计划

**Files:** 创建 `docs/Ubuntu服务器部署与运维.md`；更新 `README.md`、`docs/M1开发与云端验收计划.md`。

- [x] 文档说明 GitHub SSH 443、ACR 公网/内网区别及个人版开发测试边界。
- [x] 给出 Secrets 配置、手动发布、首次拉取、私密配置、显式迁移、管理员初始化、健康检查、SSH 隧道命令。
- [x] 更新流程包含工作区检查、备份、获取代码、拉取镜像、维护窗口、迁移、更新与验证；迁移失败即停，不继续启动新版本。
- [x] 回退区分镜像回退与数据库恢复；数据库有新写入时禁止直接覆盖恢复。备份恢复演练只使用独立数据库/命名卷，不删除应用卷。
- [x] 将旧计划的 Windows 服务器方案替换为 Ubuntu，保留 Windows 本机开发方式。

## Task 4：验证、审查和后续实机验收

- [x] 前端构建、自动保存回归、部署配置检查通过。
- [x] Docker Desktop 可用后启动隔离测试库，后端迁移/check/68 项回归通过；不调用真实 OSS。本机新建临时容器项目的迁移、代理/网页、管理员登录和 Worker 自检通过，随后移除测试容器。
- [x] 检查秘密文件排除、工作流权限与输出；保留用户既有修改并纳入本次授权范围。检查记录见 `task-evidence/2026-10-15-ubuntu-acr-preparation.md`。
- [x] 已向用户提供具体变更清单及验证边界；用户明确授权提交、推送 main，CI 通过后发布三份镜像。云端结果通过对应提交的 GitHub Actions 记录核实。
- [x] 后续交接确认旧版镜像发布、三份运行镜像 revision 核对和服务器部署；没有在本轮重新执行。
- [x] 后续交接确认旧版登录、OSS 图片链路、重启持久化及手动备份恢复，证据边界见统一 M1 计划。
- [ ] 新增 AI / PDF 功能的整合、发布、云端验收和新版本备份恢复。

## 不属于本轮自动完成的事项

免费开发联调不等同于公网正式上线。当前仍待图片孤儿对象清理、真实手机触控、新增功能的云端 AI 联调、正式 HTTPS 和当前版本数据库与文件恢复验收；本机真实文字、PDF、整图及裁片模型调用已有各自记录。实际 OSS 调用、服务器数据覆盖或额外付费云服务按具体授权范围执行。
