# 头脑风暴变更日志

## 2026-08-15：移除受控测试覆盖通道

- 电镜流程运行输入中的"受控测试覆盖"字段删除（此前会被运行准备页渲染为
  普通文本框，面向检验员无法解释）；1→2 追加登记由正式业务通道覆盖
  （份数=1 已有登记 → 人工确认 → 服务端签发 `existing_record_decision`）。
- 后端移除：input_schema 字段与映射、`_controlled_final_entry_override`
  准备门禁（电镜与纸类通用登记两处）、`EXECUTION_CONTROLLED_FINAL_ENTRY_TEST_SAMPLE_NO`
  设置项、预检摘要的 controlled_test 字段；回执改为容忍 null 但拒绝任何
  非空声明（兼容当前已部署 Writer）。
- 目录播种自动升级：微观形貌旧校验和登记进兼容集合（v3→v4），横截面新增
  升级路径（v1→v2），线上库已验证两条流程的发布定义均不再含该输入。
- registry 节点 input_schema 与 validation 映射特例保留（兼容存量已发布
  定义）；Writer/Bridge 侧三重绑定代码休眠，并入下次 Writer 编译清理。
- 后端 577 全绿（移除 3 条受控覆盖专项用例，对账用例改走正式 append 通道）。

## 2026-08-15：项目匹配规则配置化（流程管理实时可编辑）

- 编号 → 项目类型的匹配规则（项目名别名、测试方法、目录策略、结果探针）
  从代码常量迁移到新表 `execution_project_rules`（迁移 0007），管理员在
  流程管理/设计器中实时编辑、保存即生效；详见 `14_项目匹配规则配置化.md`。
- 三个匹配器改为从规则表取事实；profile 缓存按 `rule_revision` 自动失效；
  新增通用节点 `core.project_match` 与节点 config `match_rule` 参数
  （`x-param-type: project_rule_ref`）；节点输出钉 `rule_key/rule_revision`。
- 写门禁经 `rule_for_facts` 反查（电镜按 binding 编号+名称，纸类按规则
  项目名/方法）；模板绑定 SHA256 与写入契约常量仍代码钉死。
- API：`GET/PUT /project-rules[/{key}]`、`POST /project-rules/test` 干跑；
  PUT 校验配置、revision+1、写审计日志。
- 后端 580（+13）、前端 118（+3）全绿；既有匹配结果等价复现。

## 2026-08-15：移除样品识别单值确认勾选

- 电镜/横截面“确认原始记录信息”与纸类“确认样品识别与判定信息”在任务单
  样品识别只有 1 个候选时，不再渲染“确认本次录入的样品识别为…”勾选框：
  前端只读展示“已自动填入”，查看即确认，减少一次无效点击。
- 后端同步移除 `sample_identity_confirmed` 提交校验与输出字段
  （`microscopy_sample_identity_confirmation_required`、
  `paper_sample_identity_confirmation_required` 及 final-entry 准备门禁
  一并删除）；多候选交互不变：候选列表可选择/编辑，提交值仍必须严格属于
  任务单候选，份数不一致只警告不阻断。
- 既有发布定义与校验和不变；catalog 静态 schema 仍容忍旧前端提交该字段
  （可选、忽略），纸类开放任务经 `effective_human_task_form_schema`
  自动获得新 schema。

## 2026-08-15：电镜—纤维横截面流程变体（260191285）

- 新增 `electron-cross-section-gbt36422` 工作流：与微观形貌同一 DAG，各节点
  config 钉住 `record_family=cross_section`；家族注册表
  `microscopy_families.py` 是唯一事实源，写门禁按任务项目编号+名称精确复核。
- 差异点：任务项目匹配 5103.426 / 纤维横截面 / GB/T 36422-2018；生成原始
  记录 A1 写“纤维横截面原始记录”；登记模板族为旧系统配置的三个横截面
  模板（1/2/3 张图，映射指纹由 260191285 只读探针签发），选图上限 3；
  登记工作簿文件名 `…-纤维横截面-检验记录登记.xls`。
- Writer（final-entry）放行 5103.426 三元组并新增三个横截面模板白名单；
  新增跨家族模板绑定拒绝门禁
  `microscopy_final_entry_template_family_mismatch`。
- 首验中同步修复：写桥 `SUPPORTED_EXCEL_PROJECTS` 三元组集合（原硬编码
  5103.5 微观形貌）；封顶失败外部操作的复活路径
  （`_rearm_expired_external_operation` 接受 failed+空 verification 的
  结构性未写入状态，逐 attempt 校验写边界，重试节点可重新预检）；
  VM 桥写 Writer 钉值补齐（检验员绑定重编译后漏更导致写桥无法启动）。
- 260191285 真实链路首验通过：上传 260191285-1、复核、登记 1→2 并校对；
  探针事后核对既有 08-08 人工登记未受影响。
- 细节与模板指纹清单见 `13_电镜纤维横截面首版.md`。

## 2026-08-13：登记工作簿格式继承修复、顺号复核校验修正与桥运行环境根因记录

- 复核批准前校验与 prepare 对称，跟随上传回执的实际写入编号，修复人工删除
  旧记录后 Writer 顺号回退导致的 `special_wool_upload_result_changed` 误拒。
- `biff_patch` 修复登记模板格式被改动：Z7、I9–I11、G12、G13、BI8 等空格
  由模板 MULBLANK 区间承载，补丁器此前未识别该区间（`colLast` 误读末字节）
  而插入外来 XF 的重复记录，Excel 用它覆盖了模板底色/边框。现围绕目标列
  拆分区间并继承其逐列 XF（Z7=79 等）；范围校验器按同 id 配对并允许插入
  MULBLANK/BLANK 段，数值记录仍禁插。260190894 真实链路字节级核验通过。
- Windows 写入桥计划任务以最高权限运行时看不到交互会话的 `Z:` 盘映射
  （`EnableLinkedConnections` 未开），这是计划任务方式读取暂存文件连续
  `DirectoryNotFoundException` 的根因；VM 桥 `ExecutionStagingPath` 已改为
  UNC 路径 `\\Mac\Home\Downloads\exec-stage-sync`。桥 `ApiBase` 端口必须与
  前端容器发布端口保持一致（本次 8088→80 漂移已对齐）。
- 已知引擎缺口（待处理）：外部操作确定性失败重试封顶后，“重试节点”无法
  复活 failed 状态操作（rearm 仅覆盖 expired）；运行途中人工增删旧记录会
  使冻结的 `register_count` 与实况漂移，运行无自愈路径。

## 2026-08-13：纸类多份登记门禁移除、print-confirm 拓扑变化口径记录

- 纸类通用登记 prepare 阶段删除多份容量门禁（原
  `paper_registration_count_exhausted`）：多份项目已有登记数达到或超过任务份数时
  不再拒绝新增，与电镜链路、FinalEntry Writer 联网预检和桥回执转换的既有口径
  一致；对账侧本就对 `expected_task_count>1` 按超量追加放行。单份项目已有登记仍
  必须人工选择“直接新增”，并携带校验和绑定的 `existing_record_decision`。
- 记录一处此前未落文档的流程拓扑变化：电镜默认流程（v3）不再包含
  print-confirm 人工节点，原始记录生成后直接进入上传节点；旧版定义经
  `include_print_confirmation` 保留该节点，引擎对在途旧版运行仍强制打印确认。
- `docs/legacy-fibrecheck-final-entry.md` 的多份容量描述同步修正为与代码一致
  （原文误写“多份项目已有登记数小于任务份数才可新增”）。

## 2026-08-13：特种毛图片上传/复核支持任务多份数并完成真实链路验收

- 修复图片上传/复核 Writer 将任务项目总 `CheckCount` 错当成单次记录份数、硬性
  要求为 1 的问题。现在任务总份数必须为正整数并与服务端签发值精确一致；单次
  上传/复核仍严格只处理 1 条记录，不放宽单次副作用边界。
