# FibreCheck 集中式 Windows Bridge 交接手册

> 最后更新：2026-08-05
> 适用环境：Windows 11 ARM64（x64/x86 模拟层）及后续部门内常驻 Windows 主机
> 当前状态：`260187115-1` 首次受控真实写入已完成并通过写后核验；代码复查后
> 已暂停第二次真实写入，先完成副作用许可、取消、账号隔离与并发安全门禁

2026-08-05 补充：图片类特种毛的 Writer/Bridge 离线实现已完成，但后端能力开关
仍保持关闭，且本轮没有连接 Oracle、没有复制到真实文件服务器、没有执行任何真实
上传或复核。具体边界如下：

- Writer 为 `legacy_special_wool_image_upload` 创建独立执行路径：重查并绑定任务
  项目的原始 `CheckItemID`，在 `Task_CheckItem` 协作锁内复算编号族，使用
  `FileMode.CreateNew` 和 SHA-256 回读复制文件，再由官方
  `SpecialWoolDAL.SaveSpecialWoolManage` 一次保存主记录和一条
  `OriginalDataPictureFile`，最后回读两张表；
- Writer 为 `legacy_special_wool_review` 创建独立路径：检查
  `SpecialWoolCheckUI` 和配置存在时的 `btnCheck` 权限，只设置主记录
  `ReviewUser/ReviewTime`，并证明主表其它列及图片子记录指纹不变；
- Bridge 会广告三种分派，并为上传/复核使用不同的阶段与许可边界；复核不再要求
  `--source-root`；
- 服务端签发的 `execution_capability.available=false` 仍会被 Bridge 和 Writer
  双重执行，因此这些代码不能被现有生产流程领取或触发；
- 编号族锁内复算若与已签发的 `target_sample_number` 不同，Writer 失败并进入对账，
  不会自动改为 `-1/-2` 后提交与服务端回执不一致的目标号。

离线门禁已覆盖：Bridge Python 协议测试、C# 纯契约 SelfTest，以及在 Windows VM
中引用冻结 FibreCheck DLL 的完整 Writer 编译。下一步仍是只读机器观察传输、受控
测试号的最终变更清单和人工确认；不能根据“编译通过”直接打开后端能力开关。

2026-08-05 FinalEntry 补充：集中式 Bridge 已支持按部署配置分派
`legacy_microscopy_check_record_entry`，但本轮只完成离线协议验证，没有进行真实写入：

- 只有同时配置 `--final-entry-writer` 和 `--final-entry-work-root` 才向服务端广告
  FinalEntry 能力；源工作簿仍必须通过 `--source-root root_id=路径` 显式映射；
- Bridge 写给 FinalEntry Writer 的临时 JSON 只包含服务端签发的
  `operation.machine_payload`，不会把 claim、凭据或公开摘要混入 Writer 包；私有包内含完整
  `task_project`（项目键、两个脱敏 ID、项目编号/名称、方法、顺序、`CheckCount=1`）；
- Writer 的本地阶段会转换为服务端固定阶段，并在 `excel_write_ready` 后先由服务端
  持久化 `excel_collection_started`，成功后才经 stdin 发出一次性副作用许可；
- `completed` 不直接信任 stdout：Bridge 会先严格核对完整 raw receipt、Writer 从当前
  Oracle 行实测返回的 `task_project`、工作簿哈希、远端文件回读、登记数和校对指纹，转换成
  后端回执后才提交完成；转换时不会把公开 `request_summary.task_project` 回显成实测事实；
- 广告并执行 FinalEntry Excel 能力的 Bridge 必须运行在已登录 Windows 用户的交互会话中
  （`Environment.UserInteractive=true` 且 `SessionId>0`），例如任务计划选择“仅当用户登录时
  运行”。不得由 Windows 服务、SYSTEM、Session 0 或“无论用户是否登录都运行”的后台任务
  启动 FinalEntry Writer；不满足时 Writer 会在旧系统登录和 Oracle 预检前以
  `excel_interactive_session_required` 拒绝，且不进入对账状态。通用 FinalEntry 操作不受影响；
