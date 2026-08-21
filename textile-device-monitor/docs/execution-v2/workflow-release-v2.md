# Workflow Release v2 可评审设计

状态：Draft，供架构评审；尚未实现，不代表当前导入 API 已接受此格式。

配套机器可读约束：[workflow-release-v2.schema.json](./workflow-release-v2.schema.json)。该文件使用 JSON Schema Draft 2020-12。节点契约见 [NodeSpec v2](./node-spec-v2.md)，现有节点迁移结论见[当前 39 个节点迁移矩阵](./current-node-migration-matrix.md)。

## 1. 结论与设计边界

Workflow Release v2 是一个**可移植、不可变、可预检**的工作流发布契约。它描述流程图、节点契约依赖、逻辑资源槽、资产摘要、测试夹具和完整性信息，但不携带运行代码和环境私有值。

对 Dify 的参考取舍是：借鉴“图是可序列化数据、节点元数据驱动设计器、运行能力与流程 DSL 分离”；不照搬其产品 DSL 或把插件代码塞入流程。本项目额外保留受控文件 root、不可变发布快照，以及外部写入 fence/receipt/reconciliation 等领域安全语义。

它解决的是“修改工作流后无需重新构建整个应用”的问题，不试图让 JSON 变成代码容器：

- 节点增删、连线、条件、表单、字段映射和已支持规则的变化，通过 Workflow Release 分发；
- Excel 模板等非代码资源，通过资产注册表或配套 release bundle 分发；
- 新的本地执行能力，通过独立 Capability Pack 分发；
- 新外部系统、协议或远端写入行为，通过 Connector Pack/Bridge 分发；
- 只有新增内核状态、调度或持久化语义时才升级执行系统核心。

当前 `ExecutionWorkflowVersion` 已保存不可变 definition、checksum、capabilities 和 contract checksum，`ExecutionRun` 又固定运行快照，这是 v2 可以渐进落地的基础，见 [models.py](../../backend/app/execution/models.py)。当前导入只校验格式、两个 checksum 和 v1 definition，然后创建或覆盖草稿；当前导出只包含草稿 definition 和 workflow metadata，见 [execution.py](../../backend/app/api/execution.py) 与 [catalog.py](../../backend/app/execution/catalog.py)。

## 2. 目标

1. 同一 release 在不同环境得到相同内容摘要和相同图契约。
2. 导入写库前即可发现缺少的节点、Pack、Connector OperationSpec/QuerySpec、资产、角色、规则和根目录绑定。
3. 工作流只引用逻辑资源槽，部署环境负责绑定本地资源。
4. 节点实例固定正整数 `type_version`；运行时进一步固定 Pack identity、SemVer 和 implementation digest。
5. 发布是原子的，运行中的旧实例继续使用原有快照；任何历史版本都可安全回滚。
6. 保留当前人工暂停、外部操作 fence、回执和 `reconciliation_required` 安全边界。
7. 格式本身可由标准 JSON Schema 工具做结构校验，并由执行系统继续做跨字段和业务语义校验。

## 3. 非目标

- 不在 JSON 中嵌入 Python/C#/PowerShell、shell command、executor 名称或 import path。
- 不在 JSON 中嵌入密码、token、API key、credential value、绝对路径、UNC、SMB 或 `file://` 地址。
- 不允许工作流绕过 `ExecutionExternalOperation` 直接执行不可确认的外部写入。
- v2 首版不引入循环、foreach、子流程或任意代码节点；这些需要独立的内核语义设计。
- 不把草稿画布中的停放节点作为 release 内容。release 只包含可执行 DAG；草稿交换格式可另行定义。
- 不保证仅通过 JSON 就能分发新的算法、原生库、模板二进制或新的外部协议。
- 不在 portable release 中记录目标环境数据库 ID、真实用户 ID或本地绑定结果。

## 4. 规范用语与版本约定

本文中的“必须”“不得”“应当”是规范要求。

| 对象 | 版本形式 | 规则 |
|---|---|---|
| release 格式 | `format_version = "2.0"` | 格式结构出现不兼容变化时递增 |
| 工作流定义 | `definition.schema_version = "2.0"` | 图语义出现不兼容变化时递增 |
| 工作流发布 | `release.release_version` 正整数 | 同一 slug 单调递增，延续当前数据库模型 |
| 节点契约 | `type_version` 正整数 | 只有 breaking contract 变化才递增 |
| Capability/Connector Pack | SemVer | 用 `version_range` 声明要求，导入时解析并生成 lock |
| Connector operation | `contract_version` 正整数 | operation 输入、回执或副作用契约发生 breaking change 时递增 |
| Connector query | `contract_version` 正整数 | 只读请求、结果或缓存语义发生 breaking change 时递增 |

