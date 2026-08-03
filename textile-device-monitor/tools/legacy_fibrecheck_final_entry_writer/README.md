# FibreCheck 最终录入写入器

这是“检验过程管理 → 检验记录登记(Excel)”最终录入阶段的独立 x86 .NET Framework
适配器。它复用旧客户端的官方 DAL/BLL，但把定位、权限、并发、文件和写后对账放在一组
fail-closed 门禁之后。当前尚未接入上游流程，也没有对旧系统执行过真实写入。

读取现有结果使用相邻的 `legacy_fibrecheck_probe`；本目录只负责验证任务包，以及在 Bridge
明确许可后执行一次写入。

## v1 范围

支持两种 `operation_type`：

- `generic_item_record`：`CurrencyItemRecordUI` 的一次“保存”。任务包必须同时给出完整头部
  和最终有序明细；不会隐式执行通用界面的“校对”。
- `excel_check_record`：当前只开放已用 `26A045793` 证明的数据链：
  `5103.5 / 纤维微观形貌 / 微观形貌.xls`。每个包只含一个 `.xls`，只允许一个关键结果，
  `expected_key_identities` 必须是“纵面”或“横截面”。写入器会依次完成旧界面的“保存”
  与“校对”。

Excel v1 有意拒绝其它项目/模板、GAP、新中英、CLOTHING、`ShowAllTarget` 转换、G/J/S
签名分支，以及登记字段与工作簿采集值不一致的情况。这样不会把尚未复刻的桌面端业务规则
误当成已支持。

## 构建与离线测试

```powershell
.\build.ps1
.\test.ps1
```

构建固定使用 32 位 .NET Framework 编译器和 `-platform:x86`。即使宿主是 Windows 11
ARM64，也必须保持 x86，因为旧 Oracle 11.2 `OraOps11w.dll`、Instant Client 和 FibreCheck
客户端均为 x86。真实 Excel 写入还要求 x86 进程能够激活本机 `Excel.Application` COM；
离线验证和 dry-run 不会打开 Excel。

2026-08-03 已在当前 Windows 11 ARM64 虚拟机上用 32 位调用进程实际激活不可见的
Excel 16.0 COM 实例并正常退出；测试没有打开工作簿，也没有连接旧系统。

`test.ps1` 从全新临时输出目录运行，不连接 Oracle、不访问文件服务器、不启动 Excel，覆盖
任务包字段、单引号、工作簿路径/长度/SHA-256、Excel 范围和执行许可门禁。

## 任务包

示例位于 `examples/`，只展示结构，不能直接对旧系统执行。

通用包要求：

- 精确的样品号、任务项目编号和名称；不接受调用方提供内部 ID；
- `expected_existing_register_count`，用于乐观并发和幂等检查；
- 所有头部字段，即使值为空也必须出现；
- 至少一条、按最终显示顺序排列的四列明细。

Excel 包额外要求：

- 模板名、`collection_mode=standard` 和精确的映射配置 SHA-256；
- 一个工作簿相对路径、文件名、字节数和内容 SHA-256；
- `key_result_count=1`，以及精确的 `expected_key_identities`；
- v1 的四个登记字段均为空；采集后若工作簿试图改变这些字段或检验员，写入前即拒绝。

映射配置指纹由相邻的只读 probe 生成。`mapped_table_exists=false` 不是错误：现网
`微观形貌.xls` 的映射和 9 行采集配置均存在，但可选动态 DataTable 不存在；旧 DAL 的
既有行为是跳过 `DataScripts`，继续保存登记、关键结果、列表数据和其它数据。写入器保持
这一行为，仅在动态表实际存在时回读它。

所有交给旧 DAL 字符串拼接路径的业务字段均拒绝单引号；未知 JSON 字段也会拒绝。

## 三种运行状态

离线结构/文件清单验证：

```powershell
.\out\FibreCheckFinalEntryWriter.exe `
  --offline-validate `
  --package C:\controlled\package.json `
  --source-root C:\controlled\source
```

联网 dry-run 还需 `--fibrecheck-dir` 和 `--account`。口令只从进程环境变量
`FIBRECHECK_RUNNER_PASSWORD` 读取。dry-run 会登录并在 Oracle
`SET TRANSACTION READ ONLY` 中完成精确任务、计数、权限、模板、映射、分支与文件目标预检；
不会构造可能隐式写映射的官方采集服务，不创建目录，也不访问/复制远端文件。

真实执行只允许受控 Bridge 调用，必须同时提供 `--execute` 和
`--side-effect-permit-stdin`。写入器输出 `remote_write_ready` 后最多等待 60 秒，只有标准输入
精确收到一行 `PERMIT_REMOTE_WRITE` 才继续。许可后它会：

1. 对同一 `Task_CheckItem` 获取 Oracle `SELECT ... FOR UPDATE` 协作锁；
2. 重新登录并比对账号、岗位、部门和父部门；
3. 在锁内重新执行全部只读预检、完整安全指纹和源文件哈希；
4. 执行官方保存链并逐字段回读；
5. Excel 再校对并回读登记、文件哈希、关键结果、列表数据、其它数据，以及实际存在时的
   动态映射表。

锁始终回滚，只用于避免两个适配器同时通过相同计数后重复写入。它不能阻止一个未使用本
协议的旧桌面客户端恰好并发操作，所以真实执行仍应由 Bridge 串行调度。

## 失败与重试

许可前失败不会产生远端副作用。越过官方服务/DAL 的保守副作用边界后，任何异常或写后
不一致都返回退出码 `30` 和 `reconciliation_required=true`；不得自动重试，只能先用只读
probe 对账。输出不包含连接串、口令、完整内部 ID 或远端绝对路径。
