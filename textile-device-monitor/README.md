# 纺织品检测设备监控系统

用于监控纺织品检测设备状态、排队管理和数据统计的Web系统。

## 功能特性

### 核心功能
- **设备监控**: 实时显示所有设备状态、当前任务、检测进度和设备指标
- **排队辅助**: 检验员排队管理，支持位置调整，记录修改历史
- **历史记录**: 设备状态历史查询，支持多维度筛选和Excel导出
- **数据统计**: 设备利用率、任务完成量、检测时长等多维度统计分析
- **设备管理**: 设备信息的增删改查管理
- **执行系统**: 版本化 DAG、人工任务、文件索引、受控 Excel 副本写入与审计

### 技术特点
- 设备监控等既有功能保持内网免登录；执行系统使用独立账号
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
- Docker Compose 2.24.4+（HTTPS 覆盖文件使用 `!override` 合并标签）
- 一台已接入公司局域网、具有当前可达 IPv4 地址的部门服务器

当前开发和试运行默认使用 HTTP。用户和设备直接访问服务器局域网 IP，不需要
公司 IT 修改 DNS、DHCP、路由器、交换机或公司防火墙，也不需要逐台安装证书
或维护 `hosts`。仓库仍保留内部 CA、证书部署和终端迁移脚本，正式上线需要
HTTPS 时再启用可选覆盖文件。

### 快速启动

1. 克隆项目
```bash
cd textile-device-monitor
```

2. 创建生产配置并填写所有必填项
```bash
cp .env.example .env
```

数据库账号和三个独立随机密钥不得留空。可使用
`openssl rand -base64 48` 分别生成随机密钥。`.env` 不得提交到 Git。
`FRONTEND_BIND_ADDRESS` 可填服务器当前局域网 IPv4；保持默认
`0.0.0.0` 时会监听服务器所有网卡，但仍只有前端 HTTP 端口对宿主机发布。
例如服务器地址为 `192.168.106.50` 时，可设置：

```dotenv
WEB_TRANSPORT=http
FRONTEND_BIND_ADDRESS=0.0.0.0
FRONTEND_HTTP_PORT=80
PUBLIC_HOSTNAME=192.168.106.50
PUBLIC_ORIGIN=http://192.168.106.50
EXECUTION_COOKIE_SECURE=false
HSTS_MAX_AGE=0
```

若把 HTTP 端口改为非 80 值，`PUBLIC_ORIGIN` 也要包含相同端口。

执行系统读取 `//192.168.105.82/材料检测中心` 时复用现有
`area_out_cifs`。在 `.env` 填写 `SMB_USER_B/SMB_PASS_B` 后，Compose 会把
该共享卷额外只读挂载到 backend 与 execution-worker 的
`/data/execution-source`；无需先在 Docker 宿主机手工挂载共享盘。默认
`EXECUTION_SOURCE_ROOT=/data/execution-source/10特纤/02-检验`，其下直接包含
`2026-特种毛`、`2026-再生纤`、`2026-麻棉` 和 `2026-电镜`。
报告上传图片放置节点使用独立的
`SMB_USER_C/SMB_PASS_C` 将 `//192.168.105.82/公共交换文件`
可写挂载到 `/data/report-upload-images`；该变量为基础 Compose 必填项。
后台自动索引由 `EXECUTION_AUTO_INDEX_ROOT_IDS` 控制，默认扫描
`regenerated_fiber_records`、`electron_microscopy_records` 和
`paper_fiber_records`。再生纤扫描只记录
文件元数据，不会批量打开历史工作簿；电镜根目录会索引到“编号
文件夹/部位/图片”层级，供图片选择节点使用。其它目录仍可从执行系统
手动触发刷新。

“电镜—纤维微观形貌 GB/T 36422-2018”流程使用 PostgreSQL 任务快照缓存
匹配旧系统项目名和测试方法。推荐请求只读本地缓存；缓存缺失或过期
时，由独立的 Windows 只读 Bridge 合并刷新，默认 15 分钟有效。当前受控
项目名包括“纤维微观形貌”及同标准子项“膜平面形貌”。
纸类 query 节点遇到正在刷新的快照时，会按
`EXECUTION_TASK_SNAPSHOT_WAIT_SECONDS` 有限等待，默认 60 秒；设为 0
可恢复立即失败。