节点实例必须使用精确 `type + type_version`，且每种 NodeSpec、Connector OperationSpec 和 QuerySpec 必须携带 `contract_digest`。Pack 使用 SemVer 范围；若 release 对发布包或节点实现的字节级复现有要求，可分别追加 `distribution_digest` 或 node `implementation_digest` pin。portable release 不携带目标环境解析结果，导入成功后由平台保存独立 `dependency_lock` 和 import receipt。

## 5. 分发物结构

无外部资产时，单个 `workflow-release.json` 即为完整交付物。

包含二进制资产时，推荐使用扩展名为 `.twr` 的 ZIP 容器：

```text
workflow-release.twr
├── workflow-release.json
└── assets/
    └── microscopy-original-record.xlsx
```

约束：

- `workflow-release.json` 必须通过配套 Schema；
- ZIP 中只允许常规文件，拒绝符号链接、绝对路径、反斜杠和 `..` 路径穿越；
- 每个文件必须出现在 `assets[]` 中，且 size、media type 和 SHA-256 一致；
- 未声明文件、摘要不符、重复路径、解压体积或文件数超限均拒绝；
- `source.kind=registry` 表示不随包携带，由目标环境的资产注册表解析；
- `source.kind=bundle` 表示随 `.twr` 携带，`relative_path` 只能是 bundle 内相对路径。

## 6. 顶层字段

| 字段 | 必填 | 语义 |
|---|---:|---|
| `format` | 是 | 固定为 `textile-workflow-release` |
| `format_version` | 是 | 固定为 `2.0` |
| `release` | 是 | 稳定 slug、发布正整数、名称、分类和发布说明 |
| `dependencies` | 是 | 引擎、NodeSpec、Capability Pack 和 Connector OperationSpec/QuerySpec 的要求 |
| `resources` | 是 | 可移植的 root/credential/role/rule 逻辑槽声明 |
| `assets` | 是 | 内容寻址资产清单；没有资产时为空数组 |
| `capabilities` | 是 | 由节点契约推导并核对的权限与副作用摘要 |
| `definition` | 是 | 可执行 DAG，不含停放节点 |
| `fixtures` | 是 | 可选 dry-run/contract 测试夹具；没有时为空数组 |
| `migration` | 否 | 从 v1 等旧格式迁移时保留的来源证据 |
| `integrity` | 是 | canonicalization、内容摘要和可选签名 |

除 NodeSpec 所校验的 `config`、`input_mapping` 以及 JSON Schema 属性名映射外，所有对象均为封闭对象，未知字段会被 `additionalProperties: false` 拒绝。动态映射也使用 `patternProperties + additionalProperties: false`，只能出现受约束的键和值。

## 7. Release metadata

`release.slug` 是跨环境工作流身份，不是数据库 ID。`release_version` 在该 slug 内单调递增。目标环境遇到相同 `slug + release_version` 时：

- 内容摘要相同：作为幂等导入返回既有记录；
- 内容摘要不同：报 `release_version_digest_conflict`，不得覆盖；
- 版本更高：可进入 staged 状态；
- 版本更低：可归档导入，但不得默认提升为当前版本。

`published_at` 和 `publisher` 是来源声明，不替代目标环境审计记录；可信来源由 `integrity.signatures` 和本地信任策略决定。

## 8. Definition 与图语义

### 8.1 输入、全局数据与输出

`input_schema`、`global_schema`、`output_schema` 使用本 Schema 内声明的 Draft 2020-12 可移植子集。它覆盖本项目现有常用关键字，并允许 `x-*` 展示扩展，但不允许任意未知关键字。

`$ref` 只允许包内 `#/$defs/...` 或带 SHA-256 身份的 `urn:textile:schema:...@sha256:<64hex>#/...`。HTTP(S) 和其它远程引用在结构 Schema 阶段即被拒绝，导入或运行时不发起 schema 网络请求。

`global_defaults` 必须是可移植 JSON 值，并在语义预检时通过 `global_schema`。运行创建时仍应分别校验 inputs 和 globals；不能因为 package 通过结构 Schema 就跳过实例校验。

### 8.2 Node

节点实例字段：

| 字段 | 说明 |
|---|---|
| `id` | release 内稳定且唯一；运行、事件、夹具和映射都以此引用 |
| `type` | NodeSpec 注册类型，例如 `human.approval` |
| `type_version` | 精确正整数，必须与 dependencies 和注册表一致 |
| `name` | 本工作流中的展示名称，不参与类型解析 |
| `config` | 由对应 NodeSpec `config_schema` 进一步校验 |
| `input_mapping` | 由对应 NodeSpec `input_schema` 和上游输出契约进一步校验 |
| `runtime_policy` | 可选的超时、重试和错误策略覆盖；只有 NodeSpec 明确允许才有效 |
| `ui` | 画布展示信息，不参与运行语义 |

release 节点不得包含 `disabled`。草稿发布前沿用当前 `runtime_definition()` 的做法移除停放节点和关联边，再生成 release。

`config` 中使用 `root_slot`、`credential_slot`、`role_slot`、`rule_slot` 和 `asset_ref` 等逻辑引用。具体字段由 NodeSpec 定义；Workflow Release 的结构 Schema 不推测节点专用字段，但全局安全扫描仍会检查其中的每一个键和值。