- 使用 `260190894` 在 Windows 11 VM 对真实旧系统完成端到端验收：纤维微观形貌
  `CheckCount=4`、样品识别为“浴巾，枕套，床单，被套”，本次选择“被套”；
  图片上传使用新编号 `260190894-4`，随后完成复核和检验记录登记。
- Writer 离线契约自测 27 项通过，完整 x86 构建产物
  `FibreCheckWriter.exe` SHA256 为
  `3bd826efcf2f606b9c15d3e77900de4d713c8ef6f49d0bee659cc6152c791b41`，
  已部署到 VM，安装自检通过，仓库打包钉值已同步。图片类写入使用实际登录的
  旧系统 Staff 身份；回读 `CheckUser1` 时归一化旧库定长字段的首尾空白。
- FinalEntry 登记链路同步取消多份项目的登记容量上限：`CheckCount>1` 时即使
  实时登记数已经达到或超过任务份数，也允许继续新增；仅单份项目已有登记时
  保留服务端签发的明确追加决定。Windows VM 离线自测 62 项通过，新
  `FibreCheckFinalEntryWriter.exe` SHA256 为
  `a8349314c8116c499e751872e2948d8a4fdb53788113cf43dee043f2d0127a8b`，
  仓库打包钉值已同步，且已部署到 VM 并完成本次真实登记验收。
- 本地 Docker 已重建并发布电镜/纸类默认流程 v3；Windows Bridge 安装目录已
  更新新版写桥、快照桥、probe 与两份 Writer，安装自检通过。使用新版
  Snapshot Bridge 对 `260190894` 做写后只读刷新，确认 `register_count` 从 4
  增加到 5。
- 上传首次写后因旧库 `CheckUser1` 定长空白被严格比较误判，系统没有盲目重试；
  修正 Writer 后用 `--reconcile-existing-upload` 只读采用既有主记录、图片子记录
  和服务器文件，确认没有重复上传，再继续复核与登记。最终运行
  `e26bed37-1cca-45b5-989c-3a520d685da0` 全部节点完成，最终登记回执证明
  `expected_existing_register_count=4`、`resulting_register_count=5`、
  `sample_identity=被套`、`proofed=true`。
- 上传、复核、登记的自动批准不再设置过期时间，也不依赖浏览器倒计时。多份项目
  不再为登记数量强制刷新或打断用户；仅单份项目已有登记时保留“直接新增/取消”
  业务选择。验收结束后服务端三项真实写入开关已恢复关闭，VM 写入桥计划任务
  保持停止，只读任务快照桥继续运行。
- 先前生成的 `textile-execution-bridge-setup-1.0.0.exe` 使用旧 Writer 钉值，不再
  作为本轮最终安装包；下次发布安装包须使用上述 `3bd826ef...` 产物重新构建。

## 2026-08-11：通用/电镜多份登记、样品识别与外部操作自动交付

- 任务快照升级为 schema v5：只读探针按任务项目统计当前
  `CheckRecordRegister` 数量，Snapshot Bridge 将 `register_count` 与稳定
  `project_key` 一起签发；登记前再强制刷新一次，避免并发流程使用过期计数。
- 检测份数为 1 且已有登记时，流程暂停并要求用户选择“直接新增”或“取消”；
  其它尚有份数容量的项目自动继续，达到任务份数则在写入前拒绝。
- 样品识别按中文逗号、英文逗号和顿号拆分。单值自动填入并要求确认；多值提供
  可编辑候选；识别数量与检测份数不同只警告。Backend、Bridge 与 Writer 均要求
  最终值属于任务单选项，Writer 在联网只读预检中再次核对后才允许保存。
- `CurrencyItemRecord.SampleDescription` 与登记表顶部 `SampleIdentity` 使用同一个
  已确认值，并纳入写后回读。单份已有记录的追加选择以校验和绑定的
  `existing_record_decision` 传至 Writer，不能由 Bridge 临时伪造。
- 纤维微观形貌在生成原始记录前收集判定依据、指标要求、测试结果、判定和备注；
  原始记录写入 `B33/I33/B34/I34/B35`，登记模板 `Z7` 与旧系统顶部
  `SampleIdentity` 均使用同一个经任务单校验的样品识别。
- 旧系统上传、复核、登记不再等待浏览器倒计时：部署能力开启时由 Worker 在预检
  成功后自动批准，离开运行工作台不会中断交付；页面只展示进行状态与异常对账入口。
- Windows 11 Parallels VM 完成 x86 Writer 编译及 56 项离线自测；新
  `FibreCheckFinalEntryWriter.exe` SHA256 为
  `c70baeeba114f7cdd6fa95be3f73d902d3f84d5f1fc6e86e814f7b6167f4934f`，
  仓库打包钉值已同步。本轮没有连接 Oracle 执行真实写入。

## 2026-08-11：纸类判定步骤人工确认“标准值与允差”（数据源：说明列/M32）

- 背景：此前判定变体的标准值列由后端直接镜像 W32 实测值（Writer 强校验
  StandardValue == RealValue）。实际业务中存在“录入的标准值与原始记录
  不一致”的单据，同文镜像会写错，因此判定流程恢复人工确认该字段。
- 事实核对（VM 只读查 Oracle）：人工参考样例 260191286、260174495 的
  登记记录中 StandardValue 均与 RealValue 同文（如“木浆 100”），任务单
  说明列（260174495 为“定性，100%木浆”）并未被照抄——故默认仍预填
  W32 同文结果，人工按单修改。
- 后端：`paper_fiber.py` 随 W32 同读 `Sheet1!M32`（候选
  `result.m32_value`，profile version 1→2 使旧缓存失效重读）；
  `_paper_judgement_form_schema` 新增必填字段“标准值与允差”，
  `default` 预填所选记录 W32，`x-copy-sources` 携带任务单说明列
  （快照 `remark`）与 M32 两个数据源（表单生成时按 DAG 定义反查已成功
  的 human.file_selection 节点输出，不改 DAG）；提交校验
  `paper_standard_value_required`；登记包 `standard_value` 改取人工
  提交值（判定必需，缺失报 `paper_fiber_standard_value_required`；
  未要求判定时仍强制留空）。
- Writer（PackageModel 纸类判定变体）：标准值校验由“必须与实测值同文”
  放宽为“必填非空白”，允许人工改写；离线自测新增“人工改写标准值可
  通过”用例（50 项过）。新 FibreCheckFinalEntryWriter.exe SHA256
  `9495717078c2e169909dd8d53a46b02549e2beb5aa8f4728f39f3b5d3e945ed1`，
  已部署 VM 安装位与打包 staging，`BridgeConfig.psd1` 钉值、staging
  manifest/config 均已同步（`Test-BridgeInstallation` PASS）。
- 前端：`SchemaFields.jsx` 支持字段 `default` 预填与 `x-copy-sources`
  数据源展示（文本可拖选、复制图标、“填入”一键写入字段）。
- 跨层契约闭环：预检摘要新增受 `payload_checksum` 保护的
  `judgement_contract`，Windows Bridge 按摘要逐字段核对判定依据、
  判定结果和人工标准值；批准卡片与最终确认弹窗同时展示 W32 和实际
  将写入的“标准值与允差”。旧版已签发包仍只允许 StandardValue 镜像
  W32，不会借升级放宽历史包。
- 升级兼容：旧版已创建但尚未提交的判定任务动态使用新三字段表单；
  已完成旧判定、尚未生成登记预检单的运行会在任何外部副作用前自动
  退回“确认判定信息”，保留原判定依据/结果草稿并要求人工补填标准值。
- 打包：仓库 `packaging/windows-bridge/bridge-package.psd1` 已同步新
  FinalEntry Writer SHA256，正式构建不再因旧钉值拒绝新产物。
- 测试：后端 519 过（仅既有失败 1），前端 106 过；纸类判定相关用例
  覆盖默认值/数据源生成、缺标准值拒绝、登记包改写、非判定留空。
- 本轮定点回归：后端纸类/执行/外部操作 163 项及 58 个子测试通过，
  Bridge 34 项及 10 个子测试通过，前端判定字段/批准界面 17 项、
  SchemaFields 主要调用页面 38 项通过；
  后端回归直接把真实签发视图送入仓库 Bridge 校验器，防止再次出现
  两层测试分别通过但真实契约不通的问题。
