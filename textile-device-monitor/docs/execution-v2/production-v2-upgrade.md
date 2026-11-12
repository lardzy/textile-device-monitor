# 全量 v2 流程同步与旧入口退场

适用日期：2026-11-12。现场仍有 9 条 `draft_v1 / release_id=null` 流程，更新镜像后启动报“流程定义校验失败”。镜像构建及数据库结构升级不会转换数据库中的流程定义。

本次发布两条原生流程：**微观形貌**（含纤维微观形貌、纤维横截面、按客户要求）和**纸浆纤维鉴别**。旧 9 条内置流程归档、停用并退出普通目录；不删除原版本、运行、待办、回执、文件、账号或凭据。再生纤、麻棉、特种毛等旧入口本轮一并退场，不声称它们已迁移到新流程。自建流程不在归档名单中。

## 1. 备份

在生产机打开 PowerShell，进入包含 `docker-compose.yml` 的内层项目目录，沿用原 `.env` 和数据挂载。先备份当前数据库（自定义格式，避免 PowerShell 文本重定向损坏二进制）：

```powershell
$dumpName = "execution-before-v2-$(Get-Date -Format yyyyMMdd-HHmmss).dump"
New-Item -ItemType Directory -Force .\backups | Out-Null
docker exec textile-monitor-db sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc -f "/tmp/$1"' sh $dumpName
if ($LASTEXITCODE -ne 0) { throw '数据库备份失败，请先处理' }
docker exec textile-monitor-db pg_restore --list "/tmp/$dumpName"
if ($LASTEXITCODE -ne 0) { throw '备份目录校验失败' }
docker cp "textile-monitor-db:/tmp/$dumpName" ".\backups\$dumpName"
if ($LASTEXITCODE -ne 0) { throw '备份复制失败' }
```

同时保留原 `.env`。迁移命令不改业务文件，无需复制或删除原共享目录。无需 `down -v`、重建数据库或卸载 Bridge。

## 2. 更新程序

同步包含本次修复的当前分支；不要用示例 `.env` 覆盖现场文件。若现场显式保留了旧契约配置，将这两项改为：

```dotenv
EXECUTION_CONTRACT_MODE=enforced
EXECUTION_V2_ROLLOUT_PROFILE=p2_publish
```

现有 Compose 默认值已经如此，但现场 `.env` 的显式值优先。其余口令、Bridge 令牌、目录保持。

```powershell
git pull --ff-only
if ($LASTEXITCODE -ne 0) { throw '源码同步失败' }
docker compose --env-file .env -f docker-compose.yml -f ../.tmp/execution-system-local-runtime/docker-compose.production.yml up -d --build backend execution-worker frontend
if ($LASTEXITCODE -ne 0) { throw '服务更新失败' }
docker compose --env-file .env -f docker-compose.yml -f ../.tmp/execution-system-local-runtime/docker-compose.production.yml ps
```

命令与此前使用同一组 Compose 配置，限定本次更新的三个服务。后端自动执行正常数据库结构升级；本次未新增数据库迁移。Windows Bridge 继续使用 1.3.0，不需要更换安装包或重新填写凭据。

后端及 Worker 正常启动后，管理员登录执行系统一次，使现有初始化逻辑刷新目录状态并补齐安装模板（已有同名文件不会覆盖）。本次迁移命令本身不创建或改写文件。

## 3. 全量预演与同步（同时适用于旧 v1 和已有 v2）

```powershell
docker exec textile-monitor-backend python -m app.execution.deploy_builtin_workflows --sync-existing
```

默认会完整预演导入、绑定和发布，最后回滚事务；输出 `applied: false` 是正常预演结果。它不会创建 Run、领取检务任务或写入检务系统。预演失败会输出具体问题和所需参数，进程退出码非零。

它自动选择唯一有效管理员及该管理员唯一有效的个人检务凭据；如有多个，按提示指定**网页登录用户名**及凭据 ID（不是密码）：

```powershell
docker exec textile-monitor-backend python -m app.execution.deploy_builtin_workflows --sync-existing --operator admin --credential <凭据ID>
```

这里的 `admin` 和 `<凭据ID>` 需替换为实际值；有且只有一组可选值时无需这些参数。迁移不会读取或打印明文凭据。

默认绑定如下，均复用执行系统的现有存储根：

