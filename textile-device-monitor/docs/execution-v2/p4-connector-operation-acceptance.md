# P4 第二批：独立提交与真实样品验证

日期：2026-09-18。基线：`f61327b`。本批把第一种登记操作接入独立 API，复用原队列、Bridge 和 Writer；没有增加批准页面、第二套任务队列或隐藏 Run。P4 尚未全部完成，完整迁移候选仍为 **6/9**。

## 接口与边界

| 接口 | 行为 |
| --- | --- |
| `POST /api/execution/v1/connector-operations` | 提交后直接排队，首次返回 202，相同提交重试返回 200 和原操作 |
| `GET /api/execution/v1/connector-operations/{id}` | 查询状态、机器回执、尝试和错误；按提交者授权，管理员可查看 |
| `POST /api/execution/v1/connector-operations/{id}/cancel` | 未领取时直接取消；已领取时复用 Bridge 的取消协议 |
| 原 `external-operations/{id}/reconciliation` 和 `/reconcile` | 独立操作也可使用原有异常核对入口，不要求 Run 或节点 |

当前可直接调用 **`legacy_fibrecheck.check_record.generic_entry@1`**：范围仍为 GB/T 4688-2020 纸纤维的单行通用登记，**只新增，不更新已有记录**。其它六种操作继续明确标为 `legacy_workflow_only`。能力接口返回本操作的输入 schema，尚未提供通用 `external.operation@1` 节点。

请求顶层包含 `operation_ref`、当前用户的 `credential_id`、`idempotency_key` 和 `input`；可选固定 `connector_version` / `contract_digest`。`input` 必填：

- `inspection_number`、任务快照中的 `project_key`；
- `expected_existing_register_count`，即调用方核对的已有登记数；
- `result_value`，即提交的结果文本。

可选字段为 `unit`（空或 `%`）、`sample_identity`、`judge_basis`、`judgement`、`standard_value`。任务单要求判定时沿用领域必填项。直接调用冻结调用方提供的值，`source_kind=submitted_values`，不伪造工作簿读取或上游复核证据；旧工作流仍核对自己的源文件与复核结果。二者共用 `build_generic_entry_summary` 生成相同 Writer 契约。

沿用 `workflow.run`、会话、CSRF 和个人检务凭据。后端持久排队，不在 HTTP 请求内连接检务写库。Writer 继续核对精确任务项目和登记数量，通过原 DAL 保存并读回。重复键返回原操作；同键改内容返回冲突。同一样品的未完成操作共享原业务锁。写入前中断按原次数上限重试；跨过写入边界后结果未知时保留待核对状态，不自动再次新增。**自动核对未知结果仍待实现。**

## 存储与兼容

新增迁移 `0011_connector_operations`：`run_id` / `node_run_id` 可空，添加 `created_by_id` 并从旧 Run 回填。历史工作流仍按 Run → 节点 → 操作顺序锁定；直接提交按用户幂等范围、样品、凭据处理。领取、阶段上报、完成、失败、取消、租约回收和人工核对均复用原实现，独立操作保留审计和尝试记录。

已有独立操作历史时，降级到 0010 会明确拒绝，避免丢弃历史；业务恢复应使用操作结果核对和流程回切。部署应一起更新 API 与主 Worker，并先应用 0011。冻结 P1 Worker 仅增加迁移覆盖文件，保留其应用、能力与摘要。

Kernel 升为 **2.3.0**，Legacy Connector Pack / Connector 升为 **1.2.0**。Engine / 协议仍为 **2.4.0 / 2.1**，未新增节点类型。

## 两个指定编号的实际验证

本次读取本机运行环境、共享源目录及 Windows Bridge，使用现有兼容工作流；不能据此声称新 v2 完整业务流程已经生产接替。

| 编号 | 结果 |
| --- | --- |
| `260191178` | 任务项目匹配；原有微观形貌登记 1 条、任务份数 4。首次选 4 张图复现缺模板错误；改选同一目录的 3 张导出图后，原始 `.xls` 生成及重读通过，3 张图片布局和打印区域核验通过。下载可用；普通在线预览入口不支持 `.xls`。验证后取消本次预览运行，保留制品，未上传或新增检验登记。 |
| `26W006824` | 实际 `.xls` 的 W32 为“木浆、竹浆”，与原有单条登记及结果投影一致。源文件已通过真实 Bridge 上传并复核，测试上传编号为 **`26W006824-1`**；两份机器回执 `mismatches=[]`，文件摘要一致、图片子记录数为 0。随后结束新增登记分支，保留原来的一条检验登记。此次上传记录保留，不把取消登记分支描述为撤销上传。 |

微观形貌实测问题已修复：由实际消费节点的模板绑定计算可用张数，选图页显示 **1、2、3、5、6、7、10**，不支持的数量在当前待办返回并允许调整，不再先完成选图后让流程失败。仅做资料选择的流程不受登记模板限制，横截面使用自身模板。

## 验证与下一批

本地回归覆盖独立提交、权限、重复内容、取消、Bridge 包校验和成功回执、写入前后租约中断、无 Run 核对；PostgreSQL 覆盖重复提交、同键改内容、跨用户样品竞争和重复领取，并验证 0011 升降级。详细结果在本地 `.tmp/workflow-p4-operations-20260918/verification.json`；真实资料和回执也留在该忽略目录，没有放进契约 fixtures。

下一批先补精确记录读取、字段指纹和官方 DAL 更新，保存前后值并写后读回；再补通用 `external.operation`、自动核对和其余写入适配，完成三条完整业务迁移后计为 9/9。P5 顺序不变，`.twr`、Release fixture 执行器和完整绑定界面继续单列。