- 受控 1→2 测试必须同时满足任务包声明、Bridge CLI
  `--allow-controlled-final-entry-test-override` 和预先存在且目标精确一致的
  `FIBRECHECK_CONTROLLED_TEST_SAMPLE_NO`。Bridge 不会根据任务包设置该环境变量，
  普通任务即使 CLI 已打开也不会向 Writer 传递覆盖参数。

## 1. 首先必须知道的结论

旧检务系统自动化不采用“每位用户电脑安装桌面 Agent 并操作 WPF 窗口”的方案。
多人、多账号场景统一采用：

```text
浏览器用户
  -> 执行系统 API / PostgreSQL 持久队列
  -> 一台集中式 Windows Bridge
  -> 每项任务启动一个短生命周期 x86 Runner
  -> Runner 使用该任务绑定的旧系统账号无界面登录
  -> 官方 FibreCheck DAL / Oracle / 原始记录文件目录
```

- 用户可以并发提交，不需要停留在网页上。
- Bridge 首版实际写入并发固定为 1；完成稳定性验证后才能提高到 2～4。
- 每个 Runner 只承载一个账号和一项操作，结束后立即退出。
- 同账号、同一完整样品号、同一前 9 位样品家族和同一目标文件必须串行。
- WPF 界面自动化只作为人工兜底，不作为生产主路径。
- 不能只使用 `AppDomain` 隔离账号：旧系统包含静态登录态、非托管
  ODP.NET、共享目录和其它进程级状态。
- 不直接写旧数据库。文件复制、主记录保存和报告数据更新不是同一事务，
  直接拼 SQL 很容易遗漏业务副作用。

## 2. 当前仓库已经实现什么

后端已注册节点：

```text
external.legacy_regenerated_fiber_count_upload@1
显示名称：旧系统上传-再生纤-根数法
```

当前节点先执行持久化预检：

1. 使用人工选择节点输出的服务端文件 ID；
2. 重新解析实际文件格式，兼容后缀名与真实格式不一致的 OLE/OOXML 工作簿；
3. 重新读取 `根数法报告1!I8`；
4. 每次外部上传必须且只能选择一份原始记录；
5. 记录源文件 fingerprint、SHA-256、检验员、固定业务字段和目标样品编号；
6. 绑定运行创建人的旧系统凭据 ID、凭据 revision 和账号作用域；
7. 创建持久化外部操作及跨运行样品业务围栏；
8. 等待用户再次输入完整样品编号并确认；
9. 批准把本地状态从 `prepared` 改为 `approved`；已启用的 Bridge 随后可以领取；
10. Bridge 通过 `claim/heartbeat/stage/complete/fail` 持久化 attempt、租约、阶段和回执。

同一样品的预检和批准还会先取得 PostgreSQL transaction advisory lock，再进入
文件索引和外部操作行锁，避免重复运行与批准并发时形成反向锁等待。

Bridge 与 Writer 链路已经能够调用 FibreCheck 官方 DAL、复制原始记录并保存主单。
因此批准不再是无副作用动作：若 `EXECUTION_BRIDGE_ENABLED=true`、令牌已配置且
Windows Bridge 正在运行，批准后的任务可以立即进入真实写入。安全复查完成前
必须保持独立总开关为 `false`；已有令牌可以原样保留。
默认发布的再生纤根数法流程仍未自动串入这个外部节点，避免普通流程被连接器阻塞。

关键实现位置：

- `backend/app/execution/external_operations.py`
- `backend/app/execution/engine.py`
- `backend/app/execution/models.py`
- `backend/alembic/versions/0004_execution_external_operations.py`
- `backend/alembic/versions/0005_execution_external_attempts.py`
- `frontend/src/pages/execution/ExecutionExternalOperationPanel.jsx`
- `tools/legacy_fibrecheck_probe/`
- `tools/legacy_fibrecheck_bridge/`
- `tools/legacy_fibrecheck_writer/`

