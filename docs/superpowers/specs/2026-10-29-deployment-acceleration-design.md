# 镜像构建缓存与宿主机离线依赖设计

日期：2026-10-29。用户已明确要求“那你就做好这两个优化”，承接已展示的跨次镜像构建缓存及 Linux 离线依赖方案。本设计落实该范围，不新增业务功能或云资源，不修改现网、提交 / 推送或触发远端发布。

## 目标与选择

1. 采用 GitHub Actions 的 BuildKit `type=gha` 持久化缓存，而非新增 ACR 缓存仓库或自建 runner。前后端、CPU 平台使用独立且稳定的 scope；依赖锁未变时复用依赖层。PR 只读缓存，只有可信 main push / main 手工发布写缓存。
2. 宿主机离线依赖在 Linux/amd64、CPython 3.12 环境准备 wheelhouse。提供 monitoring（当前四个监测依赖）与 backup（完整后端锁定依赖）两种用途，避免每次安装与工具无关的全部库；版本均来自既有锁文件，引导 pip 单独固定为已经使用的 24.0。

未选择 ACR 缓存仓库，是为了沿用现有 GitHub 托管构建、不增加仓库权限与维护。离线包使用可校验 wheel 而非复制 Windows 虚拟环境，也不把现有宿主机指标采集器直接改为容器内采集。

## 镜像路径

- 使用 Docker 官方 setup-buildx / build-push Actions，保留 `linux/amd64`、发布标签、提交 labels、main / verify 门禁与既有 ACR 登录 / digest 记录边界。
- CI 与发布导入相同前后端 scope；CI 保留数据库迁移回归、后端 / 前端 / 根工具 / 类型契约 / Compose 配置及隔离 HTTPS 验证。避免在一次 CI 中重复构建同一前后端：复用加载的验证镜像，不削减原功能覆盖。
- 缓存丢失是正常冷构建，不是跳过构建或测试的理由。无仓库凭据进入构建上下文或缓存；保留既有 Docker ignore。

## 离线包路径

- 新增标准库 CLI：`build`、`verify`、`install`。构建只在 Linux x86_64 / Python 3.12 进行；校验支持本机；安装只在匹配目标的 Linux 创建新环境，拒绝覆盖现有虚拟环境。
- Linux 构建容器选择 `python:3.12-slim-bookworm`，兼容现网 Ubuntu 24.04 的运行边界。Docker 构建上下文使用显式白名单，只包含工具、锁文件与构建配置，不带 `.env`、私钥、备份或私人目录。
- wheel 下载只接受固定版本和二进制分发，禁止源码包触发离线编译；监测用途选择 SQLAlchemy、psycopg、psycopg-binary、typing-extensions 的锁定项，备份用途按完整锁文件的 Linux 标记处理。
- 输出 wheel、精确版本 / SHA-256 的离线安装清单、目标平台 / profile / 原锁摘要 / 逐项大小与哈希的 manifest，以及使用入口。校验拒绝缺件、额外文件、篡改、软链接及异常路径，校验失败前不得安装。
- 通过已验证的 pip wheel 引导 `venv --copies --without-pip`，不依赖服务器 ensurepip。安装仅使用本包 `--no-index` / `--find-links` / 哈希校验，清理 pip 外部配置与升级检查影响，随后 `pip check` 和导入冒烟；不修改系统 Python、现有监测环境或业务服务。
- 新增 main 手工工作流，按用途构建、验证、在禁网环境中安装验证，再上传有提交 / 平台 / 用途标识的 GitHub Artifact（制品），不接入 ACR 业务发布或服务器自动安装。

## 验证与完成标准

- 新的工作流静态检查与权限边界检查通过；相关工具单元测试覆盖校验拒绝和无网络安装命令边界。
- 本机实际 Linux Docker 构建监测与备份用途，导出离线包，在独立、无网络的干净 Linux 容器中安装并检查依赖 / 导入。所有生成物放入忽略目录，不进入 Git。
- 镜像冷 / 热构建验证缓存复用；只有实际观测到的命中才写入证据，不把本地缓存当作远端 GitHub cache 实测。
- 独立规格与质量审查后更新操作文档与执行证据。未推送前明确远端工作流尚未生效、不声称已有云端提速指标。

依据：[Docker GitHub 缓存](https://docs.docker.com/build/ci/github-actions/cache/)、[Docker 缓存组织](https://docs.docker.com/build/cache/optimize/)、[pip 可重复安装与 wheelhouse](https://pip.pypa.io/en/stable/topics/repeatable-installs/)。
