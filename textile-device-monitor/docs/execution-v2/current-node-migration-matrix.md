# 当前 39 个节点迁移矩阵

> 状态：评审稿 v0.1
>
> 代码基线：`feature/execution-system` / `6d9a23f`；SpecialWool 的 `picture_records=[]`、`picture_count=0` 与 `main_record_verified` 已作为提交内正式契约纳入 P0 快照
>
> 枚举来源：`backend/app/execution/registry.py::_register_builtins()`
>
> 适用设计：Workflow Release v2、NodeSpec v2

## 1. 结论与统计

当前注册表共 **39 个 `type@type_version`**，其中 37 个可发布，两个连接器占位类型不可发布。当前类别与执行种类如下。

| 维度 | 分布 |
| --- | --- |
| 当前类别 | 连接器 9；Excel 7；文件 7；人工 5；基础 5；控制 3；结果 2；数据 1 |
| 当前 `execution_kind` | `automatic` 27；`human` 5；`external_side_effect` 7 |
| 发布属性 | `publishable=true` 37；`publishable=false` 2 |

按每个旧节点的**主决策**统计，39 项分为：

| 主决策 | 数量 | 含义 |
| --- | ---: | --- |
| 保留 | 18 | 语义本身稳定；可以改名、补齐 NodeSpec 或拆出策略，但不取消该能力 |
| 拆分 | 5 | 当前一个节点混合了多个可独立编排、独立失败或独立分发的职责 |
| 合并 | 7 | 多个节点只有参数、介质或方法差异，应共用一个强类型节点实现 |
| Connector 化 | 9 | 图节点统一为 `external.operation@1` 或移出图，具体能力进入 Connector pack |

“主决策”用于确保 39 项互斥计数；矩阵中的复合决策仍可能包含次要动作，例如“保留＋拆分策略”或“Connector 化＋合并节点壳”。

## 2. 决策原则

1. **保留已验证的运行安全边界。** 外部写入继续使用 durable operation、operation key、payload checksum、批准、Bridge 阶段检查点、回执和 `reconciliation_required`；不能退化为普通 HTTP、任意脚本或盲重试。
2. **合并执行壳，不合并业务契约。** 七种旧系统写入可共用 `external.operation@1`，但每种 `operation_ref` 仍拥有独立输入、预检、阶段、回执和对账契约。
3. **只有相同行为才提取基础节点。** 文件选择与图片选择可以共享选择模型；复杂 `.xls` 模板、LibreOffice/图片抽取和检务业务守卫保留为强类型领域能力，不能强行塞入“万能工作簿”或任意表达式节点。
4. **节点类型不承载流程实例 ID。** `selection_node_id`、`generation_node_id`、`review_node_id`、`confirmation_node_id` 等关系应由强类型输入端口、来源证明或批准回执表达；校验器不再要求固定节点 ID。
5. **NodeSpec 是执行契约，不只是节点目录。** 当前 registry 的 `execution_kind` 迁入 v2 `execution.kind` 后必须驱动分派；输入、输出、配置、资源、UI、重试和副作用属性必须完整声明。Workflow Release 解析依赖后锁定精确 `type_version`、能力包版本和实现摘要。
6. **旧版本采用快照兼容，不原地改义。** 已发布版本和运行中实例继续使用旧 `type@1` executor；新 release 通过显式 alias/migrator 生成目标节点。任何 breaking contract 都使用新 `type_version`。
7. **前端按 NodeSpec 渲染。** 表单、选择器、批准和冲突处理由 `ui_schema`/renderer capability 决定，不再通过 `task_kind` 或隐藏 config flag 猜业务类型。

## 3. 迁移阶段

