# 执行系统本地容器环境

此目录仅用于当前物理机测试。自 2026-08-07 起，运维所需文件纳入 Git 管理
（见下"Git 管理范围"），数据目录、密钥 env 与一次性产物仍被忽略。

- PostgreSQL 使用本目录下的独立数据目录，不读取或覆盖旧数据库卷。
- 原始记录从 `/Volumes/LARD/1.Software/测试/02-检验` 只读挂载。
- staging、publish、TLS 和 Area 占位目录互相独立。
- Area Infer 不启动。
- 登录账号及临时密码保存在权限为 `0600` 的 `local.env` 中。

启动命令：

```bash
docker compose \
  --env-file ../.tmp/execution-system-local-runtime/local.env \
  -f docker-compose.yml \
  -f docker-compose.execution.yml \
  -f ../.tmp/execution-system-local-runtime/docker-compose.local.yml \
  up -d --no-build postgres backend execution-worker frontend
```

查看状态：

```bash
docker compose \
  --env-file ../.tmp/execution-system-local-runtime/local.env \
  -f docker-compose.yml \
  -f docker-compose.execution.yml \
  -f ../.tmp/execution-system-local-runtime/docker-compose.local.yml \
  ps
```

## 公司局域网真实目录

当 `192.168.105.82` 共享可访问时，在原命令中追加：

```bash
-f ../.tmp/execution-system-local-runtime/docker-compose.company-lan.yml
```

该覆盖会让 backend 和 execution-worker 通过 Docker CIFS 卷只读访问：

```text
/data/execution-source/10特纤/02-检验
```

对应局域网真实路径：

```text
\\192.168.105.82\材料检测中心\10特纤\02-检验
```

## 原始记录字体（宋体）

容器内 LibreOffice 默认没有"宋体"，生成原始记录重存 .xls 时会把模板宋体替换成
DejaVu Sans/Arial（旧系统里看到字体变化）。为此 `fonts/simsun.ttc`（来自已授权
Windows VM 的 SimSun）通过 `docker-compose.local.yml` 挂载到 backend 与
execution-worker 的 `/usr/share/fonts/truetype/simsun`（只读）。fontconfig 会惰性
扫描，无需 fc-cache。重建容器后若原始记录字体再次变化，先检查该挂载是否生效：

```bash
docker exec textile-monitor-backend fc-match '宋体'
# 期望输出: simsun.ttc: "SimSun" "Regular"
```

## 测试数据库隔离（切勿复用运行库）

本环境的运行库是 `textile_execution_local_test`（名字恰好带 `_test` 后缀）。
后端测试套件（`backend/tests/conftest.py`）会在结束时对目标库 `drop_all`。
**绝不要**把 `TEST_DATABASE_URL` 指向运行库；2026-08-07 曾因此清空全部运行
数据（后由迁移+引导自动重建，但运行历史/对账记录丢失）。跑测试一律使用
专用库 `textile_execution_pytest_test`：

```bash
TEST_DATABASE_URL=postgresql://textile_local:<口令>@textile-monitor-db:5432/textile_execution_pytest_test
```

## Git 管理范围

已跟踪（`.gitignore` 显式放行）：

- `*.yml`：本地/公司局域网/SMB 等 compose 覆盖；
- `*.md`、`compose-260111037.sh`、`execution_api_260111037.py`；
- `windows-ops/`：Windows 侧运维脚本——`run-bridge-cdde9ec.ps1`（写入桥）、
  `run-snapshot-bridge.ps1`（快照桥）、`build-/test-/extract-textile-writers-cdde9ec.ps1`
  （Writer 构建与离线自测）、`probe-final-entry-json.ps1`（中文 JSON 解析验证）、
  `probe-image-geometry.ps1` / `probe-counts-geometry.ps1` / `export-counts-pdf.ps1`
  （原始记录排版实测）、`probe-counts-260111037.py` / `probe-counts-paper.py`
  （只读对账探针）。脚本内路径（如 `textile-device-monitor-cdde9ec`）随源码包
  版本变化，换新包时需同步修改。

仍被忽略（不得入库）：

- `local.env`、`real-write-*.env` 等一切凭据 env；
- `postgres-data/`、`execution-runtime/`、`execution-publish/`、`execution-source/`、
  `fonts/`、`tls/`、共享盘挂载点等数据目录；
- `*.tar.gz` 源码包、一次性对账/排版 JSON 与其他临时产物。

Windows 侧编译产物（`tools/*/out/*.exe`）与第三方厂商运行库（FibreCheck
客户端、ODAC 11.2 32 位、ODP.NET）不入库，获取与放置见
`textile-device-monitor/docs/legacy-fibrecheck-bridge-handoff.md`。
