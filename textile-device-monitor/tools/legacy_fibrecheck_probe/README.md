# FibreCheck 旧系统只读对账探针

这个工具用于在能够连接 FibreCheck Oracle 数据库的 Windows 电脑上，对指定样品编号进行只读核验。它的用途是确认旧系统实际保存了哪些主记录、人员映射、定量结果、原始记录登记和任务项目，为后续连接器设计提供证据。

工具具有以下硬性边界：

- 不包含 `INSERT`、`UPDATE`、`DELETE`、`MERGE`、DDL、存储过程调用或文件复制代码。
- 建立数据库连接后的第一条 SQL 固定为 `SET TRANSACTION READ ONLY`。
- 所有业务查询均为参数化 `SELECT`。
- 无论成功或失败，连接关闭前都会调用 `rollback()`，不会调用 `commit()`。
- 不访问或修改 FibreCheck 文件服务器。
- 不输出数据库密码、完整连接串或数据源地址。
- 附件路径仅输出文件名、路径散列和条目数量。
- 数据库内部 ID 以散列形式输出，登录名会被掩码。

## Windows 使用方法

建议把本目录单独复制到可连接旧系统 Oracle 的 Windows 电脑，并在本目录执行：

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

先在完全不联网的清单模式中检查即将执行的 SQL：

```powershell
.\.venv\Scripts\python.exe probe.py `
  --fibrecheck-dir "C:\FibreCheck" `
  --sample-no 260187115 `
  --manifest `
  --output ".\260187115-manifest.json"
```

确认清单后再进行只读查询：

```powershell
.\.venv\Scripts\python.exe probe.py `
  --fibrecheck-dir "C:\FibreCheck" `
  --sample-no 260187115 `
  --output ".\260187115-reconciliation.json"
```

如 Python Thin 模式与旧 Oracle 版本不兼容，可安装与 Python 位数一致的 Oracle Instant Client，然后指定：

```powershell
.\.venv\Scripts\python.exe probe.py `
  --fibrecheck-dir "C:\FibreCheck" `
  --oracle-client-dir "C:\oracle\instantclient_19_22" `
  --sample-no 260187115 `
  --output ".\260187115-reconciliation.json"
```

如主配置中的数据库地址在当前网络不可达，而同一数据库存在备用网络地址（例如服务器的另一块网卡），可以仅覆盖 DATA SOURCE 的主机部分，凭据仍只读取主配置：

```powershell
.\.venv\Scripts\python.exe probe.py `
  --fibrecheck-dir "C:\FibreCheck" `
  --data-source "192.168.105.106/orcl" `
  --sample-no 260187115 `
  --output ".\260187115-reconciliation.json"
```

覆盖仅接受 `host[:port]/service` 形式；输出 JSON 中端点仍以散列表示，并带有 `data_source_overridden` 标记。

如主配置 `FibreCheckEntities` 的数据库账号在当前环境不可用（例如账号过期或只绑定特定地址），可以改用 FibreCheck 目录内其它配置条目的凭据，格式为 `配置文件名:条目名`：

```powershell
.\.venv\Scripts\python.exe probe.py `
  --fibrecheck-dir "C:\FibreCheck" `
  --credential-profile "WebService.dll.config:PanYuJianWu" `
  --data-source "192.168.105.106/orcl" `
  --sample-no 260187115 `
  --output ".\260187115-reconciliation.json"
```

凭据始终只从配置文件中读取，不通过命令行传递，也不会写入输出 JSON。

执行系统的任务推荐缓存只需要 `Task`、`Task_Sample` 和
`Task_CheckItem`。其独立 Windows
Bridge 会显式使用下面的轻量模式；默认探针行为和完整查询范围不变：

```powershell
.\.venv\Scripts\python.exe probe.py `
  --fibrecheck-dir "C:\FibreCheck" `
  --sample-no 26A029794 `
  --task-snapshot-only `
  --output ".\task-snapshot.json"
```

轻量模式仍以 `SET TRANSACTION READ ONLY` 开始并始终 `rollback()`，但只执行
上述两条查询，不生成 `final_entry_view`。输出会带有
`query_scope: task_snapshot`，供调用方确认查询范围。

`--fibrecheck-dir` 中必须存在主配置文件 `Toone.FibreCheck.Entites.dll.config`。工具只读取其中名为 `FibreCheckEntities` 的 Oracle 配置，不会自动尝试 HD 或其它区域配置。配置中的数据库账号仅在进程内用于连接，不会写入 JSON。

## 查询范围

探针会分别查询：

- `SpecialWoolManage`：完全匹配和前缀匹配；
- `User`：主记录中引用的检验、复核、创建和审核人员；
- `QuantificationTest` 与 `QuantificationTest_Detail`；
- `CurrencyItemRecordNew` 与 `CurrencyItemRecordNewDetail`：通用项目记录登记；
- `CurrExcelOriRecord`：仅用于诊断相似旧链，不作为最终录入事实源；
- `CheckRecordRegister`；
- `OriginalKeyData_CheckItem`、`OriginalKeyData_List` 与
  `OriginalKeyData_Other`；