该流程在选图后继续生成微观形貌原始记录：任务快照提供样品名称、样品识别和
按需判定字段，用户确认后把 1～10 张图片等比例最大化写入版本化 `.xls` 模板。
保存后会重新打开并核对单元格、图片边界、文件格式、SHA-256 及
`微观形貌!A1:L37` 默认打印区域；单图还会再次核对宽高比、最大填充和居中。
文件名包含“图片”，便于旧系统沿用既有
文件类型判定。后续“图片上传”和“特纤复核”目前只生成预检信息：后端、
Windows Bridge 和 Writer 均保持禁写，完成远端编号、图片子记录和复核联动
实机证明前不得启用。

再生纤根数法只识别工作表 `根数法报告1`，并检查该工作表的
`B14:J14`；其它编号工作表及汇总表不作为目标工作表。

两个再生纤流程在工作簿识别后继续逐文件读取结果：

- 根数法从 `根数法报告1` 读取 `B24/G24` 部位、两组纤维名称与含量、
  `B27:B29` 备注；
- 面积法从 `截面统计报告1` 读取 `B26/G26` 部位、两组纤维名称与含量、
  `B29:B31` 备注；
- 工作簿插图会登记为运行制品，人工任务中按需打开；用户可多选需要的
  文件，并从已选文件中指定一份主单。

当两个部位名称均为空但左右结果区均有数据时，系统会分别保留为
“结果1/结果2”，不会把两套独立百分比相加或丢弃其中一套。
系统同时保留公式缓存原值、Excel 显示值和业务结果值；若逐格四舍五入
产生 99.9/100.1 的正常尾差，会记录调整并将业务结果平衡为 100。明显
偏离 100 的数据不会被静默归一化，而会返回读取提示。

当前局域网联调也可把 SMB 凭据单独保存在 Git 已忽略的
`../.tmp/execution-system-secrets/inspection-systems.env`，并在 Compose
命令中依次传入主配置和凭据文件：

```bash
docker compose --env-file .env \
  --env-file ../.tmp/execution-system-secrets/inspection-systems.env \
  -f docker-compose.yml -f docker-compose.execution.yml config
```

后一个文件只补充或覆盖 SMB 凭据，不替代 `.env` 中的数据库和应用配置。

3. 启动本轮常用服务（不构建 Area Infer）

```bash
docker compose --env-file .env \
  --env-file ../.tmp/execution-system-secrets/inspection-systems.env \
  -f docker-compose.yml -f docker-compose.execution.yml \
  up -d --build postgres backend execution-worker frontend
```

4. 通过服务器局域网 IP 访问

```text
http://<服务器局域网IP>
```

默认 Compose 只发布前端 HTTP 端口；PostgreSQL、后端、Area Infer、OCR 和
execution-worker 仍只在 Docker 内部网络通信。`MANAGEMENT_CIDRS` 留空时
允许所有能到达该服务器端口的局域网终端；需要限制时可填写逗号分隔的 CIDR。

### 可选 HTTPS

需要恢复内部 CA 方案时，在配置好证书目录、固定域名和管理网段后叠加
`docker-compose.https.yml`：

```bash
docker compose --env-file .env \
  --env-file ../.tmp/execution-system-secrets/inspection-systems.env \
  -f docker-compose.yml -f docker-compose.execution.yml \
  -f docker-compose.https.yml \
  up -d --build postgres backend execution-worker frontend
```

HTTPS 模式的证书签发、服务器部署和终端迁移步骤见
[内部 CA 与本机 hosts 运维手册](docs/internal-ca-operations.md)。这些脚本
均保留，但默认 HTTP 试运行不需要执行。

### 本地前后端开发

本地开发可直接使用 HTTP。复制
`backend/.env.example` 为 `backend/.env`，生成本地随机密钥后，分别运行：

```bash
cd backend
python -m uvicorn app.main:app --reload --host 127.0.0.1 --port 8000
```

```bash
cd frontend
npm install
npm run dev
```

Vite 开发服务和后端均默认绑定本机回环地址；需要让其它局域网终端访问时，
优先使用上面的 Compose 方式。

### Linux + NVIDIA GPU（面积识别）部署

当服务器是 Linux 并带 NVIDIA 显卡（如 RTX 4060）时，建议使用 GPU 覆盖文件启动 `area-infer`。

1. 前置检查
```bash
nvidia-smi
docker run --rm --gpus all nvidia/cuda:12.4.1-base-ubuntu22.04 nvidia-smi
```

2. 使用 GPU 覆盖文件启动
```bash
docker compose -f docker-compose.yml -f docker-compose.gpu.yml up -d --build area-infer backend execution-worker frontend
```

3. 查看推理服务是否使用 GPU
```bash
docker compose exec backend python -c "import urllib.request; print(urllib.request.urlopen('http://area-infer:9001/health').read().decode())"
```
返回中 `runtime.effective_device` 为 `cuda:0` 表示已走 GPU。

