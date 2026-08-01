# FibreCheck 上传写入器（Runner 写入模式）

x86 .NET Framework 4.x 控制台程序，由集中式 Bridge 以“一任务一进程”方式调用，
完成旧检务系统“特纤管理—检验”上传的受控写入。

## 两种模式

### 只读探针（--probe-sample-no）

登录核验后通过官方 DAL（`SpecialWoolDAL.GetByReportNo`）只读读取一条记录，
用于验证 DAL 链路可用性。不产生任何写入。

### 受控写入（--execute-upload）

```text
FibreCheckWriter.exe ^
  --fibrecheck-dir C:\path\to\FibreCheck ^
  --account lisy ^
  --execute-upload ^
  --package C:\path\to\bridge-package.json ^
  --source-root "\\192.168.105.82\材料检测中心\10特纤\02-检验\2026-再生纤"
```

阶段协议（stdout 每行一个 JSON）：

```text
authenticated → permission_verified → remote_absence_verified →
file_copy_started → file_copy_verified →
main_record_save_started → main_record_verified → completed
```

- `file_copy_started`（含）之后的任何失败输出 `reconciliation_required` 事件并返回
  退出码 30；绝不自动重试、绝不回滚远端已发生的副作用。
- 文件复制后逐字节回读比对 SHA-256；主记录保存后通过官方 DAL 回读并逐字段核验
  （SampleNo/FibreSort/CheckWay/CheckUser1/CheckUserItem1/CheckUserNumber1/
  FilePath/FileType/CreateUser）。
- 主记录保存调用官方 `SpecialWoolDAL.SaveSpecialWoolManage`（空明细列表场景下等价于
  单行 INSERT），CreateUser/CreateTime/ID 由 DAL 按旧客户端语义写入。

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
