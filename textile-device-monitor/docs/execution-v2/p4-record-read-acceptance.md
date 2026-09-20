# P4 第三批：精确记录读取与字段指纹

日期：2026-09-18；基线：`2a21594`。本批完成更正操作需要的读取能力，复用查询 API、`connector.query@1`、只读 Bridge 和缓存队列。没有新增队列、人工任务或批准步骤。完整业务迁移候选仍为 **6/9**。

## 使用方式

继续调用 `POST /api/execution/v1/connector-queries`，增加两个 QuerySpec：

| query_ref | 必填 input | 结果 |
| --- | --- | --- |
| `legacy_fibrecheck.check_record.list@1` | `inspection_number`、`project_key` | 该任务项目的登记列表 |
| `legacy_fibrecheck.check_record.get@1` | 上述字段加 `record_ref` | 精确指定的单条登记 |

项目键从 `legacy_fibrecheck.task_snapshot.get@1` 返回的项目列表取得，记录引用从列表读取结果取得。二者不通过名称、结果文本或排列位置猜测。两种查询都支持 `refresh=true`，并保持原有权限及可选版本、契约摘要固定参数。

结果包含 `record_ref`、`content_fingerprint`、登记头、通用记录头与明细、关联关键结果/列表数据/其它数据，以及 `association_issues`。内部 ID 和文件路径沿用探针的脱敏格式。通用记录按 `CurrencyItemRecordNew.ID` 关联结果，并检查与登记的双向关联；Excel 按登记 ID 关联。相同结果文本的两条登记仍分别返回。

`content_fingerprint` 覆盖本次观察到的字段和关联行；查询返回顺序变化不影响它，字段或关联身份改变则会改变。它**不是数据库行版本，也不包含 Excel 文件内容摘要**。后续更正仍需在保存前读取当前字段并比较，不能用缓存指纹替代写入时的版本检查。

缓存缺少明细时返回 202、`records=null`/`record=null`，由调用方重读同一查询取得结果；完整查询确认没有记录时才返回空列表或 `lookup_state=not_found`。旧缓存返回 `stale`，刷新失败保留 `stale_error`，不把旧值标成新值。工作流查询节点继续返回缓存状态，不新增隐含暂停语义。

## 执行与兼容

- 普通任务推荐仍使用原来的 5 个只读查询；首次请求某编号的登记明细后，该编号后续刷新同时更新 6 组登记数据，避免任务计数和记录详情分别过期。缓存命中不访问 Oracle。
- 探针新增 `--check-records`，仅执行已注册的 SELECT；不打开 Excel、不读取文件内容、不连接写入队列。
- 迁移 `0012_connector_record_reads` 仅为现有缓存增加读取范围标记和明细 JSON，不改变 Run、操作或历史记录。API/Worker 先应用迁移并更新，再更新只读 Bridge 和 probe；未升级的 Bridge 只领取普通任务读取。
- 如果明细请求加入正在进行的旧式读取，先保存任务结果，再排队补读明细。并发请求合并为同一行，不撤销有效租约，也不重复领取。
- Kernel Pack **2.4.0**、Legacy Connector Pack / Connector **1.3.0**；Engine / 协议仍为 **2.4.0 / 2.1**。原生 NodeSpec 仍为 25 份；增加两份精确查询能力后共 **66 个绑定、64 ready**。只会任务查询的 Worker 不会领取记录查询。
- P1 冻结 Worker 仅补充 0012 迁移覆盖，应用和冻结能力摘要保持原样。新增[记录查询 Release 示例](./examples/v2-connector-record-query-smoke.json)。

## 验证

已通过后端回归、记录查询 API/Worker 等价性、精确能力领取、重复刷新与读取范围升级的 PostgreSQL 并发验证，以及 0012 的 PostgreSQL/SQLite 迁移检查。探针覆盖乱序稳定指纹、字段变化、相同文本不同记录、通用/Excel 不同关联、缺失/截断查询与孤立记录；Bridge 覆盖新旧范围协商和空任务结果。详细记录保存在忽略目录 `.tmp/workflow-p4-records-20260918/`。

| 实际编号 | 只读验证 |
| --- | --- |
| `260191178` | 找到微观形貌项目的 1 条 Excel 登记，关联问题为空；一次 Windows/Oracle 读取约 3.22 秒 |
| `26W006824` | 找到纸纤维项目的 1 条通用登记，关联问题为空；一次 Windows/Oracle 读取约 3.37 秒 |

上述真实观察结果继续经过隔离 API、缓存完成回传和 Worker 查询，验证列表与单条读取一致。时间是本次未命中缓存的观测值，不是性能保证。本批没有新增或更正检务记录，也未部署新 API/Bridge。

## 后续进展

2026-09-20：首个原记录更正与自动恢复已完成本地验证，见[第四批验收](./p4-record-update-acceptance.md)。以下保留本批结束时的计划。

## 当时的下一批计划

接入首种精确更正操作：以样品、项目、`record_ref` 和当前内容指纹定位，调用官方 DAL 的更新分支，保留修改前后值，并写后读取登记及结果投影。普通更正仍一次提交，不增加批准页。接着完成未知结果自动核对、通用 `external.operation` 及其余写入适配，再推进剩余三条完整业务迁移和 P5。`.twr`、Release 内置 fixture 执行器和完整绑定界面仍单列。