| 阶段 | 内容 | 退出条件 |
| --- | --- | --- |
| P0 契约冻结 | 导出 9 份内置流程、39 份 NodeSpec v1 事实快照、典型输入输出、失败码和副作用回执 | golden fixtures 可重复；当前流程 checksum 和节点覆盖固定 |
| P1 NodeSpec v2 接入 | 补齐 schema、资源、UI、side-effect、executor binding；把旧 `execution_kind` 转为 `execution.kind` 分派；建立 v1 compatibility router | 不改工作流即可由 v2 registry 执行所有旧快照 |
| P2 基础节点收敛 | 引入 `human.form/select/approval`、`file.query/group`、`data.*`、`flow.*`、通用 workbook 原语 | 新工作流不再依赖人工节点隐藏标志或固定节点 ID |
| P3 领域能力拆分 | 拆分电镜/纸纤维查询，合并再生纤方法节点，抽取模板渲染底座但保留领域 wrapper | 领域 golden 输出、模板和图片结果逐字段/逐摘要等价 |
| P4 Connector 化 | 引入 Connector manifest、OperationSpec/QuerySpec registry、精确 Worker/Bridge capability 调度；迁移七种旧系统写入与受限只读查询 | 新写入 release 只引用 `external.operation@1 + operation_ref`；只读 query 不获得写入权限；未知写入结果仍需对账 |
| P5 特例退场 | 清除 engine/validation/frontend 硬编码；在无草稿、发布版和运行快照引用后停止创建 v1 | 全库引用审计为零；回放、滚动升级和回滚验收通过 |

## 4. 39 项逐项迁移矩阵

表中目标 ID 均为**拟议但在本组设计内规范唯一的 ID**。改名或合并产生的新 type 从 `type_version=1` 起步；只有保留原 type 且发生 breaking contract 变化时才递增到 2。能力包与 Connector pack 单独使用 SemVer。

