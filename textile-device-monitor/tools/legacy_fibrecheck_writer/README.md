# FibreCheck 上传写入器（Runner 写入模式）

x86 .NET Framework 4.x 控制台程序，由集中式 Bridge 以“一任务一进程”方式调用，
完成旧检务系统“特纤管理—检验”上传的受控写入。

> Writer 已分别实现 `legacy_special_wool_image_upload` 与
> `legacy_special_wool_review` 的独立分派、阶段和回读代码，但这只是离线实现完成，
> **不是生产能力已开放**。后端仍签发
> `execution_capability.available=false`，Bridge 和 Writer 都会在副作用前拒绝这种
> 任务包；只有完成受控实机对账并显式修改后端门禁后才可能真实执行。

## 两种模式

### 只读探针（--probe-sample-no）

登录核验后通过官方 DAL（`SpecialWoolDAL.GetByReportNo`）只读读取一条记录，
用于验证 DAL 链路可用性。不产生任何写入。

### 受控写入（--execute-upload）

```text
FibreCheckWriter.exe ^
  --fibrecheck-dir C:\path\to\FibreCheck ^
  --account <旧系统账号> ^
  --execute-upload ^
  --side-effect-permit-stdin ^
  --package C:\path\to\bridge-package.json ^
  --source-root "\\192.168.105.82\材料检测中心\10特纤\02-检验\2026-再生纤"
```

阶段协议（stdout 每行一个 JSON）：

```text
authenticated → permission_verified → remote_absence_verified →
file_copy_ready →（等待 stdin 许可）→ file_copy_started → file_copy_verified →
main_record_save_started → main_record_verified → completed
```

图片上传使用独立阶段：

```text
authenticated → permission_verified → remote_state_verified →
task_project_verified → file_copy_ready →（等待 stdin 许可）→
file_copy_started → file_copy_verified → main_record_save_started →
main_record_verified → picture_child_verified → completed
```

复核不读取或复制源文件，使用另一条独立阶段：

```text
authenticated → permission_verified → remote_state_verified →
review_save_ready →（等待 stdin 许可）→ review_save_started →
review_main_verified → review_children_verified → completed
```

- `file_copy_ready` 之后 Writer 会阻塞，只有 Bridge 已把 `file_copy_started`
  持久化到服务端且从 stdin 写入精确的 `PERMIT_REMOTE_WRITE` 后才会继续；
  60 秒内没有许可即在副作用前安全退出。
- `file_copy_started`（含）之后的任何失败输出 `reconciliation_required` 事件并返回
  退出码 30；绝不自动重试、绝不回滚远端已发生的副作用。
- 文件复制后逐字节回读比对 SHA-256；主记录保存后通过官方 DAL 回读并逐字段核验
  （ID/SampleNo/FibreSort/CheckWay/CheckUser1/CheckUserItem1/CheckUserNumber1/
  ReviewUserNumber1/FilePath/FileType/CreateUser）。
- 主记录保存调用官方 `SpecialWoolDAL.SaveSpecialWoolManage`（空明细列表场景下等价于
  单行 INSERT），CreateUser/CreateTime/ID 由 DAL 按旧客户端语义写入。

图片上传的离线实现还固定执行以下核验：

- 根据源任务号重查 `Task_CheckItem`，由原始 ID 和项目字段重新计算脱敏项目键，
  严格绑定 `OriginalDataPictureFile.CheckItemID`；任务项目的 `CheckCount` 必须大于
  等于 1，并与服务端签发的预期总份数精确相等，但不要求任务总份数只能为 1；
- 在 `Task_CheckItem` 的 `SELECT ... FOR UPDATE` 协作锁内重新计算
  `原号、-1、-2...` 的 first-free 编号，且结果必须仍与服务端签发的
  `target_sample_number` 完全相同；Writer 不会擅自改号绕过回执契约；
- 目标文件使用 `FileMode.CreateNew`，写后重新读取大小和 SHA-256；
- 服务器目标文件名不沿用工作副本的基础编号，而是严格使用
  `最终 target_sample_number-39-8B-纤维形状截面定量试验-2026.xls`；主记录
  `FilePath`、图片子记录 `PictureFileName/OriginalDataFileName` 和机器回执必须
  全部读回为该名称；新回执会在图片记录与 readback 中同时返回
  `original_data_filename`，后端仍兼容缺少该字段的本轮早期 v1 回执；
- 官方 DAL 在一次保存中创建一条 `SpecialWoolManage` 与一条
  `OriginalDataPictureFile`，随后逐字段回读主记录和图片子记录。