### 8.3 Mapping

v2 首版保留当前映射语法，减少运行时迁移风险：

- `$.inputs.*`
- `$.globals.*`
- `$.run.*`
- `$.nodes.<node-id>.output.*`
- `$.nodes.<node-id>.status`

映射值允许标量、数组和对象递归组合。字符串以 `$.` 开头时被解释为引用，其余字符串为 literal。发布预检必须检查引用存在、上游可达、源输出 schema 与目标输入 schema 兼容。将来若要消除字符串歧义，应在 definition schema 3.0 中引入显式 AST，不在 v2 中混用两种语法。

### 8.4 Edge

v2 保留现有 `join_policy=all|any`，并把语义写死：

- `all`：等待该次运行中所有被选中的入边终结后激活一次；
- `any`：第一条被选中的入边满足后激活一次，后续入边不会让目标节点再次执行，也不自动取消其它分支。

只有解析后的 NodeSpec 将对应输出端口声明为 `ports.condition_mode=branch` 时，出边才可带 `condition`，且该分支端口必须恰好一条 `default`。当前 v1 `branch.condition@1` 在兼容 NodeSpec 中声明这一能力；目标基础节点可重命名为 `flow.branch@1`，release 校验器不得再次按类型名硬编码。条件运算符与当前实现一致。`source_handle/target_handle` 统一使用 snake_case；v1 的 `sourceHandle/targetHandle` 由迁移器转换。

若未来需要“首个成功并取消其它分支”“逐事件执行”或“收集所有当前结果”，必须新增明确语义，不能复用 `any`。

## 9. Dependencies 与运行锁

### 9.1 Node type

`dependencies.node_types[]` 列出图中每个唯一的 `type + type_version`。`contract_digest` 必填，对应 NodeSpec 的规范化契约摘要；可选 `implementation_digest` 用于字节级实现 pin。

预检必须验证：

1. 图中每种节点恰好有一项依赖声明；
2. 声明中不存在图未使用的节点，除非未来格式明确支持 optional dependency；
3. 本地注册表存在完全相同的 type/version；
4. contract digest 必须一致；如声明 implementation digest，实现也必须一致；
5. 节点执行种类、配置、输入、输出、资源和副作用声明都由该 NodeSpec 决定。

### 9.2 Capability Pack

`packs[]` 表示独立安装的执行或 UI 能力包。`required_on` 指明哪些角色必须安装：API 负责校验/展示，Worker 负责执行，Bridge 负责外部适配，Frontend 负责专用编辑器。

`version_range` 允许环境选取兼容的 SemVer；`distribution_digest` 是可选的整包强 pin，与 NodeSpec 的节点级 `implementation_digest` 不混用。导入解析后生成的 dependency lock 至少记录：

- pack id、精确 SemVer、package digest；
- node type/type_version、contract digest、implementation digest；
- 需要该能力的 Worker/Bridge 角色；
- 解析时间和注册表 revision。

调度器必须按 lock 与 Worker 广告的精确 capability 匹配，不能只用当前粗粒度 heartbeat 标志。

### 9.3 Connector

`connectors[]` 声明 connector id、Pack SemVer 范围、可选 pack `distribution_digest`，以及图实际使用的 `operations[]` / `queries[]`。两类契约都必须给出正整数 contract version 和 contract digest，且至少使用一项。operation 输入、预检结果、阶段边界、回执与 reconciliation 规则属于 OperationSpec；只读请求/结果与缓存时效属于 QuerySpec。

`external.operation` 的 `operation_ref` 规范语法固定为 `<connector_id>.<operation>@<正整数 contract_version>`，例如 `legacy_fibrecheck.special_wool.image_upload@1`。解析器先从右侧拆出 `@contract_version`，再用已锁定的 `dependencies.connectors[]` 和 Connector manifest 找到唯一的 `connector_id + "."` 前缀；剩余部分必须精确等于该 dependency `operations[]` 中的 operation。找不到或出现多重匹配都在 preflight 报错，不能靠字符串猜测或回退到最新版本。

`connector.query` 的 `query_ref` 使用同一规范形式，但必须精确匹配 `queries[]`。OperationSpec 与 QuerySpec 不得因名称相同互换；只读查询不获得写入能力，远程写入也不得伪装为 query。

工作流节点只能引用 manifest 已注册的 operation；不得在 config 中写 Bridge 路由、远端命令、数据库语句或执行器名称。`external.operation` 仍必须通过当前 durable operation、claim、stage checkpoint、receipt 与 reconciliation 状态机。

## 10. 资源声明与环境绑定

portable release 只声明槽，不携带绑定值。

| 槽类型 | release 中包含 | 环境绑定中包含 |
|---|---|---|
| root | slot id、访问等级、用途 | 本地 root record id；物理路径只存在服务端配置 |
| credential | slot id、connector、credential kind | 本地 credential record id/secret store reference |
| role | slot id、所需 permissions | 本地 role id/key |
| rule | slot id、rule type、contract version | 本地 rule record id/revision |

