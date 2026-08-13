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

机密（旧系统账号、桥令牌、SMB 凭据）**不进安装包**，由管理员安装后填写
`config\bridge.env`（模板 `bridge.env.example` 随包安装）。

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

## 安装与启用（生产 Windows 主机）

1. 运行安装包（管理员）。默认安装到 `C:\TextileExecutionBridge`。
2. 编辑 `config\BridgeConfig.psd1`：API 地址（保持 http，端口必须与前端容器
   发布端口一致，桥不可用期间的外部操作会停在“等待连接器”）、
   `ExecutionStagingPath`（必须与容器 `EXECUTION_RUNTIME_HOST_PATH` 同目录）等。
   该路径**不要使用交互会话的映射盘符**（如 `Z:`）：计划任务按最高权限运行时
   看不到未提升会话的盘符映射（除非启用 `EnableLinkedConnections`），Writer 会
   连续报 `DirectoryNotFoundException`。请用本地盘路径（如 `D:\...`）或 UNC
   路径（如 `\\Mac\Home\...`、`\\192.168.105.82\...`）。
3. 复制 `config\bridge.env.example` 为 `config\bridge.env` 并填写机密；
   收紧 ACL 仅管理员/SYSTEM 可读。
4. 自检：`ops\Test-BridgeInstallation.ps1`（无副作用）。
5. 注册计划任务：`ops\Register-BridgeScheduledTasks.ps1 -UserName <操作员账号>`。
   写入桥必须运行在已登录用户的交互会话（FinalEntry 需要 COM Excel），
   因此任务按“仅当用户登录时运行”注册；主机重启后需该用户登录。
6. 手动验证一次：`ops\Invoke-SnapshotBridge.ps1 -Once`、
   `ops\Invoke-WriteBridge.ps1 -Once`。

## 目录约定

- 安装目录固定且不含版本号，升级直接覆盖安装；`config\`、`work\` 不会被卸载删除。
- 写入桥同一时刻只允许一个进程（脚本自检）；全局写入并发仍由服务端固定为 1。