- 约束文档同步：`.tmp/execution-system-brainstorm/10_纸纤维鉴别分析首版.md`。

## 2026-08-10：特种毛上传按旧系统实况顺号（容忍人工增删漂移）

- 背景：旧检务系统的特种毛检验记录存在人工新增、删除。此前 Writer
  在只读段/写锁内强制"预检请求号 == 旧系统第一空闲号"
  （`target_allocation_stale` / `target_already_exists` /
  `target_conflict_under_lock`），快照一旦与实况漂移（TTL 15 分钟
  内的人工变更），运行即进入对账/失败，无法正常推进。
- 新规则（用户拍板）：实际写入编号以**写锁内旧系统当前实际记录**
  为准，取第一空闲号（基号、-1、-2…，规则不变）；请求号仅作为
  预检展示与审计。写入与人工并发撞号可容忍（记录可随时人工删除），
  不加额外分布式锁。
- Writer（legacy_fibrecheck_writer SpecialWoolExecutor.ExecuteUpload）：
  只读段不再因漂移失败，阶段事件改报 requested/planned/
  renumber_planned；写锁内用 `ReadOccupiedUnderLock` 重算第一空闲号，
  文件残留（人工只删记录未删文件）时逐号推进；记录、图片、文件、
  回读校验全部使用实际号。回执新增 `requested_sample_number` 与
  `renumbered`，`target_sample_number` 变为实际写入号；
  `target_conflict_under_lock` 仅作理论兜底保留。
- 后端：上传回执校验接受顺号改写（requested 必须等于预检单目标号，
  实际号必须同编号族，renumbered 标记必须与实际一致；无顺号字段的
  旧回执仍要求完全一致）；图片/纸类两个复核准备节点改为跟随上传回执
  的实际写入号；本地占用集合并入已完成操作的回执实际号。
- 测试：新增"顺号回执通过校验 + 标记不符/请求号不符/族外编号均
  拒绝 + 旧版回执仍严格 + 复核跟随实际号"用例；后端全量 515 过
  （仅既有失败 1）。Writer 离线契约自测 23 项过。