4. 设备策略说明
- `AREA_INFER_DEVICE`：`auto|cuda|cpu`（默认 `auto`）
- `AREA_INFER_GPU_POLICY`：`warn_continue|fail`（默认 `warn_continue`）
- 当 GPU 不可用且策略为 `warn_continue` 时，会回退 CPU 并在 `device_warning` 字段给出告警。

### 服务端口
- 前端: 80（默认 HTTP，可通过 `FRONTEND_HTTP_PORT` 修改）
- 前端 HTTPS: 443（仅叠加 `docker-compose.https.yml` 时发布）
- 后端: 8000（仅 Docker 内部）
- Area Infer: 9001（仅 Docker 内部）
- PostgreSQL: 5432（仅 Docker 内部）

### 配置说明

生产变量以仓库根目录的 `.env.example` 为准。应用默认按生产环境启动，
仍会拒绝缺失/示例密钥和弱数据库密码。默认 `WEB_TRANSPORT=http`、
`EXECUTION_COOKIE_SECURE=false`、`HSTS_MAX_AGE=0`；启用 HTTPS 覆盖后会
恢复 Secure Cookie、证书和管理网段校验。浏览器访问为同源模式，通常保持
`CORS_ORIGINS=` 即可。

### 执行系统

- `/execution` 使用独立账号登录；设备监控、设备状态上报及其它既有页面保持
  原来的内网免登录边界。
- 后端容器启动时先执行 Alembic。空数据库会从 `0001_legacy_baseline`
  完整建库；已有数据库只有通过结构预检后才会标记基线并升级。当前迁移头为
  `0007_project_rules`。
- `execution-worker` 通过 PostgreSQL 租约领取节点。请勿只启动
  `backend` 而遗漏 Worker，否则新运行不会被执行。
- `/health/live` 仅表示 API 进程存活；`/health/ready` 还会检查数据库、
  Alembic 版本、Worker 心跳和执行系统 staging/publish 目录。
- `EXECUTION_BRIDGE_ENABLED=false` 或 `EXECUTION_BRIDGE_TOKEN` 为空时，所有
  外部 Bridge 端点返回 503；两者同时启用后，用户批准的旧系统预检单可能被
  Windows Bridge 立即领取并产生真实副作用。安全门禁完成前保持总开关为
  `false`，启用时使用受保护的 HTTPS/内网链路。
- 源资料通过 `area_out_cifs` 只读映射到 `EXECUTION_SOURCE_ROOT`；中间副本
  和最终发布分别使用 `EXECUTION_RUNTIME_HOST_PATH`、
  `EXECUTION_PUBLISH_HOST_PATH`。三者必须彼此独立，不得相同、嵌套或通过
  符号链接指向同一目录。
- 受控写入 API 只能沿已发布运行的实际节点路径调用；核对、人工确认和发布
  上下文必须一致，发布栅栏会在物理文件副作用前持久化。
- 新旧检务系统节点目前是禁用占位节点；复杂 `.xls/.xlsm` 的高保真写入需要
  后续 Windows Agent 调用 Microsoft Excel Desktop。

仅开发或验收执行系统时，可显式使用覆盖文件跳过 Area Infer：

```bash
docker compose -f docker-compose.yml -f docker-compose.execution.yml \
  --env-file .env \
  --env-file ../.tmp/execution-system-secrets/inspection-systems.env \
  up -d --build postgres backend execution-worker frontend
```

该命令会关闭后端 Area 功能并把 `area-infer` 放入未启用的 `area` profile；
不影响不带覆盖文件的完整生产部署。

首次部署可暂时填写
`EXECUTION_BOOTSTRAP_ADMIN_USERNAME/EXECUTION_BOOTSTRAP_ADMIN_PASSWORD`
创建管理员。确认可以登录后，应从 `.env` 删除这两个值并重建后端容器。

## 设备对接

### 设备状态上报接口

**接口地址**:
`POST http://<服务器局域网IP>/api/devices/{device_code}/status`

**请求参数**:
```json
{
  "report_id": "8cb6d4df-3177-43e9-a04f-c9023af18e50", // 可选；重试必须复用
  "reported_at": "2026-07-16T15:00:00Z",              // 可选；必须包含时区
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

**离线判定**: 90秒未上报自动标记为离线

### Python示例代码
```python
import requests
import time

DEVICE_CODE = "DL001"  # 设备编码
SERVER_URL = "http://192.168.1.20"  # 替换为服务器实际局域网 IP

