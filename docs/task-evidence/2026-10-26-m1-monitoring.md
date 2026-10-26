# 2026-10-26 T15 最小只读运行监测

用户在核对M1剩余工作后确认“先做 T15 只读监测工具”。本轮只开发代码、隔离测试、操作说明和Ubuntu模板，不提交推送、不连接公网、不启用定时任务或发送通知。

## 实现与数据边界

- `scripts/site_monitor.py` 提供 `check` / `sample` / `status`，严格私密配置、单实例锁、先记录尝试再采集、原子持久化和脱敏摘要；复用已有私密状态/有界进程工具，未修改这些既有工具。
- 数据库使用独立 `NullPool`、只读可重复读事务、查询保存点和剩余时间预算，结束回滚；仅聚合五组数据，不查询笔记正文、账号、文件名、凭据或原始错误。缺全局统计权限时连接组不可用，其他分组继续。
- Linux主机内存/配置磁盘与本机精确Compose服务的状态/资源分开采集，Docker仅只读命令和有限投影。容器环境或无法确认主机来源时不能冒充宿主机。应用没有挂载Docker socket，没有新公开API或页面。
- Job/上传失败按窗口内创建、当前失败的队列口径；AI错误/用量按终态完成窗口。PDF拒绝独立统计与告警。上传分母取 `UploadSession`，用量只取已聚合 `AiRun`，不叠加 `AiStep`。
- 用量缺失/不完整保留未知；空队列必须同时观察到数量0才可解释空等待年龄。金额与HTTP请求失败率无权威来源，明确不可用，不推算费用或伪造请求率。
- 必需指标不可用、陈旧、未完成采集或策略/目标改变不能显示正常。本机事件去重、有界历史和可信恢复；不完整低值不能降级旧严重事件。数据库目标使用去凭据身份指纹，换库拒绝覆写旧历史；凭据轮换不触发换库。
- `sample`退出0只表示快照落盘；`status`为显式健康门禁：0正常、1警告、2严重/未知、3操作失败。时间截止固定为采集开始，另记完成时间。
- 提供60秒建议采样、180秒过期、60分钟窗口及systemd模板；未安装、启用或外发。无新依赖、数据库迁移、API/契约或前端修改。

## 验证结果

| 验证 | 本次结果与边界 |
|---|---|
| 真实PostgreSQL监测专项 | `34 passed`；统计窗口、创建队列、PDF拒绝、合法/坏usage、AiStep不双计、受限角色缺全局统计/表权限、保存点、只读指纹、超时与脱敏 |
| 真实CLI + PostgreSQL | `3 passed`；独立进程实际采集/私密落盘、数据库指纹不变、空队列、未知费用、缺环境变量、状态不连接不可达目标；主机为替身 |
| 最终修复后相关回归 | 数据库34项与CLI3项合并再跑：**37 passed**，12.21秒；根292项、Linux88项及Linux实际CLI均在最终修复后重跑通过 |
| 完整后端回归 | **552 passed**，232.12秒；启用隔离真实pg_dump/pg_restore测试，无跳过；1条既有Starlette/httpx弃用警告。此后仅监测路由指纹与容器告警恢复修正，另复跑相关监测专项 |
| 根工具完整回归 | **292项：286通过、6按平台跳过**，10.910秒；Windows跳过POSIX适用项 |
| 新增监测纯逻辑 | 上述根测试包含 **88项**：host 27、alerts 40、site_monitor 21 |
| Linux白名单隔离 | 同88项全部通过；缓存Python镜像、只读挂载、禁网络、移除capabilities、临时文件系统，无真实环境配置 |
| Linux实际CLI冒烟 | check/sample/status正确；配置/状态0700、文件0600；容器内宿主机数据和缺数据库均为未知，不冒充正常 |
| Alembic | 隔离库upgrade head与check通过：`No new upgrade operations detected` |
| systemd | 模板静态复核；本机/缓存测试镜像无可用 `systemd-analyze`，**未声称通过原生unit校验或运行**，需真实Ubuntu现场补验 |
| 工作区范围 | 开发前81个已有改动的SHA-256基线复核；仅允许的README、M1计划与本设计补充有变化，其余先前功能保持原样 |
| 敏感值/临时文件 | 已检查变动文本凭据模式、Git忽略与暂存区；无发现，`AGENTS.md`仍忽略且未跟踪，暂存区为空。测试输出/白名单快照存仓库外 |

独立规格/质量审查发现并修复：PDF拒绝指标白名单遗漏、部分用量错误降低旧严重告警、同名环境变量切换数据库串用历史。各项有回归用例；主机采集后数据库窗口时间漂移也已统一。

最终主机/工具只读复审补充并修复：目标指纹遗漏libpq路由环境默认值（包括完整URL也可能受影响的`PGHOSTADDR`）；容器重新可读且全部为已知状态时，旧分组未知告警不能恢复。路由环境仅哈希、不保存明文，不包含凭据轮换；不完整用量低值保护保持不变。极限配置与100条历史模拟约62KiB，未发现64KiB边界造成持续采集失败；超限仍显式失败，不丢活动告警。

## 执行环境与复现

Windows后端沿用 `backend/.venv/Scripts/python.exe`，只使用 `compose.test.yaml` 的 `127.0.0.1:55432/notes_test`，未连接 `notes_dev` 或服务器。测试显式设置本地存储并清空模型Key；未调用真实模型、OSS或任何付费接口。

```powershell
# 仓库根目录；只启动隔离回归库
docker compose -f compose.test.yaml up -d --wait
$env:TEST_DATABASE_URL='postgresql+psycopg://notes:notes@127.0.0.1:55432/notes_test'
$env:DATABASE_URL=$env:TEST_DATABASE_URL
$env:APP_ORIGIN='http://testserver'
$env:STORAGE_BACKEND='local'
$env:DEEPSEEK_API_KEY=''
$env:SITE_BACKUP_TEST_CONTAINER=(docker compose -f compose.test.yaml ps -q db)
Push-Location backend
& .venv/Scripts/python.exe -m alembic upgrade head
& .venv/Scripts/python.exe -m alembic check
& .venv/Scripts/python.exe -m pytest -q
Pop-Location
& backend/.venv/Scripts/python.exe -m unittest discover -s tests -q
docker compose -f compose.test.yaml stop db
```

测试前Docker Desktop因残留的零字节socket重命名错误无法启动。保留原socket目录后恢复引擎，仅处理本轮失败的启动进程和已核实的临时socket目录；未重置Docker、修改配置或数据库卷。原目录在本机保留供回查。临时测试库完成后停止，业务开发端口保持关闭。

## 尚待现场验收

操作与回退见[监测说明](../只读运行监测与本机告警.md)，真实设备/性能/恢复字段见[实机验收表](../M1实机与性能验收记录.md)。完整M1仍需授权发布、正式HTTPS、Ubuntu宿主机指标/角色/unit验证、真实设备并发与代表性负载、真实OSS隔离恢复、备份与主机外失联/通知配置。本轮测试不能替代这些结果，也没有更新GitHub CI或公网版本。