复核实现通过 `SpecialWoolCheckUI` 功能权限及可选 `btnCheck` 控件权限核验后，只设置
主记录的 `ReviewUser` 与 Oracle `SYSDATE` 得到的 `ReviewTime`。上传成功回执中的
脱敏 `main_record.id` 会同时写入后端复核预检和 Bridge 私有任务包；Writer 在只读
查询阶段把实际主键重新散列并精确比对，若同一样品号下的主记录已被替换，会在请求
副作用许可前拒绝。保存前后还会对主表除上述两个字段外的全部列和所有图片子记录做
确定性指纹；任何其它变化均转入人工对账。

## 环境依赖

- 完整 FibreCheck 安装目录（现行版本），含 `oracle.dataaccess.dll`；
- 32 位 Oracle 11.2 客户端（OraOps11w.dll + oci.dll 等，构建时复制到输出目录）；
- `FibreCheckWriter.exe.config` 必须包含旧客户端同款的布尔 EDM 映射开关：

```xml
<oracle.dataaccess.client>
  <settings>
    <add name="bool" value="edmmapping number(1,0)"/>
  </settings>
</oracle.dataaccess.client>
```

缺少该开关时，ODP.NET 11.2.0.2 的 provider manifest 将 number(1,0) 映射为
Int16，整个 EF 模型在 `StorageMappingItemCollection.Init` 阶段即报
MappingException 2019。这是旧客户端能工作的隐性前提，已在 2026-07-31 实测确认。

## 构建

```powershell
.\build.ps1 -FibreCheckDir "C:\path\to\FibreCheck"
```

需要本机 `csc.exe`（.NET Framework 4.x 自带）与 `.tmp/odac32/` 中的 32 位
Instant Client 运行时件（来源：部门共享 ODAC 11.2.0.2.50 xcopy 包）。

## 安全边界

- 口令只从环境变量 `FIBRECHECK_RUNNER_PASSWORD` 读取；
- 登录核验沿用只读 Runner 的全套只读事务/参数化 SELECT 约束；
- 任务包中的目标编号经过严格格式校验，源文件经大小 + SHA-256 比对；
- 目标记录已存在（精确或 Contains 语义）或目标文件已存在时直接拒绝；
- 输出中的内部 ID 以 `sha256:` 散列表示，口令被显式抹除。

## 待证明的图片上传与特纤复核边界

- 图片上传固定业务字段已经锁定为：`FibreSort=图片`、`CheckWay=''`、
  `CheckUserItem1=图片`、本次检验记录份数 1、复核项目/本次复核记录份数为图片/1。
  这里的单次记录份数与任务项目的总 `CheckCount` 是两个独立概念；任务总份数可
  大于 1，但必须与服务端签发值精确一致。
- 执行系统只接受同一次运行中由服务端签发的 `.xls` 微观形貌制品，并在批准前
  重新核对制品行、路径、大小和 SHA-256；目标号按 `原号、-1、-2...` 对本系统
  围栏做暂定分配，仍必须由 Windows 只读探针核对旧库占用后才能批准。
- 静态反编译和离线实现已经覆盖 `OriginalDataPictureFile.CheckItemID` 来源、官方
  DAL 主子记录保存、保存后回读，以及复核仅修改 `ReviewUser/ReviewTime`、图片子表
  不变的约束。当前仍缺少受控真实账号/真实文件服务器上的写后对账证据，因此两者的
  `execution_capability.available=false` 继续保持，不能批准、领取或获得 stdin
  副作用许可。

当前已提供的禁写实现包括：

- 默认流程把人工确认的 `selected_project_key` 与脱敏项目快照传给上传预检；
  后端仅接受“纤维微观形貌/膜平面形貌 + GB/T 36422-2018”，并绑定
  `Task_CheckItem`、`CheckItem` 的脱敏标识。
- Python 只读探针新增 `--special-wool-image-dry-run`，只查询远端编号族、任务
  项目、`OriginalDataPictureFile`、`SampleNo` 唯一索引和 Oracle 服务器时间，
  生成 `legacy_special_wool_image_upload_dry_run` 类型观察文档；始终声明
  `write_performed=false`、`ready_for_write=false`。
- 图片上传和复核的 observation/receipt 契约、阶段及未开放门禁记录在
  `special_wool_machine_contracts.json`。后端会对未来机器回执进行操作类型、
  operation/payload/目标编号、项目、文件哈希、主子记录和阶段顺序的严格校验。
- Bridge 已能广告三种 Writer 分派，但会继续根据任务包中的后端能力标志拒绝两种
  图片类任务；复核不会要求或传入 `--source-root`。
- `test.ps1` 可在没有 Oracle、旧系统账号或共享目录的条件下单独编译运行 C# 契约
  SelfTest；Bridge Python 测试覆盖图片和复核的独立阶段、副作用许可及 source-root
  差异。完整 Writer 仍需在 Windows 上引用冻结的 FibreCheck DLL 进行编译。
