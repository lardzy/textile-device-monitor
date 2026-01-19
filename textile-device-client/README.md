# 纺织品检测设备客户端

纺织品检测设备监控系统的 Windows 客户端程序，用于设备状态上报和进度监控。

## 功能特性

- **自动设备注册**: 首次运行时自动在服务器注册设备
- **进度文件读取**: 从网络共享路径读取设备检测进度
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

1. **打包程序**
```bash
python build.py
```

2. **安装程序**
```bash
install.bat
```

3. **卸载程序**
```bash
uninstall.bat
```

## 配置说明

首次运行时会弹出配置窗口，需要配置以下信息：

- **设备编码**: 设备的唯一标识（1号-8号或自定义）
- **设备名称**: 设备的显示名称
- **服务器地址**: 服务器 API 地址，如 `http://192.168.1.100:8000`
- **进度文件路径**: 网络共享路径，如 `\\192.168.105.66\...\AI显微镜检测进度(重要勿删)`
- **上报间隔**: 状态上报间隔（秒），默认 5 秒

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

### 进度文件

预设设备（1号-8号）会从网络共享路径读取进度：

```
\\192.168.105.66\17检验八部\10特纤\02-检验\其他\AI显微镜检测进度(重要勿删)\{设备名}\result.txt
```

result.txt 文件内容为一个 0-100 的整数，代表当前检测进度。

自定义设备由于没有对应的进度文件，进度固定为 100，状态为空闲。

## 项目结构

```
textile-device-client/
├── main.py                    # 主程序入口
├── build.py                   # PyInstaller 打包脚本
├── install.bat                # Windows 安装脚本
├── uninstall.bat              # Windows 卸载脚本
├── requirements.txt           # Python 依赖
├── modules/
│   ├── __init__.py
│   ├── config.py              # 配置管理
│   ├── logger.py              # 日志管理
│   ├── api_client.py          # 服务端 API 客户端
│   ├── device_manager.py      # 设备注册和管理
│   ├── progress_reader.py     # 进度文件读取
│   ├── metrics_collector.py   # 系统指标采集
│   ├── status_reporter.py     # 状态上报器
│   ├── tray_icon.py           # 系统托盘
│   ├── config_window.py       # 配置窗口
│   └── log_window.py          # 日志查看窗口
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

### 问题：进度文件读取失败

**原因**：网络共享路径不可访问

**解决**：
1. 检查网络共享路径是否正确
2. 确认有访问共享路径的权限
3. 使用 Windows 资源管理器手动测试路径

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

### 添加新功能

1. 在 `modules/` 目录下创建新模块
2. 在 `main.py` 中集成新功能
3. 更新 `requirements.txt` 添加新依赖
4. 更新 `build.py` 确保新模块被打包

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