session = requests.Session()
session.trust_env = False

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
        response = session.post(
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
1. 在“设备监控”中选择设备并快速前往排队区
2. 输入检验员姓名（自动保存）
3. 点击“加入排队”
4. 需要调整顺序时可按住排队表格任意非按钮区域拖动整行
5. 查看今日修改历史

### 设备上报完成
设备完成检测后，继续通过状态上报接口提交完成状态；同一次上报的
HTTP 重试必须复用同一个 `report_id`：
```http
POST http://<服务器局域网IP>/api/devices/{device_code}/status

{
  "status": "idle",
  "task_progress": 100,
  "report_id": "同一次上报保持不变的 UUID",
  "reported_at": "UTC 时间"
}
```
后端会在同一事务内记录完成事件、历史并结算一个队首。旧的
`POST /api/queue/{device_id}/complete` 已停用并返回 410，避免一次物理完成
被两个入口重复结算。

统计完成率按查询范围内开始的任务作为分母；任务跨日完成时归入其
“开始日期”所在的趋势分桶，跨出整个查询范围的完成只计完成量、不进入
该范围完成率分子。所有统计接口最长支持 366 天。

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

统计日期范围最多为366天。完成率以“所选范围内开始的任务”为分母，
仅当同一任务也在该范围内完成时计入分子；跨范围完成仍计入完成量。

## 维护说明

### 数据清理
- 每天凌晨2点自动清理30天前的历史记录
- 每天凌晨2点自动清理昨天的排队修改日志
- 每天凌晨2点清理超过 `STATUS_REPORT_RETENTION_HOURS` 的状态幂等收据

### 日志查看
```bash
# 查看所有容器日志
docker compose logs -f

# 查看特定服务日志
docker compose logs -f backend
docker compose logs -f execution-worker
docker compose logs -f frontend
docker compose logs -f postgres
```

### 重启服务
```bash
# 重启所有服务
docker compose restart

# 重启特定服务
docker compose restart backend execution-worker
```

### 更新代码
```bash
# 拉取最新代码
git pull

# 按部署时的同一条 compose 链路重建并重启（生产链路含 area-infer；
# 不要使用 docker-compose.execution.yml，那是开发机跳过 area-infer 用的）
docker compose --env-file .env -f docker-compose.yml \
  -f ../.tmp/execution-system-local-runtime/docker-compose.production.yml \
  up -d --build
```

### 数据备份

角色名与库名以 .env 的 `POSTGRES_USER` / `POSTGRES_DB` 为准
（旧部署默认是 `admin`，新部署一般是 `textile_prod`）。

**绝对不要用 PowerShell 的 `>` 或管道导出备份**：PowerShell 会按控制台编码
（中文 Windows 为 GBK）错读 docker 的 UTF-8 输出并转存为 UTF-16，中文内容
在导出瞬间就不可逆损坏，这种备份文件**无法用于恢复**。正确做法是让 pg_dump
在容器内直接落盘，再把文件原样拷出（全程不经过终端转码）：

```bat
docker exec textile-monitor-db pg_dump -U <POSTGRES_USER> -d <POSTGRES_DB> --no-owner -f /tmp/backup.sql
docker cp textile-monitor-db:/tmp/backup.sql .\backup.sql
```

辨别既有备份是否完好：文件前两个字节是 `FF FE`（UTF-16 标记）即已损坏、
不可使用；正常纯 SQL 应为 `2D 2D`（`--` 注释开头），且文件内中文显示正常。
可用 `powershell -Command "Format-Hex backup.sql | Select-Object -First 1"` 查看。

### 恢复数据库

动手前先确认备份文件完好（见上一节：文件头 `FF FE` 即已损坏，不要使用）。
恢复目标必须是**空库**，往已初始化的库里直接恢复会全篇冲突。以下命令在
**CMD** 中执行（不要用 PowerShell 管道传 SQL，避免中文被转码）。
按备份来源在 A/B 两个场景中选一个执行。

#### 场景 A：新部署之间搬迁（备份属主 = 当前 `POSTGRES_USER`）

适用于换机重装、整机迁移等"备份来自另一套新部署"的情况。网页账号、
流程开放范围等业务状态都随备份回来，恢复后无需重建。

```bat
:: 1. 停应用容器（db 保持运行）
docker compose --env-file .env -f docker-compose.yml -f ..\.tmp\execution-system-local-runtime\docker-compose.production.yml stop backend execution-worker frontend

:: 2. 重建空库
docker exec -i textile-monitor-db psql -U <POSTGRES_USER> -d postgres -c "DROP DATABASE IF EXISTS <POSTGRES_DB>;"
docker exec -i textile-monitor-db psql -U <POSTGRES_USER> -d postgres -c "CREATE DATABASE <POSTGRES_DB> OWNER <POSTGRES_USER>;"

:: 3. 恢复备份
cmd /c "docker exec -i textile-monitor-db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -v ON_ERROR_STOP=1 < backup.sql"

:: 4. 拉起全栈并确认启动日志
docker compose --env-file .env -f docker-compose.yml -f ..\.tmp\execution-system-local-runtime\docker-compose.production.yml up -d
docker logs textile-monitor-backend --tail 40
```

日志出现 `Application startup complete` 即完成。若备份来自**较低版本**的
新部署，启动时 Alembic 会自动把结构升级到当前迁移头（日志另有
`Running upgrade ...` 行），同属正常现象。

#### 场景 B：旧部署数据迁入（备份属主 = `admin`，库内无 `alembic_version`）

适用于来自执行系统上线前的旧部署备份。旧库属主是 `admin` 角色，需先建
占位角色再恢复、收尾转回属主；库内没有 `alembic_version`，后端启动时
发现"有表但没有 alembic_version"会自动比对 legacy 基线、盖章并迁移到
最新结构。

```bat
:: 1. 停应用容器（db 保持运行）
docker compose --env-file .env -f docker-compose.yml -f ..\.tmp\execution-system-local-runtime\docker-compose.production.yml stop backend execution-worker frontend

:: 2. 重建空库
docker exec -i textile-monitor-db psql -U <POSTGRES_USER> -d postgres -c "DROP DATABASE IF EXISTS <POSTGRES_DB>;"
docker exec -i textile-monitor-db psql -U <POSTGRES_USER> -d postgres -c "CREATE DATABASE <POSTGRES_DB> OWNER <POSTGRES_USER>;"

:: 3. 先建占位角色再恢复，收尾把属主转回并删除占位角色
docker exec -i textile-monitor-db psql -U <POSTGRES_USER> -d postgres -c "CREATE ROLE admin NOLOGIN;"
cmd /c "docker exec -i textile-monitor-db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -v ON_ERROR_STOP=1 < backup.sql"
docker exec -i textile-monitor-db psql -U <POSTGRES_USER> -d <POSTGRES_DB> -c "REASSIGN OWNED BY admin TO <POSTGRES_USER>;"
docker exec -i textile-monitor-db psql -U <POSTGRES_USER> -d postgres -c "DROP ROLE admin;"

:: 4. 拉起全栈并确认迁移日志
docker compose --env-file .env -f docker-compose.yml -f ..\.tmp\execution-system-local-runtime\docker-compose.production.yml up -d
docker logs textile-monitor-backend --tail 40
```

日志出现 `Running upgrade ... -> 0007_project_rules`（或更高迁移头）与
`Application startup complete` 即接管成功；若报
`does not match the verified legacy baseline`，说明旧库结构与基线有出入，
不要继续，把 drift 信息发给开发侧。恢复后流程目录会重新播种为默认全开，
需重跑 `production-bootstrap.sh` 收敛开放范围；旧库中没有执行系统账号，
需在网页里重建（引导管理员由 .env 自动重建）。

#### 恢复中途报错中止的处理（两个场景通用）

库已处于半恢复状态，后端会因结构校验失败拒绝启动，属预期保护。放弃恢复时
按"停应用容器 → `DROP DATABASE` → `CREATE DATABASE` → `up -d`"重置回空库
即可，后端会在空库上重新完整迁移（即场景 A 的步骤 1/2/4）。场景 B 还需用
`DROP ROLE IF EXISTS admin` 清理可能残留的占位角色。

## 故障排查

### 问题：无法访问系统
1. 检查容器状态：`docker compose ps`
2. 检查端口占用：`netstat -ano | findstr :80`
3. 从终端执行 `curl http://<服务器局域网IP>/health/live`
4. 查看服务器本机防火墙和 `MANAGEMENT_CIDRS`

这些操作只涉及部门服务器和当前终端，不要求公司 IT 修改网络设置。

### 问题：设备状态不更新
1. 检查设备编码是否正确
2. 检查设备能否访问服务器 HTTP 地址
3. 查看后端日志：`docker compose logs backend`
4. 确认上报频率是否正常

### 问题：WebSocket连接失败
1. 确认浏览器使用服务器当前局域网 IP
2. 确认前端与 API 通过同一 HTTP Origin 访问
3. 查看浏览器控制台和前端容器日志

## 系统架构

```
┌─────────────────────────────────────────────────────────┐
│                    局域网PC浏览器                       │
│                  http://<服务器局域网IP>                │
└────────────────────┬────────────────────────────────────┘
                     │ HTTP / WS / SSE :80
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
