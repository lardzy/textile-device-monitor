# 纺织品检测设备客户端

纺织品检测设备监控系统的 Windows 客户端程序，用于设备状态上报和进度监控。

## 功能特性

- **自动设备注册**: 首次运行时自动在服务器注册设备
- **工作路径监测**: 从工作路径中最新修改的子文件夹判断检测进度
- **系统指标采集**: 采集 CPU、内存、磁盘使用率和运行时间
- **定时状态上报**: 每 5 秒上报一次设备状态
- **系统托盘运行**: 后台运行，支持手动维护模式切换
- **图形配置界面**: PyQt6 实现的配置窗口
- **日志查看**: 实时查看运行日志

## 系统要求

- Windows 10 或更高版本
- Python 3.11+（开发环境）
- 能访问部门服务器和共享文件路径的局域网连接

测试和试运行阶段默认使用 HTTP，不需要部署证书、修改公司 DNS 或维护本机
`hosts`。HTTPS 仍作为可选能力保留；需要启用时，再使用随安装包提供的内部
CA 和迁移脚本。

## 快速开始

### 开发环境运行

1. **安装依赖**
```bash
cd textile-device-client
pip install -r requirements.txt
```

2. **运行程序**
```bash
python main.py
```

### 打包部署

打包机必须使用 64 位 CPython 3.12，并安装锁定的 Windows 构建依赖：

```bash
python -m pip install -r requirements-build.lock.txt
```

安装 Inno Setup 6 后，使用统一发布命令完成干净的 PyInstaller 构建、构建
清单校验和安装器生成：

```powershell
python scripts/build_windows_release.py
```

该命令默认生成 `http://127.0.0.1`、`compatible` 模式且不携带 CA 的测试
包。给局域网其它电脑安装前，应把默认地址替换为 Docker 服务器的固定 IP：

```powershell
python scripts/build_windows_release.py `
  --default-server-url "http://192.168.106.50"
```

需要生成可选 HTTPS 包时，仍会严格要求固定 HTTPS 主机名和有效根 CA：

```powershell
python scripts/build_windows_release.py `
  --default-server-url "https://textile-monitor.internal" `
  --tls-ca-bundle "D:\TextileMonitor-PKI\export\root-ca.pem"
```

如需指定 Inno Setup 编译器路径：

```powershell
python scripts/build_windows_release.py `
  --default-server-url "http://192.168.106.50" `
  --compiler "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
```

正式产物包括：

```bash
dist/windows/TextileDeviceClient
dist/windows/TextileDeviceClient/build-manifest.json
dist/windows/TextileDeviceClient/textile-device-client.exe.sha256
dist/windows/TextileDeviceClient/client-build-defaults.json
dist/windows/TextileDeviceClient/admin-tools/
dist/installer/textile-device-client-setup-<version>.exe
dist/installer/textile-device-client-setup-<version>.exe.sha256
```

只有 HTTPS 包会额外包含
`dist/windows/TextileDeviceClient/certs/inspection-root-ca.pem`。

安装器会拒绝版本不一致、源码已变化、哈希不匹配、控制台模式或 bootloader 调试模式的 onedir 目录。PyInstaller 子进程使用隔离的 DLL 搜索路径，避免 Conda 或其它 Python 环境中的 DLL 混入产物。找不到 Inno Setup 编译器时命令会返回失败，不会把已有安装包误报为新产物。
构建清单还会记录配置 Schema、默认 Origin、传输模式、可选根 CA SHA-256
以及 Requests、Certifi、Cryptography 和 PyInstaller 版本。升级安装保留
现有 `config.json`、备份和当前 CA。

拥有 Authenticode 代码签名证书时，可签名客户端和安装器：

```powershell
$env:TDC_SIGN_CERT_THUMBPRINT = "<证书 SHA-1 指纹>"
python scripts/build_windows_release.py `
  --default-server-url "http://192.168.106.50" `
  --sign
```

可通过 `SIGNTOOL_EXE` 指定 `signtool.exe`；也可使用 `--signtool`、`--certificate-thumbprint` 和 `--timestamp-url` 参数。

以下命令仅用于分步排查，不是正式发布入口：

```powershell
python scripts/build_windows_installer.py --sync-only
python scripts/build_windows_onedir.py `
  --default-server-url "http://192.168.106.50"
python scripts/build_windows_installer.py
```

