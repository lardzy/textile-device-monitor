# 修复说明 - 首次运行问题

## 问题描述

用户报告：每次运行程序都默认为第一次运行，即使 config.json 中已经明确设置 `is_first_run: false`。

## 问题原因

在 `main.py` 的 `run()` 方法中（第87-91行），存在逻辑错误：

```python
# 错误的代码（修复前）
if self.config.is_first_run():
    self.logger.info("首次运行，进入配置流程")
if not self._show_config_dialog():  # ❌ 这里应该是 elif
    self.logger.warning("用户取消配置，程序退出")
    return
else:
    self.logger.info("加载已保存的配置")
    self.initialize()
```

### 问题分析

这段代码有两个独立的 `if` 语句，而不是 `if-elif-else` 结构：

1. 第一个 `if`：如果 `is_first_run()` 返回 `True`，打印日志
2. 第二个 `if`：**无论** `is_first_run()` 返回什么，都会执行

结果：
- 即使 `is_first_run` 为 `False`，程序仍然会尝试显示配置对话框
- 因为第二个 `if not self._show_config_dialog()` 总是被执行

## 修复方案

修改为正确的 `if-elif-else` 结构：

```python
# 修复后的代码
if self.config.is_first_run():
    self.logger.info("首次运行，进入配置流程")
    if not self._show_config_dialog():
        self.logger.warning("用户取消配置，程序退出")
        return
else:
    self.logger.info("加载已保存的配置")
    self.initialize()
```

### 修复说明

1. 当 `is_first_run()` 返回 `True`：
   - 打印"首次运行，进入配置流程"
   - 显示配置对话框
   - 如果用户取消，程序退出
   - 如果用户确认，继续执行（后续代码）

2. 当 `is_first_run()` 返回 `False`：
   - 执行 `else` 分支
   - 打印"加载已保存的配置"
   - 初始化客户端
   - 不显示配置对话框

## 测试验证

### 测试脚本

创建了 `test_config.py` 验证配置加载和保存逻辑：

```bash
python test_config.py
```

### 测试结果

✅ **测试1**：加载现有配置文件
- `is_first_run` 正确读取为 `False`
- `device_registered` 正确读取为 `True`

✅ **测试2**：标记为已配置
- 保存后 `is_first_run` 仍然为 `False`
- 配置持久化成功

✅ **测试3**：创建新配置文件
- 新配置 `is_first_run` 默认为 `True`

✅ **测试4**：重新加载配置文件
- 重新加载后配置正确
- 标记的值持久化成功

## 影响范围

### 修改的文件

1. **textile-device-client/main.py** (第87-91行)
   - 修复了 `run()` 方法的逻辑错误

### 新增的文件

1. **textile-device-client/test_config.py**
   - 配置加载和保存的单元测试

## 验证步骤

### 1. 测试现有配置

```bash
cd textile-device-client
python test_config.py
```

应该看到：
```
[OK] 正确：is_first_run 是 False
[OK] 正确：device_registered 是 True
```

### 2. 测试主程序

```bash
python main.py
```

如果 `config.json` 中 `is_first_run` 为 `False`：
- 应该打印"加载已保存的配置"
- 应该**不显示**配置对话框

如果 `config.json` 中 `is_first_run` 为 `True`：
- 应该打印"首次运行，进入配置流程"
- 应该显示配置对话框

### 3. 模拟首次运行

```bash
# 删除配置文件
del config.json

# 运行程序
python main.py
```

应该显示配置对话框。

### 4. 验证配置持久化

```bash
# 首次运行后
python main.py

# 检查配置文件
type config.json
```

应该看到：
```json
{
  "is_first_run": false,
  "device_registered": true,
  ...
}
```

再次运行程序：
```bash
python main.py
```

应该**不显示**配置对话框。

## 相关配置

### config.json 结构

```json
{
  "device_code": "1号",
  "device_name": "1号",
  "server_url": "http://127.0.0.1:8000",
  "progress_base_path": "F:\\tmp\\test",
  "report_interval": 5,
  "log_level": "INFO",
  "manual_status": null,
  "is_first_run": false,      // 关键字段
  "device_registered": true   // 关键字段
}
```

### 默认配置（modules/config.py）

```python
DEFAULT_CONFIG = {
    "device_code": "1号",
    "device_name": "1号",
    "server_url": "http://127.0.0.1:8000",
    "progress_base_path": r"F:\tmp\test",
    "report_interval": 5,
    "log_level": "INFO",
    "manual_status": None,
    "is_first_run": True,       // 默认值
    "device_registered": False, // 默认值
}
```

## 总结

✅ 问题已修复
✅ 逻辑错误已更正
✅ 测试验证通过
✅ 配置持久化正常工作

修复后的程序行为：
- 首次运行：显示配置对话框
- 后续运行：加载已保存的配置，不显示对话框
- 配置持久化：正常保存和读取

---

**修复日期**: 2026-01-18
**影响版本**: 1.0.0
**修复人**: AI Assistant