<!-- node-matrix:start -->
| # | 当前 `type@version` | 当前类别 / kind | 主决策 | 目标 NodeSpec / `operation_ref` | 理由 | 阶段 | 兼容与回归要点 |
| ---: | --- | --- | --- | --- | --- | --- | --- |
| 1 | `artifact.publish@1` | 文件 / `automatic` | 保留＋拆分策略 | `artifact.publish@2`；批准策略改为输入 `approval_receipt` | 原子发布、崩溃恢复和不可中断临界区是核心能力；`confirmation_node_id` 不应把策略和具体节点 ID 写死在执行器中 | P2 | v1 快照仍走原发布器；migrator 把确认来源转换为批准回执端口。回归原子替换、目标冲突、取消/失败等待、回执持久化和已发布制品不可重复写 |
| 2 | `branch.condition@1` | 控制 / `automatic` | 保留 | `flow.branch@1` | 受限条件分支属于稳定编排原语；目标契约应显式声明表达式版本、default 端口和多命中策略 | P2 | alias 保留 v1 的“所有命中，否则唯一 default”语义；回归运算符、类型比较、default 唯一性及边选择事件 |
| 3 | `core.end@1` | 基础 / `automatic` | 保留 | `core.end@1`（NodeSpec v2，声明动态结果端口） | 结束节点是运行终态原语；当前多种未声明输入应通过受控 dynamic ports/`result_schema` 表达 | P1 | 不修改 v1 输出合并次序；回归多结束节点、跳过分支、最终 output 和 run terminal status |
| 4 | `core.project_match@1` | 基础 / `automatic` | 保留并领域化 | `project.match@1`，由 project-rules capability pack 提供 | 项目规则有版本、事实和业务身份，不能简化为任意表达式；但它不应冒充核心内核节点 | P3 | v1 alias 继续在执行时解析当前规则并输出实际 `rule_revision`；若 v2 改为锁定规则版本必须另升节点版本。回归编号边界、多个候选及运行期间规则修订 |
| 5 | `core.start@1` | 基础 / `automatic` | 保留 | `core.start@1`（NodeSpec v2） | 唯一入口、输入/global 注入和运行启动属于稳定内核 | P1 | 原类型直接兼容；回归唯一性、起始 ready 激活、输入/global schema 失败不创建半成品运行 |
| 6 | `data.microscopy_record_context@1` | 数据 / `automatic` | 保留并领域化 | `microscopy.record_context@1`，microscopy capability pack | 项目身份、样品名分析、判定要求是强业务规则；可把纯映射下沉，但不应变成任意脚本 | P3 | v1 executor 随旧快照保留；对相同任务快照与选图做逐字段 golden 比较，覆盖 microscopy/cross_section、身份缺失和多项目歧义 |
| 7 | `electron.group@1` | 文件 / `automatic` | 保留并泛化 | `file.group@1`，`group_strategy=same_acquisition_name` | 聚合同名采集文件是可复用的无副作用文件原语，不需要保留电镜专名 | P2 | alias 输出适配为旧 `groups/count`；回归大小写、扩展名、稳定排序、截断、跨目录同名和 root 边界 |
| 8 | `excel.classify@1` | Excel / `automatic` | 保留 | `workbook.classify@1` | 按模板特征分类是独立、纯读取的工作簿能力，与摘要提取失败语义不同 | P2 | v1 alias；回归 `.xls/.xlsx`、多 sheet、ambiguous、`data_only` 和损坏文件错误码 |
| 9 | `excel.extract_summary@1` | Excel / `automatic` | 保留 | `workbook.extract_fields@1` | 声明式字段读取可作为基础节点，但需强类型 field specs，禁止任意代码 | P2 | v1 `fields` 配置迁移为 field specs；回归公式/缓存值、空值、逐字段错误与 `fail_on_error` |
| 10 | `external.legacy_generic_check_record_entry@1` | 连接器 / `external_side_effect` | Connector 化＋合并节点壳 | `external.operation@1` + `legacy_fibrecheck.check_record.generic_entry@1` | 与其他外部写入共享调度外壳，但通用登记“不执行校对”的守卫、标准值要求和回执必须独立 | P4 | v1 alias 仅转换 envelope；operation key、payload checksum、审批、前/后远端写阶段与 receipt schema 不变。回归 W32 值、补标准值重开任务和未知结果对账 |
| 11 | `external.legacy_inspection@1` | 连接器 / `automatic`，不可发布 | Connector 化并移出图 | Connector dependency `legacy_fibrecheck`；无可执行 NodeSpec | 这是系统级占位符而不是可执行操作；目标应由 release 的 connector dependency/credential binding 表达 | P4 | 老草稿可显示 deprecated placeholder，但仍禁止发布；确认删除占位节点不会删除凭据槽、operation 历史或旧版本可视化信息 |
| 12 | `external.legacy_microscopy_check_record_entry@1` | 连接器 / `external_side_effect` | Connector 化＋合并节点壳 | `external.operation@1` + `legacy_fibrecheck.microscopy.check_record_entry@1` | 登记、校对、模板绑定和选图数量证明属于一个强类型远端 operation，不属于通用引擎分支 | P4 | 保留 registration workbook 摘要、项目身份、review receipt 和 controlled test override 限制；回归新增/复用记录、校对失败、写后未知和 reconciliation |
| 13 | `external.legacy_regenerated_fiber_count_upload@1` | 连接器 / `external_side_effect` | Connector 化＋合并节点壳 | `external.operation@1` + `legacy_fibrecheck.regenerated_fiber.count_upload@1` | 选择主单和上传是旧系统 operation；不能继续由 validation 硬编码上游节点类型和路径字符串 | P4 | v1 alias 生成同一 payload；强类型输入证明替代 `selection_node_id`。回归主单、多个附件、预检自动批准、重复投递和写后回执 |
| 14 | `external.legacy_special_wool_image_upload@1` | 连接器 / `external_side_effect` | Connector 化＋合并节点壳 | `external.operation@1` + `legacy_fibrecheck.special_wool.image_upload@1` | 图片类特纤主记录、项目身份和实机语义证明高度专用；当前基线契约明确禁止生成图片子记录，只合并节点壳，不与定性文档上传混成弱契约 | P4 | 保留原始记录 artifact digest、project key、主记录保存/读回，并验证 `picture_records=[]`、`picture_count=0` 与 `main_record_verified` 终止阶段；未证明环境继续 preflight 拒绝 |
| 15 | `external.legacy_special_wool_qualitative_review@1` | 连接器 / `external_side_effect` | Connector 化＋合并节点壳 | `external.operation@1` + `legacy_fibrecheck.paper_fiber.qualitative_review@1` | 文档型复核要求图片子记录数仍为零，和图片型复核不是同一个业务契约 | P4 | 依赖上传 receipt 的强类型引用而非 `upload_node_id`；回归零图片证明、主记录状态、重复复核和未知回执对账 |
| 16 | `external.legacy_special_wool_qualitative_upload@1` | 连接器 / `external_side_effect` | Connector 化＋合并节点壳 | `external.operation@1` + `legacy_fibrecheck.paper_fiber.qualitative_upload@1` | 纸纤维原始记录上传与图片型上传共享基础协议，但输入和“不生成图片子记录”后置条件必须独立 | P4 | v1 alias 保持单文件上限、主文件、项目身份和零图片验证；回归 `.xls` 内容摘要、重复上传与部分远端成功 |
| 17 | `external.legacy_special_wool_review@1` | 连接器 / `external_side_effect` | Connector 化＋合并节点壳 | `external.operation@1` + `legacy_fibrecheck.special_wool.image_review@1` | 图片类特纤复核要求主记录身份与零图片子记录证明，适合独立 operation spec，而不是 engine 类型分支 | P4 | 上传 receipt 作为 provenance 输入；回归主记录匹配、复核顺序、`picture_count=0` 前后不变、已复核幂等和 after-remote-write 失败 |
| 18 | `external.new_inspection@1` | 连接器 / `automatic`，不可发布 | Connector 化并移出图 | Connector dependency slot，例如 `inspection_system`；无可执行 NodeSpec | 尚无实现的系统占位不应占用图节点类型；真正接入时新增 Connector pack 和 operation specs | P4 | 老草稿保留只读占位和明确缺依赖提示；继续禁止发布，不能误认为配置 credential 后即可执行 |
| 19 | `file.electron_microscopy_gbt36422@1` | 文件 / `automatic` | 拆分 | `file.query@1` + `connector.query@1`（`automatic/read_only`，`legacy_fibrecheck.task_snapshot.read@1`）+ `microscopy.image_candidates@1` | 当前同时负责索引查询、编号目录/图片候选、旧系统任务缓存刷新和规则校验，具有不同等待、失败和复用边界 | P3/P4 | P3 先用 v1 composite adapter 保持旧图；P4 只允许同步/持久化缓存 QuerySpec，异步 Bridge 查询需另升内核语义。回归目录、1–10 张、truncated、pending/empty/stale 和项目身份 |
| 20 | `file.index_query@1` | 文件 / `automatic` | 保留并泛化 | `file.query@1` | 持久化索引查询是基础能力；filters、sort、limit 和 projection 应完整声明，物理 root 仍由 slot 绑定 | P2 | v1 alias 固定旧“最近天数＋编号”行为；回归路径不可逃逸、稳定排序、索引陈旧、limit 和空结果 |
| 21 | `file.paper_fiber_gbt4688_qualitative@1` | 文件 / `automatic` | 拆分 | `file.query@1` + `workbook.extract_fields@1` + `connector.query@1`（`automatic/read_only`，`legacy_fibrecheck.task_snapshot.read@1`）+ `paper_fiber.candidate_validate@1` | 当前混合目录搜索、`Sheet1!W32` 读取、任务快照和 GB/T 4688 项目规则；这些能力应能分别重试和组合 | P3/P4 | P3 先用 v1 composite adapter；P4 的 QuerySpec 限同步/缓存读。golden 回归 W32 显示值、损坏/缺 sheet、100% 独立词匹配、任务缓存状态和项目快照 |
| 22 | `file.regenerated_fiber_area_method@1` | 文件 / `automatic` | 合并 | `regenerated_fiber.find_records@1`，`method=area` | 与根数法共用匹配、profile/index 缓存和候选契约，差异应进入受限 method rule，不复制节点类型 | P3 | alias 固定 `method=area`；回归工作表识别、编号边界、已保存单元格、缓存 fingerprint、候选排序和诊断 |
| 23 | `file.regenerated_fiber_count_method@1` | 文件 / `automatic` | 合并 | `regenerated_fiber.find_records@1`，`method=count` | 与面积法只有强类型方法规则差异，适合共享领域节点；不降级为任意单元格查询 | P3 | alias 固定 `method=count`；与 v1 对同一语料逐候选/digest 比较，覆盖历史 `.xls/.xlsx` 与异常文件 |
| 24 | `human.confirm@1` | 人工 / `human` | 保留并收紧 | `human.approval@1` | 批准/驳回是通用人工原语，应产出带 subject digest、actor、decision 和时间的 durable receipt | P2 | v1 alias 接受旧 `{approved,...}` 结果；回归候选角色、拒绝路径、过期 subject、防篡改和 artifact.publish 只接受匹配 receipt |
| 25 | `human.file_selection@1` | 人工 / `human` | 合并 | `human.select@1`，`item_kind=artifact` | 文件/图片选择共享候选、min/max、primary、自动单候选和展示协议，差异可由 item schema/renderer capability 表达 | P2 | v1 输出 adapter 保留 `selected_files/primary_file_id/primary_file`；回归 task 快照透传、单候选自动完成、多选和主单要求 |
| 26 | `human.image_selection@1` | 人工 / `human` | 合并 | `human.select@1`，`item_kind=image`，folder selector 扩展 | 选择状态机与文件选择一致；图片缩略图、目录预选和数量上限是 UI/validation 扩展，不应复制执行内核 | P2 | v1 输出 adapter 保留 selected folder/image/primary 字段；回归 1–10 限制、截断提示、无效 ID、防跨 run 选择和任务状态透传 |
| 27 | `human.input@1` | 人工 / `human` | 拆分＋合并 | `human.form@1`、`human.decision@1`、`file.batch_place@1`；领域 form specs | 一个类型目前靠多个隐藏 flag 承担登记决策、纸纤维判定、显微记录录入和报告图片放置，导致 engine/frontend 按业务猜行为 | P2–P3 | 先以 v1 compatibility adapter 识别旧 flags；新 release 禁止这些 flags。逐一回归自动完成、动态 schema、standard value 重开、图片冲突/断点续放和原 task 输出 |
| 28 | `input.form@1` | 人工 / `human` | 合并 | `human.form@1` | 与 `human.input` 的基础职责相同；保留一个由 JSON Schema + UI Schema 驱动的通用表单节点 | P2 | `input.form@1` alias；回归 required/optional、nullable、candidate role、校验错误路径、保存后恢复和旧 submission shape |
| 29 | `parallel.join@1` | 控制 / `automatic` | 保留＋拆分语义 | `flow.join@1`，显式模式集合 `{all_selected, first_selected, collect_selected}` | 当前 `any` 实际是一次性 OR merge，不能同时表达竞速取消、收集和多次触发；模式必须有精确状态语义 | P2 | migrator 将 `all` 映射 `all_selected`、`any` 映射兼容的 `first_selected` 且不自动取消兄弟；回归晚到边、不重复执行、跳过边和失败传播 |
| 30 | `parallel.split@1` | 控制 / `automatic` | 保留 | `flow.fork@1` | 明确的 fan-out 节点便于可视化、事件审计和未来分支策略；其执行仍应无副作用 | P2 | v1 alias；回归全部选中出边激活、零出边校验、取消/失败传播和与 join 的到达计数 |
| 31 | `result.aggregate@1` | 基础 / `automatic` | 保留并泛化 | `data.aggregate@1` | 汇总上游结构是通用纯数据节点；需声明聚合模式、稳定 key 和冲突策略，而不是开放字典透传 | P2 | 默认模式保持 v1 pass-through/merge 顺序；回归缺失可选输入、同名 key、数组顺序和大对象持久化界限 |
| 32 | `result.regenerated_fiber_area_method@1` | 结果 / `automatic` | 合并 | `regenerated_fiber.read_results@1`，`method=area` | 与根数法共享现代/旧工作簿读取、图片恢复、源 fingerprint 和结果 envelope，差异属于方法 profile | P3 | alias 固定 area profile；逐字段和图片 digest 回归部位、含量、备注、失败文件、LibreOffice 路径和源文件变化检测 |
| 33 | `result.regenerated_fiber_count_method@1` | 结果 / `automatic` | 合并 | `regenerated_fiber.read_results@1`，`method=count` | 复用同一复杂领域读取器能减少重复，但不能用通用 extract 节点替代其格式、百分比和插图规则 | P3 | alias 固定 count profile；与 v1 比较 success/failed 计数、显示精度、平衡组分、图片定位及失败隔离 |
| 34 | `variables.set@1` | 基础 / `automatic` | 保留并改名 | `data.assign@1` | 受限变量赋值是基础数据原语；应声明写入 namespace、类型和冲突策略，禁止动态代码 | P2 | v1 alias 默认写 globals 且保持求值顺序；回归嵌套映射、null、覆盖、schema 不匹配和重放确定性 |
| 35 | `workbook.copy@1` | Excel / `automatic` | 保留 | `workbook.copy@1`（NodeSpec v2） | 工作副本与 mutation ledger 是受控写入链的必要起点，不能用普通文件复制绕过来源摘要和暂存 root | P1–P2 | 直接兼容 v1；回归 source digest、mutation id 幂等、暂存路径隔离、崩溃残留和源文件变化 |
| 36 | `workbook.microscopy_check_record@1` | Excel / `automatic` | 拆分 | `microscopy.check_record.render@1` wrapper + `workbook.render_template@1` 底座 | 模板加载/渲染可复用，但选图数量绑定、唯一项目身份和旧系统登记 workbook 契约必须留在领域 wrapper | P3 | v1 executor 保留；对 1–10 张图逐个 golden 文件/关键单元格/摘要比较，回归 template binding、复用、项目歧义和 `.xls` 兼容 |
| 37 | `workbook.microscopy_original_record@1` | Excel / `automatic` | 拆分 | `microscopy.original_record.render@1` wrapper + `workbook.render_template@1` + image placement capability | 字段绑定、模板渲染、图片放置和打印元数据可分层；但 GB/T 36422/横截面业务校验仍由领域 wrapper 守卫 | P3 | v1 executor 保留；回归两种 record family、1–10 图、日期/样品字段、版式、content digest、打印元数据与失败时无半成品 |
| 38 | `workbook.verify@1` | Excel / `automatic` | 保留并收紧 | `workbook.verify@2` | 保存后重读核对是 controlled write 的独立安全节点；输出应是绑定 mutation/artifact digest 的 verification receipt | P2 | v1 快照继续旧契约；migrator 显式生成 expectations。回归公式/值/类型差异、目标摘要、失败不可批准和重试确定性 |
| 39 | `workbook.write_cells@1` | Excel / `automatic` | 保留并收紧 | `workbook.write_cells@2` | 受控字段写入可复用，但 writes 必须是强类型 cell/range operations，并继续依附 mutation ledger | P2 | v1 writes adapter；回归 `.xls/.xlsx`、值/公式/日期/格式、越界、部分失败回滚、重复 attempt 和 working copy digest |
<!-- node-matrix:end -->