- `Task` 与 `Task_CheckItem`；
- `CheckItem.OriginalDataInputUIClassName`：任务项目实际原始记录入口类；
- `StandardDocument JOIN Document`：任务项目实际配置的原始记录模板；
- `OriginalKeyDataTableMapping`、`OriginalKeyDataConfig` 与 `USER_TABLES`：
  只在进程内生成 writer 可校验的模板映射配置 SHA-256；动态表名和配置原文
  不会写入输出。

查询失败会按查询项记录安全错误类型和 Oracle 错误代码，后续查询仍会继续。输出 JSON 仍可能包含内部业务信息，只应保存在受控临时目录，不应提交到 Git。

## 最终录入读取视图

正常探测输出会增加 `final_entry_view`。它不是另一组数据库查询，而是只基于已经脱敏的查询结果生成的规范化只读视图：

- 每个 `projects[]` 元素严格对应一条 `Task_CheckItem`；项目范围只用精确
  `CheckItemID`，同一任务中出现重复 `CheckItemID` 时会拒绝分摊并标为
  `incomplete`。
- `expected_result_count` 直接来自 `Task_CheckItem.CheckCount`。
- `generic_record_count` 来自同一 `CheckItemID` 的
  `CurrencyItemRecordNew`。每条 `generic_records[]` 都包含主字段和按
  `SeqNum` 排序的四列表格：`standard_location`、`standard_value`、
  `real_location`、`real_value`。
- `register_count` 是同一 `CheckItemID` 的 `CheckRecordRegister` 行数。
  `file_reference_count` 只统计 `OriginalDataFilename` 非空的登记行；
  `TemplateFilename` 不计入上传文件，而由 `template_reference_count` 和
  `unique_template_names` 单独报告。
- `key_result_count` 来自 `OriginalKeyData_CheckItem`，但关联路径按已证明的
  数据结构拆开：存在同项目 `CurrencyItemRecordNew` 时，
  `OriginalKeyData.OriginalRecordID` 必须等于通用主记录 `ID`，并且必须有
  同项目 `CheckRecordRegister.OriginalRecordID` 反向指向该主记录；没有通用
  主记录的 Excel 项目才允许
  `CheckRecordRegister.ID = OriginalKeyData.OriginalRecordID`。视图通过
  `key_result_linkage_mode` 明示实际使用的路径，通用项目的结果放在对应
  `generic_records[].key_results` 中。
- `list_data_count`、`other_data_count` 分别来自
  `OriginalKeyData_List`、`OriginalKeyData_Other`，只走 Excel 的
  `CheckRecordRegister.ID` 路径。以上关联都会再次核对项目
  `CheckItemID`（`Other` 同时核对 `CheckItemNo`）；桥接缺失、无法关联或
  项目不一致时不会猜测归属。
- `entry_route` 来自 `CheckItem.OriginalDataInputUIClassName`；
  `configured_templates` 来自 `StandardDocument JOIN Document`。旧的
  `CurrencyExeclTemplateSet` 不再查询，`CurrExcelOriRecord` 只保留诊断用途。
- 每个 `configured_templates[]` 都包含独立的 `mapping_config`。其中
  `expected_mapping_config_sha256` 与 writer 使用相同的
  `DataTableName + OriginalKeyDataConfig` canonical 规则，配置按
  `SeqNum, KeyDataField` 排序；只有这两个字段同时重复时才拒绝生成指纹。
  `mapped_table_exists` 只是事实字段：旧 DAL 在动态 DataTable 不存在时会跳过
  `DataScripts`，因此值为 `false` 不会使指纹或项目变为 `incomplete`。
- 已有 Excel 登记只用 `CheckRecordRegister.TemplateFilename` 与配置模板的
  `path_hash` 精确匹配；basename 仅用于展示和错误提示，同名但路径散列不同
  不会被当作同一模板。未被已有登记引用的其它配置模板即使映射不完整，也
  不会污染已有结果的读取状态。
- `special_wool_prefix`、`QuantificationTest*` 和 `CurrExcelOriRecord` 都不会被
  用来推断最终录入项目、登记、文件或结果。

任一核心读取查询失败时，`final_entry_view.status` 和受影响项目的 `status`
会变为 `incomplete`。映射配置查询错误另列在
`writer_preflight_incomplete_queries`：generic 或没有 Excel 登记的项目不受
影响；已有 Excel 登记的目标模板无法证明时才会标为 `incomplete`。无法证明
的计数使用 `null`，不会把查询失败误报成 `0`。内部 ID 仍为散列；文件和
模板路径仍只包含 basename 与路径散列，`DocumentUploadIndex` 也会散列。

## 样品编号限制

样品编号必须为 9–20 位大写字母或数字，可选一个 `-` 后缀，后缀为 1–8 位大写字母或数字。例如：

- `260187115`
- `260187115-1`
- `26X909953`

通配符、空格、路径字符、引号和 SQL 片段都会在连接数据库前被拒绝。

## 运行测试

测试不需要 Oracle，也不会联网：

```powershell
py -3 -m unittest discover -s tests -v
```