推荐导入请求单独提交 deployment binding：`release_digest + environment_id + slot bindings`。该对象由部署 API 的独立 Schema 管理，不属于 release 内容摘要。系统保存 binding revision，并在发布与运行创建时再次校验。

绑定规则：

- `required=true` 的 slot 必须解析。optional slot 只能在当前 node config 和 NodeSpec requirements 都未引用时不绑定；v2 不根据环境 binding 裁剪节点或改写图；
- root 的本地授权必须覆盖 release 声明的 access，不能把 `read` 自动提升为 `write/publish`；
- credential 的 connector id 和 credential kind 必须一致；
- role 必须覆盖 required permissions；
- rule type、contract version 和启用状态必须匹配；
- 节点引用未声明 slot，或声明 slot 从未被任何节点使用，均在预检报告中指出；前者为 error，后者默认 warning。

## 11. 资产

资产使用 `asset_id + version + digest` 形成可复现身份。节点只能通过 `asset_ref` 等 NodeSpec 字段引用 asset id，不能引用真实文件名或绝对路径。

导入器在写库前验证：

1. 所有节点资产引用都存在于 `assets[]`；
2. registry asset 或 bundle 文件的 SHA-256、size 和 media type 一致；
3. NodeSpec 接受对应 asset kind/media type；
4. 同一 asset id 没有歧义；
5. 目标存储完成内容寻址落库后，再创建 staged release；
6. 发布版本引用的资产不可原地覆盖；更新模板必须形成新 asset version/digest 和新 workflow release。

大文件不允许 base64 塞入 JSON。裸 JSON 只能引用 registry 中已有的内容；离线分发使用 `.twr`。

## 12. Capabilities

`capabilities` 是便于权限预审和 UI 展示的**可验证摘要**，不是 release 作者自行授予的权限。导入器根据 NodeSpec、Connector OperationSpec/QuerySpec、root access 和边语义重新计算：

- `declared` 必须与计算结果完全一致；
- `side_effect_level` 取图中最高级别：`none < local_write < publish < external_write`；
- 只要任一节点或 operation 要求人工批准，`requires_human_approval` 必须为 true；
- 声明少于实际能力属于安全错误，声明多于实际能力属于契约漂移错误，两者都不得发布。

NodeSpec 的 `side_effect.class` 映射为 release 摘要时固定为：`none/read_only → none`、`reversible_local_write → local_write`、`durable_write → publish`、`external_write → external_write`。资源 access 或 Connector OperationSpec 推导出更高级别时取更高值，不允许作者手工降级。

迁移器可把当前 `{read, write, ...}` map 转成规范化 capability keys；原 map 保存在迁移 receipt，不继续作为 v2 运行契约。

## 13. 完整性与签名

内容摘要算法固定为：

1. 从文档移除整个顶层 `integrity`；
2. 按 RFC 8785 JSON Canonicalization Scheme 规范化；
3. 对 UTF-8 bytes 计算 SHA-256；
4. 以 64 位小写十六进制写入 `integrity.digest`。

这样避免 digest 自引用。`integrity.signatures[]` 使用 Ed25519 签署 digest；`signed_digest` 必须等于同级 `digest`。结构 Schema 只能检查形状，摘要重算、签名验证和 key trust 必须由导入器完成。

是否强制签名属于环境发布策略：开发环境可接受无签名 package，生产环境可要求至少一个受信 key。签名不替代权限检查、依赖校验和人工发布批准。

portable release digest 不能替代部署后的运行契约摘要。发布时还应保存两个派生值：

- `definition_checksum`：可执行 definition 移除 `ui/viewport` 后的规范化摘要，用于识别运行语义相同、仅画布布局不同的定义；
- `deployed_contract_checksum`：对 `definition_checksum + 重算 capabilities + dependency_lock digest + asset digests + binding identity/revision` 规范化计算，固定到 WorkflowVersion 和 Run。

deployment binding 的真实 secret 和物理路径永远不进入摘要；摘要只使用不泄密的本地 binding record identity/revision。这样既保留当前 definition/contract 两级 checksum 的用途，也不会把环境私有值写回 portable release。

## 14. 导入预检

导入分为只读 preflight 与显式 apply/publish，不能边校验边产生外部副作用。

推荐顺序：