### 调试打包

排查启动问题时，可以临时生成控制台版。该目录会被正式安装器明确拒绝，重新执行统一发布命令后才能生成安装包：

```powershell
python scripts/build_windows_onedir.py `
  --default-server-url "http://192.168.106.50" `
  --console
```

## 配置说明

首次运行时会弹出配置窗口，需要配置以下信息：

- **设备编码**: 设备的唯一标识（1号-8号或自定义）
- **设备名称**: 设备的显示名称
- **服务器地址**: 只填写纯 Origin；测试默认 `http://127.0.0.1`，局域网
  客户端填写 Docker 服务器 IP，例如 `http://192.168.106.50`
- **传输安全**: 默认选择“兼容 HTTP / HTTPS”；也可选择“强制 HTTPS”
- **内部 CA**: 仅 HTTPS 地址需要填写
- **工作路径**: 监测根目录，如 `F:\\tmp\\AiCodingTest\\参考文件\\bak`
- **上报间隔**: 状态上报间隔（秒），默认 5 秒

服务器地址禁止用户名、密码、`/api` 路径、查询参数和片段。`required`
模式仍会拒绝 HTTP；选择 HTTPS 地址时，无论传输模式为何都必须提供并校验
CA。客户端不会读取系统代理环境变量。
配置文件使用临时文件和原子替换，失败时继续使用旧运行配置并保留
`config.json.bak`。

已有客户端首次升级时，缺少 `config_schema_version` 的旧配置会迁移为
Schema 2 的 `compatible` 并保留原服务器地址，不会中断原有 HTTP 上报。

客户端结果服务监听 `0.0.0.0:9100`。客户端会根据服务器地址选择实际使用的局域网网卡并上报该网卡 IP；当服务器地址是本机环回地址时，会使用 `host.docker.internal` 供本机 Docker 后端访问。生产环境还需在 Windows 防火墙中允许服务器访问客户端 TCP 9100 端口。

当前测试和试运行配置的核心字段如下：

```json
{
  "config_schema_version": 2,
  "server_url": "http://192.168.106.50",
  "transport_security": "compatible",
  "tls_ca_bundle": ""
}
```

### 可选：迁移已有客户端到内部 HTTPS

先由管理员线下核对内部根证书 SHA-256 指纹。在服务器启用 HTTPS 之前，
于安装目录 `admin-tools` 中以管理员身份执行 Prepare，仅预置根证书和
本机 `hosts`，不会访问 443，也不会修改仍在运行的客户端配置：

```powershell
.\migrate_to_internal_https.ps1 `
  -Phase Prepare `
  -ServerIp "192.168.106.50" `
  -RootCertificate "D:\Deploy\root-ca.cer" `
  -ExpectedRootSha256 "<线下核对的 64 位 SHA-256>"
```

服务器 HTTPS 上线并通过部署预检后，再执行 Activate：

```powershell
.\migrate_to_internal_https.ps1 `
  -Phase Activate `
  -ServerIp "192.168.106.50" `
  -RootCertificate "D:\Deploy\root-ca.cer" `
  -CaBundle "D:\Deploy\root-ca.pem" `
  -ExpectedRootSha256 "<线下核对的 64 位 SHA-256>"
