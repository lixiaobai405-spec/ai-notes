# 发布复用同提交 CI 设计

用户已确认：优化手动发布时重复运行 CI，复用同一提交已通过的检查，保留必要安全校验。初始范围为实现与本地验证；随后用户授权仅将本次相关改动提交并推送到 main、检查 CI 并修复必要问题。不触发 ACR 发布或更新服务器，旧的未提交改动保持原状。

## 选择

采用“查询可信 CI → 复用或运行原完整 CI → 显式发布门禁”。相比发布时永远重跑，命中时省去一次完整 CI；相比完全依赖一次绿灯查询，保留完整检查回退与登录 ACR 前的复核。原 `ci.yml` 的检查内容不变，不新增自动发布触发器或强制跳过输入。

## 可信依据

- 唯一来源为当前仓库 `.github/workflows/ci.yml` 的 `main` / `push` 运行，完整 `head_sha` 等于触发本次发布的 `github.sha`。
- 核对工作流 ID / 路径、当前仓库及 head repository 的 ID / 名称、事件、分支与完整 SHA；不按可重复的显示名称或仅凭绿色 Check 匹配。
- 查询不按 `status=success` 过滤；选择最新 run_number 的运行。最新运行失败、取消、待执行或运行中时不能退回较早绿灯。
- 获取该运行最新 attempt 的 jobs，要求 `verify` 确实成功、相关 job 均完成且成功，并校验 job 的 run / attempt / SHA 身份。再次读取运行确认在查询期间没有重跑或改变。
- 缺失、不完整、超界、API 失败或状态不明均返回“运行完整 CI”。读取只有 `contents: read` / `actions: read`；不传入 ACR 凭据，不请求 Actions 写权限。

## 工作流

1. `select-ci` 仅在手动触发的 `refs/heads/main` 上运行，checkout 固定 `github.sha`，输出规范化的 `reuse` / `run_id` / `attempt`。
2. `verify` 依赖选择成功，仅在 `reuse == 'false'` 时调用原可复用 `ci.yml`。选择 job 失败不得伪装为未命中。
3. `publish` 显式依赖两者；只接受“选择成功 + reuse=true + verify=skipped”或“选择成功 + reuse=false + verify=success”。使用 `always()` 处理正常 skipped 依赖，同时拒绝取消、失败和异常输出。
4. 发布 checkout 明确固定 SHA，并断言 `git rev-parse HEAD` 等于事件 SHA。保持镜像标签、OCI revision 与代码来源一致；触发后 main 前进不把新代码混入旧 SHA 的发布。
5. 复用路径在镜像准备之后、ACR 登录之前重新查询，要求仍为相同 run ID / attempt 的最新成功依据；若已重跑、出现更新失败、取消或 API 无法确认，则终止本次发布。回退路径已经由同次发布的完整 CI 成功结果授权，不依赖外部查询恢复。
6. 保留 ACR 登录仅在登录步骤持有 Secrets、唯一 release tag、三镜像 push / digest 摘要、logout 和现有并发控制。摘要记录是否复用、检查来源及 SHA。

## 验证

标准库 helper 与模拟 API 测试覆盖正确复用、错误身份、最新失败 / 重跑、空 / 缺页 / malformed、API 失败、verify skipped、attempt 竞态与 require 复核失败。配置测试执行真实条件表达式的成功 / 失败 / skipped / cancelled 矩阵，确认权限、固定 checkout、复核先于 Secrets、原检查内容与发布契约未变。

本机实际 GitHub GET 查询仅检验已通过的 `423190a...`，不触发发布或付费 API。运行项目隔离 Python 的根工具回归与 actionlint，再做独立规格及代码质量审查。远端工作流调度未实测须明确注明。

参考：[工作流运行 API](https://docs.github.com/en/rest/actions/workflow-runs)、[attempt jobs API](https://docs.github.com/en/rest/actions/workflow-jobs)、[skipped 依赖与条件](https://docs.github.com/en/actions/how-tos/write-workflows/choose-what-workflows-do/use-jobs)。
