# Textile Execution Bridge 安装包打包

把执行系统旧系统桥（写入桥 + 任务快照桥）所需的全部 Windows 侧物料打成单一
Inno Setup 安装包，用于生产 Windows 主机的一次性部署。

## 打包内容

| staging 子目录 | 安装位置（默认 `C:\TextileExecutionBridge`） | 来源 |
| --- | --- | --- |
| `app\legacy_fibrecheck_bridge` | `app\...` | 源码树 `tools/`（Python，无第三方依赖） |
| `app\legacy_fibrecheck_task_snapshot_bridge` | `app\...` | 同上 |
| `app\legacy_fibrecheck_probe` | `app\...` | 同上（只读 Oracle 探针） |
| `writers\FibreCheckWriter` | `writers\...` | 已编译 exe + 11.2 native client + x86 `Oracle.DataAccess.dll` |
| `writers\FibreCheckFinalEntryWriter` | `writers\...` | 同上 |
| `fibrecheck` | `fibrecheck\` | 冻结 FibreCheck 客户端（Git 外受控复制） |
| `oracle-ic-x64` | `oracle-ic-x64\` | Instant Client 19.31 x64（探针 Thick 模式） |
| `python` | `python\` | CPython 3.12 x64 完整运行时 |
| `python-deps` | `python-deps\` | probe 的 pip 依赖（oracledb），构建时离线装入 |
| `ops` / `config` | `ops\` / `config\` | 本目录 `runtime/` 下脚本与模板 |

桥令牌和共享凭据由管理员在安装后的配置中心填写。检务账号密码由各用户在执行系统中维护，Bridge 每次领取任务时使用该任务发起人的凭据，不要求本机重复配置。

## 构建（Windows 构建机）

1. 确认 `bridge-package.psd1` 中的路径与 Writer SHA-256 钉值；
   Writer 重新编译后必须先跑通其离线自测，再更新钉值。
2. 执行：

   ```powershell
   powershell -ExecutionPolicy Bypass -File Build-BridgePackage.ps1
   # 只收集物料、不调 Inno：  -StageOnly
   ```

3. 产物：`output\textile-execution-bridge-setup-<version>.exe`（附 `.sha256`）。

`staging\`、`output\`、`version.auto.iss` 为构建产物，不进入 Git。

> **编码约束**：本目录所有 `.ps1` / `.psd1` / `.iss` 必须是 **UTF-8 带 BOM**。
> 目标环境是 Windows PowerShell 5.1，无 BOM 的 UTF-8 会按 GBK 误读中文注释，
> 直接造成解析失败（`Unexpected token` / `missing the terminator`）。
> 编辑器保存或脚本化编辑后请确认 BOM 仍在（文件头三字节 `EF BB BF`）。

## 安装与启用

现场操作见 [Windows Bridge 1.3.2 一页说明](现场操作.md)。配置中心使用 Windows PowerShell 5.1 和 WinForms。安装器创建开始菜单入口，首次安装后由操作员在窗口中启用各桥。

程序安装在 `C:\TextileExecutionBridge`，现场设置存放在 `%ProgramData%\TextileExecutionBridge\config\settings.json`。配置与备份使用同一目录 ACL，凭据由 Windows DPAPI LocalMachine 加密。运行状态与待核对回执保存在同一数据根的 `runtime` 子目录。两个 Writer 的摘要取自当前安装清单。

安装器维护钩子先让现有任务在回执边界停止，再覆盖程序。1.2.x 首次升级需在现场确认队列空闲。两个计划任务继续使用操作员的交互式会话和 AtLogOn 触发器。写入桥日志、快照桥日志按日期保存在 `work\logs`。

Windows 验收脚本位于 `tests`，应在临时数据根使用测试凭据及模拟服务执行。正式启用会领取真实操作。

1.3.1 会自动处理旧版留下的写入桥待回执标记。单条操作失败或等待结果核对不会阻止后续操作；临时网络失败自动重试。恢复只移交原操作状态，不重新执行 Writer。完整异常保存在服务端操作与本机 `runtime/receipts`，界面可展开复制。

1.3.2 将检务登录切换为每项任务的个人凭据。同一座 Bridge 可处理多个用户，保留原先每项任务独立 Writer 的执行方式；任务密码不进入临时包、待回执或日志。旧本机检务配置仍可读取，但不再参与启动或登录。先更新 Docker，再覆盖安装 Bridge，操作步骤不变。

安装器的临时预检逻辑独立于已安装的旧版 drain 实现，因此 1.3.0 存在 pending 时也能直接覆盖升级；实际 Writer 进程仍需退出。测试 `Test-BridgeUpgradeRecovery.ps1` 使用冻结的真实 1.3.0 runtime 复现并验证该路径。