## 5. Engine / Validation / Frontend 特例清除顺序

清除顺序必须遵循“先有新契约和兼容层，再删除旧分支”，避免一次性重写破坏现有流程。

### 5.1 第一步：分派元数据化

- 引擎用 NodeSpec 的 `execution.kind` 选择 `automatic`、`human`、`external_side_effect` 执行通道，先替代 `engine.py` 中的 `HUMAN_NODE_TYPES` 与 `EXTERNAL_NODE_TYPES`。
- executor binding 从 Worker 启动时的逐项 `set_executor()` 转为能力包 manifest 注册；启动时验证所有 publishable 节点都有实现。
- Worker heartbeat 上报精确 `type@version`、pack SemVer 和 implementation digest；claim 必须按运行快照依赖过滤，之后才允许异构 Worker 滚动升级。

**验收：** 删除硬编码集合后，39 个旧快照仍能执行；缺 executor 的 release 在发布/调度前失败，不能领取后才报错。

### 5.2 第二步：人工节点特例外移

- 先上线 `human.form@1`、`human.select@1`、`human.approval@1` 和 compatibility adapter。
- 把显微记录录入、打印确认、既有登记决策、纸纤维判定、standard value 重开等内容迁入 versioned form/decision specs 或领域 validator。
- 把报告图片的物理放置迁入 `file.batch_place@1`；只有冲突决策停在人工节点，长文件 I/O 继续在 Worker 且不持有慢 SQLAlchemy 会话。
- 最后删除 `_normalize_human_submission()` 对 `task_kind`、`paper_*`、`legacy_*`、`report_image_placement` 的分支。

