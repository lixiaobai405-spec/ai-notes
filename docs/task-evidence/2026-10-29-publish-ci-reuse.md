# 2026-10-29 发布复用同提交 CI 验证记录

## 范围与行为

用户明确要求优化发布时重复运行 CI，复用同一提交已通过的结果并保留安全校验。实现阶段只修改发布门禁、增加只读判定工具及测试 / 文档；不改变完整 CI 检查内容、镜像构建、ACR 发布内容或运行中服务。随后用户授权仅提交并推送本次优化到 main，并检查该提交的 CI，必要时修复；其他未提交改动不纳入。上述本地验收阶段没有提交 / 推送；本轮不调度真实 ACR 发布或访问生产服务器。

发布先通过 `select-ci` 查询当前仓库指定 `ci.yml` 的 `main` / `push` 运行，要求完整 SHA 一致，最新 run / attempt 已完成成功且 `verify` 实际执行成功。没有可靠依据时调用原完整 CI。发布 job 只允许“选择成功 + 可信复用 + verify 正常 skipped”或“选择成功 + 不复用 + verify 成功”；取消、失败或异常输出不放行。

源码 checkout 固定为触发事件 SHA，并核对本地 HEAD。复用路径在所有镜像准备后、ACR 登录前再次确认 run / attempt 未失效；无法确认则终止发布。摘要保存来源和 SHA。Actions API 只读，ACR Secrets 仍只出现在原登录步骤。

## 自动化与真实查询

| 检查 | 结果 |
|---|---|
| 标准库判定工具测试 | 32 项通过；包括最新失败不复用旧绿灯、错误仓库 / SHA / 路径 / 事件、attempt 竞态、分页 / 缺页、截断响应、API 失败、无成功 verify、输出与令牌保护 |
| 工作流契约测试 | 11 项通过；其中发布条件实际表达式覆盖 512 组依赖状态 / 输出 / 取消 / 分支 / 事件组合，另验证完整 CI 回退条件、最小权限、固定 SHA 和复核顺序 |
| 完整工作区根工具回归 | 390 项，384 通过、6 项既有平台条件跳过 |
| 提交范围独立副本根工具回归 | 386 项，380 通过、6 项平台跳过；其他任务的 4 项未提交测试未混入提交 |
| 断网 Linux / CPython 3.12 容器运行判定工具测试 | 32 项全部通过，只读挂载脚本和测试，无业务 / 配置挂载 |
| actionlint 1.7.12 检查 CI / 发布 YAML | 通过 |
| 真实 GitHub GET：lookup | 对 `423190a5405b1f5eb4540fdae60fa3295ac53b80` 返回 reuse=true，run `40734911272` / attempt `1` |
| 真实 GitHub GET：require | 对相同 SHA / run / attempt 再次确认成功 |
| 真实 GitHub GET：不存在的完整 SHA | 返回 reuse=false / no_matching_run；没有把其他提交的成功结果套用过来 |
| 独立规格审查 | 通过；独立复跑 32 + 11 项测试，确认完整 CI 未变且已有文件哈希一致 |
| 独立质量审查 | 通过；独立复跑 32 + 11 项测试，未发现需修复问题 |

真实查询仅使用 GitHub CLI 已有连接，在子进程内存中传递令牌，不打印、不写入文件；所有调用为固定 GitHub API 的 GET。模拟数据测试不访问网络。令牌只允许传到固定 HTTPS API；重定向被拒绝，响应大小 / 分页 / 请求时间均有限制。

测试先观察到缺失门禁 / 工具的失败，再实现。额外对“截断但仍可解析为 JSON 的 HTTP 内容”做 RED/GREEN：增加 Content-Length 完整性核对后，不再误用不完整证据。

## 验证边界与留存

真实通过的历史 CI：[40734911272](https://github.com/lixiaobai405-spec/ai-notes/actions/runs/40734911272)。此次仅验证读取和判定，没有调度新发布、取得 ACR 凭据或推送镜像。尚未在 GitHub 上实测新工作流的 skipped 依赖调度与回退；本地实际条件矩阵及 actionlint 不能代替远端运行证据。

本次改动提交到 main 后，先等待新提交完整 CI 成功再手动发布，才能命中复用。不要对新增门禁后的 SHA 复用旧提交记录。未命中时完整检查仍需数分钟；没有任何强制跳过开关。

先前 14 项 modified / untracked 文件已在忽略目录记录 SHA-256；本轮不改它们。原始测试日志、只读查询结果和基线保存于 `.local/ops/publish-ci-reuse-*`，不纳入 Git。设计 / 计划见同日期 `docs/superpowers/` 文档，使用说明见 [部署提速与离线依赖](../部署提速与离线依赖.md)。

依据：[GitHub 工作流运行 API](https://docs.github.com/en/rest/actions/workflow-runs)、[指定 attempt 的 jobs](https://docs.github.com/en/rest/actions/workflow-jobs)、[skipped 依赖条件](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-jobs)。
