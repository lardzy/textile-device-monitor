# 纺织品检测设备监控系统

用于监控纺织品检测设备状态、排队管理和数据统计的Web系统。

## 功能特性

### 核心功能
- **设备监控**: 实时显示所有设备状态、当前任务、检测进度和设备指标
- **排队辅助**: 检验员排队管理，支持位置调整，记录修改历史
- **历史记录**: 设备状态历史查询，支持多维度筛选和Excel导出
- **数据统计**: 设备利用率、任务完成量、检测时长等多维度统计分析
- **设备管理**: 设备信息的增删改查管理

### 技术特点
- 无需登录，局域网内公开访问
- WebSocket实时更新设备状态和排队信息
- 响应式设计，支持PC端访问
- 数据自动清理（保留30天）
- Docker容器化部署

## 技术栈

### 后端
- Python 3.11
- FastAPI 0.104
- PostgreSQL 15
- SQLAlchemy 2.0
- WebSocket

### 前端
- React 18
- Ant Design 5
- Recharts
- Axios
- WebSocket Client

## 部署指南

### 前置要求
- Docker 20.10+
- Docker Compose 2.0+
- 服务器IP（用于局域网访问）

### 快速启动

1. 克隆项目
```bash
cd textile-device-monitor
```

2. 启动服务
```bash
docker-compose up -d
```

3. 访问系统
```
http://服务器IP
```

### 服务端口
- 前端: 80
- 后端: 8000
- 数据库: 5432（内部）

### 配置说明

#### 环境变量 (backend/.env)
```bash
DATABASE_URL=postgresql://admin:password123@postgres:5432/textile_monitor
SECRET_KEY=your-secret-key-change-in-production
HEARTBEAT_TIMEOUT=30
DATA_RETENTION_DAYS=30
CORS_ORIGINS=["http://localhost", "http://localhost:80"]
```

## 设备对接

### 设备状态上报接口

**接口地址**: `POST http://服务器IP:8000/api/devices/{device_code}/status`

**请求参数**:
```json
{
  "status": "idle|busy|maintenance|error",
  "task_id": "TASK_20240116_001",           // 可选
  "task_name": "棉纤维检测",                // 可选
  "task_progress": 75,                      // 可选, 0-100
  "metrics": {                              // 可选
    "temperature": 45.2,
    "runtime": 3600,
    "pressure": 1.2
  }
}
```

**上报频率**: 建议5秒一次

**离线判定**: 30秒未上报自动标记为离线

### Python示例代码
```python
import requests
import time

DEVICE_CODE = "DL001"  # 设备编码
SERVER_URL = "http://192.168.1.100:8000"

def report_status(status, task_id=None, task_name=None, progress=None, metrics=None):
    """上报设备状态"""
    data = {
        "status": status,
        "task_id": task_id,
        "task_name": task_name,
        "task_progress": progress,
        "metrics": metrics
    }
    
    try:
        response = requests.post(
            f"{SERVER_URL}/api/devices/{DEVICE_CODE}/status",
            json=data,
            timeout=5
        )
        print(f"上报成功: {response.json()}")
        return response.json()
    except Exception as e:
        print(f"上报失败: {e}")

# 示例：定时上报
while True:
    # 获取当前设备状态
    current_status = get_device_status()
    
    # 上报
    report_status(
        status=current_status['status'],
        task_id=current_status.get('task_id'),
        task_name=current_status.get('task_name'),
        progress=current_status.get('progress'),
        metrics={
            "temperature": current_status.get('temperature'),
            "runtime": current_status.get('runtime')
        }
    )
    
    # 每5秒上报一次
    time.sleep(5)
```

## API文档

启动服务后访问：
- Swagger UI: `http://服务器IP:8000/docs`
- ReDoc: `http://服务器IP:8000/redoc`

## 使用说明

### 设备管理
1. 访问"设备管理"页面
2. 点击"添加设备"
3. 填写设备信息：
   - 设备编码（唯一标识，用于设备上报）
   - 设备名称
   - 设备型号
   - 设备位置
   - 设备描述

### 排队使用
1. 访问"排队辅助"页面
2. 输入检验员姓名（自动保存）
3. 选择设备
4. 点击"加入排队"
5. 可通过上下箭头调整位置
6. 查看今日修改历史

### 设备上报完成
设备完成检测后调用：
```http
POST http://服务器IP:8000/api/queue/{device_id}/complete
```
排队列表会自动减少1

### 历史查询
1. 访问"历史记录"页面
2. 设置筛选条件：
   - 设备
   - 状态
   - 任务ID
   - 日期范围
3. 点击"查询"
4. 可点击"导出Excel"

### 数据统计
1. 访问"数据统计"页面
2. 选择统计类型（日/周/月）
3. 选择日期范围
4. 查看图表分析

## 维护说明

### 数据清理
- 每天凌晨2点自动清理30天前的历史记录
- 每天凌晨2点自动清理昨天的排队修改日志

### 日志查看
```bash
# 查看所有容器日志
docker-compose logs -f

# 查看特定服务日志
docker-compose logs -f backend
docker-compose logs -f frontend
docker-compose logs -f postgres
```

### 重启服务
```bash
# 重启所有服务
docker-compose restart

# 重启特定服务
docker-compose restart backend
```

### 更新代码
```bash
# 拉取最新代码
git pull

# 重新构建并启动
docker-compose up -d --build
```

### 数据备份
```bash
# 备份数据库
docker exec textile-monitor-db pg_dump -U admin textile_monitor > backup.sql

# 恢复数据库
docker exec -i textile-monitor-db psql -U admin textile_monitor < backup.sql
```

## 故障排查

### 问题：无法访问系统
1. 检查容器状态：`docker-compose ps`
2. 检查端口占用：`netstat -ano | findstr :80`
3. 查看防火墙设置
4. 确认服务器IP正确

### 问题：设备状态不更新
1. 检查设备编码是否正确
2. 检查设备能否访问服务器IP:8000
3. 查看后端日志：`docker-compose logs backend`
4. 确认上报频率是否正常

### 问题：WebSocket连接失败
1. 检查服务器IP配置
2. 确认前端配置的WS_URL正确
3. 查看浏览器控制台错误

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    局域网PC浏览器                       │
│                    (React前端:80)                       │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP/WebSocket
┌────────────────────▼────────────────────────────────────┐
│              服务器 (Docker Compose)                   │
│  ┌─────────────────────────────────────────────────┐   │
│  │ 前端 (Nginx + React静态文件)                 │   │
│  │ - 设备状态看板                                   │   │
│  │ - 排队辅助工具                                   │   │
│  │ - 历史记录查询                                   │   │
│  │ - 数据统计仪表板                                 │   │
│  └─────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────┐   │
│  │ 后端 (FastAPI:8000)                            │   │
│  │ - REST API                                      │   │
│  │ - WebSocket                                     │   │
│  │ - 定时任务                                      │   │
│  └─────────────────────────────────────────────────┘   │
│  ┌─────────────────────────────────────────────────┐   │
│  │ 数据库 (PostgreSQL:5432)                        │   │
│  │ - 设备信息                                      │   │
│  │ - 状态历史                                      │   │
│  │ - 排队修改日志                                  │   │
│  └─────────────────────────────────────────────────┘   │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP (设备上报接口)
┌────────────────────▼────────────────────────────────────┐
│              分散设备 (外部程序)                        │
│              定时调用 /api/devices/{code}/status         │
└─────────────────────────────────────────────────────────┘
```

## 许可证

MIT License

## 联系方式

如有问题请联系系统管理员。
