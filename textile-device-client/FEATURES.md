# 纺织品检测设备客户端 - 完整功能说明

## 项目概述

纺织品检测设备客户端是一个 Windows 桌面应用程序，用于：
- 自动上报设备状态到服务器
- 从网络共享路径读取设备检测进度
- 采集系统基础指标（CPU、内存、磁盘）
- 后台运行，通过系统托盘进行管理

## 完整功能列表

### 1. 自动设备注册 ✅
- 首次运行时自动在服务器注册设备
- 通过 GET /api/devices 检查设备是否存在
- 设备不存在则调用 POST /api/devices 创建
- 标记已注册，避免重复创建

### 2. 进度文件读取 ✅
- 从网络共享路径读取进度文件
- 预设设备（1号-8号）：从 `\\192.168.105.66\...\{设备名}\result.txt` 读取
- 自定义设备：进度固定为 100
- 文件不存在或读取失败：进度返回 0
- 自动验证进度值范围（0-100）

### 3. 系统指标采集 ✅
- **CPU 使用率**：使用 psutil.cpu_percent()
- **内存使用率**：使用 psutil.virtual_memory()
- **磁盘使用率**：使用 psutil.disk_usage('/')
- **运行时间**：自程序启动以来的秒数

### 4. 定时状态上报 ✅
- 每 5 秒上报一次（可配置）
- 上报到 POST /api/devices/{device_code}/status
- 自动生成 task_id（格式：TASK_YYYYMMDD_HHMMSS）
- 固定 task_name = "AI显微镜检测"
- 上报失败自动重试 3 次
- 超时时间：5 秒

### 5. 状态判断逻辑 ✅
- **手动模式优先**：如果设置 maintenance 或 error，直接使用
- **自动模式**：
  - 进度 > 0 → status = "busy"
  - 进度 = 0 → status = "idle"

### 6. 系统托盘 ✅
- 后台运行，不显示主窗口
- 托盘图标显示当前状态（颜色区分）
- 右键菜单功能：
  - 打开配置
  - 维护模式 / 正常模式（切换）
  - 查看日志
  - 重新连接
  - 退出

### 7. 图形配置界面 ✅
- 首次运行自动弹出
- 可配置项：
  - 设备编码（下拉选择或自定义）
  - 设备名称（自动填充或自定义）
  - 服务器地址
  - 进度文件基础路径
  - 上报间隔（秒）
- 配置保存到 config.json

### 8. 日志查看窗口 ✅
- 实时显示最新日志
- 支持自动刷新（2秒间隔）
- 可显示行数可配置
- 日志文件：logs/client.YYYYMMDD.log
- 自动保留 7 天日志

## 技术架构

### 项目结构
```
textile-device-client/
├── main.py                      # 主程序入口
├── test_core.py                 # 核心功能测试
├── build.py                     # PyInstaller 打包脚本
├── install.bat                  # Windows 安装脚本
├── uninstall.bat                # Windows 卸载脚本
├── setup-dev.bat                 # 开发环境安装脚本
├── start.bat                     # 快速启动脚本
├── requirements.txt              # Python 依赖
├── README.md                     # 使用说明
├── DEPLOY.md                     # 部署指南
├── .gitignore                    # Git 忽略文件
├── modules/
│   ├── __init__.py              # 模块包（GUI 模块可选导入）
│   ├── config.py                # 配置管理
│   ├── logger.py                # 日志管理
│   ├── api_client.py            # 服务端 API 客户端
│   ├── device_manager.py        # 设备注册和管理
│   ├── progress_reader.py       # 进度文件读取
│   ├── metrics_collector.py     # 系统指标采集
│   ├── status_reporter.py       # 状态上报器
│   ├── tray_icon.py             # 系统托盘
│   ├── config_window.py         # 配置窗口（PyQt6）
│   └── log_window.py            # 日志查看窗口（PyQt6）
├── resources/
│   └── icon.ico                 # 托盘图标
├── logs/                         # 日志目录（运行时生成）
└── config.json                  # 配置文件（运行时生成）
```

