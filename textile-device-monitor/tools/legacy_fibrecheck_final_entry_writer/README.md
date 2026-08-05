# FibreCheck 最终录入写入器

这是“检验过程管理 → 检验记录登记(Excel)”最终录入阶段的独立 x86 .NET Framework
适配器。它复用旧客户端的官方 DAL/BLL，但把定位、权限、并发、文件和写后对账放在一组
fail-closed 门禁之后。当前尚未接入上游流程，也没有对旧系统执行过真实写入。

读取现有结果使用相邻的 `legacy_fibrecheck_probe`；本目录只负责验证任务包，以及在 Bridge
明确许可后执行一次写入。

## v1 / v2 范围

支持两种 `operation_type`：

- `generic_item_record`：`CurrencyItemRecordUI` 的一次“保存”。任务包必须同时给出完整头部
  和最终有序明细；不会隐式执行通用界面的“校对”。
- `excel_check_record`：只开放已用 `26A045793` 证明的
  `5103.5 / 纤维微观形貌` 数据链。每个包只含一个 `.xls`，只允许一个关键结果。
  schema v1 继续只接受 `微观形貌.xls`，且 `expected_key_identities` 必须是“纵面”或
  “横截面”；schema v2 接受下表列出的七个精确模板及其映射指纹，关键结果身份允许为空。
  写入器会依次完成旧界面的“保存”与“校对”。

Excel v1 有意拒绝其它项目/模板、GAP、新中英、CLOTHING、`ShowAllTarget` 转换、G/J/S
签名分支，以及登记字段与工作簿采集值不一致的情况。这样不会把尚未复刻的桌面端业务规则
误当成已支持。

Excel v2 的模板白名单为：

| 模板文件名 | 映射配置 SHA-256 |
| --- | --- |
| `微观形貌.xls` | `a09399783171826d10b239bd01cb596569428bbc34a8c4636077e98f34dc690e` |
| `纤维微观形貌-GB T 36422-2018-2张图.xls` | `2ff546b96da9ac423613374ee28955bf7dfe3e62e5d40d8ad979c93636836f23` |
| `纤维微观形貌-GB T 36422-2018-3张图.xls` | `43ae3872f231c2499b98976ea63827162b4fddf7151e3e8daceee8a7591d4268` |
| `纤维微观形貌-GB T 36422-2018-5张图.xls` | `d21e82cd1672ada28beab35673e1d679bf3ffa1099969f02dbed466645dc9b6d` |
| `纤维微观形貌-GB T 36422-2018-6张图.xls` | `5f56deb633c0dd2b2dc046ba70ab012c2780dbbae9039663b4d40903804a6304` |
| `纤维微观形貌-GB T 36422-2018-7张图.xls` | `3aea5aa68bccb1a8e9035be8762d305bb2f0d6e4104ad29557082a3b1abada01` |
| `纤维微观形貌-GB T 36422-2018-10张图.xls` | `976a88ed86af2a3fb30df5aa830e0529ea35e15e1e2fa59244e3035578940f74` |

模板名和映射指纹必须成对精确命中，不能混用。schema v2 在核对同一项目的既有记录时，
允许不同登记使用白名单中的不同模板；每条关键结果仍必须按自己的 `OriginalRecordID` 关联，
且其 `ExcelTemplateName` 必须等于对应登记的模板。`SampleIdentity` 可以为空或重复，不能作为
唯一键；登记 ID、项目范围和计数仍按原门禁严格校验。

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

`test.ps1` 从全新临时输出目录运行，不连接 Oracle、不访问文件服务器、不启动 Excel，当前
覆盖任务包字段、单引号、工作簿路径/长度/SHA-256、v1 兼容、七模板精确映射、v2
空身份、任务项目绑定漂移和受控测试覆盖的三重绑定。它还会单独编译纯分支规则 SelfTest，
覆盖固定附件模板计数为 0 和 2 时与 GAP、新中英/FILA、CLOTHING 的组合行为；计数为 2
不会因记录不唯一而拒绝，并按旧客户端 `FirstOrDefault` 的存在性语义参与真实分支顺序。
会话规则 SelfTest 另行覆盖非交互会话、Session 0、正常登录用户会话，以及通用操作和离线
Excel 验证的豁免行为。

## 任务包

示例位于 `examples/`，只展示结构，不能直接对旧系统执行。

通用包要求：

- 精确的样品号、任务项目编号和名称；不接受调用方提供内部 ID；
- `expected_existing_register_count`，用于乐观并发和幂等检查；
- 所有头部字段，即使值为空也必须出现；
- 至少一条、按最终显示顺序排列的四列明细。

Excel 包额外要求：

- schema v2 必须携带完整 `task_project`：`project_key`、两个脱敏 ID、项目编号/名称、
  `GB/T 36422-2018`、`seq_num` 以及严格等于 `1` 的 `check_count`；`project_key` 必须能由
  其余字段按只读探针的同一算法重算；
