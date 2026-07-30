# FibreCheck 集中式 Windows Bridge 交接手册

> 最后更新：2026-07-30
> 适用环境：Parallels Desktop 中的 Windows 11，以及后续部门内常驻 Windows 主机
> 当前状态：静态逆向、只读探针和服务端预检围栏已完成；任何旧系统远端写入仍未开放

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

当前节点只执行本地预检：

1. 使用人工选择节点输出的服务端文件 ID；
2. 重新解析实际文件格式，兼容后缀名与真实格式不一致的 OLE/OOXML 工作簿；
3. 重新读取 `根数法报告1!I8`；
4. 多文件必须属于同一检验员；
5. 记录源文件 fingerprint、SHA-256、检验员、固定业务字段和目标样品编号；
6. 绑定运行创建人的旧系统凭据 ID、凭据 revision 和账号作用域；
7. 创建持久化外部操作及跨运行样品业务围栏；
8. 等待用户再次输入完整样品编号并确认；
9. 批准仅把本地状态从 `prepared` 改为 `approved`。

同一样品的预检和批准还会先取得 PostgreSQL transaction advisory lock，再进入
文件索引和外部操作行锁，避免重复运行与批准并发时形成反向锁等待。

当前代码没有 Bridge 领取接口、没有远端提交接口、没有完成回执接口，也不会调用
FibreCheck DLL、复制到旧系统目录或执行 Oracle 写入。默认发布的再生纤根数法
流程也尚未自动串入这个外部节点，避免 Bridge 不存在时把普通测试流程卡死。

关键实现位置：

- `backend/app/execution/external_operations.py`
- `backend/app/execution/engine.py`
- `backend/app/execution/models.py`
- `backend/alembic/versions/0004_execution_external_operations.py`
- `frontend/src/pages/execution/ExecutionExternalOperationPanel.jsx`
- `tools/legacy_fibrecheck_probe/`

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

### 阶段 A：无界面只读登录 Runner

- 新建独立 x86 .NET Framework 控制台程序；
- 首版从完整 FibreCheck 目录运行，不裁剪 DLL；
- 只完成配置加载、账号校验、组织上下文、权限检查和人员查询；
- 输出脱敏 JSON，进程退出码表达稳定错误码；
- 不调用保存方法，不复制文件。

门禁：两个不同账号依次和并行启动时，人员上下文不得串号；每个进程结束后无
残留 WPF 窗口或后台线程。

### 阶段 B：集中式 Bridge 外壳

- Bridge 作为无界面 Windows 服务或受控常驻进程；
- 主动向执行系统领取任务，不要求服务器反向访问 Windows；
- 每个任务启动独立 Runner，并传入一次性任务清单；
- 全局容量初始为 1；
- 保存 attempt、租约、阶段检查点、标准输出摘要和进程退出信息。

还需新增持久资源锁：

1. 旧系统账号；
2. 完整目标样品编号；
3. 样品号前 9 位家族；
4. 目标文件路径；
5. Bridge 全局容量。

所有路径按固定顺序加锁，避免两个任务互相等待。

### 阶段 C：只读前后对账和 dry-run

- Bridge 领取 `approved` 操作；
- 重新验证批准时效、凭据 revision、账号绑定、源 fingerprint/SHA-256/I8；
- Runner 登录并只读查询远端是否已存在目标；
- 生成“将复制什么、将创建什么、预计修改哪些记录”的最终清单；
- 不产生远端副作用，回传 dry-run 回执。

### 阶段 D：首次受控真实写入

只有在用户再次查看最终清单并明确确认后才能开放。首次候选目标可以是
`260187115-1`，但需要先解决“源文件查询编号”和“远端目标样品编号”目前共用
`run.inspection_number` 的问题。正式实现应拆成：

```text
source_inspection_number
target_sample_number
```

否则不能可靠表达“读取 260187115 的原始记录，但上传为 260187115-1”。

真实写入按阶段记录：

1. `authenticated`
2. `permission_verified`
3. `remote_absence_verified`
4. `file_copy_started`
5. `file_copy_verified`
6. `main_record_save_started`
7. `main_record_verified`
8. `report_update_started`
9. `report_update_verified`
10. `completed`

从 `file_copy_started` 起发生超时、进程崩溃或失联，状态必须进入
`reconciliation_required`，不得自动再次提交。

## 7. 开放写入前仍缺少的硬门禁

- Bridge 机器身份认证、claim/heartbeat/complete/reconcile API；
- 外部 attempt 表和持久资源锁表；
- 领取事务内再次校验批准 TTL、凭据 revision、账号及全部源文件哈希；
- `in_progress` 操作参与取消状态机：取消只能进入 `cancel_pending`，待 Runner
  回执或只读对账后才能成为终态；
- 远端成功业务键的永久幂等记录，不能因本地运行结束而允许重复创建；
- 旧账号密码的 Windows 端受控解密/传递方式；
- 番禺/花都区域路由及权限 ID 的真实验证；
- 文件复制、DAL 保存和报告更新各阶段的只读核对；
- 失败注入：断网、Oracle 超时、同名文件、部分保存、Runner 崩溃；
- 首次真实写入前由用户对最终变更清单进行单独确认。

任何一项缺失时，都只能运行只读登录、对账和 dry-run。

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
.tmp/execution-system-brainstorm/07_首版实现与验收记录.md
.tmp/execution-system-brainstorm/06_待决策问题与决策日志.md
```

然后明确下达：

```text
当前只进行 Windows 环境核验和只读对账。
不得复制到 FibreCheck 业务目录，不得调用 SaveSpecialWoolManage，
不得执行 Oracle 写 SQL。先输出环境、登录链、权限和 260187115 对账证据。
```

只有在只读结果返回当前主任务、用户审阅最终清单并再次授权后，才进入首次写入
阶段。