1. **容器检查**：大小、文件数、ZIP 路径和 media type。
2. **结构检查**：使用本文件配套 Draft 2020-12 Schema。
3. **完整性检查**：重算 release/asset digest，按环境策略验签。
4. **可移植性扫描**：递归拒绝 secret keys、凭据值、绝对路径、UNC/SMB/file URI、executor/import/module/shell/command 字段。
5. **身份冲突检查**：slug、release version 和 digest 的幂等/冲突规则。
6. **依赖解析**：engine、NodeSpec、Pack、Connector OperationSpec/QuerySpec；生成候选 dependency lock。
7. **节点契约检查**：用 NodeSpec 校验 config、mapping、runtime policy、资产和资源声明。
8. **DAG 语义检查**：单 start、至少一 end、无环、可达性、branch default、join policy 和条件合法性。
9. **资源绑定检查**：返回未绑定、类型不匹配和权限不足的槽；不把真实值回显到报告。
10. **能力重算**：与 release capabilities 严格比较。
11. **资产暂存**：校验后写入内容寻址 staging，不覆盖已发布资产。
12. **fixture/dry-run**：只使用 mocks 和受控临时根；禁止真实外部写入。

preflight 应返回稳定的机器可读报告：release identity/digest、errors、warnings、resolved dependencies、unresolved slots、asset status、fixture results，以及 opaque `preflight_token`。报告不得返回凭据或物理路径。apply 必须提交该 token，并在事务内确认 registry/binding revision 未变化，否则要求重新预检。

最低错误码集合建议包含：

- `release_schema_invalid`
- `release_digest_mismatch`
- `release_signature_untrusted`
- `release_version_digest_conflict`
- `dependency_missing`
- `dependency_contract_mismatch`
- `node_executable_missing`（未安装或未就绪的 executor，发布 blocker）
- `resource_binding_missing`
- `resource_access_insufficient`
- `asset_missing`
- `asset_digest_mismatch`
- `workflow_semantic_invalid`
- `capability_declaration_mismatch`
- `fixture_failed`

## 15. 发布、运行快照与回滚

### 发布

1. preflight 通过后创建 staged release、asset refs 和 dependency lock；
2. 具有发布权限的用户显式批准；涉及外部写入时仍遵循更高权限/双人批准策略；
3. 单个数据库事务写入不可变 WorkflowVersion、contract checksum、lock 和审计日志，再原子更新 active pointer；
4. 发布后不得修改 definition、依赖 lock、资产摘要或 capabilities；任何变化都产生新的正整数 release version；
5. Run 创建时把 definition、capabilities、dependency lock 和 binding revision 固定为快照。

### 回滚

回滚不是修改旧版本，也不是覆盖同一 `slug + release_version`。推荐把 active pointer 原子指向已通过当前依赖/绑定预检的历史版本，并记录 rollback receipt：操作者、原因、from/to version、两个 digest 和预检报告 id。

已经开始的 Run 不迁移、不重启，继续使用原快照。只有回滚之后创建的 Run 使用目标历史版本。若历史版本依赖已不可用，回滚必须失败，除非先恢复对应 Pack/Connector/资产；不得偷偷改用“看起来兼容”的新实现。

## 16. v1 兼容与迁移

当前 v1 格式为 `textile-execution-workflow/1.0`，导入后覆盖草稿或创建工作流；默认流程仍由 Python builder 生成。v2 采用适配器而非原地解释：

1. 验证 v1 `checksum` 与 `contract_checksum`；
2. 调用当前 `runtime_definition()` 排除 disabled 节点和边；
3. `schema_version: 1.0` 转成 v2 definition，并把 `sourceHandle/targetHandle` 规范化为 snake_case；
4. 保留精确 node `type/type_version`，从 NodeSpec registry 推导 dependencies；
5. 把 v1 `root_slots.root_id` 变为逻辑 `slot_id`，要求操作者选择本地 root binding；v1 文件中的 root id 不直接当物理绑定；
6. 把 `credential_slots.system_key` 映射为 connector/credential kind 槽；
7. 从节点契约重新计算 capabilities；
8. 对当前未完整声明的 config/input 字段先使用 compatibility NodeSpec，不静默丢弃；
9. 在 `migration` 写入 v1 format/version/source digest；
10. 生成 v2 candidate，完整 preflight 通过后才能作为新 release 发布。

迁移不改已有 `ExecutionWorkflowVersion` 和 `ExecutionRun.definition_snapshot`。现有历史 checksum 继续用于审计；v2 的 RFC 8785 digest 是新身份，不与 v1 checksum 假定相等。

过渡期建议支持：

- v1 import：兼容入口，内部转换为 v2 staged draft；
- v1 export：只用于旧端兼容并标记 deprecated；
- v2 export：只允许已发布 immutable release，不能继续把可变草稿伪装成 release；
- 对旧系统默认 builder 先生成 golden v1/v2 对照，确认图、表单、文件结果和外部副作用边界等价。

## 17. 安全与副作用边界

JSON Schema 是第一层，不是完整安全边界。实现还必须：