| 目录槽 | 用途 |
| --- | --- |
| `electron_microscopy_records` | 电镜源图片 |
| `paper_fiber_records` | 纸浆原始 Excel |
| `execution_templates` | 随安装包提供的 XLS 模板 |
| `execution_staging` | 生成 XLS；与 Bridge 执行暂存目录对应同一物理目录 |
| `report_upload_images` | 报告共享图片；默认对应 `\\192.168.105.82\公共交换文件` |

若存储根使用了其他标识，可加 `--root 目录槽=实际root_id`，例如 `--root execution_templates=shared_templates`。绑定值是存储根标识，不是 Windows 路径。缺挂载、模板摘要不一致、未配置凭据、契约模式不符或没有匹配的在线 Worker 都会明确阻止迁移。应修正实际问题后重试，不修改封存摘要或伪造可用状态。

预演成功后，用相同参数加上 `--apply`：

```powershell
docker exec textile-monitor-backend python -m app.execution.deploy_builtin_workflows --sync-existing --apply
if ($LASTEXITCODE -ne 0) { throw '接替未完成；事务已回滚，请查看上方具体错误' }
```

两条新流程发布与旧入口归档在同一事务提交；任一环节失败，全部回滚。旧流程若仍有在途运行、待办或未收尾外部操作，命令列出 ID 并停止；先在网页正常处理，不会代为取消、强制收尾或重新上传。

本次使用 `--sync-existing`，明确以仓库交付的两份完整 JSON 为准：已有较旧 v2 也会发布新版，并启用两条目标流程；同时同步设计草稿和绑定，替换这两条流程尚未发布的编辑，使设计器与执行版本使用同一业务配置。草稿 revision 更新，已经打开的旧编辑页会收到版本冲突提示，应重新载入。现有 v2 Run 继续原快照。相同内容与绑定重复同步不增加发布版本或草稿 revision。若现场已有更高的来源版本，或同一来源版本对应不同内容，会拒绝降级／冲突，不伪造版本号。省略此参数时保留首次接替的保守模式，不覆盖已有不同 v2。日后单纯更新容器仍不会同步 JSON，需明确运行此同步命令或使用发布页。

原 `production-bootstrap.sh` 已改为全量同步命令的包装（带 `--sync-existing`），默认也仅预演；旧的“仅启用 5 个 v1 slug”白名单已移除。

## 4. 确认结果与开始使用

- `applied: true`，两条 `workflows` 的 `is_enabled` 和 `worker_ready` 都是 `true`；`workflows` 列出 `fiber-microscopy-v2` 与 `paper-fiber-v2`，`retired` 列出本机实际存在的旧内置流程。
- 刷新执行系统目录，应看到“微观形貌”和“纸浆纤维鉴别”；使用新入口，旧链接显示归档状态。
- 进入“微观形貌”的设计器 → “业务方案”，应有“纤维横截面 / GB/T 36422-2018”。
- 横截面匹配项目 `5103.426`、方法 `GB/T 36422-2018`，允许 1／2／3 张图片；登记模板分别为 `纤维横截面.xls`、`纤维横截面-2张图.xls`、`纤维横截面-3张图.xls`。
- 原始记录标题为“纤维横截面原始记录”，E2 取任务条目方法。共享位置沿用 `数据分析中心/3-报告上传图片/8-材料检测中心/1-微观形貌-GB T 36422/{编号}`，图片格式与顺序保持。
- 开始执行失败时，页面会保留具体错误列表、节点位置和请求编号。旧定义还会明确提示需要迁移 v2。

本机和生产机以以下**来源 Release 内容摘要**一致为准。本机原有的 Workflow 版本号会继续递增，生产首次导入可能显示 v1；各机 Workflow ID、目录／凭据绑定摘要也会不同，这是正常的环境差异。

| 流程 | 来源版本 | `release_digest` |
| --- | --- | --- |
| 微观形貌（含三个业务方案） | 12 | `ef5802569840220d06e9a27924fbaed82db3d4bae261378508012e5f1c05e950` |
| 纸浆纤维鉴别 | 16 | `11d453cffb4d965d76f213ca0b1b9e28dce8be3c63e62748d2c3c1e045809032` |

发布成功只表示流程、资源与 Worker 匹配，不表示已真实提交检务。第一次使用实际横截面编号时核对选图、生成 XLS、共享图片及回执；本次交付测试不会替现场创建额外登记。

本次验证范围见 [验证记录](./verification/cross-section-upgrade-20261112.md)。