**验收：** 新 release 中上述隐藏 flag 数量为零；旧任务恢复、候选角色、动态表单、自动完成、重开和图片放置崩溃恢复全部回放等价。

### 5.3 第三步：校验规则契约化

- 通用 validation 只检查 DAG、端口类型、资源/凭据绑定、依赖、循环/分支/join 语义及副作用策略。
- `selection_node_id`、`generation_node_id`、`upload_node_id`、`review_node_id`、固定 `record-input` / `registration-decision` 等校验迁为来源证明、强类型端口或 Connector operation composition rule。
- `artifact.publish` 的“必须经过确认”改为验证不可伪造且 subject digest 匹配的 approval receipt，不再要求特定上游节点 ID。
- credential system 枚举和 root access 表由 pack manifest 声明，release 只引用逻辑 slot。

**验收：** 重命名任意流程节点 ID 不影响合法性；交换成错误来源或错误 receipt 仍在发布前被拒绝；旧系统 operation 的约束没有减弱。

### 5.4 第四步：外部操作注册表化

- 为七种 operation 建立独立 schema、preflight、stage graph、receipt verifier 和 reconciliation policy。
- 为旧系统只读任务快照建立 QuerySpec；v2 首版只允许同步或已持久化缓存读，不暗中新增 Bridge 异步状态。
- 引擎只认识 `external.operation@1` 的持久状态机，不再按旧 node type 映射 preparer。
- Bridge claim 以 `operation_ref@version` 和 digest 匹配能力；新旧 Connector pack 可并存，运行快照精确锁定实现。
- 两个不可发布的系统占位节点移入 release dependencies/credential bindings。

