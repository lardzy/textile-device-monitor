# FibreCheck 最终录入与读取模块

本文记录“检验过程管理 → 检验记录登记(Excel)”中两类最终结果的已验证数据链、自动化边界和样本对账结果。结论来自旧客户端静态分析，以及 2026-08-01、2026-08-03 公司内网 Oracle 只读事务核对。

当前状态：读取模块与安全写入适配器已经按本文契约实现；尚未对旧系统执行任何真实保存、文件上传或校对操作。

## 入口与路由

外层界面为：

`Toone.FibreCheck.BusinessProcess.BusinessProcessUI.CheckRecord.CheckRecordRegisterUI`

任务定位必须使用以下精确链路，不能用样品号前缀、特纤实验记录或相似项目名称猜测：

`Task.ReportNo → Task_CheckItem.CheckItemID → CheckItem`

“增加原始记录”的实际分流依据是：

- `CheckItem.OriginalDataInputUIClassName` 非空时，可进入专用录入界面；
- 原始记录模板来自 `StandardDocument.StandardID = CheckItem.ID`，再连接 `Document`；
- 只有一个可用入口时直接打开；存在多个模板或同时存在界面与模板时先显示选择框。

旧的 `CurrExcelOriRecord` / `CurrencyExeclTemplateSet` 不是本需求的最终事实源。

## A：通用项目记录登记

已验证界面类：

`Toone.FibreCheck.OriRecord.CurrencyItem.CurrencyItemRecordUI`

### 人工语义

上半部分包含单位、判定依据、测试方法等字段。判定依据、等级或标准类型发生变化时，旧界面会重新加载提示数据并清空/重建下方明细，因此自动化任务包必须先形成完整头部快照，再提供最终有序明细，不能模拟成任意顺序的逐字段点击。

明细的规范列顺序为：

1. 标准试验部位；
2. 标准值与允差；
3. 实测试验部位；
4. 实测值。

下方还包括备注和总评定。用户描述的本类流程以一次“保存”结束；通用界面本身也有独立“校对”能力，但不应在未明确要求时隐式执行。

### 数据链

官方保存顺序为：

1. `CurrencyItemRecordDAL.Save` 写入 `CheckRecordRegister`；
2. 写入 `CurrencyItemRecordNew`；
3. 写入 `CurrencyItemRecordNewDetail`；
4. `CurrencyItemGenerateReportDataService.UpdateOriginalData` 生成报告关键数据投影。

此 DAL 的多次写入不是一个覆盖全部步骤的事务。任一步骤越过首个远端写入边界后失败，都必须标记 `reconciliation_required`，只读对账后由人工决定，不能自动重试。

本类型的关联与 Excel 类型不同：

```text
CheckRecordRegister.OriginalRecordID = CurrencyItemRecordNew.ID
OriginalKeyData_CheckItem.OriginalRecordID = CurrencyItemRecordNew.ID
```

`CurrencyItemRecordNew.CheckRecordRegisterID` 同时反向指向该 `CheckRecordRegister.ID`。读取器必须验证这座双向桥，不能把通用投影错误地按登记 ID 关联。

### 样本 260039770

目标项目为 `51.SSJ1 / 纸、纸板和纸浆纤维组成定量分析`。在线只读结果：

- 期望结果数 1；通用主记录 1；登记记录 1；报告关键结果 1；
- 单位：`%`；
- 判定依据：空；
- 测试方法：`GB/T 4688-2020,按客户要求`；
- 明细 1：木浆，实测值 `60-80`；
- 明细 2：莱赛尔，实测值 `20-40`；
- 备注：`质量因子为参考值，非实测值，结果仅供参考。`；
- 标准值与允差、总评定、等级、样品描述均为空；
- 保存时间 `2026-02-03 12:39:32`，校对时间 `2026-02-03 13:39:32`。

同一任务中的 `51.9326 / 纸、纸板和纸浆纤维组成定量分析（不出证）` 是另一条精确项目，当前没有最终记录，不能与目标项目混合。

## B：Excel 原始记录

### 人工语义

系统可能直接打开唯一模板，也可能先要求选择模板。填写 Excel 并在 Excel 中保存、关闭后，还必须回到旧系统依次完成“保存”和“校对”。

一个任务项目可以要求多个结果；每个工作簿应作为独立写入操作处理。一个工作簿又可能提取出多条 `OriginalKeyData_CheckItem`，所以必须分别记录：

- `register_count`：登记行数；
- `file_reference_count`：`OriginalDataFilename` 非空的登记行数；
- `key_result_count`：关键结果行数。

三者不能互相替代，也不能把 `TemplateFilename` 误算成已上传文件。

### 数据链

实际事实链为：

```text
CheckRecordRegister.ID
  ├─ OriginalKeyData_CheckItem.OriginalRecordID
  ├─ OriginalKeyData_List.OriginalRecordID
  └─ OriginalKeyData_Other.OriginalRecordID
```