- 部署：新 FibreCheckWriter.exe SHA256
  `d23f3f11cae2c5a493f97c9f4ca9a240c6109a62cb90397214f9e6c45775be81`，
  已同步 VM 安装位 `C:\TextileExecutionBridge\writers\FibreCheckWriter\`
  与打包 staging（manifest 哈希已更新）。
- 追加修复（同日，运行 82aea921 / 26W006740 暴露）：桥端
  `validate_special_wool_qualitative_upload_receipt` 同样按旧契约严格
  拒绝新回执（未知键 requested_sample_number/renumbered + 编号/文件名
  不再等于预检值），导致**写入已成功但回执被桥拒收**、操作进入对账。
  桥端校验同步顺号规则（requested 绑定预检单、实际号限同族、
  最终文件名按编号前缀替换推导、renumbered 必须属实；无顺号字段的
  旧回执仍严格）；桥测试新增 3 个用例（32 过）。已部署
  `C:\TextileExecutionBridge\app\legacy_fibrecheck_bridge\bridge.py`
  并更新 `BridgeConfig.psd1` 的 Writer 钉值（该钉值由
  Build-BridgePackage.ps1 在打包时重新生成，源码模板无需改）。
  写桥计划任务已重启加载新代码。26W006740 由用户人工接手，卡住的
  运行保留现状、不要重跑同号（重跑会按实况顺号写出 -2）。
- 多份数（check_count>1，如 26A045793 份数 2）本轮不处理，仍按
  `legacy_special_wool_task_project_count_invalid` 快速失败。

## 2026-08-08：人工任务交互提效（免领取、待办角标/气泡、收件箱轮询、大图内选图）

- 免领取：`submit`/`draft`/`reject` 在任务 open 且未领取时由服务端隐式
  领取（engine.py `_ensure_human_task_ownership`，追加
  `human_task.claimed` 事件含 `implicit: true`）；已被他人领取仍
  `human_task_not_owned` 冲突兜底，并发保护不变。前端 HumanTaskCard
  删除"领取并处理"按钮，开放任务直接呈现表单；显式
  `/human-tasks/{id}/claim` 接口保留兼容。状态文案"待领取"→"待处理"。
- 待办提醒：ExecutionChrome 每 15 秒轮询 `/human-tasks`（隐藏标签页
  跳过），头部"待办任务"按钮显示 Badge 计数；检测到新任务 ID 时右下
  角 notification 气泡（含标题与"前往处理"按钮）；收件箱页内跳过该
  轮询（页面自有 10 秒轮询）。testServer.js 增加全局默认空待办
  handler，避免 onUnhandledRequest=error 误伤其他页面测试。
- 收件箱自动刷新：每 10 秒静默轮询列表与当前详情，无需退出重进；
  HumanTaskCard 本地未提交编辑按 revision 保护不受影响。
- 大图内选图：ExecutionImageSelector 预览弹窗新增底部工具条（已选
  计数 + 选为结果图片/取消选择按钮），已选图片在舞台上角显示对勾
  角标；左右切换与键盘导航保持不变。
- 测试：后端新增"open 任务直接提交自动领取/他人已领取仍拒绝"两个
  用例，全量 514 过（仅既有失败 1）；前端收件箱首测改为直接提交，
  全量 106 过。

## 2026-08-08：纸类 query 节点快照等待（替代立即失败）

- 背景：前一条目的 fail-fast 解决了裸 `mapping_value_missing`，但
  "快照抓取中 → 运行直接失败 → 人工重跑"仍影响效率；实测竞态通常
  只差几秒（快照桥 15 秒轮询、单次抓取数秒，289db134 的快照在运行
  启动后约 5 秒落地）。
- 纸类 query 节点在快照 `pending` 时改为节点内有限等待：每 2 秒
  轮询一次快照缓存，最长 `EXECUTION_TASK_SNAPSHOT_WAIT_SECONDS`
  （默认 60 秒，0 表示不等待）；等待期间先 commit 一次使本节点
  入队的刷新请求对快照桥可见（快照状态接口同款模式）；节点租约
  由 worker 的 `_LeaseHeartbeat` 后台续期，阻塞不会丢租约。
- 等待中检测到运行取消（cancel_pending/cancelled/failed/
  failure_pending）立即退出等待，按原 pending 错误收尾，取消流程
  不被拖住。
- 超时仍按 `task_snapshot_pending` 失败兜底；快照 `failed` 仍立即
  失败（桥故障时等待无意义）。DAG 定义不变（无版本升级），行为
  变化仅在执行器。
- 测试：等待成功后继续、取消立即退出、0 等待超时兜底三个用例；
  后端全量 512 过（仅既有失败 test_config_security 1 个）。

## 2026-08-08：任务单快照未就绪的快速失败与状态接口分流程评估

- 背景：运行 289db134（26W006742）在快照桥抓取完成前 4 秒启动，
  query 节点容忍 pending 输出 `matched_task_project=null`，upload-record
  映射时裸报 `mapping_value_missing`（paper_fiber.py 下游节点无条件映射
  `matched_task_project`）。
- 纸类 query 节点快速失败（paper_fiber.py `_paper_fiber_executor`）：
  任务单条件缺失且快照 `pending` → `task_snapshot_pending`
  （提示稍后重新运行）；快照 `failed` → `task_snapshot_unavailable`
  （提示检查快照连接器）；快照就绪但任务单无纸类项目 →
  DAG `require_full_task_match` 翻为 `true`，走既有
  `paper_fiber_rule_not_matched`。失败发生在首节点，带可操作提示，
  不再出现裸映射错误。电镜流程不改：其下游不硬依赖
  `matched_task_project`，选图人工任务按设计容忍 pending 并轮询。
- 目录升级：纸类流程 v4（require_full_task_match=true），v3 校验和
  `cebf82bb4abad019…` 加入已知系统校验和集合，未被人为修改的流程
  自动升级，历史运行钉在各自版本。
- 快照状态接口 `task_snapshot_status`（electron_microscopy.py）：
  任务单条件改为对电镜别名与纸类项目名/方法两套规则取最佳匹配，
  纸类编号不再误报 `missing_conditions=[task_item_name, test_method]`。
  前端无需改动——该接口的轮询仅由电镜图片选择人工任务触发，
  纸类流程的快照状态来自 query 节点输出（本就走纸类规则）。
- 测试：纸类新增 3 个快速失败用例（pending/failed/未匹配），
  EM 新增 2 个状态接口用例（纸类项目命中、两流程都不命中）；
  后端全量 510 过（仅既有失败 test_config_security 1 个）。

## 2026-08-08：纸类判定分支（GiveJudgement=1）

- 背景：旧系统纸类项目存在"要求判定"的任务单（人工正确登记参照
  260191286：`JudgeBasis=按客户要求`、`TotalJudge=符合`、报告项目名称
  必填、标准值列与实测值同文）。此前流程对判定字段恒置空，Writer 端
  `LegacySafetyGuards` 会在执行前以 `judgement_requires_interactive_confirmation`
  拒绝（fail-closed，不写半截数据）。
- 后端：纸类 DAG 在"特纤复核"与"检验记录登记"之间新增
  `judgement-input`（`human.input`，config 标记 `paper_judgement`）。
  任务单 `give_judgement` 为假时引擎直接自动完成该节点
  （`auto_submit_reason=judgement_not_required`，零人工干预）；为真时
  动态生成人工表单——判定依据选项取自任务快照 `check_basis`（按
  ，、、 分隔；无依据时回退为必填自由文本），判定结果固定
  "符合/不符合"下拉。提交统一整形为
  `{judgement_required, judge_basis, judgement}`。
- 登记包判定变体：`give_judgement` 为真且判定值齐备时，
  `judge_basis`/`total_judge` 填入人工选择、`report_check_item_name`
  填项目名、明细 `standard_value` 与实测值同文；为假时全部留空并
  忽略任何游离判定值。缺失判定值时以 `paper_fiber_judgement_required`
  冲突拒绝。`give_judgement` 不进入 `task_project` 严格键集契约
  （Writer `ParseTaskProject` 与回执校验均为白名单）。
- Writer（FinalEntry）：`ValidatePaperGenericScope` 拆分为共用基线
  （结果/方法/单位/空白字段）+ 两形态——无判定（维持原全空约束）与
  判定变体（依据/总评定必填、报告项目名等于项目名、标准值与实测值
  同文）；与任务单 `GiveJudgement` 的一致性仍由 `LegacySafetyGuards`
  执行前强制核验。离线自测新增 5 个判定用例。
- 目录升级：纸类流程跟随机制改为"已知系统校验和"模式
  （只读首版/完整首版/单候选版），未被人为修改的流程自动升级出
  新版本（本次升 v3），历史运行钉在各自版本；只读种子库升级时
  补齐 external_write 能力。
- 纸类登记维持"只保存、不校对"不变。

## 2026-08-07：生产部署准备与 Windows Bridge 集中打包

- 外部操作确定性失败热重试修复（同日补记）：批准后的操作若在远端写入
  边界前反复失败（如目标编号被人工删除导致的 `target_allocation_stale`），
  此前会无限重领（实测 12 分钟 105 次冲击旧系统）。现按
  `EXECUTION_EXTERNAL_MAX_ATTEMPTS`（默认 5）封顶：达到上限后操作按
  “未写入”失败收尾（`external_attempts_exhausted`），节点/运行进入可
  人工重试状态，重试会以最新快照重新预检；边界后失败仍只走人工对账。
- 检验员加速改造（同日补记）：运行页左侧新增“旧系统上传”自动批准卡片
  （`ExecutionAutoApprovalCard`），预检单 5 秒倒计时自动批准，期间可
  “终止自动批准”转手动；右侧「旧系统上传」标签页不再提供批准动作
  （仅状态/历史/人工对账）。后续按用户反馈继续收口为：
  “选择原始记录”仅匹配到一份候选时由引擎直接规范化并 `complete_node`，
  不再创建人工任务（0 人工干预，输出带 `auto_submitted` 标记，候选
  校验失败仍 fail-closed）；需要人工处理的内容统一收进页头下方全宽
  “需要您处理”行动区（人工任务卡与自动批准卡按 minmax(480px,1fr)
  网格铺开），左侧 310px 栏只保留本次执行上下文，不再堆叠操作项。
- 纸类人工单选 409 循环修复（同日补记）：`file.paper_fiber_gbt4688_qualitative`
  候选在“后台索引扫描间隔内文件被重新保存”时会携带过期指纹，提交被
  `_validate_index_candidate` 以 `file_candidate_stale` 永久拒绝，前端又把所有
  409 统一提示为“任务已被其他人员更新”，导致无法推进。修复：
  `paper_fiber._profile_for_entry` 在组候选前按实时 stat 校正索引指纹
  （连带 size/modified_at，指纹变化自动失效结果缓存并重读 W32）；
  `HumanTaskCard` 按 409 错误码给出区分提示（`file_candidate_stale` →
  明确告知文件已变化、需取消后重新发起）。电镜选图路径存在同类窗口，
  暂未展开，如出现同样症状按同模式处理。
- 新增 `12_生产部署方案.md`：Win10 64 位 Docker 主机试运行，仅开放
  再生纤根数法/面积法、电镜微观形貌、纸类 GB/T 4688-2020 定性 4 个流程；
  **全链路保持 HTTP、不启用 HTTPS**（用户确认，长期有效）。
- 新增生产覆盖 `.tmp/execution-system-local-runtime/docker-compose.production.yml`
  （postgres 数据落宿主机、backend/worker 挂 SimSun）与幂等初始化脚本
  `production-bootstrap.sh`（禁用 3 个不开放流程、宋体检查、存储根核对）。
- 新增 `textile-device-monitor/packaging/windows-bridge/`：把写入桥、快照桥、
  只读探针、两个 Writer（SHA-256 钉值校验 + 随包补 x86
  `Oracle.DataAccess.dll`，生产机无需装 ODAC/GAC）、冻结 FibreCheck 客户端、
  x64 Instant Client、CPython 运行时和 probe 离线依赖打成单一 Inno Setup
  安装包；机密不进包，安装后由管理员填 `config\bridge.env`。
- 写入桥计划任务按“仅当用户登录时运行”（FinalEntry COM Excel 需交互会话）。
- 踩坑：PowerShell 5.1 下无 BOM 的 UTF-8 脚本按 GBK 误读中文注释导致解析
  失败，本目录全部 PowerShell/Inno 文件强制 UTF-8 带 BOM；uv 版 CPython 的
  ensurepip 被拒（externally-managed），probe 离线依赖改用构建机既有
  probe-venv 的 pip `--target` 装入。

## 2026-08-05：微观形貌图片比例和末端契约收口

- 真实 Windows Excel 定位 BIFF8 水平锚点解释差异，按当前版本化模板
  加入 `1.2745` 水平补偿；单图与 2/5/10 图均通过 LibreOffice/UNO 和
  Microsoft Excel 双重只读验收。
- 回读校验扩展到每张图的源标识、比例、几何、边界和重叠；清理
  模板普通 `Print_Area`，仅保留唯一 `$A$1:$L$37`。
- 打印改为可选操作；选择打印时必须人工确认 Excel 已完成打印，单纯
  打开/下载不会误记为已打印。
- 任务快照升为 v3；微观形貌项目必须带脱敏 `Task_CheckItem/CheckItem`
  标识，旧快照自动刷新。图片上传预检改为绑定人工选中的精确项目。
- 新增图片上传零写入探针、类型化 observation/receipt 和机器契约清单；
  真实上传和复核能力仍为 unavailable，Writer 继续拒绝执行。
- 验证：容器 UNO 21 项、后端联合 113 项、只读探针/Bridge 38 项、前端
  12 项及生产构建通过。

## 2026-08-03：电镜纤维微观形貌首版

- 扩展默认流程为“选图 → 任务字段确认 → `.xls` 原始记录生成 → 打印确认 →
  图片上传预检 → 特纤复核预检”；现有未修改的图片选择旧版自动兼容升级，管理员
  已编辑流程不覆盖。
- 纳管微观形貌 `.xls` 模板并固定 SHA-256；实现样品名称业务拆分+jieba 建议、
  样品识别多分隔符、按需判定字段、1～10 张图片等比例最大化布局和保存后重读。
- 默认打印区域明确写入并核对为 `微观形貌!A1:L37`；单图必须贴满画布的一条边。
- `260061860` 实测命中 4 个目录、376 张图片，使用真实 BMP 生成并渲染成功；
  单图高度 11698/11700，等比例横向居中。
- 新增图片上传和特纤复核持久化协议、能力协商及禁写预检；后端、Bridge、Writer
  在实机证明完成前均拒绝真实执行。
- 联调发现并修复旧图片选择版本少一个映射导致升级不命中，以及完整流程缺少
  `external_write` 能力快照的问题；后者仅授权创建预检单，不解除禁写。
- 新增“电镜—纤维微观形貌 GB/T 36422-2018”默认流程、专用文件节点和图片
  人工任务，支持多文件夹、1～10 张、大图和前后切换。
- 电镜索引改为后台递归扫描全部嵌套层级；用户搜索只查 PostgreSQL，旧任务信息
  由独立 Windows 只读 Bridge 刷新到缓存，避免每次推荐重复查询 Oracle。
- 真实任务单确认 `膜平面形貌` 是该标准下的有效子项；名称与方法仍必须来自
  同一 Task_CheckItem，不能模糊或跨项目拼接。
- `26A029794` 实测命中 2 个文件夹和 48 张 BMP，网页选 2 张后完成流程；
  144 个相关文件指纹无变化，未执行旧系统或共享盘写入。
- 迁移头更新为 `0006_task_snapshot_cache`；专项后端、只读探针、Bridge、前端
  测试与生产构建、容器就绪检查全部通过。

## 2026-08-01：待对账管理员人工处置闭环

- 新增管理员专用 `external_operation.reconcile` 权限、只读上下文和带 CSRF 的
  人工处置端点；服务层仍二次拒绝非管理员或停用账号。
- 只接受“完整写入”与“完全未写入”两种强结论，绑定最新 failed post-boundary
  attempt、payload checksum、完整目标样品、探针实际核验时间、备注和结构化
  计数/字段/检验员/文件 SHA-256；部分或矛盾结果不释放围栏。
- 相同请求幂等，不同处置冲突；前端明确展示待核对业务内容，显式选择高风险
  结论，证据过期保留表单，管理员身份与专用权限双门。
- 修复“先进入待对账、后取消/兄弟失败”会取消节点但永久遗留围栏的问题；
  `in_progress`、`cancel_pending`、`reconciliation_required` 统一作为不可中断
  外部操作，Bridge 写前失败、写后失败、完成以及两种人工结论均有终局收敛测试。
- 当前证据明确标记为 `admin_attestation_v1`，不是机器生成或签名的探针产物；
  第二次真实写入继续冻结，下一步是故障注入、只读 observation 绑定和凭据 ACL。
- 验证：后端联合回归 91 项，前端 Node 18 项 + Vitest 79 项，生产构建全部通过。

## 2026-08-01：PostgreSQL 并发门禁与 Windows xlsx 句柄修复

- 确认 Parallels 私网中 macOS 宿主机为 `10.211.55.2`；当前 SSH、Docker API、
  PostgreSQL 和后端端口均未开放，因此本轮未尝试不安全的 Docker TCP 暴露。
- 在 Windows ARM64 的 x64 模拟层中临时启动 EDB PostgreSQL 15.18，严格只
  监听 `127.0.0.1:55432` 并使用独占 `_test` 数据库；Python 依赖继续使用 uv
  管理的 x64 CPython 3.12 环境。
- PostgreSQL 并发模块 6 项全过，新增的 Bridge 全局容量事务 advisory lock
  已在两个独立连接之间验证阻塞、超时和释放后重取；便携数据库、数据、日志、
  ZIP 和测试临时目录随后全部停止并删除。
- 测试暴露 Windows 上 openpyxl 只读工作簿校验会残留底层 ZIP 文件句柄，导致
  紧随其后的 `os.replace` 报 WinError 32。校验器改为显式拥有二进制流并在原子
  替换前关闭；新增跨平台句柄回归，文件变更套件 23 项全过。
- 开发拓扑确定为 macOS Docker/PostgreSQL 后端 + Windows ARM64 Bridge；
  Windows x64 物理机作为生产 Bridge 和驱动/长期运行验收节点，不整体迁移。
- 第二次真实写入仍冻结。下一步先实现 `reconciliation_required` 人工处置端点、
  故障注入和 Windows 凭据 ACL 收口。

## 2026-08-01：Windows ARM64 迁移复查与写入安全暂停

- 复查仓库结构、最近 12 个提交和本目录全部设计文档；总体方向继续采用
  “中心执行系统 + 集中式 Windows Bridge + 每任务 x86 Runner/Writer”，
  不改为 WPF 桌面自动化、直接 SQL 或 ARM 原生重写。
- 暂停 UTF-8 修复后的第二次真实写入。首次写入证明官方 DAL 链路可行，但
  `024cf10` 的原协议仍存在副作用阶段回报丢失后重领、取消不终止 Writer、
  账号绑定与本地账号脱节、服务端未强制全局并发 1、完成回执可绕过核验等风险。
- 当前安全收敛包括：Writer 在 `file_copy_ready` 阻塞；Bridge 先把
  `file_copy_started` 持久化后才通过 stdin 发放一次性许可；阶段单调化；
  取消前置拒绝；账号作用域领取与双重核对；服务端 PostgreSQL advisory lock
  容量门禁；严格要求 `main_record_verified`；原子创建目标文件和更完整回读。
- 本机 `backend/.env` 仍保留首次写入令牌；未破坏或回显密钥，改为新增独立
  `EXECUTION_BRIDGE_ENABLED=false` 总开关并验证实际配置为关闭。只有总开关与
  令牌同时配置时端点才可用。
- Windows 11 ARM64 主机继续使用 x64 Python 模拟层和 x86 .NET Framework
  旧系统边界；生产后端仍要求 Linux/PostgreSQL。当前主机没有 Docker/Podman/
  可用 WSL，因此 SQLite 仅作本机排练，不能替代 PostgreSQL 并发验收。
- 文档迁移头修正为 `0005_execution_external_attempts`；执行领域表为 29 张。
- `backend/.env` 与 `.tmp/execution-system-secrets/inspection-systems.env` 的
  Windows ACL 都仍继承两项无法解析的 Modify 主体，不等价于 macOS `0600`；
  恢复真实写入前需确认主体、收紧 ACL，并评估轮换其中的凭据。

## 2026-08-01：260187115-1 首次受控真实写入完成

- 端到端链路全部走通：执行系统预检 → 用户批准 → Bridge 领取 → Runner
  官方 DAL 写入 → 写后核验；目标 `260187115-1` 已真实写入旧检务系统：
  - DB 记录字段逐项核验通过（SampleNo=260187115-1、棉再生纤/定量/
    棉再生纤定量-根数法×1、CheckUser1=辜惠珊（与 I8 一致）、
    CreateUser=李舒洋（lisy）、CreateTime=2026-08-01 11:22:18 服务器时间）；
  - 物理文件 `2026/8/1/SpecialWool/260187115-辜-...新系统.xls` 存在，
    235,520 字节、SHA-256 与共享盘源文件完全一致；
  - 前缀查询现为基记录 + `-1` 共 2 条，与旧系统既有命名规则一致。
- 官方 DAL 路径打通的关键配置：ODP.NET 需要旧客户端
  `FibreCheck.exe.config` 第 224–228 行的
  `<oracle.dataaccess.client><settings><add name="bool" value="edmmapping number(1,0)"/></settings></oracle.dataaccess.client>`；
  缺它时 11.2.0.2 的 provider manifest 把 number(1,0) 映射为 Int16，整个
  EF 模型在 `StorageMappingItemCollection.Init` 即报 MappingException 2019
  （396+ 个 Boolean↔number 映射实体全部失败）。写入器配置必须带此开关。
- 32 位客户端来源：`\\192.168.105.66\software\2-业务系统\ODAC`
  （11.2.0.2.50 xcopy 包），本机复制到 `.tmp/odac32/`；本机 GAC_32 已有
  4.112.2.50（C:\oracle 为既有 xcopy 安装点）。
- 用户现行客户端目录为 `C:\Users\lishuyang\Downloads\FibreCheck`（与
  `.tmp` 冻结副本存在版本差；spike 与写入均以现行目录为准）。
- 新增组件：
  - 后端 Bridge（迁移 `0005_external_attempts`）：attempt 表、
    claim/heartbeat/stage/complete/fail 端点、租约清扫、取消集成
    （cancel_pending + abort_requested）、凭据 revision/批准 TTL/源指纹
    复核后才发放领取；17 项 Bridge 测试与 36 项外部操作回归全过；
  - `tools/legacy_fibrecheck_writer`：只读探针 + `--execute-upload` 写入
    执行器（八阶段检查点、file_copy_started 后失败即 reconciliation_required、
    复制后 SHA-256 回读、保存后 DAL 逐字段回读核验）；
  - `tools/legacy_fibrecheck_bridge/bridge.py`：单任务领取-调度-回报控制器；
  - `worker_state.py` 修复 `os.uname` 在 Windows 不存在的问题。
- 演练环境：本机原生运行（uvicorn + worker + SQLite 演练库；便携
  PostgreSQL 322MB 因外网速度不足未能下载，SQLite 偏离已记录，生产仍以
  Docker/PostgreSQL 为准）；迁移头 0005。
- 已知缺陷与处置：Bridge 控制器以 UTF-8 解析 Runner stdout，而中文 Windows
  控制台默认 GBK，导致写入虽成功但被误判失败（attempt=failed、操作回到
  approved、存在重复领取风险）；已按人工对账把操作/attempt/节点/运行置为
  completed 并写审计，Runner 输出已强制 `Console.OutputEncoding=UTF8`。
  该误判未造成重复写入（及时冻结）。
- 待办：UTF-8 修复后的第二次端到端验证（新目标编号）；`reconciliation_required`
  人工解决端点；PostgreSQL 环境下的 Bridge 并发门禁；正式把上传节点接入
  默认流程前的配置化开关。

## 2026-07-31：source/target 编号拆分落地

- 旧系统上传的"读取编号"与"目标编号"正式拆分：运行创建新增可选
  `target_sample_number`（API 顶层字段，缺省回退 `inspection_number`），
  并镜像进不可变运行输入 `input_data.target_sample_number`；
- 统一解析入口 `resolve_legacy_target_sample_number(run)`：预检创建与批准
  路径共用，业务围栏 `remote_business_key` 始终绑定目标编号——修复了批准
  路径仍按源编号重推围栏键导致 split 运行必报 run_changed 的问题；
- 预检单新增 `source_inspection_number` 字段，公开视图同步返回（旧预检单
  回退显示目标编号）；批准确认始终针对目标编号；
- 严格 `additionalProperties:false` 的流程须在 `input_schema` 声明
  `target_sample_number` 属性后才能使用该字段；input_data 与顶层字段不一致
  时返回 422 `target_sample_number_mismatch`；
- 前端运行创建页随表单提交顶层 `target_sample_number`；预检面板在源/目标
  不一致时增显示"源检验编号"；
- 后端测试：外部操作 19 项全过（新增拆分/回退/不匹配/批准绑定/幂等镜像
  5 项）；Windows 本机全量回归中 excel mutation、area_jobs(UNO)、
  Postgres 并发为环境性失败（基线同样失败，UNO 与 Postgres 缺位），
  `backend/runtime/` 已加入 .gitignore；
- 本轮仍未执行任何远端写入。

## 2026-07-31：双账号门禁闭环与 dry-run 最终清单

- 工作区提交 `a83cf31`（探针扩展 + Runner 首版 + 交接手册）；测试口令夹具
  已替换为合成值，真实凭据不入 Git；
- 第二账号 `fuzhu1`（辅助岗1，检验八部/特种纤维）只读登录核验 exit=0，
  同样获“特纤管理—检验”页面授权；`lisy`+`fuzhu1` 双真实账号并行核验
  人员上下文互不串号，阶段 A 门禁全部闭环；
- 反编译补齐保存链事实：
  - 上传在点击时即 `File.Copy(overwrite: true)` 到
    `\\192.168.105.82\fibrecheckfile$\OriginalData\Files\年\月\日\SpecialWool\`
    （日期取 CreateTime=服务器 SYSDATE），保留原始文件名；
  - `FileServer`/`OriginalData` 分别来自 `KeyValues` 与 `FileDirectory` 表；
  - 保存时 `FilePath`=逗号连接的文件名、`FileType`=逗号连接的类型；
    无解析明细时不触发报告数据更新；
  - UI 重复检查 `GetByReportNo` 为 `SampleNo.Contains` 语义；
  - `SpecialWoolManage.ID` 为 36 位带连字符 GUID，CreateTime 取
    `SELECT SYSDATE FROM DUAL`；
- 文件链路实物核验：共享目录 2026/7/28 下 `260187115` 上传文件存在且唯一，
  与 DB `FilePath` 完全一致；目标编号无同名冲突；
- Runner 新增 `--dry-run-upload` 模式：在只读登录/授权/人员映射之后，
  按精确 + Contains 语义核验目标编号远端缺失（冲突退出码 17，非法编号 18），
  解析文件服务器配置并输出“将复制的文件 + 将创建的记录字段”最终清单；
  FakeDb 自测扩展至 10 组场景全部通过；
- `260187115-1` dry-run 实测 exit=0：远端精确/包含计数均为 0、源记录存在、
  清单字段与真实 `260187115` 记录逐项一致（检验员=辜惠珊、
  CheckUserItem1=棉再生纤定量-根数法、FileType=定量试验等）；
- 仍未执行任何远端写入；首次写入仍需：后端拆分
  `source_inspection_number`/`target_sample_number`、Bridge 外壳与写入实现、
  用户对 dry-run 清单的最终确认。

## 2026-07-30：阶段 A 无界面只读登录 Runner 完成

- ilspycmd 反编译确认登录链精确语义：`KeyValues` 开关、番禺/花都区域判定、
  `User` 表 + `MD5(Encoding.Default, X2 大写)` 口令比对、部门岗位与父部门加载；
- 权限模型确认：操作链=父部门+部门+岗位+本人；`PV_FunctionDefinition` 按
  FunctionType 解析功能、`PV_PurviewAssign` PurviewType='0' 功能级授权、
  PurviewType='1' 控制级授权；`fadmin` 直通；
- 目标页面确认为“特纤管理(0004-0004-0008)—检验(0004-0004-0008-0001)”
  `SpecialWoolSearchUI`，其控制级权限定义为空；
- 数据库连接串以旧程序 `SystemData` 硬编码回退为准（番禺库备用地址 +
  FIBRECHECK 账号），只读连接验证通过；Runner 运行时反射读取，凭据不入本项目；
- 用户确认：实验室仅用番禺，账号无花都权限，花都路径不实现；
- 新增 `tools/legacy_fibrecheck_runner`：x86 .NET Framework 控制台，首条
  `SET TRANSACTION READ ONLY`、仅参数化 SELECT、结束 ROLLBACK；口令仅经
  环境变量/stdin；输出脱敏 JSON（登录名掩码、ID 散列、中文名保留）；
  稳定退出码 0/2/3/10–16；构建产物 `out/` 已加入 .gitignore；
- 自测（FakeDb，不连库）覆盖纯函数与 7 组流程场景，全部通过；
- `lisy` 实测 exit=0：口令匹配、番禺、`检验八部/特种纤维/综合组`、
  目标“检验”页面 granted=true、控制级权限为空、`辜惠珊` 唯一映射且其
  ID 散列与 `260187115` 主记录 `CheckUser1` 完全一致（交叉验证闭环）；
- 并行隔离：正确口令/错误口令/不存在账号三实例并行，各自返回 0/12/10，
  互不干扰；进程退出后无残留窗口或线程；
- 双真实账号隔离仍需第二个旧系统账号，待用户提供后补验；
- 本轮仍未调用保存方法、未复制业务文件、未执行任何 Oracle 写入。

## 2026-07-30：Windows 只读对账打通与探针扩展

- 环境迁入 Parallels Windows 11（ARM64，x86/x64 模拟可用）：`.tmp` 材料已受控
  复制，Git 工作区干净（基线 c5a758d），未提交任何新代码到主仓库；
- 使用 uv 安装 ARM64 与 x64 CPython 3.12.13，oracledb 4.0.2；
- 主配置地址 `10.1.30.146:1521` 从本环境连接超时不可达；`WebService.dll.config`
  的 `PanYuJianWu` 条目指向 `192.168.105.106/orcl`（番禺检务库备用网卡地址）可达；
- 数据库实为 Oracle 11g 11.2.0.1.0：python-oracledb Thin 模式不支持（DPY-3010），
  改用 x64 Python + Instant Client 19.31 的 Thick 模式；
- 主配置 `FibreCheckEntities` 账号在两台可达 orcl 实例上均被拒（ORA-01017），
  冻结配置中的该账号已不可用于当前环境；改用 `PanYuJianWu` 条目凭据只读连接成功；
- 探针新增 `--data-source`（备用地址覆盖）与 `--credential-profile`
  （配置文件内其它凭据条目）参数，凭据仍只从配置文件读取、不写入输出；
- 修复探针脱敏缺口：`TaskAssignUser` 内部 ID 与 `ReportName` UNC 路径现在
  分别按 ID 散列和路径条目输出；单元测试 11 项通过；
- `260187115` 只读对账完成，证据存 `.tmp/fibrecheck-reconciliation/`（不入 Git）：
  - `SpecialWoolManage` 精确记录存在：FibreSort=棉再生纤、CheckWay=定量、
    CheckUserItem1=棉再生纤定量-根数法、CheckUserNumber1=1，另挂
    CheckUserItem2=棉再生纤定性；
  - CheckUser1=辜惠珊（与根数法 `I8` 及文件名“辜”一致），AuditUser=曹楚凤，
    CreateTime=2026-07-28 10:43:35；
  - 前缀查询仅基记录 1 条：`260187115-1` 目标编号当前空闲；
  - `QuantificationTest` 及明细为 0、`IsImmediacyPublish` 为 null：上传不触发
    定量解析，本节点范围确认为“文件复制 + 主记录保存”；
  - `CheckRecordRegister` 中上传原始文件被重命名为 `GUID.xls`，而
    `SpecialWoolManage.FilePath` 保留原始文件名；
  - 任务侧 `Task`(ReportNo=260187115, Status=Valid) 与 4 个 `Task_CheckItem`
    均存在；
- 仍未开放：Bridge claim/dry-run、x86 Runner、任何远端写入；首次写入前必须拆分
  `source_inspection_number` / `target_sample_number` 并由用户确认最终清单。

## 2026-07-30：Windows Bridge 交接与过期围栏收口

- 明确 Parallels Windows 11 作为首个集中式 FibreCheck Bridge 验证环境，
  不在每位用户电脑部署旧系统桌面自动化 Agent；
- 新增可随 Git 同步的 `docs/legacy-fibrecheck-bridge-handoff.md`，汇总现有代码
  边界、旧系统登录链、只读对账命令、x86 Runner、多人并发模型、阶段检查点、
  首次写入门禁和 Windows Codex 启动提示；
- 预检或批准过期后由 execution-worker 自动转为 `expired`，同步结束等待节点
  和运行并释放同一样品业务围栏；
- 同一样品的预检与批准在 PostgreSQL 中先取得 transaction advisory lock，
  再按固定顺序取得文件和外部操作行锁，消除重复预检/批准/过期清理竞态；
- 将 Bridge 的 `in_progress` 取消/失败收敛列为开放领取和真实写入前硬门禁；
- 明确 `260187115 -> 260187115-1` 场景需拆分源查询编号和远端目标编号；
- 继续保持零远端副作用：没有 Bridge 领取/提交接口，没有文件复制和 Oracle
  写入。

## 2026-07-29：旧系统上传节点逆向与写入边界

- 定位旧 FibreCheck 的“特纤管理—检验”业务界面、DAL 保存方法、人员映射、
  文件目录和两阶段数据库更新链路；
- 确认旧系统没有可直接复用的独立 HTTP 上传接口，现有人工流程本质是共享盘
  文件复制与 Oracle 业务保存的组合；
- 明确 Docker 不直接加载 32 位 .NET Framework 业务 DLL；针对多账号并发，
  主连接器调整为集中式 Windows Bridge，每笔操作使用独立 x86 子进程隔离
  登录态和旧系统全局变量，不采用单桌面 UI 自动化作为生产主路径；
- 直接写数据库仅作为否决方案保留，不进入生产实现；
- 旧系统外部操作增加持久化幂等围栏、操作摘要、最终人工确认、远端回执和
  `reconciliation_required` 状态，成功未知时不得自动重试；
- 外部操作预检绑定运行创建人的旧系统凭据版本、账号作用域和跨运行样品业务键；
  修改账号或口令、源文件指纹/SHA-256/I8、预检超时都会令原确认失效；
- 预检默认有效 30 分钟、批准默认有效 15 分钟；批准接口由服务端再次核对完整
  样品编号，不再只依赖浏览器端输入框校验；
- 当前实现没有 Bridge 领取、远端提交或完成接口，批准只写入本地审核状态；
  旧系统真实副作用保持关闭；
- 所有真实上传前先生成样品号、检验员、固定业务字段、文件名、文件指纹、
  目标冲突和预期远端变更清单，再由用户针对该次操作确认；
- 工作簿解析改为按实际文件头识别 OLE/OOXML，并输出 `I8` 检验员及多文件
  人员冲突摘要；
- 当前仅完成静态逆向、真实工作簿只读核验和本地安全实现，不曾执行旧系统写入。

## 2026-07-28：试运行默认恢复 HTTP

- 当前开发和生产试运行以易部署、易联调为优先，默认入口改为
  `http://<服务器局域网IP>`，不依赖公司 DNS、本机 hosts 或内部根证书；