预检默认有效 30 分钟，批准默认有效 15 分钟。过期记录由 execution-worker
自动转为 `expired`，对应等待节点和运行结束为失败，释放同一样品业务围栏。

## 3. Windows 环境需要准备的材料

Git 仓库不包含旧程序和凭据。进入 Windows 11 后需单独准备：

1. 完整冻结的 FibreCheck 安装目录；
2. `.tmp/fibrecheck_tools/` 中既有只读取证成果；
3. 部门内部允许使用的旧系统测试账号；
4. 与 `Oracle.DataAccess.dll` 匹配的 32 位 Oracle Client；
5. .NET Framework 4.0/4.x 运行和构建环境；
6. 能访问旧 Oracle、FibreCheck 文件目录及再生纤共享目录的网络。

当前 Mac 工作区中的取证材料位于：

```text
.tmp/FibreCheck/
.tmp/fibrecheck_tools/
.tmp/execution-system-secrets/inspection-systems.env
```

这些目录或文件被 Git 忽略。若 Windows 使用独立克隆，必须通过受控方式另行复制，
不要把 DLL、配置中的连接串、账号密码或对账输出提交到 Git。

Windows 迁移后必须重新核验 secrets 文件 ACL，不能把 macOS 的 `0600` 描述直接
视为仍然成立。本次复查发现 `backend/.env` 与 `inspection-systems.env` 都继承了
两个无法解析且具有 Modify 权限的主体；真实写入前应由管理员确认主体、移除不需要
的继承权限，并评估轮换其中的凭据。本轮未擅自改写 ACL 或密钥。

## 4. 已定位的旧系统调用链

FibreCheck 是 32 位 WPF/.NET Framework 4.0 程序。已静态确认的登录链为：

```text
LoginViewModel
  -> LoginSystemHandle.LoginSytem
  -> CanLoginSystem
  -> GetAccoutIsPanYuOrHuaDu
  -> CheckAccoutInfo
  -> LoadDepartmentAndPositionInfo
  -> LoadMainWindowInfo
```

认证本质为 Oracle `User` 查询、旧 MD5 口令比较以及人员、区域、部门、岗位和
权限上下文初始化，最终写入
`SystemData.Instance.CurrentLoginedStaff`。原 `LoginSytem` 不能直接用于
无界面 Runner，因为方法尾部还会创建 WPF 主窗、启动后台服务并写登录缓存。

Runner 应复现其必要的公开初始化步骤：

1. 加载旧程序配置和 `SystemData`；
2. 验证账号密码；
3. 加载人员、区域、部门、岗位、子部门和父部门；
4. 使用旧权限表做显式只读授权检查；
5. 确认所属区域后选择正确 DAL；
6. 仅在全部预检通过后进入副作用阶段。

区域 DAL 不可混用：

- 番禺：`OriRecord.dll`
- 花都：`HDOriRecord.dll` 及对应 HD DAL

目标业务类已经定位到 `SpecialWoolSearchUI`、`SpecialWoolAddUI` 和
`SpecialWoolDAL`。`SpecialWoolDAL.SaveSpecialWoolManage(...)` 本身不进行
功能权限检查，只依赖全局登录人写审计字段，因此 Bridge 必须在副作用前另做权限
核验。

根数法固定业务字段：

```text
FibreSort = 棉再生纤
CheckWay = 定量
CheckUserItem1 = 棉再生纤定量-根数法
CheckUserNumber1 = 1
CheckUser1 = 由工作簿 I8 中文姓名唯一映射得到的旧系统 User ID
```

## 5. Windows 中第一步：只读对账

先复制 `tools/legacy_fibrecheck_probe/` 到能访问 Oracle 的 Windows 环境。
该工具只解析主 `FibreCheckEntities` 配置；所有业务 SQL 均为参数化
`SELECT`，连接后的第一条 SQL 是 `SET TRANSACTION READ ONLY`，结束固定
`rollback()`，不会复制文件。

