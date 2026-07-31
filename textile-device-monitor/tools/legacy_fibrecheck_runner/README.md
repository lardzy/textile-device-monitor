# FibreCheck 无界面只读登录核验 Runner

x86 .NET Framework 4.x 控制台程序，复现旧检务系统登录链的只读部分，供集中式
Windows Bridge 在开放任何写入前验证：账号口令、组织上下文、旧权限表授权和
检验员中文名映射。

## 硬性边界

- 连接后的第一条 SQL 固定为 `SET TRANSACTION READ ONLY`，结束固定 `ROLLBACK`。
- 仅执行参数化 `SELECT`；不包含任何 DML、DDL、存储过程或文件复制代码。
- 不创建 WPF 窗口、不启动后台服务、不写登录缓存（不执行 `LoadMainWindowInfo`）。
- 不调用 `SaveSpecialWoolManage` 或任何保存方法。
- 口令只从环境变量 `FIBRECHECK_RUNNER_PASSWORD` 或 `--password-stdin` 读取，
  不进入命令行参数、日志或输出 JSON。
- 数据库连接串从旧程序自己的 `SystemData` 类反射读取（含其硬编码回退值），
  不复制到本项目的任何文件。
- 输出 JSON 中：登录名掩码、内部 ID 以 `sha256:` 散列、中文姓名保留。
- 按 2026-07-30 确认：实验室仅使用番禺库；解析为花都账号时以退出码 16 拒绝。

## 构建

需要 .NET Framework 4.x（Windows 自带 `csc.exe`）和完整 FibreCheck 安装目录：

```powershell
.\build.ps1 -FibreCheckDir "C:\path\to\FibreCheck"
# 输出 out\FibreCheckRunner.exe（x86）
```

## 自测（不连接 Oracle）

```powershell
.\run-tests.ps1 -FibreCheckDir "C:\path\to\FibreCheck"
```

覆盖 MD5 格式、口令策略、掩码/散列、部门码前缀展开、FakeDb 全流程
（成功、账号不存在、重复账号、口令错误、权限不足、检验员歧义），并断言
首条 SQL 为只读事务、仅 SELECT、结束 rollback、内部 ID 与口令不泄漏。

## 使用

```powershell
$env:FIBRECHECK_RUNNER_PASSWORD = '...'
.\out\FibreCheckRunner.exe `
  --fibrecheck-dir "C:\path\to\FibreCheck" `
  --account lisy `
  --inspector-names "辜惠珊" `
  --out ".\probe-result.json"
```

- `--function-type` 默认为 `Toone.FibreCheck.OriRecord.SpecialWool.SpecialWoolSearchUI`
  （特纤管理—检验），可覆盖为其它功能界面类型名。
- `--inspector-names` 以逗号分隔，逐一要求中文姓名在 `User` 表中唯一解析。
- `--out -` 输出到标准输出。

## dry-run 上传核验模式

在只读登录核验之后，追加“将复制什么、将创建什么”的最终清单，仍零写入：

```powershell
.\out\FibreCheckRunner.exe `
  --fibrecheck-dir "C:\path\to\FibreCheck" `
  --account lisy `
  --inspector-names "辜惠珊" `
  --dry-run-upload `
  --source-inspection-number 260187115 `
  --target-sample-number 260187115-1 `
  --source-file-name "260187115-辜-根数法-定量试验原始记录（原号905-924）20190122-新系统.xls" `
  --out ".\dry-run.json"
```

- 目标编号按与探针相同的严格规则校验（9–20 位大写字母/数字，可加 `-` 后缀）。
- 远端缺失核验包含精确匹配和与旧客户端 `GetByReportNo` 相同的 Contains 语义，
  任一命中即返回退出码 17。
- 清单含 `SpecialWoolManage` 拟定字段（固定业务字段、检验员 ID 散列、登录人
  散列）和文件复制计划（FileServer 配置、目标目录规则、覆盖行为说明）。
- 输出 JSON 含文件服务器内网路径，属受控证据，只应保存在受控目录，不入 Git。

## 退出码

| 码 | 含义 |
|---|---|
| 0 | 全部核验通过 |
| 2 | 参数/配置错误 |
| 3 | 基础设施错误（数据库连接、SQL 异常） |
| 10 | 账号不存在 |
| 11 | 账号重复 |
| 12 | 口令不匹配 |
| 13 | 系统停用（KeyValues.CanLoginSystem=0） |
| 14 | 目标功能未授权 |
| 15 | 检验员映射失败（缺失或歧义） |
| 16 | 非番禺账号（未支持区域） |
| 17 | dry-run 冲突：目标编号远端已存在 |
| 18 | 样品编号格式不合法 |

## 复现的登录链（与旧客户端逐步对应）

1. `CanLoginSystem`：`KeyValues` 开关查询；
2. `GetAccoutIsPanYuOrHuaDu`：经部门名判定账号区域；
3. `CheckAccoutInfo`：`User` 表 + `MD5(Encoding.Default, X2)` 口令比对，
   口令策略仅报告不重置；
4. `LoadDepartmentAndPositionInfo`：单条部门岗位记录时填充组织上下文，
   并按 `DeptCode` 前缀展开父部门；
5. 授权检查：操作链 = 父部门 + 部门 + 岗位 + 本人，查 `PV_FunctionDefinition`
   （按 FunctionType 解析）+ `PV_PurviewAssign`（PurviewType='0'）；
   控制级权限仅报告；`fadmin` 按旧系统规则直接放行；
6. 检验员映射：`User` 表按中文姓名唯一解析。