```

Activate 会核验真实 HTTPS、更新客户端 CA 和配置、重启客户端，并等待后端
观察到该 `device_code` 的新心跳。只有全部验证通过才会把迁移清单标记为
完成。首次把旧客户端迁到 HTTPS 时，
`-TransportSecurity` 默认使用 `compatible`；此模式仍会严格校验证书且
不会自动降级 HTTP。连续观察稳定后再次以
`-TransportSecurity required` 执行加固。

HTTPS 可信探测和配置激活之前的失败会完整回滚；激活后的重启或上报核验
失败不会恢复、运行旧 HTTP 配置，而会停止客户端、保留安全 HTTPS 配置并
写入 `activation_failed` 审计状态，等待管理员排查。整个过程不查询公司
DNS，也不继承 Windows 或环境变量中的代理。

根 CA 轮换时，先把“旧根 + 新根”的 PEM bundle 分发给终端，并把附加根
指纹和当前服务器仍使用的根指纹显式传给脚本：

```powershell
.\migrate_to_internal_https.ps1 `
  -Phase Activate `
  -ServerIp "192.168.106.50" `
  -RootCertificate "D:\Deploy\new-root-ca.cer" `
  -CaBundle "D:\Deploy\old-and-new-roots.pem" `
  -ExpectedRootSha256 "<新根 SHA-256>" `
  -AllowedAdditionalRootSha256 "<旧根 SHA-256>" `
  -ExpectedServerRootSha256 "<当前服务器根 SHA-256>" `
  -TransportSecurity required
```

只有在终端和客户端完成重叠信任后，服务器才能切换到新根签发的证书；最后
再通过受控批次移除旧根，禁止直接覆盖造成信任中断。

## 使用说明

### 托盘图标功能

右键点击系统托盘图标，可以执行以下操作：

- **打开配置**: 修改设备配置
- **维护模式/正常模式**: 切换维护状态
- **查看日志**: 打开日志查看窗口
- **重新连接**: 重新连接服务器
- **退出**: 退出程序

### 状态说明

- **绿色 - 空闲**: 设备空闲，未进行检测
- **蓝色 - 运行**: 设备正在检测（进度 > 0）
- **橙色 - 维护**: 手动维护模式
- **红色 - 错误**: 手动错误状态
- **灰色 - 离线**: 服务器连接异常

### 进度判定逻辑（工作路径）

程序会监测 **工作路径** 下最近修改的子文件夹，并按以下规则判定进度：

```
result/ 非空                     -> 100
original_image + mask + cut_pic + result(空) -> 80
original_image + cut_pic         -> 20
其他                             -> 0
```

示例：

```
F:\\tmp\\AiCodingTest\\参考文件\\bak
└── 26X900143-2H-1
    ├── original_image
    ├── mask
    ├── cut_pic
    └── result
```

检测逻辑只扫描 **工作路径下的一层子目录**，不会递归多级。

## 项目结构

```
textile-device-client/
├── main.py                    # 主程序入口
├── build.py                   # 兼容入口，转发到 scripts/build_windows_onedir.py
├── requirements.txt           # Python 依赖
├── requirements-build.lock.txt # Windows 打包环境锁定依赖
├── modules/
│   ├── __init__.py
│   ├── version.py             # 应用版本号
│   ├── config.py              # 配置管理
│   ├── logger.py              # 日志管理
│   ├── api_client.py          # 服务端 API 客户端
│   ├── transport_security.py  # Origin、CA 与 TLS 错误诊断
│   ├── device_manager.py      # 设备注册和管理
│   ├── progress_reader.py     # 进度计算与目录监测
│   ├── metrics_collector.py   # 系统指标采集
│   ├── status_reporter.py     # 状态上报器
│   ├── tray_icon.py           # 系统托盘
│   ├── config_window.py       # 配置窗口
│   └── log_window.py          # 日志查看窗口
├── scripts/
│   ├── build_support.py       # 版本同步和打包辅助逻辑
│   ├── build_windows_onedir.py # PyInstaller onedir 构建入口
│   ├── build_windows_installer.py # Inno Setup 安装包构建入口
│   ├── build_windows_release.py # 正式发布统一入口
│   ├── migrate_to_internal_https.ps1 # 已安装客户端 HTTPS 迁移入口
│   └── verify_client_https_reporting.ps1 # 迁移后状态上报核验
├── packaging/
│   ├── pyinstaller/
│   │   └── textile_device_client.spec
│   └── inno-setup/
│       ├── textile_device_client.iss
│       └── version.auto.iss
├── resources/
│   └── icon.ico              # 托盘图标
├── config.json               # 配置文件（运行时生成）
└── logs/                     # 日志目录
    └── client.YYYYMMDD.log   # 日志文件
```

## API 接口

客户端调用以下服务端接口：

### 创建设备
```
POST /api/devices
Content-Type: application/json