### 核心模块说明

#### 1. config.py - 配置管理
- 默认配置和用户配置合并
- 配置验证和持久化
- 提供便捷的 getter/setter 方法
- 标记首次运行和设备注册状态

#### 2. logger.py - 日志管理
- 多级别日志（DEBUG, INFO, WARNING, ERROR）
- 文件和控制台双输出
- 按天轮转日志文件
- 自动清理 7 天前的日志

#### 3. api_client.py - 服务端 API 客户端
- HTTP 请求封装
- 自动重试机制（最多 3 次）
- 请求超时处理（5 秒）
- 统一的错误处理

#### 4. device_manager.py - 设备管理器
- 检查设备是否存在
- 自动注册新设备
- 预设设备列表管理
- 设备信息缓存

#### 5. progress_reader.py - 进度读取器
- 网络共享文件读取
- 进度值解析和验证
- 异常处理（文件不存在、权限不足）
- 路径可访问性检查

#### 6. metrics_collector.py - 指标采集器
- 使用 psutil 采集系统指标
- 轻量级实现，不消耗过多资源
- 异常安全

#### 7. status_reporter.py - 状态上报器
- 定时上报（独立线程）
- 状态判断逻辑
- 任务 ID 生成
- 进度获取

#### 8. tray_icon.py - 系统托盘
- 使用 pystray 库
- 动态图标（根据状态变色）
- 右键菜单
- 通知功能

#### 9. config_window.py - 配置窗口（PyQt6）
- 图形配置界面
- 表单验证
- 设备编码自动填充

#### 10. log_window.py - 日志查看窗口（PyQt6）
- 实时日志显示
- 自动刷新
- 可配置显示行数

## 依赖清单

```txt
requests>=2.31.0      # HTTP 客户端
psutil>=5.9.6          # 系统指标采集
pystray>=0.19.5        # 系统托盘
Pillow>=10.1.0         # 图像处理（托盘图标）
PyQt6>=6.6.0           # 图形界面
```

## API 接口调用

### 1. 健康检查
```
GET /health
```

### 2. 获取设备列表
```
GET /api/devices
返回：Device[]
```

### 3. 创建设备
```
POST /api/devices
Content-Type: application/json

请求体：
{
  "device_code": "1号",
  "name": "1号设备",
  "model": null,
  "location": null,
  "description": null
}

返回：Device（201）
```

### 4. 上报设备状态
```
POST /api/devices/{device_code}/status
Content-Type: application/json

请求体：
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

返回：MessageResponse（200）
```

## 配置文件示例

```json
{
  "device_code": "1号",
  "device_name": "1号设备",
  "server_url": "http://192.168.1.100:8000",
  "progress_base_path": "\\\\192.168.105.66\\17检验八部\\10特纤\\02-检验\\其他\\AI显微镜检测进度(重要勿删)",
  "report_interval": 5,
  "log_level": "INFO",
  "manual_status": null,
  "is_first_run": false,
  "device_registered": true
}
```

## 部署方式

### 开发环境运行

```bash
# 1. 安装依赖
cd textile-device-client
pip install -r requirements.txt

# 2. 运行程序
python main.py
```

### 打包为可执行文件

```bash
# 1. 安装 PyInstaller
pip install pyinstaller

# 2. 打包
python build.py

# 3. 安装到 Windows
install.bat
```

### 开机自启动

install.bat 会自动设置开机自启动：
- 注册表：`HKCU\Software\Microsoft\Windows\CurrentVersion\Run\TextileDeviceClient`

## 使用说明

### 首次运行

1. 双击 `textile-device-client.exe`（或运行 `python main.py`）
2. 配置窗口自动弹出
3. 选择设备编码（1号-8号）或自定义
4. 输入服务器地址
5. 输入进度文件路径
6. 点击确定
7. 程序自动注册设备并开始上报
8. 程序最小化到系统托盘

