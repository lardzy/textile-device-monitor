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
- 网络连接（访问服务器和共享文件路径）

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

打包环境除运行时依赖外，还需要安装 PyInstaller：

```bash
pip install -r requirements.txt pyinstaller
```

1. **同步安装器版本**
```bash
python scripts/build_windows_installer.py --sync-only
```

2. **打包静默版程序**
```bash
python scripts/build_windows_onedir.py
```

产物目录：

```bash
dist/windows/TextileDeviceClient
```

3. **生成安装包（Windows + Inno Setup）**
```bash
python scripts/build_windows_installer.py
```

安装包输出目录：

```bash
dist/installer
```

如需指定 Inno Setup 编译器路径：

```bash
python scripts/build_windows_installer.py --compiler "C:\Program Files (x86)\Inno Setup 6\ISCC.exe"
```

### 调试打包

正式安装包只包含无控制台的静默版。排查启动问题时，可以临时生成控制台版：

```bash
python scripts/build_windows_onedir.py --console
```

## 配置说明

首次运行时会弹出配置窗口，需要配置以下信息：

- **设备编码**: 设备的唯一标识（1号-8号或自定义）
- **设备名称**: 设备的显示名称
- **服务器地址**: 服务器 API 地址，如 `http://192.168.1.100:8000`
- **工作路径**: 监测根目录，如 `F:\\tmp\\AiCodingTest\\参考文件\\bak`
- **上报间隔**: 状态上报间隔（秒），默认 5 秒

客户端结果服务监听 `0.0.0.0:9100`。客户端会根据服务器地址选择实际使用的局域网网卡并上报该网卡 IP；当服务器地址是本机环回地址时，会使用 `host.docker.internal` 供本机 Docker 后端访问。生产环境还需在 Windows 防火墙中允许服务器访问客户端 TCP 9100 端口。

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
├── modules/
│   ├── __init__.py
│   ├── version.py             # 应用版本号
│   ├── config.py              # 配置管理
│   ├── logger.py              # 日志管理
│   ├── api_client.py          # 服务端 API 客户端
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
│   └── build_windows_installer.py # Inno Setup 安装包构建入口
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

### 健康检查
```
GET /health
```

## 故障排查

### 问题：无法连接服务器

**原因**：服务器地址错误或网络不通

**解决**：
1. 检查服务器地址是否正确
2. 使用浏览器访问服务器地址确认可访问
3. 检查防火墙设置

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