- 对 config、mapping、fixtures 和嵌入 schema 的 defaults/examples 做递归 portable-value 扫描；
- 不信任 package 声明的 capabilities、asset media type 或 dependency digest；
- 不根据 release 内容动态 import 模块、执行 shell 或访问任意 URL；
- 外部 operation 必须由已安装、已批准的 Connector manifest 注册；
- dry-run 必须 mock 外部写入，不能把测试称作真实旧系统验收；
- 未知外部写入结果保持 `reconciliation_required`，不得因 workflow retry policy 自动重试；
- `runtime_policy.retry` 只对 NodeSpec 声明为 safely retryable 的错误有效；外部 side effect 重试仍由 operation fence 和 receipt verifier 决定；
- 长耗时文件、UNO、网络和 Bridge 工作继续在数据库事务外执行；
- import、bind、stage、publish、rollback 都产生审计记录。

## 18. 完整示例

以下例子展示资产、逻辑绑定、Capability Pack、Connector operation、fixtures 和 integrity。它描述目标 v2 契约，不代表示例中的新节点已经在当前运行时注册。

```json
{
  "format": "textile-workflow-release",
  "format_version": "2.0",
  "release": {
    "slug": "microscopy-original-record-upload",
    "release_version": 3,
    "name": "微观形貌原始记录生成与图片上传",
    "description": "生成受控原始记录并在人工确认后提交旧检验系统。",
    "category_key": "electron_microscopy",
    "release_note": "将模板与旧系统操作改为可解析依赖。",
    "published_at": "2026-08-20T10:30:00+08:00",
    "publisher": "textile-lab",
    "labels": {
      "standard": "GB/T 36422"
    }
  },
  "dependencies": {
    "engine": {
      "version_range": ">=2.0.0 <3.0.0"
    },
    "node_types": [
      {
        "type": "core.start",
        "type_version": 1,
        "contract_digest": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      },
      {
        "type": "project.match",
        "type_version": 1,
        "contract_digest": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      },
      {
        "type": "microscopy.original_record.render",
        "type_version": 1,
        "contract_digest": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      },
      {
        "type": "human.approval",
        "type_version": 1,
        "contract_digest": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd"
      },
      {
        "type": "external.operation",
        "type_version": 1,
        "contract_digest": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee"
      },
      {
        "type": "core.end",
        "type_version": 1,
        "contract_digest": "ffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffffff"
      }
    ],
    "packs": [
      {
        "pack_id": "textile.microscopy-workbook",
        "version_range": ">=2.3.0 <3.0.0",
        "required_on": [
          "api",
          "worker",
          "frontend"
        ]
      },
      {
        "pack_id": "textile.project-rules",
        "version_range": ">=1.0.0 <2.0.0",
        "required_on": [
          "api",
          "worker",
          "frontend"
        ]
      }
    ],
    "connectors": [
      {
        "connector_id": "legacy_fibrecheck",
        "version_range": ">=1.2.0 <2.0.0",
        "operations": [
          {
            "operation": "special_wool.image_upload",
            "contract_version": 1,
            "contract_digest": "9999999999999999999999999999999999999999999999999999999999999999"
          }
        ],
        "queries": []
      }
    ]
  },
  "resources": {
    "root_slots": [
      {
        "slot_id": "staging",
        "name": "受控暂存目录",
        "description": "模板渲染结果写入此逻辑根。",
        "access": "write",
        "required": true
      }
    ],
    "credential_slots": [
      {
        "slot_id": "legacy_account",
        "name": "旧检验系统账号",
        "connector_id": "legacy_fibrecheck",
        "credential_kind": "operator_account",
        "required": true
      }
    ],
    "role_slots": [
      {
        "slot_id": "record_reviewer",
        "name": "原始记录复核人",
        "required_permissions": [
          "human_task.claim",
          "human_task.submit"
        ],
        "required": true
      }
    ],
    "rule_slots": [
      {
        "slot_id": "project_match",
        "name": "项目匹配规则",
        "rule_type": "project_match",
        "contract_version": 1,
        "required": true
      }
    ]
  },
  "assets": [
    {
      "asset_id": "microscopy-original-record-v4",
      "kind": "workbook_template",
      "version": "4.0.0",
      "media_type": "application/vnd.ms-excel",
      "size_bytes": 245760,
      "digest": "1111111111111111111111111111111111111111111111111111111111111111",
      "source": {
        "kind": "registry",
        "registry_key": "templates/microscopy/original-record-v4"
      }
    }
  ],
  "capabilities": {
    "declared": [
      "external.write",
      "file.read",
      "file.write",
      "human.task",
      "rule.read"
    ],
    "side_effect_level": "external_write",
    "requires_human_approval": true
  },
  "definition": {
    "schema_version": "2.0",
    "input_schema": {
      "type": "object",
      "properties": {
        "inspection_number": {
          "type": "string",
          "title": "检验编号",
          "minLength": 1,
          "maxLength": 200
        },
        "selected_images": {
          "type": "array",
          "minItems": 1,
          "maxItems": 10,
          "items": {
            "type": "object",
            "properties": {
              "artifact_id": {
                "type": "string"
              },
              "content_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$"
              }
            },
            "required": [
              "artifact_id",
              "content_sha256"
            ],
            "additionalProperties": false
          }
        },
        "record_fields": {
          "type": "object",
          "properties": {
            "sample_name": {
              "type": "string",
              "minLength": 1
            }
          },
          "required": [
            "sample_name"
          ],
          "additionalProperties": false
        }
      },
      "required": [
        "inspection_number",
        "selected_images",
        "record_fields"
      ],
      "additionalProperties": false
    },
    "global_schema": {
      "type": "object",
      "properties": {},
      "additionalProperties": false
    },
    "output_schema": {
      "type": "object",
      "properties": {
        "upload_receipt": {
          "type": "object",
          "properties": {
            "operation_key": {
              "type": "string",
              "pattern": "^[0-9a-f]{64}$"
            },
            "payload_checksum": {
              "type": "string",
              "pattern": "^[0-9a-f]{64}$"
            },
            "verified": {
              "const": true
            },
            "reconciliation_required": {
              "const": false
            }
          },
          "required": [
            "operation_key",
            "payload_checksum",
            "verified",
            "reconciliation_required"
          ],
          "additionalProperties": false
        }
      },
      "required": [
        "upload_receipt"
      ],
      "additionalProperties": false
    },
    "global_defaults": {},
    "nodes": [
      {
        "id": "start",
        "type": "core.start",
        "type_version": 1,
        "name": "开始",
        "config": {},
        "input_mapping": {},
        "ui": {
          "x": 40,
          "y": 180
        }
      },
      {
        "id": "match-project",
        "type": "project.match",
        "type_version": 1,
        "name": "匹配检测项目",
        "config": {
          "rule_slot": "project_match"
        },
        "input_mapping": {
          "inspection_number": "$.inputs.inspection_number"
        },
        "ui": {
          "x": 280,
          "y": 180
        }
      },
      {
        "id": "render-record",
        "type": "microscopy.original_record.render",
        "type_version": 1,
        "name": "生成原始记录",
        "config": {
          "template_asset": "microscopy-original-record-v4",
          "staging_root_slot": "staging",
          "record_family": "microscopy"
        },
        "input_mapping": {
          "inspection_number": "$.inputs.inspection_number",
          "selected_images": "$.inputs.selected_images",
          "record_fields": "$.inputs.record_fields",
          "project": "$.nodes.match-project.output.matched_project"
        },
        "ui": {
          "x": 520,
          "y": 180
        }
      },
      {
        "id": "confirm-record",
        "type": "human.approval",
        "type_version": 1,
        "name": "确认原始记录",
        "config": {
          "title": "请确认原始记录可以上传",
          "role_slot": "record_reviewer"
        },
        "input_mapping": {
          "subject": "$.nodes.render-record.output.artifact"
        },
        "ui": {
          "x": 760,
          "y": 180
        }
      },
      {
        "id": "upload-record",
        "type": "external.operation",
        "type_version": 1,
        "name": "上传特种毛图片",
        "config": {
          "connector_slot": "legacy_account",
          "operation_ref": "legacy_fibrecheck.special_wool.image_upload@1"
        },
        "input_mapping": {
          "original_record": "$.nodes.render-record.output.artifact",
          "selected_project": "$.nodes.match-project.output.matched_project",
          "approval_receipt": "$.nodes.confirm-record.output"
        },
        "runtime_policy": {
          "timeout_seconds": 1800,
          "retry": {
            "max_attempts": 1,
            "backoff": "none"
          },
          "failure_strategy": "fail_run"
        },
        "ui": {
          "x": 1000,
          "y": 180
        }
      },
      {
        "id": "end",
        "type": "core.end",
        "type_version": 1,
        "name": "结束",
        "config": {},
        "input_mapping": {
          "upload_receipt": "$.nodes.upload-record.output.receipt"
        },
        "ui": {
          "x": 1240,
          "y": 180
        }
      }
    ],
    "edges": [
      {
        "id": "e1",
        "source": "start",
        "target": "match-project",
        "join_policy": "all"
      },
      {
        "id": "e2",
        "source": "match-project",
        "target": "render-record",
        "join_policy": "all"
      },
      {
        "id": "e3",
        "source": "render-record",
        "target": "confirm-record",
        "join_policy": "all"
      },
      {
        "id": "e4",
        "source": "confirm-record",
        "target": "upload-record",
        "join_policy": "all"
      },
      {
        "id": "e5",
        "source": "upload-record",
        "target": "end",
        "join_policy": "all"
      }
    ],
    "viewport": {
      "x": 0,
      "y": 0,
      "zoom": 1
    }
  },
  "fixtures": [
    {
      "fixture_id": "happy_path",
      "name": "正常生成并返回受控回执",
      "inputs": {
        "inspection_number": "2026-A-0001",
        "selected_images": [
          {
            "artifact_id": "fixture-image-1",
            "content_sha256": "2222222222222222222222222222222222222222222222222222222222222222"
          }
        ],
        "record_fields": {
          "sample_name": "纤维样品 A"
        }
      },
      "globals": {},
      "mocks": [
        {
          "node_id": "match-project",
          "outcome": "succeeded",
          "output": {
            "matched_project": {
              "project_key": "microscopy"
            }
          }
        },
        {
          "node_id": "render-record",
          "outcome": "succeeded",
          "output": {
            "artifact": {
              "artifact_id": "fixture-original-record-1",
              "root_slot": "staging",
              "relative_path": "2026-A-0001/original-record.xls",
              "content_sha256": "3333333333333333333333333333333333333333333333333333333333333333"
            },
            "verification": {
              "verified": true,
              "artifact_digest": "3333333333333333333333333333333333333333333333333333333333333333"
            },
            "image_count": 1
          }
        },
        {
          "node_id": "confirm-record",
          "outcome": "succeeded",
          "output": {
            "decision": "approved",
            "subject_digest": "3333333333333333333333333333333333333333333333333333333333333333",
            "actor_id": "fixture-reviewer",
            "decided_at": "2026-08-20T10:35:00+08:00"
          }
        },
        {
          "node_id": "upload-record",
          "outcome": "succeeded",
          "output": {
            "receipt": {
              "operation_key": "7777777777777777777777777777777777777777777777777777777777777777",
              "payload_checksum": "8888888888888888888888888888888888888888888888888888888888888888",
              "verified": true,
              "reconciliation_required": false
            }
          }
        }
      ],
      "assertions": [
        {
          "path": "$.run.status",
          "operator": "eq",
          "value": "completed"
        },
        {
          "path": "$.nodes.upload-record.output.receipt.verified",
          "operator": "eq",
          "value": true
        }
      ]
    }
  ],
  "integrity": {
    "algorithm": "sha256",
    "canonicalization": "RFC8785",
    "scope": "document_without_integrity",
    "digest": "4e73e1d9f5db8e9c05a09aa1001b25145827bc3eee4b2adc499d7232f2f6983d",
    "signatures": []
  }
}
```

