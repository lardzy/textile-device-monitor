# P2 归档接替：实现、验收与上线边界

P2 验收记录：2026-09-05。实现基于合并提交 `3b5bf1b`，生产切换尚未执行。2026-09-17 的 [P3 首批](./p3-regenerated-fiber-acceptance.md)新增再生纤两条候选，累计覆盖 6/9；下文保留 P2 当批范围与验证结果。

## 本轮已实现

- 迁移前调用 `runtime_definition()` 排除停放节点和关联边，再生成依赖；`migration.source_digest` 仍取原始定义。原草稿、已发布版本与历史 Run 不改写。关键节点被停放时返回迁移阻塞说明，不再抛出未处理错误。
- 迁移预览可传 `target_slug`，UI 建议 `<原slug>-v2`；重新计算完整性摘要。省略字段保留原 API 行为。目标 slug 与其他流程冲突时拒绝。
- `0010_workflow_replacement` 增加 Workflow 归档时间、唯一的接替来源 Workflow ID、Release 本地迁移来源 JSON。首次发布时创建独立 Workflow 并保持停用；后续版本发布、版本回滚保持启停状态。
- 发布预检冻结来源版本 ID/号、revision、定义与契约摘要；发布重新核查来源。来源记录持久化到 Release，后续 Release 继承原接替关系；本地身份不进入 portable JSON。
- 新旧 Workflow 关系不可更换，每个 Release 的来源版本分别冻结。若旧流程在接替前更新了已发布版本，旧候选拒绝发布/激活；可重新迁移，以未使用的 `release_version` 导入新 Release 并冻结新来源，历史 Release 保持原证据。回切依据最近激活审计中的来源，v2 版本回滚不会阻断恢复旧入口。
- 原子接替与回切复用发布权限、CSRF 和审计。两条 Workflow 按 ID 排序加锁，同时校验双方 revision/版本，操作后递增双方 revision；回切还校验最近激活的审计 ID，防止重复或过期回切。
- 普通目录/推荐隐藏归档、待接替和已回切的新流程；管理页可查看。归档详情链接新入口，禁止编辑、发布、草稿测试与新 Run；初始化目录不会复活归档流程。
- 创建 Run 在读取启用、归档和当前版本前锁定 Workflow。既有 Run、待办和外部操作继续原快照；业务回切不会改写既有 v2 Run。

四条完整候选仍为 **4/9**：`electron-source-selection`、`hemp-cotton-source-selection`、`special-wool-source-selection`、`system-controlled-xlsx-write-test`。前三条为首批生产接替对象，最后一条仅用于内部验收，接替 API 明确拒绝为它激活生产入口。

完整执行测试发现电镜 `file.group` 输出的成员列表未被 `human.select` 输入契约接受。Kernel Pack **2.1.1** 为选择候选增加可选成员列表，保留稳定 ID 和每个成员的索引/文件指纹校验。旧 v1 节点事实、业务定义、外部回执及兼容 NodeSpec 契约摘要不变；P0 当前安装映射中的 21 项 Kernel Pack 版本/实现摘要随新包更新，旧快照仍保留在 Git 历史与冻结镜像中。

浏览器验收还发现人工待办的节点摘要覆盖了 renderer 摘要，导致前端拒绝展示表单。新待办分别保存两种摘要；旧 P2 待办仅在完全匹配其原运行依赖锁中的身份与已知覆盖模式时，投影出正确 renderer 摘要，不改写历史记录，也不接受未知摘要或协议。

通用表单补充对象及对象数组/未指定元素类型数组的 JSON 输入与类型校验，使内部 Excel 验收可从页面提交工作簿引用、写入计划和发布目标；原有标量数组仍使用选择控件。这是运行输入支持，不是完整资源绑定界面或 v2 设计器。

## 管理操作

| 操作 | API | 关键条件 |
|---|---|---|
| 生成候选 | `POST /api/execution/v2/migrations/v1/preview` | `workflow_id`、`source=published`、`target_profile=native_p2`、新 `target_slug`；返回独立 `replacement_source` |
| 内容预检/导入 | `/workflow-releases/preflight`、`/workflow-releases/apply` | 仅传 portable candidate 和一次性预检令牌 |
| 环境绑定 | `PUT /api/execution/v2/workflow-releases/{id}/deployment-binding` | 数据根必须真实可用、访问等级与 revision 匹配 |
| 发布预检 | `POST /api/execution/v2/workflow-releases/{id}/preflight` | 额外传 `replacement_source`；从预览响应原样取值 |
| 发布 | `POST /api/execution/v2/workflow-releases/{id}/publish` | 使用该次发布预检令牌；新接替流程保持停用 |
| 读取接替状态 | `GET /api/execution/v2/workflows/{new_id}/replacement` | 返回双方版本/revision、启停、归档和最近审计记录 |
| 接替 | `POST .../replacement/activate` | 原子归档/停用旧流程并启用新流程；校验新版本的精确 Worker、锁、绑定和资产 |
| 回切 | `POST .../replacement/revert` | 最新激活回执、双方 revision/版本匹配；需要恢复可运行旧入口时校验其资源和在线 Worker |

