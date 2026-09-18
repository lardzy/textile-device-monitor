# P4 首批：通用 Connector 查询

日期：2026-09-17。基线：`164abec`。本批完成查询接口、能力发现、QuerySpec 冻结和 `connector.query@1` 执行，继续复用已有只读 Bridge 与任务缓存。独立写入、更正和自动核对留在下一批；P4 尚未全部完成。

后续进度（2026-09-18）：首个独立登记 API 与操作生命周期已交付，见[第二批验收](./p4-connector-operation-acceptance.md)。下文保留首批交付时的版本与验证事实。

## 已实现

| 入口 | 行为 |
| --- | --- |
| `GET /api/execution/v1/connectors/legacy_fibrecheck/capabilities` | 返回查询/操作契约及可用性；首个查询已可直接调用，七种旧写入明确标记 `direct_api_available=false`、`legacy_workflow_only` |
| `POST /api/execution/v1/connector-queries` | 按注册的 `query_ref` 校验输入并调用共享查询服务；直接调用不创建 Workflow、Run 或人工待办 |
| `connector.query@1` | config 只需 `query_ref`；输入/输出 schema 来自已锁定的 QuerySpec，立即返回查询结果与缓存状态 |

首个查询是 **`legacy_fibrecheck.task_snapshot.get@1`**，读取现有检务任务快照，包括项目身份、项目名称/方法、登记数量、样品信息和编号族占用事实。它不读取某条登记记录的完整字段，也不把登记数量当作“某次写入成功”的证据。

沿用 execution 的 `workflow.read` 权限、会话和 POST CSRF 约定，不新增权限模型、数据库迁移、审批步骤或队列。查询不调用任何写入 Writer，也不获得外部写入能力。现有原生领域节点和旧查询入口继续使用同一份缓存。

## 直接调用

```http
POST /api/execution/v1/connector-queries
Content-Type: application/json
X-CSRF-Token: <当前会话 CSRF token>

{
  "query_ref": "legacy_fibrecheck.task_snapshot.get@1",
  "input": {"inspection_number": "26W006701"}
}
```

已有有效快照时直接返回 `200`。缓存缺失或过期时，复用现有持久刷新请求，返回 `202` 和 `refresh_request`，包含检验编号及现有状态接口地址。调用方可查询状态，随后重复上述请求取得结果。多个调用方重复请求不会重复排队，也不会撤销正在执行的 Bridge claim。

结果放在 `result` 中，包含 `inspection_number`、`cache_state`、`refresh_status`、`snapshot`、`revision`、`fetched_at`、`expires_at` 和 `error_code`。`snapshot.projects=[]` 是有效的空项目结果，与 `snapshot=null` 的缓存缺失不同。`stale`/`stale_error` 表示过期缓存；`failed` 表示刷新失败，不能作为最新远端事实。失败请求由原缓存重试间隔控制，返回错误状态不会忙等或无限新增请求。

需要主动更新时，在 `input` 增加 `"refresh": true`；它跳过有效缓存的刷新间隔，但仍合并已有请求。排队期间可能同时返回尚未过期的旧快照，应同时读取 `refresh_status`。普通调用不必强制刷新。`revision` 是本地缓存修订，**不能作为远端登记记录的更正版本号**。

客户端可选传 `connector_version`（版本范围，默认已安装的最高稳定版本）及 `contract_digest`（能力接口返回的摘要）固定契约。未知查询、把 operation 当 query、错误摘要、额外 SQL/URL 等输入会在调用前拒绝。完整 `query_ref` 使用 `连接器.查询名@契约版本`，不能省略连接器前缀。

## 工作流与调度

[可导入示例](./examples/v2-connector-query-smoke.json)：开始 → 通用任务快照查询 → 返回缓存状态及快照。无需目录、规则、凭据或角色资源槽；使用空部署绑定即可发布，最低配置为 `enforced + p1_readonly`。

节点的查询依赖需显式列在 `dependencies.connectors[].queries`；预检解析并冻结具体 Connector 版本、分发摘要、QuerySpec、查询实现摘要及有效输入/输出 schema。Worker 的执行绑定同时覆盖通用节点和具体查询实现，仅有通用壳或旧查询实现的 Worker 无法领取任务。整个过程复用既有 Worker 能力表和领取机制。

工作流节点始终同步返回当前缓存状态：它可以触发现有后台刷新，但不会挂起为新的异步查询状态，不占住 Worker 等待 Windows。消费方要根据 `cache_state` 判断是否可继续业务；本示例仅展示查询结果，未把“查询节点执行完成”解释成“远端刷新已完成”。

## 版本与本地验证

Engine **2.4.0**、协议 **2.1**；Kernel **2.2.0**；Legacy Connector Pack **1.1.0**、Connector **1.1.0**。仍为 **7 个 Pack**，合计 **25 份原生契约、64 个 Worker 精确绑定、62 ready**。更新当前 Pack 摘要、P0 外部操作元数据摘要和六份示例；冻结 P1 Worker 快照不变。已有精确绑定的历史运行仍需要对应版本 Worker，不将其解释为新实现。

- 新增 **23 项测试**，覆盖直接 API/只读 Bridge claim 和回执、缓存状态、重复刷新、契约拒绝、权限边界、发布/Run/Worker 全链，以及通用壳不能领取具体查询。
- 后端 **751 项测试、117 个子测试通过**；6 项跳过，1 项依赖宿主机 UNO 的旧面积识别测试未执行。
- 前端 **18 项 Node 测试、142 项组件测试通过**，生产构建通过；保留既有依赖弃用和构建块体积提示。
- Pack 摘要、当前契约快照、六份示例及冻结 P1 检查通过。记录位于 `.tmp/workflow-p4-queries-20260917/verification.json`。

本批没有改变数据库结构、外部写入状态机或只读缓存的领取逻辑，未重复 PostgreSQL 并发验收；Bridge 回执使用本地测试数据。以上不等同于现场 Windows/检务系统或生产验收，未部署或推送。

## 下一批

1. 抽出操作所有者、来源、幂等和事件处理，提供独立的 `POST /connector-operations`、结果读取及 `external.operation@1`，继续复用现有 durable operation/Bridge 队列，一次提交直接交付。
2. 先完成通用登记的一种精确 `record_ref` 读取和字段指纹，再接入官方 DAL/BLL 更新、写后读回及前后值记录。已有 Writer 目前只实现新增，不能凭新增接口签名猜测更新语义。
3. 用精确身份及预期字段自动核对未知结果：已写入则完成，证明未写入才重试，部分成功只补缺失步骤；无法确认或发生真实并发编辑时才交给人处理。
4. 扩展其余操作和精确 Bridge 能力，收敛电镜/纸纤维业务输入，完成剩余三条业务流程后再把完整迁移候选从 **6/9** 计为 **9/9**。随后推进 P5；`.twr`、Release fixture 执行器和完整绑定界面继续单列。