**验收：** 七种外部流程的 operation key、payload、阶段、回执和故障注入结果与 v1 等价；`before_remote_write` 可安全重试，`after_remote_write` 未知结果只能进入 `reconciliation_required`。

### 5.5 第五步：前端完全由 NodeSpec 驱动

- 设计器由 `config_schema + ui_schema + ports` 生成配置表单、变量选择器和连接约束，原始 JSON 仅作为高级只读/受控编辑入口。
- 任务收件箱由 `renderer_capability` 和提交 schema 选择组件，不再检测 `microscopy_record_input`、`microscopy_print_confirmation` 或具体旧 node type。
- 旧 alias 显示为“兼容节点”，可查看但新建时不可选；迁移预览展示旧节点到目标节点/operation 的逐项变化。

**验收：** 增加一个只依赖现有 renderer capability 的 NodeSpec/Workflow Release 时无需修改前端；缺 renderer 时阻止发布或使用声明的通用安全 renderer，不静默降级。

### 5.6 最后：v1 退场门槛

只有同时满足以下条件才可停止注册某个 v1 executor：

1. 没有草稿、已发布 WorkflowVersion、运行快照、待办人工任务或未结外部 operation 引用该 `type@1`；
2. 所有默认流程已生成 v2 release，并通过 golden、故障注入和真实受控环境验收；
3. alias/migrator 可重复执行且 checksum 稳定，迁移失败不会覆盖原草稿；
4. 回滚到上一发布版仍能找到其精确 NodeSpec、能力包和 Connector 实现；
5. 审计事件保留原始 node type、operation_ref、版本和 implementation digest。

## 6. 总体验收清单

- 注册表枚举与本矩阵严格一一对应：39 行、无遗漏、无重复。
- 现有 9 份内置流程迁移前后，输入、节点结果、最终输出、制品 digest 和外部回执均有 golden 对比。
- 所有新节点 config/input/output 均能被 NodeSpec schema 覆盖，不再依赖 `additionalProperties=true` 吞掉隐式字段。
- Workflow Release 导入阶段能准确报告缺失节点、能力包、Connector operation、资产和 Worker capability。
- 已启动运行继续使用 definition/contract/implementation 快照；导入或发布新 release 不改变其行为。
- 文件系统、Excel 和外部写入均保留幂等、租约、attempt、崩溃恢复、原子边界和审计事件。
- 任意外部写入在结果未知时不盲目重试，仍进入 `reconciliation_required`。
- 重组现有 operation、表单、规则和模板映射只下发 Workflow Release；为既有外部系统增加新的远端 operation 只更新 Connector pack；只有新增运行状态或调度语义才需要核心应用更新。
