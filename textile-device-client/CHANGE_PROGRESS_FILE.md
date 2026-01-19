# 修改说明 - 进度文件获取方式

## 修改内容

将进度文件获取方式从"路径拼接"改为"直接指定完整路径"。

## 修改前

### 配置结构
```json
{
  "progress_base_path": "\\\\192.168.105.66\\17检验八部\\...",
  "device_name": "1号"
}
```

### 文件路径构建
```python
# modules/progress_reader.py
progress_file = os.path.join(
    self.base_path, device_name, "result.txt"
)
```

### 实际路径
```
\\192.168.105.66\17检验八部\...\1号\result.txt
```

## 修改后

### 配置结构
```json
{
  "progress_file_path": "\\\\192.168.105.66\\17检验八部\\...\\1号\\result.txt"
}
```

### 文件路径构建
```python
# modules/progress_reader.py
self.progress_file_path = progress_file_path  # 直接使用
```

### 实际路径
```
\\192.168.105.66\17检验八部\...\1号\result.txt
```

## 修改的文件

### 1. modules/config.py

#### 修改前
```python
DEFAULT_CONFIG = {
    ...
    "progress_base_path": r"F:\tmp\test",
    ...
}

def get_progress_base_path(self) -> str:
    return self.config.get("progress_base_path", "")
```

#### 修改后
```python
DEFAULT_CONFIG = {
    ...
    "progress_file_path": "",
    ...
}

def get_progress_file_path(self) -> str:
    return self.config.get("progress_file_path", "")
```

### 2. modules/progress_reader.py

#### 修改前
```python
class ProgressReader:
    def __init__(self, base_path: str, logger: Logger):
        self.base_path = base_path
        self.logger = logger

    def read_progress(self, device_name: str, is_preset: bool) -> int:
        if not is_preset:
            return 100

        progress_file = os.path.join(self.base_path, device_name, "result.txt")
        # 读取文件...
```

#### 修改后
```python
class ProgressReader:
    def __init__(self, progress_file_path: str, logger: Logger):
        self.progress_file_path = progress_file_path
        self.logger = logger

    def read_progress(self) -> int:
        if not self.progress_file_path:
            return 0

        # 直接读取 self.progress_file_path
        with open(self.progress_file_path, "r", encoding="utf-8") as f:
            content = f.read().strip()
        # ...
```

### 3. modules/status_reporter.py

#### 修改前
```python
def __init__(self, ..., device_name: str, is_preset: bool, ...):
    self.device_name = device_name
    self.is_preset = is_preset

def _determine_status(self):
    progress = self.progress_reader.read_progress(self.device_name, self.is_preset)

def _get_task_progress(self):
    return self.progress_reader.read_progress(self.device_name, self.is_preset)
```

#### 修改后
```python
def __init__(self, ..., device_code: str, ...):  # 移除 device_name 和 is_preset
    self.device_code = device_code

def _determine_status(self):
    progress = self.progress_reader.read_progress()

def _get_task_progress(self):
    return self.progress_reader.read_progress()
```

### 4. modules/config_window.py

#### 修改前
```python
# UI 元素
progress_path_label = QLabel("进度文件基础路径:")
self.progress_path_edit = QLineEdit()

# 加载配置
self.progress_path_edit.setText(self.current_config.get("progress_base_path", ""))

# 保存配置
"progress_base_path": progress_path,
```

#### 修改后
```python
# UI 元素
progress_file_label = QLabel("进度文件路径:")
self.progress_file_edit = QLineEdit()
self.browse_button = QPushButton("浏览...")

# 加载配置
self.progress_file_edit.setText(self.current_config.get("progress_file_path", ""))

# 保存配置
"progress_file_path": progress_file_path,

# 新增：浏览文件功能
def _browse_progress_file(self):
    file_path, _ = QFileDialog.getOpenFileName(
        self,
        "选择进度文件",
        "",
        "Text Files (*.txt);;All Files (*.*)"
    )
    if file_path:
        self.progress_file_edit.setText(file_path)
```

### 5. main.py

#### 修改前
```python
# 初始化 ProgressReader
self.progress_reader = ProgressReader(
    base_path=config["progress_base_path"], logger=self.logger
)

# 初始化 StatusReporter
self.status_reporter = StatusReporter(
    ...
    device_name=config["device_name"],
    is_preset=self.device_manager.is_preset_device(config["device_code"]),
    ...
)
```

#### 修改后
```python
# 初始化 ProgressReader
self.progress_reader = ProgressReader(
    progress_file_path=config["progress_file_path"], logger=self.logger
)

# 初始化 StatusReporter
self.status_reporter = StatusReporter(
    ...
    device_code=config["device_code"],
    ...  # 移除 device_name 和 is_preset
)
```

### 6. config.json

#### 修改前
```json
{
  "device_code": "1号",
  "device_name": "1号",
  "server_url": "http://127.0.0.1:8000",
  "progress_base_path": "F:\\tmp\\test",
  "report_interval": 5,
  ...
}
```

#### 修改后
```json
{
  "device_code": "1号",
  "device_name": "1号",
  "server_url": "http://127.0.0.1:8000",
  "progress_file_path": "",
  "report_interval": 5,
  ...
}
```

## 改进效果

### 1. 更灵活
- 不再依赖设备名称和固定的文件结构
- 可以指定任意位置的进度文件

### 2. 更简单
- 直接指定完整路径，无需拼接
- 减少出错的可能性

### 3. 更直观
- 配置窗口中可以直接浏览选择文件
- 一步完成配置

### 4. 移除依赖
- 不再需要"预设设备"列表
- 不再需要判断设备类型
- 简化代码逻辑

## 迁移说明

### 对于现有配置

**旧配置**：
```json
{
  "progress_base_path": "\\\\192.168.105.66\\...\\AI显微镜检测进度(重要勿删)",
  "device_name": "1号"
}
```

**新配置**：
```json
{
  "progress_file_path": "\\\\192.168.105.66\\...\\AI显微镜检测进度(重要勿删)\\1号\\result.txt"
}
```

### 转换方法

在首次运行时：
1. 浏览选择进度文件
2. 系统会自动保存完整路径
3. 后续运行直接使用保存的路径

## 配置界面变化

### 修改前
```
进度文件基础路径:
[输入框 - 如: \\192.168.105.66\...\AI显微镜检测进度(重要勿删)]
```

### 修改后
```
进度文件路径:
[输入框] [浏览...]
```

---

**修改日期**: 2026-01-18
**版本**: 1.1.0