- HTTP 对外端口恢复为标准 80，用户继续使用原服务器 IP，无需追加端口；
- 后端、数据库、execution-worker、Area Infer 和 OCR 继续只在 Docker
  内部网络通信，HTTP 模式只发布前端入口；
- 内部 CA、证书校验、终端迁移脚本和 HTTPS Nginx 配置均保留，通过
  `docker-compose.https.yml` 作为正式上线阶段的可选覆盖；
- Windows 客户端新安装默认允许配置纯 HTTP Origin，HTTPS 模式仍保留证书
  校验且不使用 `verify=False`。

## 2026-07-28：再生纤结果读取与主单选择

- 新增根数法、面积法两个版本化结果读取节点，按固定工作表和坐标逐文件读取
  部位、成分、含量、备注与插图；
- 结果页和人工任务改为逐文件卡片展示，图片进入深一级弹窗；
- 支持多选“需要”的文件，并从已选项中指定主单；服务端校验稳定文件 ID、
  索引指纹和主单归属；
- 真实样本确认“部位为空”不等于只有一组结果，左右有数据时无损保留为
  “结果1/结果2”；
- 同时保存公式原值、Excel 显示值和业务结果值；对正常四舍五入产生的
  99.9/100.1 尾差做可追溯平衡，保证每个部位的业务结果合计为 100；