接替/回切请求包含 `expected_source_revision`、`expected_target_revision`、`expected_source_version`、`expected_target_version`、非空 `reason`。回切额外传 `activation_audit_id`。全部取自刚读取的状态；遇到 409 刷新并重新核对，不自动重放旧切换请求。

UI 可从“v1 候选预览”载入新 slug 的候选，完成绑定和发布，再使用“新旧流程接替”区操作。首次 staged 页面刷新后若未保留本地来源，应重新选择来源流程并预检。后续 Release 自动显示已有来源。普通版本回滚与新旧 Workflow 回切是两个独立操作。

回切恢复的是旧流程**切换前状态**。例如新装目录中电镜原始资料选择可能已被默认目录升级逻辑停用；回切不会擅自把原本停用的入口启用。上线前应记录每条来源流程的真实可用状态。

## 已验证与待验收

当前自动化覆盖：

- 停放的未知节点不参与依赖解析，关联边排除、原始摘要保留、重复预览稳定、无 target slug 的调用兼容。
- 四条候选走真实 HTTP API、数据库和 Worker claim/execute；人工选择/批准、工作簿重读和持久发布回执。
- 三条选择流程的新旧切换、旧待办完成、回切后 v2 待办完成、历史详情、目录过滤、初始化不复活与启停状态不被后续发布更改。
- PostgreSQL 对来源 ID 在目标之前/之后两种锁顺序验证：切换与 Run 创建、重复切换/回切、双方版本发布竞争、回切与新 Run、相同幂等键的并发创建。另保留原有 10 项 PostgreSQL 并发回归。
- 前端候选 slug、来源冻结、归档详情/新入口、接替/回切 payload、冲突后刷新；数据库从旧 schema 升级后历史版本、Run 和锁字节保持不变。
- 冻结 P1 与旧 P2 镜像仅叠加迁移脚本，在 PostgreSQL `0010` 下分别创建精确绑定 Run；重启容器后领取、续租并完成全部 3 个节点。当前 Worker 拒绝领取两种旧绑定，P1 能力摘要与冻结快照一致。

2026-09-05 本地结果：后端 678 项与 115 个子测试通过，PostgreSQL 并发 24 项通过；宿主机缺少 UNO 的 1 项在 Linux/UNO 容器内通过。前端 18 项 Node 测试、132 项组件测试和生产构建通过。三条业务选择流程的 API/Worker 验收使用 `enforced + p2_human`，内部 Excel 使用 `p2_publish`。

浏览器已实际完成四条候选，验证原生选择/批准、页面接替/回切、归档详情、新入口链接与 SSE 状态更新；服务重启后归档状态保持，已等待人工的任务继续完成。Excel 发布回执的 SHA-256 与落盘文件一致，重开目标文件确认 `Sheet1!B2` 为写入值，源工作簿未变。样本、运行 ID、审计记录和回执见本地记录的 `browser-evidence.json`。

本轮本地记录位于 `.tmp/workflow-replacement-20260905/`；最终汇总见该目录 `verification.json`。这些结果不等于生产 CIFS、UNO、Windows Bridge、旧系统写入或全业务输出等价验收。Release 自带 fixtures 的执行器尚未交付，以上使用普通自动化测试与隔离样本。

## 验收环境与生产接替

1. 在独立数据库和临时数据根完成迁移与候选验证，逐级使用 `enforced + p1_readonly / p2_human / p2_local_write / p2_publish`。四条候选的真实前后端/Worker 验收在该环境完成；受控 Excel 使用独立的内部验收入口，不启用其生产接替关系。
2. 盘点实际部署版本及未完成 Run 的精确绑定。无精确绑定的旧 v1 继续走兼容路径；有 P1 精确绑定才配置 [冻结 P1 Worker](./p1-compatibility-worker.md)。如果现场已有 P2 Kernel 2.1.0 精确绑定，也保留对应旧 Worker，直到旧运行完成且回滚需求结束。
3. 所有共用数据库的 Worker 镜像均包含 `0010` 迁移脚本用于 schema-head 检查，冻结镜像只覆盖迁移脚本，不覆盖业务代码或伪造 capability digest。验证实际镜像的启动、续租、历史任务恢复和当前 schema 兼容性。
4. 首批生产配置为 `enforced + p2_human`，只绑定/发布三条业务选择流程。先保持新流程停用，现场确认来源版本、根目录、候选与失败行为，再执行接替并记录审计编号。
5. 需要业务回退时调用 `replacement/revert`，保持 `enforced`，不降低契约模式、不回退数据库、不改写已创建的 v2 Run。

默认配置仍为 `legacy + p1_readonly`；本轮不会自动修改部署配置或替用户执行生产接替。

## 后续交付

P3 再生纤面积法/根数法（`method=area|count`）已在 2026-09-17 首批完成，共享服务、直接 API、原生节点和同批 `.xls/.xlsx` 比较见独立验收记录；下一批为电镜/纸纤维领域拆分。P4 完成通用 Connector 查询/操作、精确调度、更正与未知写入自动核对。P5 完成 NodeSpec 画布，并在引用审计、业务回放和回滚验收后逐项退役旧执行器。

`.twr`、Release 内置 fixture 执行器和完整资源绑定界面单独登记，未包含在本轮完成范围内。