2026-07-30 环境核验补充（重要）：

- 目标库为 Oracle 11g（11.2.0.1.0），python-oracledb Thin 模式不支持，
  必须使用 Thick 模式：x64 Python + Oracle Instant Client（19.31 已验证，
  ARM64 Windows 经 x64 模拟运行正常）。
- 主配置地址不可达时，可使用同一数据库在当前网络可达的备用地址，探针参数
  `--data-source "host[:port]/service"`。
- 主配置 `FibreCheckEntities` 账号在当前环境被拒（ORA-01017）；可改用
  FibreCheck 目录内其它凭据条目，探针参数
  `--credential-profile "配置文件名:条目名"`，凭据仍只从配置文件读取。

```powershell
cd C:\path\to\textile-device-monitor\tools\legacy_fibrecheck_probe
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

先生成不连接数据库的 SQL 清单：

```powershell
.\.venv\Scripts\python.exe probe.py `
  --fibrecheck-dir "C:\FibreCheck" `
  --sample-no 260187115 `
  --manifest `
  --output ".\260187115-manifest.json"
```

人工检查清单后再执行只读对账：

```powershell
.\.venv\Scripts\python.exe probe.py `
  --fibrecheck-dir "C:\FibreCheck" `
  --sample-no 260187115 `
  --output ".\260187115-reconciliation.json"
```

若 Thin 模式不兼容旧 Oracle，再使用与 Python 位数匹配的 Instant Client 并
增加 `--oracle-client-dir`。对账 JSON 含内部业务信息，只能保存在临时受控目录，
不得加入 Git。

第一轮必须确认：

- `260187115` 的 `SpecialWoolManage` 精确记录；
- `260187115%` 前缀下是否已有追加记录；
- 检验员中文姓名与用户 ID 的唯一映射；
- `QuantificationTest`、明细、`CheckRecordRegister` 和原始文件引用；
- `IsImmediacyPublish` 以及真实上传是否触发即时解析；
- 当前账号所属区域及目标页面的 FunctionID/PurviewID；
- 目标文件目录、文件名规则和同名冲突行为。

## 6. Bridge 与 Runner 的下一步实现顺序

### 阶段 A：无界面只读登录 Runner —— 已完成（2026-07-30）

实现位于 `tools/legacy_fibrecheck_runner/`（x86 .NET Framework 控制台，
csc.exe 直接编译）：

- 从完整 FibreCheck 目录运行，不裁剪 DLL；连接串反射读取旧程序 `SystemData`；
- 复现登录链只读部分：配置加载、账号校验、组织上下文、权限检查和人员查询；
- 首条 `SET TRANSACTION READ ONLY`、仅参数化 SELECT、结束 ROLLBACK；
- 输出脱敏 JSON，进程退出码表达稳定错误码（0/2/3/10–16）；
- 不调用保存方法，不复制文件，不启动 WPF/后台服务/登录缓存。

实测门禁结果：

- FakeDb 自测 7 组场景通过；
- `lisy` 真实只读登录 exit=0：口令匹配、番禺、组织上下文、
  “特纤管理—检验”页面授权通过、控制级权限为空、`辜惠珊` 唯一映射且与
  `260187115` 主记录 `CheckUser1` 散列一致；
- 正确口令/错误口令/不存在账号三实例并行返回 0/12/10，互不干扰，
  进程退出后无残留窗口或线程；
- 双真实账号隔离仍需第二个旧系统账号，待补验；
- 用户已确认实验室仅使用番禺，花都路径不实现；Runner 对花都账号以
  退出码 16 拒绝。

### 阶段 B：集中式 Bridge 外壳 —— 已完成（2026-08-01）

- 后端迁移 `0005_external_attempts`：`execution_external_attempts` 表
  （attempt/租约/阶段检查点/stdout 摘要/退出信息），
  `(operation_id, attempt_no)` 唯一；
- Bridge 端点（独立总开关 + `X-Execution-Bridge-Key` 令牌；关闭/未配置 503，
  令牌不匹配 401）：
  `claim`、`heartbeat`、`stage`、`complete`、`fail`；