保存路径使用 `OriginalData/Files/<年>/<月>/<日>/<CheckItemID>/<OriginalDataFilename>`。旧 BLL 的保存链为 `CollectOriginalDataService` 采集工作簿数据、上传原始文件，再由 `CheckRecordRegisterDAL.SaveCheckRecordRegister` 在事务中保存登记与关键数据。校对会设置 `ProofTime` / `ProofUser` 并再次保存。

`CollectOriginalDataService` 构造函数会调用 `OriginalKeyDataConfigUtility.GetTableName`。旧 DAL 在缺少 `OriginalKeyDataTableMapping` 时会自动插入映射，因此即使只是构造服务也可能产生隐式写入。适配器必须在构造任何官方采集服务之前，用只读事务确认：

1. 项目与模板精确且唯一；
2. 映射存在且唯一；
3. 映射配置存在，且配置指纹与任务包一致；
4. 记录动态表是否存在。旧 DAL 在动态表不存在时会跳过 `DataScripts`，但仍会正常保存
   登记、关键结果、列表数据和其它数据；适配器复刻该行为，仅在表实际存在时回读它。

dry-run 不得构造该服务，也不得调用会创建目录的 `FileDirectoryUtility`。

### 样本 26A045793

目标项目为 `5103.5 / 纤维微观形貌`，任务要求 2 个结果。在线只读结果：

- 登记记录 2；真实文件引用 2；关键结果 2；
- 两条结果身份分别为“纵面”和“横截面”；
- 两条登记实际使用的模板均为 `微观形貌.xls`；
- 两条记录均已保存并校对；
- 该项目当前配置了 7 个可选模板，因此自动写入任务包必须明确模板名，不能只取第一个；
- `微观形貌.xls` 的映射唯一，采集配置为 9 行；对应的可选动态 DataTable 当前不存在，
  与旧 DAL 跳过 `DataScripts` 的正常分支一致，不影响现有两条结果；
- `CurrExcelOriRecord` 为 0，进一步确认本项目不走该旧链。

该任务还包含其他项目及文件，它们不能按样品号或 SpecialWool 文件后缀并入 `5103.5`。

## 读取输出契约

`tools/legacy_fibrecheck_probe` 在 Oracle `SET TRANSACTION READ ONLY` 中执行参数化 `SELECT`，强制回滚，并输出 `final_entry_view`：

- 每个项目严格对应一条 `Task_CheckItem`；
- 内部 ID 只输出稳定散列；路径只输出 basename 与路径散列；
- A/B 使用各自已证明的 `OriginalRecordID` 关联；
- 每个模板输出与写入器一致的映射配置 SHA-256 和 `mapped_table_exists`；模板引用按完整
  路径散列匹配，basename 只用于展示；
- 任一依赖查询失败、项目歧义、桥接缺失或跨项目关联都会 fail closed；
- 无法证明的计数输出 `null` 和 `incomplete`，不会伪装成 0。

2026-08-01 的两份最终核对文件位于忽略 Git 的受控临时目录：

- `.tmp/fibrecheck-reconciliation/260039770-final-view.json`
- `.tmp/fibrecheck-reconciliation/26A045793-final-view.json`

2026-08-03 重跑后两份结果均为 `complete`，只读事务已建立，查询错误数为 0。目标
Excel 模板的配置指纹已成功生成，配置行数为 9，`mapped_table_exists=false`。

## 写入安全协议

最终写入器默认只做 dry-run。真实执行必须同时满足：

1. 命令行显式提供 `--execute`；
2. 命令行显式提供 `--side-effect-permit-stdin`；
3. 所有只读预检完成并输出 ready 阶段后，标准输入精确收到一次 `PERMIT_REMOTE_WRITE`。

任务包只接受样品号、项目编号和精确项目名，不接受调用方提供的旧系统内部 ID。Excel 操作每包只允许一个工作簿，并固定相对路径、文件名、长度和 SHA-256；正式写入使用 GUID 文件名隔离目标，预检必须确认不存在冲突。

许可令牌之前只能执行登录、权限校验、参数化只读解析、计数对账、模板/映射检查和源文件哈希检查。越过副作用边界后发生的异常统一返回 `reconciliation_required=true`，禁止自动重试；后续只能使用读取模块核对远端登记、文件引用、关键结果、保存人与校对人。

当前模块尚未接到未完成的前置流程节点，也尚未执行首次真实写入。Probe 离线测试
22 项通过，x86 writer 离线测试 12 项通过；下一阶段应使用一份尚未录入的新任务和用户
确认的工作簿执行 dry-run，再进行一次受控实写验证。当前 Windows 11 ARM64 虚拟机已用
32 位调用进程实际完成 Excel 16.0 COM 激活与退出冒烟测试，Excel 位数不再是当前阻塞项。