示例中的模板、NodeSpec 和 OperationSpec contract digest 是说明性 pin；实际导出器必须写入安装器重算并登记的摘要。`integrity.digest` 在本文最终自检时按“移除 integrity 后、排序紧凑 UTF-8 JSON”的等价数据计算。生产实现必须使用正式 RFC 8785 库，不能把普通 `sort_keys` 当作所有数值和 Unicode 情况下的完整替代。

## 19. 验收条件

1. Schema 自身可通过 `Draft202012Validator.check_schema()`。
2. 上述完整示例可通过结构 Schema，增加未知顶层/节点/edge 字段会失败。
3. 在 config 中加入 `password`、`executor` 或绝对/UNC 路径会失败或被语义扫描拒绝。
4. 未安装节点/Pack/Connector、operation 版本不符或 executor 未就绪时，preflight 在写库前失败；只是暂无新鲜兼容 Worker/Bridge heartbeat 时记录 health warning，运行节点保持未领取并报 `node_capability_unavailable`。
5. 缺少 root/credential/role/rule binding 时不泄露环境候选值，只返回 slot 级问题。
6. 相同 slug/version/digest 可幂等导入；相同 slug/version 但不同 digest 永远拒绝。
7. 发布后 definition、lock、asset 和 binding revision 均固定到 Run 快照。
8. 回滚不改变旧 release，不迁移已开始的 Run。
9. fixture 执行不触发 CIFS、UNO、真实数据库或旧系统写入。
10. v1 的 9 个当前内置工作流都能生成通过 preflight 的 v2 release，并有行为等价证据。