- 领取前复核：批准 TTL、凭据 revision、账号绑定、源文件指纹/SHA-256/I8；
- `in_progress` 参与取消状态机（cancel_pending + heartbeat 回 abort_requested）；
- 租约过期清扫：按阶段回 approved 或转 reconciliation_required；
- 17 项 Bridge 测试 + 36 项外部操作回归全过。

### 阶段 C：只读前后对账和 dry-run —— Runner 侧已完成（2026-07-31）

Runner 的 `--dry-run-upload` 模式已实现并实测：

- 严格编号校验（退出码 18）；
- 精确匹配 + 与旧客户端 `GetByReportNo` 相同 Contains 语义的远端缺失核验
  （冲突退出码 17）；
- 解析 `KeyValues.FileServer` + `FileDirectory.OriginalData` 得到目标目录规则；
- 输出“将复制的文件、将创建的 `SpecialWoolManage` 字段”最终清单；
- `260187115-1` 实测 exit=0：目标远端精确/包含计数均为 0、源记录存在、
  清单字段与真实 `260187115` 记录逐项一致。

### 阶段 D：首次受控真实写入 —— 已完成（2026-08-01）

`260187115-1` 已由集中式 Bridge + Runner 真实写入并核验通过：

- DB 记录字段逐项正确（棉再生纤/定量/棉再生纤定量-根数法×1、
  CheckUser1=辜惠珊、CreateUser=李舒洋、CreateTime=2026-08-01 11:22:18）；
- 物理文件与共享盘源文件 SHA-256 完全一致；
- 报告数据未触发更新（与旧客户端空明细行为一致）。

关键实现事实：

- 写入经官方 `SpecialWoolDAL.SaveSpecialWoolManage`（空明细列表 → 单行
  INSERT 语义），CreateUser/CreateTime/ID 由 DAL 按旧客户端语义写入；
- **EF 工作区加载的隐性前提**：进程配置必须含
  `<oracle.dataaccess.client><settings><add name="bool" value="edmmapping number(1,0)"/></settings></oracle.dataaccess.client>`，
  否则 ODP.NET 11.2.0.2 把 number(1,0) 映射为 Int16，全模型 MSL 校验报
  MappingException 2019（旧客户端 `FibreCheck.exe.config` 自带该开关）；
- 32 位运行时件来自 `\\192.168.105.66\software\2-业务系统\ODAC`
  （11.2.0.2.50 xcopy 包），本机复制到 `.tmp/odac32/`；
- 演练后端为原生 uvicorn + worker + SQLite（便携 PostgreSQL 因外网速度
  不足未下载；生产仍以 Docker/PostgreSQL 为准）；
- 已知缺陷：Bridge 控制器以 UTF-8 解析 Runner stdout，中文 Windows 控制台
  默认 GBK，曾把一次成功写入误判为失败（已人工对账置 completed 并写审计；
  Runner 输出编码已修复；无重复写入）。

首次写入后的代码复查发现，原阶段 D 协议尚不能严格保证上述语义：Writer 在输出
`file_copy_started` 后立即写文件，未等待 Bridge 把边界持久化；阶段响应丢失、
取消竞态或多 Bridge 同时领取时仍存在重复/错误写入风险。当前工作树已增加
`file_copy_ready` → 服务端持久化 `file_copy_started` → stdin 一次性许可的
fail-closed 握手，并补阶段单调化、账号匹配和全局容量门禁；验证完成前继续冻结写入。

### 阶段 E：图片类特纤上传与复核预检 —— 已实现、保持禁写（2026-08-03）

执行系统新增两个操作类型：

```text
legacy_special_wool_image_upload
legacy_special_wool_review
```

