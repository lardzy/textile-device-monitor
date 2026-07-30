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

`--fibrecheck-dir` 中必须存在主配置文件 `Toone.FibreCheck.Entites.dll.config`。工具只读取其中名为 `FibreCheckEntities` 的 Oracle 配置，不会自动尝试 HD 或其它区域配置。配置中的数据库账号仅在进程内用于连接，不会写入 JSON。

## 查询范围

探针会分别查询：

- `SpecialWoolManage`：完全匹配和前缀匹配；
- `User`：主记录中引用的检验、复核、创建和审核人员；
- `QuantificationTest` 与 `QuantificationTest_Detail`；
- `CheckRecordRegister`；
- `OriginalKeyData_CheckItem`；
- `Task` 与 `Task_CheckItem`。

查询失败会按查询项记录安全错误类型和 Oracle 错误代码，后续查询仍会继续。输出 JSON 仍可能包含内部业务信息，只应保存在受控临时目录，不应提交到 Git。

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