## 20. 待评审问题

1. 是否接受 `release_version` 以来源工作流为准，允许目标环境出现版本号间隙；还是需要同时保留 source version 与 local version？
2. 生产环境是否从 v2 首发起就强制 Ed25519 签名，还是先允许可信内网人工导入？
3. Pack/Connector 的 `distribution_digest` 和 node `implementation_digest` 应默认强 pin，还是仅对受控写入节点强制？`contract_digest` 在本设计中已必填，不在此选项内。
4. dependency lock 是仅保存在数据库，还是在导出已发布 release 时作为独立、带环境标识的 companion 文件提供？本设计不把它放入 portable release。
5. root slot 的 access 是否继续只分 `read/write/publish`，还是需要补充 `delete`、`move` 等更细权限？
6. rollback 应直接移动 active pointer，还是始终生成一个内容相同、版本号递增的新 release？本文建议移动 pointer 并写不可变 rollback receipt。
7. 是否接受 v2 首版继续沿用字符串 mapping；若要立即引入显式 expression AST，会扩大迁移和前端改造范围。
8. fixtures 是否作为所有 external-write release 的强制发布门槛？建议强制至少一个正常夹具和一个 reconciliation/failure 夹具。
9. bundle 单资产上限目前 Schema 为 1 GiB；环境总包上限、压缩率上限和文件数上限应取何值？
10. role/rule 的本地绑定是否允许“按 selector 自动选择”，还是所有生产绑定都要求人工确认？