- 图片上传固定为 `FibreSort=图片`、`CheckWay=''`、`CheckItem=图片`、`CheckCount=1`；
  检验员使用执行系统当前用户显示名。源文件必须是同一运行中生成、登记并重新
  核对 SHA-256 的 `编号-39-8B-纤维形状截面定量试验-2026.xls`。服务器端文件名
  必须按最终分配编号重新生成，例如 `260111037-1-39-8B-纤维形状截面定量试验-2026.xls`，
  主记录 `FilePath`、图片子记录 `PictureFileName/OriginalDataFileName` 及回执均需
  读回该名称。新回执会显式携带 `original_data_filename`；后端仍兼容本轮早期
  已保存但缺少该字段的 v1 回执，字段一旦存在就必须等于最终目标文件名。
- 复核使用独立阶段：`authenticated → permission_verified → remote_state_verified
  → review_save_ready → review_save_started → review_main_verified →
  review_children_verified → completed`；副作用边界是 `review_save_started`。
- 当前后端预检声明 `execution_capability.available=false` 并拒绝批准；Bridge
  虽会广告独立分派，但领取到禁用能力时仍拒绝启动，Writer 也会再次核对该标志，
  因而不能回落到根数法写入实现。
- 2026-08-05 静态取证确认 `SpecialWoolAddUI` 创建一条
  `OriginalDataPictureFile`，其 `CheckItemID` 来自用户选中的任务项目；主记录与
  图片子记录由官方 DAL 在同一 `SaveChanges` 边界保存。已对
  `260061860` 执行实库只读回读，但该编号没有图片子记录，仍需一条已有
  图片主/子记录作为真实样本。
- `SpecialWoolCheckUI` 的图片类复核仅更新主记录
  `ReviewUser/ReviewTime`，不修改图片子记录；未来回执必须证明子记录数量、
  外键、`CheckItemID` 和字段指纹在复核前后不变。复核还必须绑定上传回执中的
  脱敏主记录 ID；同号主记录被替换时，Writer 必须在副作用许可前拒绝。
- 图片上传预检已绑定人工选中的脱敏 `Task_CheckItem/CheckItem`；
  Python 探针 `--special-wool-image-dry-run` 可以只读查询编号族、精确项目、
  图片主子记录、唯一索引和服务器时间，但始终输出 `ready_for_write=false`。
- 2026-08-05 在 Windows/Oracle 只读事务中实测 `260061860`：任务项目
  唯一命中“纤维微观形貌 / GB/T 36422-2018”；旧库有 2 条精确同号
  主记录，且没有 `SampleNo` 单列唯一索引。因此后续必须由 Bridge 全局串行
  执行“重查编号族 → 分配 → 保存 → 精确回读”，不能把查询后的编号当成并发唯一保障。

Windows 下一轮仍必须先做只读/断点取证，不做真实保存：

1. 已证明目标编号远端精确/Contains 占用查询和 `SampleNo` 无唯一索引；
   Writer 已在服务端全局单写容量之外，再以 `Task_CheckItem` 协作锁复算
   `原号/-1/-2`，但仍需在受控环境做并发断点验证。
2. 找到一条真实已有图片主/子记录，以零写入 dry-run 确认
   `OriginalDataPictureFile` 的实库回读结构。
3. 在 Windows 实机只读验证 `SpecialWoolCheckUI` 功能权限与可能存在的
   `btnCheck` 控件权限，并确认文件服务器目标目录及 ACL。
4. 完成上述证据、测试和最终变更清单后，再回到用户确认是否允许一次受控写入。

## 7. 后续阶段仍缺少的能力

- ~~Bridge 机器身份认证、claim/heartbeat/complete/reconcile API~~（2026-08-01 已实现）；
- ~~外部 attempt 表~~（2026-08-01 已实现）；
- ~~领取事务内再次校验批准 TTL、凭据 revision、账号及全部源文件哈希~~（已实现）；
- ~~`in_progress` 操作参与取消状态机~~（已实现 cancel_pending + abort 回报）；
- ~~`reconciliation_required` 的管理员人工解决端点~~（2026-08-01 已实现；只
  接受完整写入或完全未写入，部分结果继续锁定）；
