# 纺织品检测设备客户端 - 部署指南

## 部署前准备

### 1. 开发环境配置

在开发机器上完成以下步骤：

```bash
cd textile-device-client
python setup-dev.bat
```

### 2. 安装 PyInstaller

```bash
pip install pyinstaller
```

### 3. 准备图标文件

将托盘图标文件 `icon.ico` 放入 `resources/` 目录。

如果没有图标，可以跳过，程序会使用默认的空白图标。

## 打包流程

### 方法一：自动打包

```bash
python build.py
```

### 方法二：手动打包

```bash
pyinstaller --name=textile-device-client --onedir --clean --noconfirm ^
    --add-modules=modules ^
    --hidden-import=PyQt6 ^
    --hidden-import=PyQt6.QtWidgets ^
    --hidden-import=PyQt6.QtCore ^
    --hidden-import=pystray ^
    --hidden-import=PIL ^
    --hidden-import=psutil ^
    --hidden-import=requests ^
    --icon=resources/icon.ico ^
    --paths=. main.py
```

打包完成后，可执行文件位于 `dist/textile-device-client/` 目录。

## 部署流程

### 方式一：使用安装脚本（推荐）

在目标 Windows 机器上：

1. **复制打包后的文件夹**
   - 将整个 `dist/textile-device-client/` 目录复制到目标机器
   - 同时复制 `install.bat` 文件

2. **运行安装脚本**
   ```batch
   install.bat
   ```

3. **配置设备**
   - 首次运行会自动弹出配置窗口
   - 配置完成后程序会自动注册设备并开始运行

### 方式二：手动部署

1. **复制文件到安装目录**
   ```
   C:\Program Files\TextileDeviceClient\
   ```

2. **创建桌面快捷方式**
   - 右键点击 `textile-device-client.exe`
   - 发送到桌面快捷方式

3. **配置开机自启动**
   - Win + R，输入 `shell:startup`
   - 将快捷方式复制到启动文件夹

## 验证部署

### 1. 检查进程

打开任务管理器，查看是否有 `textile-device-client.exe` 进程。

### 2. 检查托盘图标

查看系统托盘区域，应该看到客户端图标。

### 3. 查看日志

打开 `C:\Program Files\TextileDeviceClient\logs\` 目录，查看最新的日志文件。

### 4. 验证服务器连接

右键点击托盘图标，选择"重新连接"，查看连接状态。

## 常见部署问题

### 问题：安装失败，权限不足

**解决**：以管理员身份运行 `install.bat`

### 问题：程序无法启动

**解决**：
1. 检查是否安装了必要的运行时库
2. 查看日志文件了解错误详情
3. 尝试在命令行手动运行程序查看错误

### 问题：托盘图标不显示

**解决**：
1. 检查 Windows 通知区域设置
2. 确认程序进程正在运行
3. 重新启动程序

### 问题：设备注册失败

**解决**：
1. 检查服务器地址是否正确
2. 确认服务器正在运行
3. 检查网络连接
4. 查看服务器日志

## 卸载

运行卸载脚本：

```batch
uninstall.bat
```

或手动删除：
1. 停止程序进程
2. 删除安装目录
3. 删除快捷方式
4. 删除注册表启动项

## 更新版本

### 更新步骤

1. **停止旧版本**
   - 通过托盘图标退出程序

2. **备份配置**
   - 复制 `config.json` 文件

3. **安装新版本**
   - 运行新的 `install.bat`

4. **恢复配置**
   - 将备份的 `config.json` 复制回安装目录

### 批量更新

可以使用以下 PowerShell 脚本批量更新多台设备：

```powershell
$computers = Get-Content "computers.txt"
$sourceDir = "\\server\share\textile-device-client"

foreach ($computer in $computers) {
    Write-Host "更新 $computer..."

    Stop-Process -ComputerName $computer -Name "textile-device-client.exe" -Force -ErrorAction SilentlyContinue

    Copy-Item -Path "$sourceDir\*" -Destination "\\$computer\c$\Program Files\TextileDeviceClient\" -Recurse -Force

    Write-Host "$computer 更新完成"
}
```

## 性能优化

### 1. 减少启动时间

- 使用 `--onefile` 模式打包（启动稍慢但体积小）
- 减少不必要的依赖
- 优化初始化代码

### 2. 减少内存占用

- 定期清理日志文件
- 优化数据采集频率
- 避免频繁的 GUI 操作

### 3. 网络优化

- 增加上报间隔（减少服务器压力）
- 实现数据缓存和批量上报
- 优化重试策略

## 安全建议

### 1. 配置文件保护

- 限制配置文件的访问权限
- 不要在配置文件中存储敏感信息
- 定期检查配置文件完整性

### 2. 网络安全

- 使用 HTTPS 连接（如果服务器支持）
- 实现客户端证书认证
- 加密敏感数据

### 3. 日志管理

- 不要在日志中记录敏感信息
- 定期清理旧日志
- 设置日志文件权限

## 监控和维护

### 1. 监控指标

- 客户端在线率
- 上报成功率
- 平均响应时间
- 错误率

### 2. 日志分析

- 定期分析日志文件
- 关注错误和警告信息
- 建立异常告警机制

### 3. 更新管理

- 建立版本发布流程
- 测试新版本兼容性
- 提供回滚方案

## 附录

### A. 配置文件示例

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

### B. 注册表位置

```
HKCU\Software\Microsoft\Windows\CurrentVersion\Run\TextileDeviceClient
```

### C. 日志文件位置

```
C:\Program Files\TextileDeviceClient\logs\client.YYYYMMDD.log
```

### D. 常用命令

```batch
# 查看进程
tasklist | findstr textile-device-client

# 停止进程
taskkill /F /IM textile-device-client.exe

# 查看日志
type "C:\Program Files\TextileDeviceClient\logs\client.%date:~0,10%.log"

# 查看配置
type "C:\Program Files\TextileDeviceClient\config.json"
```