- 模板名、`collection_mode=standard` 和精确的映射配置 SHA-256；
- 一个工作簿相对路径、文件名、字节数和内容 SHA-256；
- `key_result_count=1`，以及精确的 `expected_key_identities`；
- v1 的四个登记字段均为空；采集后若工作簿试图改变这些字段或检验员，写入前即拒绝。

schema v2 示例见 `examples/excel-package-v2.example.json`。它仍要求恰好一个关键结果，
但允许 `expected_key_identities` 中的唯一元素为空字符串。

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

### 受控测试覆盖

schema v2 提供一个严格限定的测试覆盖，用于任务 `CheckCount=1` 且项目中已经存在 1 条登记时，
在人工确认的单一样品上追加第 2 条登记。它不是普通业务模式，只有以下三项同时精确匹配才会
激活：

1. 任务包包含完整的 `controlled_test_override` 对象；
2. 命令行显式提供 `--allow-controlled-test-override`；
3. 环境变量 `FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO` 与任务包和覆盖对象中的样品号完全一致。

覆盖对象固定声明 `expected_task_check_count=1`、`expected_existing_register_count=1` 和
`resulting_register_count=2`。联网预检还会再次核对旧系统中的实际 `CheckCount` 与登记计数；
任一条件不符即拒绝。运行回执会分别记录 `active` 和 `applied`，离线验证只能证明三重绑定已
激活，因此 `applied=false`。结构示例见
`examples/excel-controlled-test-override-v2.example.json`。

联网 dry-run 还需 `--fibrecheck-dir` 和 `--account`。口令只从进程环境变量
`FIBRECHECK_RUNNER_PASSWORD` 读取。dry-run 会登录并在 Oracle
`SET TRANSACTION READ ONLY` 中完成精确任务、计数、权限、模板、映射、分支与文件目标预检；
不会构造可能隐式写映射的官方采集服务，不创建目录，也不访问/复制远端文件。

联网 Excel dry-run 和真实执行都必须由已登录 Windows 用户的交互会话启动，同时满足
`Environment.UserInteractive=true` 和当前进程 `SessionId>0`。Windows 服务、SYSTEM、
Session 0，以及任务计划中“无论用户是否登录都运行”的后台会话会在读取口令、安装旧程序集
解析器、登录 FibreCheck 或执行 Oracle 预检前，以 `excel_interactive_session_required`
（退出码 21、`reconciliation_required=false`）拒绝。`generic_item_record` 与
`--offline-validate` 不受此门禁影响。

固定附件模板的分支判定按旧桌面端 `SaveOriginalDataRecord` 的真实顺序复刻：G/J/S 前置条件
由本工具另行拒绝；随后依次判断 GAP、仅在固定模板不存在时判断新中英/FILA、判断
CLOTHING，最后进入标准分支。因此固定模板匹配数为任意正数（包括 2 条及以上）只会抑制
新中英/FILA，不会越过 GAP，也不会抑制后续 CLOTHING。精确匹配数仍写入安全指纹，因此
首次预检和锁内复检之间若记录数发生变化，写入仍会被拒绝。

真实执行只允许受控 Bridge 调用，必须同时提供 `--execute` 和
`--side-effect-permit-stdin`。写入器输出 `remote_write_ready` 后最多等待 60 秒，只有标准输入
精确收到一行 `PERMIT_REMOTE_WRITE` 才继续。许可后它会：

1. 只读查询当前 `Task_CheckItem`，对原始 ID 重新做单向散列并重算 `project_key`，逐字段核对
   项目编号、名称、方法、顺序和 `CheckCount=1`；任一合同评审变更都会在副作用前拒绝；
2. 对同一 `Task_CheckItem` 获取 Oracle `SELECT ... FOR UPDATE` 协作锁；
3. 重新登录并比对账号、岗位、部门和父部门；
4. 在锁内重新执行全部只读预检、完整安全指纹、任务项目绑定和源文件哈希；
5. 执行官方保存链并逐字段回读；
6. Excel 再校对并回读登记、文件哈希、关键结果、列表数据、其它数据，以及实际存在时的
   动态映射表。

成功 raw receipt 中的 `task_project` 来自 Writer 对当前 Oracle 行的实测结果，不从任务包或
公开摘要回显；Bridge 和后端会再次与私有包逐字段严格比对。

锁始终回滚，只用于避免两个适配器同时通过相同计数后重复写入。它不能阻止一个未使用本
协议的旧桌面客户端恰好并发操作，所以真实执行仍应由 Bridge 串行调度。

## 失败与重试

许可前失败不会产生远端副作用。越过官方服务/DAL 的保守副作用边界后，任何异常或写后
不一致都返回退出码 `30` 和 `reconciliation_required=true`；不得自动重试，只能先用只读
probe 对账。输出不包含连接串、口令、完整内部 ID 或远端绝对路径。