- ~~图片上传探针生成绑定 operation/payload/目标编号/任务项目的类型化
  只读 observation~~（已实现文档生成与后端严格校验）；仍缺 Bridge 认证
  回传/持久化、attempt 绑定和机器签名。现有对账材料仍可能是
  `admin_attestation_v1` 人工声明，不可混为机器事实；
- 远端成功业务键的永久幂等记录（当前以操作/attempt 终态 + 唯一索引承担，
  跨运行重复创建仍靠业务围栏与人工确认）；
- 旧账号密码的 Windows 端受控解密/传递方式（首版为 Bridge 本地受控
  secrets 文件，凭据不出执行系统服务端）；
- 文件复制、DAL 保存各阶段失败注入演练（断网、Oracle 超时、同名文件、
  部分保存、Runner 崩溃）；
- PostgreSQL 下两个 Bridge 并发领取的真实竞争验证（服务端容量目标为全局 1）；
- UTF-8 编码修复后的第二次端到端验证；
- Bridge 完成/失败响应中断时的本地持久回执与运维恢复流程。
- ~~图片上传的远端编号锁内复算、`OriginalDataPictureFile + CheckItemID` 子记录创建
  与保存后回读代码~~（离线实现和完整编译已完成）；仍缺一次受控真实写入后的实库
  与文件服务器对账证明；
- ~~特纤复核只更新主记录 `ReviewUser/ReviewTime` 且图片子记录指纹不变的代码~~
  （离线实现和完整编译已完成）；仍缺真实 `SpecialWoolCheckUI/btnCheck` 权限及一次
  受控复核的前后对账证明。

上述安全门禁缺失或未验证时，只能运行只读登录、对账、dry-run 和无副作用的
自动化测试；`EXECUTION_BRIDGE_ENABLED` 必须保持为 `false`。

## 8. 已知真实样本事实

已只读核验的根数法源文件：

```text
7月/260187115-辜-根数法-定量试验原始记录（原号905-924）20190122-新系统.xls
```

- 实际格式：OLE，而不是根据后缀猜测；
- 工作表：`根数法报告1`；
- `I8`：已能正常读取；
- 不存在旧定量解析器固定查找的 `分组统计` 工作表；
- 本轮核验前后源文件大小、mtime 和 SHA-256 未变化。

不要把 `I8` 的中文姓名直接作为数据库外键；必须在当前账号对应区域的
`User` 表中唯一解析。不要根据文件扩展名选择解析器，必须检测文件头。

## 9. 给 Windows Codex 的启动提示

进入 Windows 后，先让新的 Codex 阅读本文件和：

```text
tools/legacy_fibrecheck_probe/README.md
tools/legacy_fibrecheck_runner/README.md
tools/legacy_fibrecheck_writer/README.md
.tmp/execution-system-brainstorm/07_首版实现与验收记录.md
.tmp/execution-system-brainstorm/06_待决策问题与决策日志.md
```

`260187115-1` 首次受控真实写入已于 2026-08-01 完成并通过写后核验
（证据在 `.tmp/fibrecheck-reconciliation/`）。它证明 DAL 路径可行，不代表
生产闭环已经安全。进入新环境后先确认 Bridge 总开关为 `false`，再验证许可握手、边界前取消、
边界后失联转对账、账号不匹配拒绝、PostgreSQL 全局并发 1 和故障注入。

人工解决端点和 PostgreSQL 容量门禁已经完成。后续顺序为：故障注入、只读
observation 绑定与 Windows 凭据 ACL 收口；门禁通过后再进行第二次端到端验证，
最后用配置开关把上传节点接入默认流程。任何成功未知都不得自动重试、不得释放
业务围栏、不得静默重复提交。

微观形貌工作流及 `260061860` 的本轮实现进度见：

```text
.tmp/execution-system-brainstorm/09_电镜纤维微观形貌首版.md
```

本轮只在 Docker 测试数据库和 staging 生成 `.xls`，没有执行图片上传或特纤复核。
