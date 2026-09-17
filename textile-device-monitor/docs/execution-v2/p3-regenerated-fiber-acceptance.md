# P3 首批：再生纤共享服务与精简流程

日期：2026-09-17。基于 `feature/execution-system` 的 `3b5bf1b` 及工作区中的 P2 接替、首批简化改动。已完成本地实现和隔离验收，尚未部署或接替生产流程。

## 本批交付

面积法、根数法共用查询与结果读取服务，以 `method=area|count` 保留方法差异。旧四个节点、两个新原生节点和直接 API 都调用这两项服务，不增加数据库表、队列或独立服务。

- `regenerated_fiber.find_records@1`：查找匹配工作簿，保留索引、方法规则、候选排序和诊断。
- `regenerated_fiber.read_results@1`：读取字段、含量、检验员、备注和插图；请求内重复引用同一文件时复用解析，仍核查源文件是否变化。
- `human.select@1`：只接收成功读取的候选。唯一有效候选自动完成；多候选沿用结果卡片、图片预览、复选和主单，一次提交。
- Release 管理页新增 `native_p3` 迁移选项及项目规则版本绑定。已有规则名称唯一匹配时自动填入，发布时一并保存绑定和检查。

完整迁移候选从 **4/9 增至 6/9**：原四条 P2 候选，加 `regenerated-fiber-area-method`、`regenerated-fiber-count-method`。原 `native_p2` API 行为保持。新候选仍需发布、接替，生成预览不会修改旧流程或自动切换入口。

## 直接 API

复用现有执行系统登录、`workflow.read` 权限和 CSRF，不增加批准步骤。

| 请求 | 输入 | 输出与副作用 |
| --- | --- | --- |
| `POST /api/execution/v1/regenerated-fiber/query` | `method`、`inspection_number`；可选 `root_id`、`limit`（1–6，默认 6） | `candidates`、数量与诊断；复用现有工作簿特征缓存 |
| `POST /api/execution/v1/regenerated-fiber/read-results` | `method`、查询返回的 `files`（1–6）；可选 `root_id` | 字段、逐文件成功/失败、图片元数据及 SHA-256；不创建 Run，不保存图片制品 |

两项 API 的 `root_id` 默认 `regenerated_fiber_records`。读取请求直接传查询返回的候选；每项身份包括 `id/root_id/relative_path/fingerprint`，无需调用方计算摘要。

```json
{"method":"area","inspection_number":"260144785"}
```

以上请求发往查询接口；把响应的 `candidates` 作为读取请求的 `files`，保留同一 `method/root_id`。调用方已有候选时可直接读取，不必重复查询。工作流执行另行保存图片制品，供现有鉴权图片预览接口使用；直接读取 API 不返回图片二进制或临时下载地址。

部分文件无法读取时，返回成功结果和逐文件错误；全部失败返回 `result_workbooks_unreadable`。修改过的来源不会被旧文件引用静默替换。

## 迁移、发布与配置

预览使用既有 `POST /api/execution/v2/migrations/v1/preview`，传 `target_profile=native_p3` 和建议的新 `target_slug=<旧slug>-v2`。查询、读取、选择按类型和映射识别，支持改过的节点 ID；停放的必需节点或自定义来源映射会报告具体阻塞，原草稿和历史版本保持不变。

绑定包含来源根目录、项目规则版本、结果插图根目录（建议 `execution_staging`）。插图属于本地制品写入，因此使用既有 **`enforced + p2_local_write` 或更高 profile**；无需新增 P3 配置开关。三条 P2 业务选择流程仍可使用 `p2_human`，内部受控 Excel 验收继续使用 `p2_publish`。

本批版本：Engine **2.2.0**、协议 **2.1**、`textile.regenerated-fiber` **1.0.0**、`textile.execution-v1-compat` **2.1.1**。Kernel 保持 **2.1.2**。累计 6 个 Pack、20 份原生契约、59 个能力绑定、57 ready。共享代码变化按新实现摘要发布，不改写冻结 P1 身份；有旧精确绑定时按[兼容 Worker 说明](./p1-compatibility-worker.md)保留对应运行能力。

## 验证与边界

- 同一组合语料覆盖两种方法、`.xls/.xlsx`、单/多候选；通过直接 API、旧节点、原生 Worker 比较候选、字段和图片摘要。
- 验证无额外 Run/制品的直接读取、重复文件解析复用、来源变化、部分失败和全部失败。
- 发布、接替、旧人工任务完成、回切后既有 v2 Run 保持完成；新入口仍出现在推荐中。
- 浏览器完成面积法多候选选择、图片预览和根数法唯一候选自动完成；分别产生 1 个和 0 个人工任务。结果页不再重复展示内部主单 ID。
- Linux/LibreOffice 容器读取同批四份合成工作簿，与改动前读取器比较字段和图片摘要，4/4 一致。
- 后端 693 项和 115 个子测试通过（5 项跳过，1 项依赖宿主机 UNO 的旧面积识别测试未执行）；PostgreSQL 接替与执行并发 24 项通过。
- 前端 18 项 Node 测试、140 项组件测试和生产构建通过；最后的结果展示调整另通过 10 项工作区测试。契约快照、6 个 Pack 摘要和 3 份示例检查通过。

可复核日志、浏览器运行身份、语料和最终汇总保存在 `.tmp/workflow-p3-20260917/verification.json` 及同目录。浏览器验收使用只有执行 API 的隔离服务，设备监控 WebSocket 未接入；现有 Ant Design 开发告警和构建体积提示仍在。

以上是本地合成语料与隔离运行证据，不等于现场历史语料、生产 CIFS/Windows Bridge/检务写入验收，也未证明现场耗时已快于人工。

## 下一批

1. P3 电镜、纸纤维：抽取可直接调用的查询、领域校验和模板渲染服务，旧节点先共用；集中必要输入，复用现有选择界面。
2. P4：通用 Connector 查询/操作 API、精确记录读取与更正、未知结果自动核对/续办，继续复用现有持久队列。
3. P5：NodeSpec 驱动配置/画布和少量常用业务组合；引用审计、业务回放、回切通过后再退役旧执行器。

`.twr`、Release 内置 fixture 执行器和完整资源绑定界面仍单独登记；本批最小规则选择器不代表完整绑定界面交付。
