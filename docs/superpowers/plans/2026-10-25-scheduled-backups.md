# Scheduled Site Backups Implementation Plan

> **For agentic workers:** Use subagent-driven-development to implement the isolated tasks and request independent specification and code reviews. Steps use checkboxes for tracking. Work in the current clean main checkout as required by the user's project instructions; do not commit, push, deploy, activate timers, or read real credentials.

**Goal:** 提供每日维护窗口备份、最近 7 份成功备份保留、独立故障记录与可选通知。

**Architecture:** 标准库调度执行器调用既有整站备份 CLI；Compose 容器按原状态停止和恢复，独立私密状态根保存恢复现场、成功回执和通知事件。保留模块仅删除被登记且完整验证的 bundle；systemd 模板负责每日触发与异常后恢复。

**Tech Stack:** Python 3.12+ standard library、现有 backend/.venv、Docker Compose、Ubuntu systemd、unittest；不新增依赖和业务迁移。

## Task 1: Private state, locking and retention

Files: create `scripts/site_backup_state.py`, `tests/test_site_backup_state.py`.

- [x] 先测试临时目录状态原子写入、锁互斥、路径别名/硬链接拒绝、回执约束与只读保留计划。
- [x] 实现 `ScheduleError(code)`、`private_path(path)`、`StateStore(root)`、`StateStore.lock()`、`read(name, default=None)`、`write(name, value)`；JSON 限制 64 KiB，目录 0700、文件 0600，拒绝重复 JSON key。
- [x] 实现 `register_backup(store, backup_root, name, manifest)`；严格目录名 `scheduled-YYYYMMDDTHHMMSSZ-<12 hex>`，回执记录名称/时间/schema/manifest SHA-256，不向 bundle 加文件。
- [x] 实现 `retention_plan(store, backup_root, keep_last)` 和 `prune_backups(store, backup_root, keep_last, verify)`。plan 返回候选名称；prune 必须在完整验证、回执匹配和固定文件列表检查后逐文件删除，保留未知/损坏/旧 schema，返回保留、删除和跳过结果。

Run (repo root): `backend/.venv/Scripts/python.exe -m unittest discover -s tests -p test_site_backup_state.py`。先确认 RED，再实现并确认 GREEN。示例行为：

```python
with store.lock():
    store.write('status.json', {'phase': 'preflight'})
    assert store.read('status.json')['phase'] == 'preflight'
assert retention_plan(store, backups, 7)['candidates'] == []
```

## Task 2: Scheduled orchestration and notification

Files: create `scripts/site_backup_schedule.py`, `scripts/site_backup_process.py`, `tests/test_site_backup_schedule.py`.

- [x] 以替身覆盖成功/备份失败/部分 stop 失败/恢复失败/身份变化/维护时间外/迁移中/子进程超时；审查复现的问题先确认 RED 再修复，无业务库。
- [x] 实现 JSON 配置加载（只含路径和非敏感值），`check`, `run`, `status`, `retention-plan`, `recover-services`, `retry-notifications` 子命令，所有子进程用参数数组、受控环境及固定错误码。
- [x] 按已确认设计实现 `preflight -> stop -> backup -> verify -> restore -> receipt -> retention -> status`，finally 恢复服务；未完成恢复状态不得被新运行覆盖。
- [x] 调用 CLI 形式固定为：

```python
command = [python, str(repository / 'scripts/site_backup.py'), 'backup',
           '--database-url-file', database_url_file,
           '--postgres-container', database_container,
           '--storage-backend', storage_backend,
           '--output', str(destination), '--maintenance-confirmed']
```

- [x] 通知先持久化脱敏事件，然后可选调用配置的绝对可执行路径，JSON 从 stdin 输入；失败保留重试，按 event_id 支持接收端幂等。
- [x] 验证超时、重复恢复、日志与错误敏感哨兵不泄漏，默认无真实通知。
- [x] 实际外部命令限定 POSIX，进程组在组长退出后仍受超时清理；Windows 仅执行替身调度/状态/保留测试并验证真实执行入口拒绝启动，进程专项在 Linux 隔离容器验证。

Run: `backend/.venv/Scripts/python.exe -m unittest discover -s tests -p test_site_backup_schedule.py`，预期全部通过。

## Task 3: Deployment templates and operator guide

Files: create `ops/backup-schedule.example.json`, `ops/systemd/ai-notes-backup.service`, `ops/systemd/ai-notes-backup.timer`, `compose.backup.yaml`, `docs/定时备份与失败通知.md`; update README and M1 status/evidence.

- [x] 配置模板仅使用占位路径，默认 `keep_last=7`、`maintenance_start_utc="19:00"`/北京时间 03:00 对应的配置，明确窗口与超时。
- [x] timer 使用 `OnCalendar=*-*-* 03:00:00 Asia/Shanghai`、`Persistent=false`；oneshot service 设置 `UMask=0077`、`TimeoutStartSec` 和 `ExecStopPost=... recover-services`。
- [x] 单独 loopback Compose 覆盖不应用到现网；说明需要完整仓库、独立锁定运维环境、连接文件、排除外部写入者及可选通知接收端去重。
- [x] 运维文档包括启用前 check、首次手动运行、status/journal、保留只读计划、恢复服务、重试通知、禁用 timer 的回退；本轮不执行启用命令。

## Task 4: Verification and review

- [x] 根目录运行 `backend/.venv/Scripts/python.exe -m unittest discover -s tests -p 'test_*.py'`；现有基线 56 项通过。
- [x] 对配置模板执行纯 `check`/静态测试，确认没有访问 Docker/DB/OSS 或停止应用。
- [x] 独立规格审查后独立代码质量审查；修复重要问题并重跑相关测试。
- [x] `git diff --check`、`git status --short` 与敏感信息检查；AGENTS.md 保持忽略，确认本地业务端口仍关闭。
- [x] 更新上述执行清单和 `docs/task-evidence/2026-10-25-scheduled-backups.md`，区分替身验证、历史真实备份证据和未执行的云端启用/真实推送。

不执行 commit：用户仅授权开发，项目 Git 规则要求另行明确授权。