- 旧 `.xls` 插图只转换临时副本，继续保持共享盘源文件只读；
- 两个系统默认流程升级为
  “匹配工作簿 → 读取结果 → 人工选单/主单 → 结束”，仅自动升级未经管理员
  修改的旧默认版本。
- 使用真实编号 `26X909953`、`262039607` 在浏览器中完成两条全流程验收：
  多文件选择、主单切换、单文件自动主单、插图弹窗和终态结果回显均通过，
  页面控制台无错误；
- 三份真实源文件验收前后大小、mtime_ns 和 SHA-256 完全一致；两条流程每个
  节点均只执行一次，临时 UI 代理和调试文件已清理。

## 2026-07-27：再生纤两类文件节点首版

- 登记材料检测中心下再生纤、特种毛、麻棉和电镜四个生产 UNC 路径；
- 明确复用 `area_out_cifs`，向 backend 和 execution-worker 提供只读
  `/data/execution-source` 挂载，不要求宿主机预先手工挂载共享盘；
- 固化“再生纤-根数法”和“再生纤-面积法”的路径、文件名、工作表和
  `B14:J14` 内容规则；
- 登记真实验收编号 `260144785`、`260162847` 及只读核验边界；
- SMB 凭据只保存在 Git 忽略、权限为 `0600` 的本地 secrets 文件，不写入
  头脑风暴文档。