{
  "device_code": "1号",
  "name": "1号设备",
  "model": null,
  "location": null,
  "description": null
}
```

### 获取设备列表
```
GET /api/devices
```

### 上报状态
```
POST /api/devices/{device_code}/status
Content-Type: application/json

{
  "report_id": "8cb6d4df-3177-43e9-a04f-c9023af18e50",
  "reported_at": "2026-07-16T15:00:00Z",
  "status": "busy",
  "task_id": "TASK_20240118_143000",
  "task_name": "AI显微镜检测",
  "task_progress": 75,
  "metrics": {
    "cpu": 45.2,
    "memory": 60.5,
    "disk": 80.0,
    "runtime": 3600
  }
}
```

`report_id` 是单次采样的幂等键；同一次 HTTP 重试必须复用相同值，
下一次状态采样再生成新的 UUID。`reported_at` 必须使用带时区的 UTC 时间。

### 健康检查
```
GET /health/ready
```

健康检查、注册、状态上报和重连均复用同一个 Requests Session，并拒绝
重定向。HTTPS 模式下会统一使用配置的 CA bundle。

## 故障排查

### 问题：无法连接服务器

**原因**：服务器地址、端口、本机防火墙或局域网连接异常

**解决**：
1. 确认服务器地址是 Docker 服务器的实际局域网 IP，例如
   `http://192.168.106.50`
2. 使用浏览器打开同一地址，确认服务可访问
3. 检查服务器和当前电脑的本机防火墙（不涉及公司网络设备）
4. 若主动启用了 HTTPS，再核对本机 `hosts`、根证书 SHA-256 和
   `certs/inspection-root-ca.pem`

HTTPS 模式下，日志会分别报告“根 CA 不受信任、证书域名不匹配、证书
过期、证书尚未生效、CA 文件缺失或损坏”以及普通超时。客户端没有
`verify=False` 或自动协议降级逻辑。

### 问题：工作路径读取失败

**原因**：工作路径不可访问或目录为空

**解决**：
1. 检查工作路径是否正确
2. 确认有访问工作路径的权限
3. 确认工作路径下有子文件夹

### 问题：设备注册失败

**原因**：设备编码已存在或服务器错误

**解决**：
1. 检查设备编码是否已被使用
2. 查看日志文件了解详细错误
3. 手动在服务器创建设备

### 问题：托盘图标不显示

**原因**：程序未正常启动

**解决**：
1. 检查任务管理器是否有 textile-device-client.exe 进程
2. 查看日志文件了解启动错误
3. 尝试卸载后重新安装

## 日志说明

日志文件位于 `logs/client.YYYYMMDD.log`，包含以下信息：

- 程序启动和关闭
- 设备注册过程
- 状态上报结果
- 错误和警告信息

日志保留最近 7 天，自动清理。

## 开发说明

### Olympus 日志回放测试

生产客户端只监听实时更新的 `Olympus.log`。测试时可将当前日志或归档日志按原始时间间隔追加到独立的测试文件：

```powershell
.venv\Scripts\python.exe scripts\replay_olympus_log.py `
  "F:\tmp\test\olympus log\Olympus.log" `
  --output "F:\tmp\test\olympus-replay\Olympus.log" `
  --truncate
```

默认 `--speed 1` 为真实速度。联调时可使用 `--speed 30` 进行 30 倍速回放；也可使用 `--start-at` 和 `--end-at` 限定源日志时间范围。传入包含日志的目录时，程序会按各日志首条时间戳排序并回放其中全部 `.log` 文件。

### 添加新功能

1. 在 `modules/` 目录下创建新模块
2. 在 `main.py` 中集成新功能
3. 更新 `requirements.txt` 添加新依赖
4. 如需额外 hidden import，更新 `scripts/build_support.py`
5. 如需调整打包布局，更新 `packaging/pyinstaller/textile_device_client.spec`

### 调试模式

修改 `config.json` 中的日志级别为 `DEBUG` 以获取详细日志：

```json
{
  "log_level": "DEBUG"
}
```

## 许可证

MIT License

## 联系方式

如有问题请联系系统管理员。
