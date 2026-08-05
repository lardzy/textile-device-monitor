# 旧检务系统任务快照 Bridge

这是执行系统与 FibreCheck 旧系统之间的独立、只读 Windows 进程。它从执
行系统领取待刷新的样品编号，调用相邻目录中的
`legacy_fibrecheck_probe/probe.py`，再把任务检测依据、项目列表和特种毛编号族
占用事实提交到缓存。
Bridge 固定传入探针的 `--task-snapshot-only`，因此每次刷新只查询
`Task`、`Task_Sample`、`Task_CheckItem` 和最小化的
`SpecialWoolManage.SampleNo` 编号族，不会为首页推荐重复执行完整旧系统对账查询。

它不会上传文件、修改 Oracle、操作 FibreCheck 界面，也不会复用任何旧系统
写入工具。现有探针会先执行 `SET TRANSACTION READ ONLY`，所有查询完成后
始终 `ROLLBACK`。

## 运行前准备

1. 在 Windows x64 Python 环境安装只读探针依赖：

   ```powershell
   py -3.12-64 -m venv .venv-task-snapshot
   .\.venv-task-snapshot\Scripts\python.exe -m pip install -r ..\legacy_fibrecheck_probe\requirements.txt
   ```

2. 只在当前进程环境中设置执行系统 Bridge 令牌：

   ```powershell
   $env:EXECUTION_BRIDGE_TOKEN = "由执行系统部署环境提供的令牌"
   ```

3. 确认该 Windows 主机可访问执行系统 API 和旧 Oracle。旧系统数据库凭据仍
   由探针从 FibreCheck 配置读取，Bridge 不接收账号或密码参数。

## 单次运行

```powershell
.\.venv-task-snapshot\Scripts\python.exe .\bridge.py `
  --api-base http://192.168.106.33/api/execution/v1 `
  --bridge-id task-snapshot-win-01 `
  --probe-python .\.venv-task-snapshot\Scripts\python.exe `
  --probe-script ..\legacy_fibrecheck_probe\probe.py `
  --fibrecheck-dir C:\Users\lishuyang\Downloads\FibreCheck `
  --oracle-client-dir C:\Users\lishuyang\Downloads\textile-device-monitor\.tmp\oracle-ic\instantclient_19_31 `
  --credential-profile WebService.dll.config:PanYuJianWu `
  --data-source 192.168.105.106/orcl `
  --once
```

移除 `--once` 后，Bridge 默认每 15 秒领取一次；可用 `--poll-seconds` 调整。
`--api-base` 应包含 `/api/execution/v1`，但不应包含 Bridge 的具体路由。

## 协议和失败策略

- `POST /task-snapshot-bridge/claim` 领取编号。
- 探针必须确认真实连接和只读事务，且四项快照查询均成功。
- 同编号没有 Task 时提交空项目快照；同编号存在多个 Task、编号错配或查询失
  败时调用 `.../fail`，不会用不确定数据覆盖缓存。
- `Task_Sample` 只保留属于已确认 Task 的非空样品名称，并去重后返回。
- `Task_CheckItem` 只保留属于已确认 Task 的项目。
- 编号族只保留底单以及 `-1/-2...` 的真实占用编号，供图片上传预检稳定选择
  首个空闲编号；Windows Writer 写入锁内仍会再次核对。
- 成功调用 `.../{inspection_number}/complete`；请求都携带
  `X-Execution-Bridge-Key`。

Bridge 日志只打印领取、成功及稳定错误码，不打印样品编号、检测项目、备注、
Oracle 配置、令牌或探针输出。临时探针 JSON 无论成功、失败或超时都会尝试删
除。

后端领取租约默认为 180 秒，单次探针默认在 90 秒终止，因此完成/失败回传仍有
充足余量；超时后的租约可由后续 Bridge 安全重新领取。

## 测试

```powershell
.\.venv-task-snapshot\Scripts\python.exe -m unittest discover -s tests -v
```