- 完成两个公开文件节点、两个默认流程、推荐接口和前端软排序交互；
- 自动索引首版仅调度再生纤目录，短编号不打开共享工作簿，瞬时失败不长期缓存；
- 将再生纤根数法规则修正为仅匹配 `根数法报告1`，并检查其 B14:J14；
- 两个真实编号均以 5/5 分排到对应流程首位，并由 Worker 跑通至完成；
- 验收前后源文件大小和 mtime 一致，确认全程只读。

## 2026-07-26：本机隔离容器环境启动

- 删除已废弃的 `docker-compose.windows-lan.template.yml`；
- 清理用户明确允许删除的旧停止容器、旧 PostgreSQL 数据卷和旧 CIFS 卷；
- 在 `.tmp/execution-system-local-runtime/` 建立不入 Git 的本地环境：
  PostgreSQL 使用独立 bind 数据目录，source 只读挂载真实样本根，staging 和
  publish 使用互相独立的可写目录；
- 启动 PostgreSQL、backend、execution-worker、frontend，四个容器均达到
  healthy，Area Infer 未启动；
- HTTPS 仅绑定 `127.0.0.1:58443`，执行系统登录验证通过；
- 后台索引完成：特种毛 2,730、麻棉 657、电镜 379；再生纤目录缺失并保持
  不可用状态。