### 日常使用

- **查看状态**：看托盘图标颜色
- **修改配置**：右键 → 打开配置
- **维护设备**：右键 → 维护模式
- **查看日志**：右键 → 查看日志
- **重新连接**：右键 → 重新连接
- **退出程序**：右键 → 退出

### 托盘图标颜色说明

- 🟢 **绿色**：空闲（idle）
- 🔵 **蓝色**：运行中（busy）
- 🟠 **橙色**：维护中（maintenance）
- 🔴 **红色**：错误（error）
- ⚪ **灰色**：离线

## 故障排查

### 问题：无法启动（ImportError: DLL load failed）

**原因**：缺少 Visual C++ Redistributable

**解决**：下载并安装 Visual C++ Redistributable
```
https://learn.microsoft.com/en-us/cpp/windows/latest-supported-vc-redist
```

### 问题：无法连接服务器

**原因**：
1. 服务器地址错误
2. 网络不通
3. 防火墙拦截

**解决**：
1. 检查服务器地址配置
2. 用浏览器访问服务器地址测试
3. 检查防火墙设置

### 问题：进度文件读取失败

**原因**：
1. 网络共享路径不可达
2. 权限不足
3. 文件不存在

**解决**：
1. 检查网络共享路径配置
2. 确认有访问权限
3. 检查文件是否存在

### 问题：设备注册失败

**原因**：
1. 设备编码已存在
2. 服务器错误
3. 网络问题

**解决**：
1. 检查设备编码是否已被使用
2. 查看服务器日志
3. 检查网络连接

### 问题：上报状态不更新

**原因**：
1. 设备离线（30秒无心跳）
2. 服务器问题

**解决**：
1. 检查客户端是否正常运行
2. 查看客户端日志
3. 检查服务器状态

## 测试验证

### 核心功能测试

运行 `python test_core.py` 测试以下功能：
- ✅ 配置管理
- ✅ 日志记录
- ✅ 指标采集
- ✅ 进度读取
- ✅ API 客户端
- ✅ 设备管理

### 完整功能测试

1. 启动客户端
2. 完成首次配置
3. 验证设备自动注册
4. 创建测试进度文件
5. 验证进度读取正确
6. 验证状态上报正常
7. 测试维护模式切换
8. 查看日志窗口
9. 测试重新连接
10. 测试退出和重启

## 性能指标

- **启动时间**：< 2 秒
- **内存占用**：< 50 MB
- **CPU 占用**：< 1%（空闲时）
- **上报延迟**：< 1 秒
- **网络带宽**：< 1 KB/s

## 安全建议

1. 配置文件权限：限制为只有当前用户可读写
2. 网络连接：使用 HTTPS（如果服务器支持）
3. 日志管理：定期清理旧日志
4. 更新机制：定期检查并更新客户端

## 后续优化建议

1. **加密传输**：支持 HTTPS 连接
2. **数据缓存**：离线时缓存数据，恢复后上传
3. **批量上报**：支持批量上报数据
4. **版本管理**：自动更新功能
5. **监控告警**：异常自动通知
6. **性能优化**：减少资源占用
7. **多线程**：优化并发性能

## 开发说明

### 添加新功能

1. 在 `modules/` 目录创建新模块
2. 在 `main.py` 中集成
3. 更新 `requirements.txt`
4. 更新 `build.py` 打包配置

### 调试模式

修改配置文件：
```json
{
  "log_level": "DEBUG"
}
```

### 单元测试

建议添加单元测试覆盖：
- 配置管理
- API 客户端
- 设备管理
- 进度读取
- 指标采集

## 许可证

MIT License

## 联系方式

如有问题请联系系统管理员。

---

**版本**：1.0.0
**最后更新**：2026-01-18
