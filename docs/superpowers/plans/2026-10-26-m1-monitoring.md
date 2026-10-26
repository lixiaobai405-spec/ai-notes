# M1 最小只读运行监测实施计划

> **For agentic workers:** 使用 subagent-driven-development，独立文件并行实现、规格/质量双审查；用户只授权代码与隔离验证，不提交推送部署或启用公网任务。

**Goal:** 独立采集主机/容器/数据库运行摘要，输出私密快照和有界本机告警，支持陈旧/未知状态。

**Architecture:** 数据库聚合、主机Docker只读采集、纯阈值事件计算、CLI/私密持久化分离。复用已验证私密状态与有界POSIX进程工具，不改应用API/业务数据或引入新依赖。

**Tech Stack:** Python标准库、SQLAlchemy/psycopg、PostgreSQL、Docker CLI、Ubuntu systemd模板；Windows纯逻辑/Fake数据和隔离PostgreSQL测试。

## Task 1：批准范围与基线

- [x] 用户确认T15独立只读监测；核对当前数据与现有日志/健康功能。
- [x] 在仓库外持久保存当前81个已有改动的SHA-256基线，后续不覆盖先前四轮交付。
- [x] 明确指标口径、未知/部分值、费用与HTTP失败率限制及默认采集/过期参数。

## Task 2：数据库聚合

- [x] 新增 scripts/monitoring_database.py，只读事务、语句/连接超时、无应用配置导入；权限不足按分组不可用。
- [x] 连接使用量、Job/AI积压与最老可运行等待、创建窗口上传/处理失败、完成窗口AI错误及已知usage；禁止步骤双计费和未知转零。
- [x] backend/tests/test_monitoring_database.py 使用隔离notes_test证明真实统计口径、只读权限、坏usage、缺权限/超时与数据库指纹不变。

## Task 3：宿主机和容器

- [x] 新增 scripts/monitoring_host.py、tests/test_monitoring_host.py，正确解析Linux内存/配置磁盘，区分容器内运行与宿主机。
- [x] Docker只允许精确本地Compose项目/服务的只读ps/inspect/stats，禁止读取环境变量或raw inspect/日志；限定输出/对象数/时间，异常不显示正常。
- [x] 临时proc文件、磁盘mock、Docker输出覆盖缺失/停止/不健康/多副本、单位、超限、重复/错误身份及未知字段；不操作实际应用容器。

## Task 4：阈值与本机事件

- [x] 新增 scripts/monitoring_alerts.py、tests/test_monitoring_alerts.py，有限数值阈值、必需分组未知、状态异常、去重/升级/恢复、有界本机事件历史。
- [x] 陈旧/未完成采集不显示正常；可选金额/HTTP失败率不可用保持显式，不伪造费用或请求失败率。

## Task 5：CLI与模板

- [x] scripts/site_monitor.py 提供check/sample/status，严格私密配置/快照、锁、原子更新、失败尝试状态和脱敏摘要；不外发、不修复、不启动或停止业务。
- [x] 根CLI测试及backend/tests/test_site_monitor_cli.py 验证部分采集、陈旧状态、事件与只读真实CLI。
- [x] ops/monitoring.example.json、ops/systemd/ai-notes-monitor.service/timer提供60秒模板，只验证不启用。

## Task 6：验证与收尾

- [x] 运行新增/相关测试，再完整后端和根工具回归；无API/schema变更，检查既有契约/依赖未改。
- [x] Linux隔离白名单快照88项测试和真实CLI冒烟通过，模板完成静态复核；本机无systemd-analyze，原生unit/真实主机/公网/账单仍待现场验收。
- [x] 独立规格与质量审查、修复阻断问题。
- [x] 更新M1进度、docs/只读运行监测与本机告警.md、阶段证据和实机/性能验收表；检查基线、敏感值和进程，关闭测试服务。

交付证据：`docs/task-evidence/2026-10-26-m1-monitoring.md`。后端全量552通过；最终监测专项37通过；根工具292项（286通过、6平台跳过）；Linux监测88通过与实际CLI冒烟通过。无提交推送、真实模型/OSS调用、通知外发或公网任务启用。

- [ ] 后续获授权后在真实Ubuntu执行原生unit校验、主机/数据库权限验证与定时器验收；本轮未安装或启用。