## 2026-07-25：首版基础框架实现与实测收尾

### 实现状态同步

- 执行系统领域模型扩展为 27 张表，新增 Worker 数据库心跳及迁移版本等待；
- 存活与就绪拆分为 `/health/live` 和 `/health/ready`，就绪同时检查数据库、
  Alembic、Worker 及 staging/publish 存储；
- 增加 `failure_pending`、`cancel_pending` 发布栅栏，失败、驳回或取消时不再
  忽略已开始的发布副作用；
- 新增 `0003_execution_contract`：发布版本和运行固化能力快照、契约校验和，
  文件变更在物理发布前持久化栅栏；
- 分阶段写入 API 必须绑定实际已到达节点、不可变运行输入和已完成的人工确认，
  不能从页面直接绕过 DAG 或伪造核对上下文；
- 发布工作流限制为单个 `artifact.publish` 节点，节点租约最多重试 5 次；
- 测试环境统一采用生产式 `autoflush=False`，发现并修复边决策未刷入导致的
  后继节点停滞；
- 明确外部检务连接器仍禁用，复杂 `.xls/.xlsm` 高保真写入由后续 Windows
  Excel Agent 承担。

### 验证记录

- SQLite 本机完整后端回归除 UNO 环境项外为 228 项通过、3 项跳过、6 个
  子测试通过；真实 PostgreSQL 为 238 项通过、6 个子测试通过；唯一依赖
  `python3-uno` 的 Area Excel 单例已在最终后端镜像内通过；
- PostgreSQL 并发门禁覆盖双 Worker 领取、租约续期/耗尽扫描竞态、发布栅栏/
  重复发布/取消收敛和同根索引刷新仲裁；
- Alembic 在真实 PostgreSQL 上完成空库升级、既有结构严格预检后
  `stamp`/升级，以及 `check` 模型差异检查，迁移头为
  `0003_execution_contract (head)`；
- 前端 18 项 Node 测试与 44 项 Vitest 通过，生产构建通过；
- 后端、execution-worker 和前端三套镜像构建完成，并在不构建 Area Infer 的
  临时容器环境验证 API 存活/就绪、Worker 心跳、HTTPS、安全响应头、执行系统
  统一 401 和旧设备接口代理；
- 浏览器验证独立登录及编号恢复、角色隔离、缺失数据根禁用、真实运行推进到
  人工任务、三栏工作台、任务收件箱和管理员设计器核心入口；
- 完成真实样本目录只读索引：特种毛 2,730 个文件（解析成功 2,689、失败
  38），麻棉 657/657，电镜 379 条且包含 2 个工作簿；再生纤根目录缺失并
  保持不可运行。

### 最终加固

- Alembic 既有库预检从关键字段探测升级为完整结构比较，并拒绝未版本化执行表；
- 设计器节点库改为服务端注册表驱动，增加根目录槽位和凭据槽位编辑；
- 运行事件增加游标分页和前端“加载更早动态”；
- 文件索引 API 仅暴露只读根、拒绝 staging/publish 刷新，扫描深度限制为一层；
- 新增受控写入分阶段公共 API；DAG 与 API 复用同一运行时，复制前先落变更计划，
  发布确认绑定核对上下文并全程写审计、事件和 Outbox；
- 发布 API 在物理复制前提交节点领取与 mutation fence；发布中取消最终收敛为
  `cancelled`，且只产生一份文件和一条回执；
- 三类执行根在 API、Worker 和部署脚本中统一拒绝相同、父子嵌套、符号链接或
  同一 inode 别名；
- 在线预览拒绝 SVG，并对 OOXML 压缩包设置条目数、解压体积、单项大小、加密
  标志和异常压缩比门禁；
- 修复 Nginx/Uvicorn 代理头，使 HTTPS 同源访问旧设备尾斜杠接口时不降级为
  HTTP 或丢失端口；
- 记录前端完整依赖树仍有 22 项构建告警（10 high、4 critical），列为上线前
  独立治理项，不用离线审计结果掩盖风险。

## 2026-07-24：初始化

### 用户输入

- 提出建设检测业务“执行系统”；
- 希望参考 Dify 的节点/流程图方式；
- 节点可自由命名，工作流可导出 JSON；
- 新模块需要账号和角色；
- 每位用户维护自己的新、旧检务系统账号；
- 描述棉与再生纤维素纤维物理法定量的原始记录、复核、第三人、检务上传和结果录入流程；
- 提出文件查询、类型判断、主单、Excel 合并、人工输入、共享目录索引和高密度结果预览等想法；
- 明确本轮先头脑风暴，不开发。

### 本轮整理

- 将产品定位为可持久化的人机协同执行系统；
- 区分流程设计器与检验员执行工作台；
- 建立工作流版本、运行、节点运行、人工任务、文件制品和外部回执概念；
- 建议主单为运行级角色，不修改全局文件池；
- 建议 Excel 合并使用副本和预检查；
- 将 7 天和 6 个定义为默认查询/UI 限制；
- 提出文件索引、内容指纹和定向刷新机制；
- 提出 Windows Agent 和服务端编排的边界；
- 建议先借鉴 Dify，不立即决定 Fork；
- 制定第一条影子模式垂直切片；
- 建立 P0 业务问题和暂定决策清单。

### 后续入口

下一轮优先讨论：

1. 一个真实正常案例；
2. 一个触发第三人的案例；
3. 原始记录模板与字段；
4. 阈值和最终取值规则；
5. 主单实际承担的业务含义。
