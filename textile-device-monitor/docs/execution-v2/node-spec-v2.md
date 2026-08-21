# NodeSpec v2 评审设计

状态：Draft for Review

规范版本：`schema_version = "2.0"`

适用范围：执行系统节点注册、设计期校验、发布依赖解析、Worker 调度、人工任务、外部副作用以及能力包生命周期

非目标：本文件不修改现有节点实现，也不定义 39 个节点的具体迁移结论；后者见[当前 39 个节点迁移矩阵](./current-node-migration-matrix.md)。Workflow Release 的包结构与节点实例约束见 [Workflow Release v2](./workflow-release-v2.md)。

本文中的“必须”“禁止”“应”“可以”分别表示强制约束、强制否定、推荐约束和可选能力。

## 1. 结论先行

NodeSpec v2 定位为**已安装执行能力的声明式契约**，而不是工作流节点实例，也不是从 Workflow JSON 动态加载代码的入口。

核心决策如下：

1. 节点在工作流中的稳定身份仍为 `type + type_version`；`type_version` 是正整数，仅在发生破坏性契约变化时递增。
2. NodeSpec 总规范固定为 `schema_version = "2.0"`；能力包和 Connector 包使用 SemVer。
3. `execution.kind` 继续使用 `automatic | human | external_side_effect`，但 v2 中它必须成为引擎的唯一分派依据，禁止再维护节点类型白名单。
4. `execution.kind` 与 `side_effect.class` 分离：人工等待不是副作用；自动节点也可能有本地写入或发布副作用。
5. `config_schema`、`input_schema`、`output_schema` 必须成为运行时契约，而不仅是设计器提示；运行前校验输入，成功落库前校验输出。
6. NodeSpec 和 Workflow Release 均禁止声明 Python/JavaScript/.NET import path、命令行或任意 executor 路径。执行器只能由受信能力包在安装阶段注册。
7. 可移植 Workflow Release 声明精确 contract digest 与 pack SemVer 范围；环境导入后生成独立 `dependency_lock`，发布与运行固定 `contract_digest`、pack 版本和 `implementation_digest`。
8. Worker 必须精确上报它能执行的节点版本与实现摘要；调度器只把节点交给匹配的 Worker。API 进程“认识节点元数据”不等于 Worker“具备执行器”。
9. 外部写入继续使用 durable operation、阶段检查点、写入边界、回执与 `reconciliation_required`；通用化不得削弱现有副作用防线。

## 2. 当前实现证据与缺口

当前注册表已经是良好的 v1 起点，但还不足以成为独立分发契约：

| 当前证据 | 现状 | v2 必须解决的问题 |
|---|---|---|
| [`registry.py`](../../backend/app/execution/registry.py) | `NodeType` 已包含 type、整数 version、execution kind、config/input/output schema、test/publish 标志 | 缺少资源、凭据、能力、UI、重试、超时、副作用、生命周期、pack owner 和摘要 |
| 当前 39 个注册类型 | 27 个 `automatic`、5 个 `human`、7 个 `external_side_effect` | `execution_kind` 已存在，但尚未真正控制执行分派 |
| [`engine.py`](../../backend/app/execution/engine.py) | 仍通过 `HUMAN_NODE_TYPES`、`EXTERNAL_NODE_TYPES` 和外部 preparer 字典分派 | 新增人工或外部节点仍需修改引擎 |
| [`worker.py`](../../backend/app/execution/worker.py) | Worker 启动时逐个调用 `register_*_executors()` | 没有包级发现、所有权冲突检查和 executor readiness 证明 |
| [`worker_state.py`](../../backend/app/execution/worker_state.py) | heartbeat 只上报 `dag/file_index/controlled_write/outbox` | 调度不能识别 `type@version`、pack 版本或实现摘要；异构 Worker 不安全 |
| [`validation.py`](../../backend/app/execution/validation.py) | 节点版本精确查询、config schema 和映射引用校验已经存在 | root access、credential system、若干固定节点 ID 与业务连线规则仍硬编码在中央校验器 |
| 当前 schema helper | 大量对象 schema 默认 `additionalProperties: true` | 未声明字段会静默通过，现有业务标志与契约元数据发生漂移 |
| [`complete_node()`](../../backend/app/execution/engine.py) | executor 输出直接写入 `output_data` | `output_schema` 当前未作为成功提交前的运行时闸门 |
| [`ExecutionWorkflowDesigner.jsx`](../../frontend/src/pages/execution/ExecutionWorkflowDesigner.jsx) | 节点库从服务端读取，但参数和映射主要编辑原始 JSON | 缺少 schema 驱动控件、变量选择器、资源槽位选择器和策略边界提示 |
| [`external_operations.py`](../../backend/app/execution/external_operations.py) | 已有 operation key、Bridge claim、stage、write boundary、receipt、reconciliation | operation 类型、阶段和回执校验仍集中在单个模块和 Pydantic Literal 中 |

另外，当前 27 份非空 config schema、27 份非空 input schema 和 27 份非空 output schema 都是开放对象。这并不说明它们都错误，但证明 v2 不能继续把“未知字段默认允许”当成全局策略。

必须保留的现有可靠性资产：运行定义快照、节点 attempt、租约与 heartbeat、人工任务 revision、外部 operation fence、外部 attempt stage、写入边界、回执核验、未知结果进入 `reconciliation_required`，以及长耗时文件/外部 I/O 不占用长数据库事务。

## 3. 概念与边界

### 3.1 NodeSpec

描述一个已安装节点类型的声明式契约，包括身份、输入输出、配置、UI、资源要求、执行种类、副作用与策略边界。NodeSpec 本身不得包含可执行代码。

### 3.2 Node implementation

NodeSpec 对应的实际执行实现。实现只由受信的 kernel 或能力包加载器注册，并绑定到 `(type, type_version, contract_digest)`。

### 3.3 Capability Pack

可独立部署的本地能力包，例如文件解析、Excel/BIFF8、图片处理或领域算法。包使用 SemVer，可以包含 NodeSpec、受信代码、schema 和版本化资产。

### 3.4 Connector Pack / OperationSpec / QuerySpec

声明和实现某个外部系统的一组强类型 operation 与只读 query。OperationSpec 拥有请求、回执、执行阶段、远端写入边界、重试与对账规则；QuerySpec 拥有只读请求、结果、缓存时效与一致性规则。`external.operation` / `connector.query` 节点分别通过 `operation_ref` / `query_ref` 使用它们，不把远端协议细节写进工作流。

### 3.5 Workflow node instance

Workflow Release 中的节点实例，只包含 `id/type/type_version/name/config/input_mapping/runtime_policy/ui`。它不能改写 NodeSpec 的 execution kind、schema、side-effect class 或 executor owner。

### 3.6 Dependency lock

可移植 release 导入到具体环境后产生的依赖解析结果，固定精确 NodeSpec contract、pack 版本、实现摘要、Connector OperationSpec/QuerySpec 和资产摘要。root/credential/role/rule 的实际绑定保存在独立 `deployment_binding` receipt；发布版和 Run 同时固定两者的 revision。这些都属于环境部署记录，不回写进可移植 release。

## 4. 身份、版本与摘要

### 4.1 身份规则

```text
node identity       = type + type_version
runtime binding     = type + type_version + contract_digest
executable binding  = runtime binding + pack_id + pack_version + implementation_digest
```

- `type` 必须是稳定、小写、全局唯一且至少两段的逻辑名称，与 Workflow Release Schema 统一为 `^[a-z][a-z0-9_]*(?:\.[a-z][a-z0-9_]*)+$`。连字符只用于 pack/asset 等其它 ID，不用于 node type。
- 现有 `core.start`、`workbook.copy` 等名称可以继续保留；新第三方/项目能力应使用有所有权边界的命名。
- `type_version` 必须为大于等于 1 的整数。节点实例不得省略；兼容读取 v1 文档时才允许将缺省解释为 1。
- 发布版本和运行快照禁止使用 `latest`、范围或隐式默认版本。

### 4.2 何时递增 `type_version`

以下任一变化都属于破坏性变化，必须递增：

- 新增必填 config/input，删除字段，收窄类型或枚举；
- 改变既有 output 字段的含义、类型或可空性；
- 改变 `execution.kind`、suspend/resume 语义或 side-effect class；
- 提升资源访问权限，例如 `read` 变为 `write/publish`；
- 改变幂等、取消、远端写入边界或未知结果处置；
- 改变同一输入的业务结果，且旧工作流无法无感接受。

下列兼容契约变化可以保留 `type_version`，但必须发布新的 pack 版本和新的 `contract_digest`：

- 增加可选字段且旧行为不变；
- 放宽校验约束且不会改变既有输入的结果；
- 增加新的受限 retry/error 选项，但既有默认策略不变；
- 增加新的可选资源/capability 声明且旧节点实例不使用它。

下列变化不改变 `contract_digest`：

- 名称、说明、翻译、palette 排序和其它纯展示 metadata/UI 调整；这类变化只发布新的 pack 版本，必要时记录 presentation digest；
- 修复实现但不改变任何声明契约；这类变化只发布新的 pack 版本和 `implementation_digest`；
- 文档、示例和测试夹具调整，且它们不参与运行默认值或执行判断。

同一 `(type, type_version)` 可以随兼容 pack 版本出现新的 contract digest，但已经发布的 digest 不得原地覆盖。环境必须在被引用的 WorkflowVersion 和活动 Run 排空前保留旧 binding。

### 4.3 两类摘要

`contract_digest`：对影响执行语义的规范字段做 RFC 8785 风格 canonical JSON 后计算 SHA-256。至少包含：

- type/type_version；
- execution kind 和 owner；
- config/input/output schema；
- dynamic schema bindings；
- control ports；
- requirements；
- side-effect contract；
- retry/timeout/error 边界；
- suspension contract；
- lifecycle 中影响发布的字段。

本地化名称、说明、palette 排序和纯展示 `ui_schema` 不进入 contract digest；如果 UI 字段会生成执行默认值，则默认值必须搬入受摘要保护的 config contract。

`implementation_digest`：对实际可执行制品和与执行有关的包内资产计算的内容摘要。它回答“Worker 实际运行哪份代码/模板”，不能由工作流作者填写。

两类摘要都不是 NodeSpec 作者可以自报后直接信任的普通字段：`contract_digest` 由安装器根据规范字段重新计算并与 pack manifest 核对，`implementation_digest` 由安装器根据包内容计算。它们作为注册表元数据、dependency lock 和运行快照保存，不放入自身参与哈希的 NodeSpec JSON 主体。

所有 `*_digest`、pack distribution digest 和 asset digest 契约字段都使用 **64 位小写十六进制字符串**，不带 `sha256:` 前缀；算法由字段契约固定为 SHA-256。`content_sha256` 同样遵循这一表示，避免 dependency lock 精确比较时出现两种等价格式。

### 4.4 确定性解析算法

导入器必须按以下顺序解析，不能依赖注册顺序或“最后加载者获胜”：

1. 从节点实例读取精确 `type + type_version`，并找到唯一的 `dependencies.node_types` 声明；节点类型不接受范围。
2. 确认提供该类型的唯一 `pack_id`。不同 pack 声明同一节点 identity 时视为所有权冲突，不进入候选集。
3. 按 `dependencies.packs[].version_range` 过滤已安装 pack；选择满足范围的最高稳定 SemVer。若 release 给出 pack `distribution_digest`，则继续做包级精确过滤。
4. 在所选 pack 中取得该 type/version 的 NodeSpec；release 必填的 `contract_digest` 必须精确匹配。若 `dependencies.node_types[]` 另给出 `implementation_digest`，实现也必须精确匹配。
5. 检查 engine version、额外 capabilities、Connector OperationSpec 和资产依赖；任一缺失则返回带路径的未解析项。
6. 所有依赖唯一解析后生成 `dependency_lock`。发布和运行只读 lock，不重新执行“最高版本”选择。

如果最高版本包含损坏或未就绪 executor，不得自动回退到较低版本掩盖部署错误；导入者应明确修复 binding 或收窄 version range 后重新预检。草稿设计器可以展示环境首选版本，但保存到 definition 时仍必须物化精确 `type_version`。

## 5. NodeSpec v2 顶层结构

```json
{
  "schema_version": "2.0",
  "type": "microscopy.original_record.render",
  "type_version": 1,
  "name": "生成微观形貌原始记录",
  "category": "Excel",
  "description": "把受控字段和图片写入版本化模板",
  "execution": {},
  "config_schema": {},
  "input_schema": {},
  "output_schema": {},
  "schema_bindings": {},
  "ui_schema": {},
  "ports": {},
  "requirements": {},
  "side_effect": {},
  "policies": {},
  "suspension": null,
  "lifecycle": {}
}
```

| 字段 | 必填 | 约束 |
|---|---:|---|
| `schema_version` | 是 | v2 固定为字符串 `2.0` |
| `type` | 是 | 稳定节点类型 ID |
| `type_version` | 是 | 正整数，破坏性变化递增 |
| `name/category/description` | 是 | 展示元数据，不参与执行分派 |
| `execution` | 是 | execution kind、实现所有权和返回协议 |
| `config_schema` | 是 | JSON Schema Draft 2020-12；配置对象契约 |
| `input_schema` | 是 | 映射完成后的节点输入契约 |
| `output_schema` | 是 | 节点成功结果契约 |
| `schema_bindings` | 是 | 声明 input/output 是静态 NodeSpec schema，还是由 workflow/config/OperationSpec/QuerySpec 编译生成的有效 schema |
| `ui_schema` | 是 | 设计器声明式渲染元数据，不可包含脚本 |
| `ports` | 是 | 控制流输入/输出 handle、连线基数和条件能力 |
| `requirements` | 是 | root/credential/role/rule/capability/connector 要求 |
| `side_effect` | 是 | 副作用类别、幂等、取消、回执和对账要求 |
| `policies` | 是 | 节点允许的 retry/timeout/error 策略及默认值 |
| `suspension` | 是 | 非暂停节点为 `null`；人工/外部节点声明 suspend/resume 协议 |
| `lifecycle` | 是 | active/deprecated/retired、发布模式、替代和迁移提示 |

未知顶层字段默认禁止。扩展字段必须位于显式的 `extensions` 对象中，并使用拥有者命名空间；扩展不能改变内核语义。

## 6. Execution contract

```json
{
  "execution": {
    "kind": "automatic",
    "owner": {
      "kind": "capability_pack",
      "pack_id": "textile.microscopy-workbook"
    },
    "result_mode": "object"
  }
}
```

### 6.1 `execution.kind`

| kind | 内核动作 | executor | 正常状态路径 |
|---|---|---|---|
| `automatic` | 调度到兼容 Worker 并调用已安装 executor | 必须存在；kernel built-in 也通过受信注册表绑定 | `ready → running → succeeded/failed` |
| `human` | 原子创建/重开人工任务并释放 Worker lease | 禁止节点自带 executor；内核按 suspension contract 处理 | `ready → running → waiting_human → succeeded` |
| `external_side_effect` | 解析 Connector OperationSpec、建立 durable fence、释放 Worker lease | 禁止直接调用远端 executor；远端由 Connector/Bridge claim | `ready → running → waiting_external → succeeded/failed/reconciliation_required` |

v2 引擎必须先读取 NodeSpec，再按 kind 分派。删除中央 `HUMAN_NODE_TYPES`、`EXTERNAL_NODE_TYPES` 和 node type → preparer 字典。类型特有的自动提交、表单归一化或 reopen 行为应拆成声明式规则或独立自动节点，不能继续追加 `if node_type/config_flag`。

约束：

- `human` 必须对应 `suspension.kind = human_task`。
- `external_side_effect` 必须对应 `suspension.kind = external_operation`、`side_effect.class = external_write`，并解析到已安装 OperationSpec。
- 外部只读查询使用 `automatic + read_only`；不得借用 `external_side_effect`。
- `automatic + external_write` 禁止。所有远端写入必须经过 external operation fence。
- `result_mode` v2 固定为 `object`；executor 非对象结果必须显式包装并通过 output schema，不能由引擎静默猜测。

### 6.2 Executor 所有权

`execution.owner.kind` 只允许：

- `kernel`：start/end/branch/join/variables 以及通用 human/external 生命周期；
- `capability_pack`：本地文件、工作簿、图片和领域算法；
- `connector_pack`：用于 Connector 内部的 operation preparer/verifier，以及受信 `automatic + read_only` QuerySpec executor；不允许工作流直接调用远端 writer。

NodeSpec 可以声明 `pack_id`，但不得声明 module、class、function、assembly、binary、shell command 或 URL。受信包加载器在安装阶段将 `(pack_id, type, type_version, contract_digest)` 映射到 callable；公共 `/node-types` API 和 Workflow Release 永远不返回或接收该内部映射。

`external.operation@1` 和 `connector.query@1` 的通用 NodeSpec owner 都是 `kernel`；前者解析 Connector Pack 拥有的 OperationSpec preparer/verifier，后者在同一受信 Worker 中调用 Connector Pack 注册的 QuerySpec executor。dependency lock 同时固定通用 node binding 和 operation/query binding。Connector Pack 也可为特殊只读协议提供专用 `automatic + read_only` NodeSpec，但不得以此绕过 `external.operation` 执行写入。

## 7. Schema 与设计器契约

### 7.1 JSON Schema 规则

- 使用 JSON Schema Draft 2020-12。
- `config_schema`、`input_schema`、`output_schema` 顶层必须为 object。
- `config_schema.required` 是必填配置的唯一事实来源；v2 删除与 schema 重复、可能漂移的 `required_config` 列表。
- object 默认 `additionalProperties: false`。确有动态键需求时，必须在明确的 map 字段上使用带 value schema 的 `additionalProperties`。
- `$ref` 只允许同文档 `#/$defs/...` 或安装时已解析的 `urn:textile:schema:<schema-id>@sha256:<64hex>#/...`；URN 中的 digest 就是 registry schema 身份。运行时禁止网络访问或解析 HTTP(S) `$ref`。
- secrets、绝对路径、物理挂载路径不属于 config schema；只能引用 Workflow Release 中的逻辑槽位。
- JSON Schema 的 `default` 只作为作者提示，不由执行器隐式填充。真正的执行默认值必须在发布编译阶段物化进节点 config，确保 checksum 稳定。

### 7.2 三次强制校验

1. 导入/发布：config 必须通过 `config_schema`；先解析 `schema_bindings`得到该节点实例的 effective input/output schema，再检查 `input_mapping` 的 key、可达性与类型兼容。
2. 调度前：完成映射后，整个 input 实例必须通过已锁定的 effective input schema，随后才能调用 executor、创建人工任务或建立外部 operation。
3. 成功前：automatic 返回值、human resume 结果、external receipt 归一化结果必须通过已锁定的 effective output schema，随后才能将节点置为 `succeeded`。

输出校验失败使用稳定错误码 `node_output_contract_invalid`。它是确定性实现/契约错误，默认不自动重试；不得把不合格输出传播给下游。

### 7.3 Dynamic effective schema

`config_schema` 始终是静态的。input/output 对于大多数节点也是静态的，但 `core.end`、`human.form`、`external.operation` 和 `connector.query` 必须显式声明动态契约来源，不得使用开放 object schema 躲避校验。

静态节点使用：

```json
{
  "schema_bindings": {
    "input": {"source": "node_spec"},
    "output": {"source": "node_spec"}
  }
}
```

动态来源只允许以下白名单：

| 节点 | effective schema 来源 | 编译规则 |
|---|---|---|
| `core.end` | `workflow_definition` | input 绑定 `/definition/output_schema`；按当前 end 映射编译，不允许任意动态 key |
| `human.form` | `node_config` | output 绑定 node config 内受限 `/form_schema`；它必须通过可移植 schema 子集 |
| `external.operation` | `operation_spec` | input/output 分别绑定已锁定 OperationSpec 的 node request/result schema |
| `connector.query` | `query_spec` | input/output 绑定已锁定 QuerySpec，且 QuerySpec 必须为 `read_only` |

`schema_bindings` 本身进入 NodeSpec `contract_digest`。导入器把每个 node id 解析后的 effective schemas 做 canonical JSON + SHA-256，将摘要写入 `dependency_lock.node_instances[]`。动态 schema 的源要么在 release digest 内，要么由必填的 OperationSpec/QuerySpec contract digest 锁定；Run 不得重新读取“最新”schema。解析失败、来源不受信或编译结果不是封闭 object schema 时禁止发布。

### 7.4 UI Schema

`ui_schema` 只允许声明式渲染：

```json
{
  "palette": {
    "group": "Excel",
    "icon": "workbook",
    "tone": "red",
    "order": 40
  },
  "inspector": {
    "field_order": [
      "/staging_root_slot",
      "/record_family",
      "/template_asset"
    ],
    "widgets": {
      "/staging_root_slot": {
        "widget": "root-slot-picker",
        "options": {"required_access": "write"}
      },
      "/record_family": {"widget": "select"},
      "/template_asset": {"widget": "asset-picker"}
    }
  },
  "mapping": {
    "widget": "typed-variable-picker"
  }
}
```

- widget ID 必须来自平台 allowlist；NodeSpec 不得携带 JSX、HTML script 或远程组件 URL。
- 变量选择器由 input/output schema 推导可选路径和类型兼容性。
- root、credential、role、rule、asset 必须使用对应槽位选择器，避免用户手写物理 ID。
- 复杂专用 UI 只有在预先安装了受信前端扩展时才能引用；Workflow JSON 本身不能分发前端代码。
- 服务端始终是最终校验者，UI Schema 不能扩大 JSON Schema 允许范围。

### 7.5 Control ports

数据通过 `input_mapping + input/output schema` 流动；`ports` 只定义 DAG 控制边，不把两者混为一套隐式连线协议：

```json
{
  "ports": {
    "inputs": [
      {"id": "in", "min_edges": 1, "max_edges": null, "default": true}
    ],
    "outputs": [
      {"id": "out", "min_edges": 1, "max_edges": null, "default": true}
    ],
    "condition_mode": "none"
  }
}
```

- port `id` 使用稳定小写标识；`null` max 表示不设 NodeSpec 上限。
- `core.start` 没有 input port，`core.end` 没有 output port；fork/join 用 edge cardinality 明确 fan-out/fan-in。
- `condition_mode` v2 只允许 `none | branch`。只有声明为 `branch` 的节点输出边可以携带 condition，且 default、多命中等规则由该节点的 kernel contract 定义。
- Workflow edge 的 `source_handle/target_handle` 必须引用对应 NodeSpec port。若省略，只能解析到唯一标记为 `default` 的 port；多默认或无默认均为发布错误。
- port 变化可能改变图合法性，因此进入 contract digest。UI 只读取它绘制 handle，不能自行增加运行端口。
- 计划中的 error edge 必须新增独立 `kind = error` port/语义版本；v2 首期不能把普通 output port 临时解释成错误分支。

## 8. Requirements contract

```json
{
  "requirements": {
    "resources": {
      "root_slots": [
        {
          "config_pointer": "/staging_root_slot",
          "access": "write",
          "required": true
        }
      ],
      "credential_slots": [],
      "role_slots": [],
      "rule_slots": []
    },
    "capabilities": [
      {
        "capability_id": "workbook.biff8.render",
        "version_range": ">=1.4.0 <2.0.0"
      }
    ],
    "connectors": [],
    "assets": [
      {
        "config_pointer": "/template_asset",
        "allowed_kinds": ["workbook_template"],
        "allowed_media_types": ["application/vnd.ms-excel"],
        "required": true
      }
    ]
  }
}
```

### 8.1 资源槽位

- `config_pointer` 使用 RFC 6901 JSON Pointer，指向节点 config 中的逻辑槽位名称。
- root access 只允许 `read | write | publish`。NodeSpec 声明节点需要的最小访问级别，Workflow Release 声明所用槽位，环境 deployment binding 再绑定实际 root。
- credential slot 声明所需 Connector 或 credential contract，而不是当前中央校验器中的固定 system key 集合。
- role/rule slot 使 candidate role 和项目规则保持可移植；流程不能把某环境数据库主键写进 JSON。
- 导入编译器必须验证每个 pointer 存在且为字符串、release 已声明对应槽位、环境 binding 权限不低于 NodeSpec 要求。

### 8.2 能力要求

`capability_id` 表示除节点 owner 外仍依赖的稳定能力，例如 BIFF8、UNO 渲染或某种图片编解码器。版本范围使用 SemVer；导入后解析到精确 pack version 和 implementation digest。

### 8.3 Connector 要求

通用 external 节点使用动态 operation 引用：

```json
{
  "connectors": [
    {"operation_ref_pointer": "/operation_ref"}
  ]
}
```

专用兼容节点也可以声明固定引用：

```json
{
  "connectors": [
    {"operation_ref": "legacy_fibrecheck.special_wool.image_upload@1"}
  ]
}
```

通用只读 Connector 节点则声明 QuerySpec 指针：

```json
{
  "connectors": [
    {"query_ref_pointer": "/query_ref"}
  ]
}
```

`operation_ref` 统一写成 `<connector_id>.<operation>@<正整数 contract_version>`，例如 `legacy_fibrecheck.special_wool.image_upload@1`。`query_ref` 形式相同，但只能解析到 release `queries[]` 中的 QuerySpec。名称可以分段；解析器必须用 release dependencies 与已安装 manifest 中的 connector ID 做唯一前缀匹配，不能简单按最后一个点猜 connector。Connector pack 版本是 SemVer；OperationSpec/QuerySpec contract version 在请求、结果/回执或副作用边界发生破坏性变化时递增。

`connector.query@1` 在 v2 首版只支持同步远程读或已持久化缓存读：它是 `automatic + read_only`，由受信 Connector Pack 的 QuerySpec executor 在 Worker timeout/retry 内完成，并用 QuerySpec 动态绑定 input/output schema。它不建立写入 fence，也不能执行远端变更。如果某外部系统的读取必须由 Bridge 异步完成，则需另行设计 `external_query` suspend/resume 状态，属于内核语义升级，不在 v2 首版暗中复用 `waiting_external`。

### 8.4 资产要求

`requirements.assets[]` 是识别节点配置中发布资产引用的唯一事实来源；导入器不得通过字段名或 UI widget 猜测：

```json
{
  "assets": [
    {
      "config_pointer": "/template_asset",
      "allowed_kinds": ["workbook_template"],
      "allowed_media_types": ["application/vnd.ms-excel"],
      "required": true
    }
  ]
}
```

编译器必须确认 pointer 指向 config 中的资产 ID，Workflow Release `assets[]` 存在唯一同 ID 项，kind/media type 在 allowlist 内，且内容 digest 可用。该 requirements 进入 contract digest；资产本身的版本与 digest 进入 release 与 Run 快照。

## 9. Side-effect contract

```json
{
  "side_effect": {
    "class": "reversible_local_write",
    "idempotency": "engine_key",
    "cancellation": "cooperative",
    "receipt": "optional",
    "unknown_outcome": "fail"
  }
}
```

`class` 定义：

| class | 示例 | 必需保护 |
|---|---|---|
| `none` | branch、aggregate、纯变换 | 普通 attempt 即可 |
| `read_only` | 文件索引查询、外部只读快照 | 可按安全策略重试，不得产生业务写入 |
| `reversible_local_write` | 暂存工作副本 | staging 隔离、清理/回滚记录、幂等键 |
| `durable_write` | artifact publish、不可静默撤销的本地/共享盘发布 | fence、唯一业务键、不可中断边界、receipt、未知结果对账 |
| `external_write` | FibreCheck 登记、上传、复核 | durable external operation、Connector stage、write boundary、receipt、reconciliation |

约束：

- `external_write` 的 `unknown_outcome` 必须是 `reconciliation_required`。
- `durable_write` 也必须声明 receipt 和未知结果处置；不能因为目标是共享盘而按普通文件复制重试。
- `none/read_only` 不得申请 publish 权限。
- `human` 默认必须为 `none`。人工提交会产生审计状态，但不应直接执行业务文件或远端写入；需要写入时由后续 automatic/external 节点完成。
- side-effect class 和资源权限必须相互一致；例如 root `publish` 与 `side_effect.class = none` 是发布错误。

## 10. Retry、timeout 与 error policy

NodeSpec 声明“允许范围和默认值”，Workflow 节点实例只能在 `runtime_policy` 中选择允许值，不能扩大上限或降低副作用保护。

```json
{
  "policies": {
    "retry": {
      "mode": "safe",
      "default_max_attempts": 3,
      "max_attempts_limit": 5,
      "backoff": {
        "strategy": "exponential",
        "initial_seconds": 1,
        "max_seconds": 30
      },
      "retryable_error_codes": ["worker_lease_expired", "temporary_io"]
    },
    "timeout": {
      "execution_seconds_default": 300,
      "execution_seconds_max": 1800
    },
    "error": {
      "default_action": "fail_run",
      "allowed_actions": ["fail_run"]
    }
  }
}
```

Workflow Release 的 `runtime_policy.timeout_seconds` 必须位于 NodeSpec default/max 边界内；`runtime_policy.retry.max_attempts/backoff/initial_delay_seconds/max_delay_seconds` 必须是 NodeSpec 允许策略的收紧或选择。未写 override 时使用发布编译阶段物化并进入运行快照的 NodeSpec 默认值，不能在不同 Worker 上临时读取不同默认值。

### 10.1 Retry mode

- `never`：确定性校验、纯人工任务或不允许自动重试的节点。
- `safe`：只有明确未越过副作用边界的错误才自动重试。
- `idempotent`：实现使用稳定 engine operation key，重复执行产生同一结果。
- `fenced`：由 durable fence/receipt 状态机决定是否可重领；外部写入必须使用此模式。

普通 executor 抛出的未知异常不应一律重试。`retryable_error_codes` 必须是有限集合，lease 丢失与业务执行失败分开处理。

对于 `external_side_effect`，NodeSpec 只声明 `fenced`；具体 Bridge attempt 次数、stage 列表、write boundary 和 receipt verifier 归 OperationSpec。写入边界之前可以安全重领，边界之后任何未知失败必须进入 `reconciliation_required`，禁止自动重试。

### 10.2 Timeout

- `automatic` 使用 execution timeout；Worker lease 是存活证明，不等于无限执行授权。
- cooperative heartbeat 可以续租，但不得越过 NodeSpec 的最大执行时限；需要更长时间必须新版本契约或显式审批。
- `human` 等待期限放在 suspension contract，不使用 Worker execution timeout。
- `external_side_effect` 的 preflight/approval/Bridge lease TTL 放在 OperationSpec；普通 Worker lease 在 operation 持久化后立即释放。

### 10.3 Error action

计划支持 `fail_run | route_error | continue_with_output`，但 v2 第一落地阶段建议只启用 `fail_run`：

- `route_error` 必须有显式 error output schema 和专用错误边，不能复用普通条件边；
- `continue_with_output` 仅允许 `none/read_only`，且 fallback output 必须通过 output schema；
- `durable_write/external_write` 禁止用 continue 掩盖未知副作用。

## 11. Suspend / resume contract

### 11.1 Human task

```json
{
  "execution": {"kind": "human", "owner": {"kind": "kernel"}},
  "side_effect": {"class": "none"},
  "suspension": {
    "kind": "human_task",
    "form_schema_pointer": "/form_schema",
    "candidate_role_slot_pointer": "/candidate_role_slot",
    "resume": {
      "mode": "submitted_object",
      "idempotency": "task_revision"
    },
    "expiry": {"default_seconds": null, "action": "remain_waiting"}
  }
}
```

规范要求：

- 创建/重开 task、节点转为 `waiting_human`、attempt 转为 waiting、释放 lease 必须在同一短事务中完成。
- 表单提交先按任务创建时固定的 form schema 校验，再按 NodeSpec output schema 校验。
- 每个 node run 同一时刻只能有一个 open/claimed task；提交使用 revision CAS，重复或迟到提交拒绝。
- `resume.mode` v2 只支持直接返回提交对象或受限声明式 output mapping；禁止在 human 节点上挂任意 normalizer callback。
- 业务归一化、旧系统快照刷新、文件放置等行为应拆成前后自动节点；不再通过 `task_kind` 或布尔 config flag 进入引擎分支。

### 11.2 External operation

```json
{
  "execution": {"kind": "external_side_effect", "owner": {"kind": "kernel"}},
  "side_effect": {
    "class": "external_write",
    "idempotency": "operation_key",
    "cancellation": "before_write_boundary",
    "receipt": "required",
    "unknown_outcome": "reconciliation_required"
  },
  "suspension": {
    "kind": "external_operation",
    "operation_ref_pointer": "/operation_ref",
    "resume": {
      "success_event": "external_operation.completed",
      "failure_event": "external_operation.failed",
      "unknown_event": "external_operation.reconciliation_required"
    }
  }
}
```

规范要求：

- 内核解析已锁定的 OperationSpec，调用受信 preparer 生成规范 request、business key、payload checksum 和 credential binding。
- 创建 operation fence、节点进入 `waiting_external`、attempt 释放普通 Worker lease 必须原子完成；该步骤不得执行远端写入。
- Bridge 只可 claim 自己精确上报支持的 connector operation contract；Literal 列表不再写死在 API schema。
- OperationSpec 定义有序 stage、首次可能写入的 boundary、完成核验 stage、receipt schema 和 verifier。
- complete 只有在完成核验 stage 和 receipt 校验均通过后才可 resume 节点。
- 取消、兄弟失败或 lease 过期时：边界前可证明无写入才能重领/取消；边界后不得释放业务 fence，必须等待回执或人工对账。

## 12. Capability Pack manifest

NodeSpec 不负责定位代码。能力包 manifest 建立受信安装边界：

```json
{
  "manifest_schema_version": "1.0",
  "pack_id": "textile.microscopy-workbook",
  "pack_version": "2.3.1",
  "engine_version_range": ">=2.0.0 <3.0.0",
  "distribution_digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "provides": {
    "capabilities": [
      {"capability_id": "workbook.biff8.render", "version": "1.4.0"}
    ],
    "nodes": [
      {
        "type": "microscopy.original_record.render",
        "type_version": 1,
        "spec_resource": "node-specs/microscopy-original-record-v1.json",
        "contract_digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
        "implementation_digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
      }
    ]
  },
  "assets": [
    {
      "asset_id": "microscopy-original-record-v4",
      "digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ]
}
```

安装规则：

1. 安装器校验 manifest、包摘要、engine compatibility 和所有 NodeSpec。
2. 同一 `(type, type_version)` 全局只能属于一个 `pack_id` owner；同 owner 可以并存兼容的多个 `contract_digest`。其它 pack 声明同一 node identity 时，整个 pack 安装失败。
3. manifest 中的 `spec_resource` 只能引用包内资源；不能是网络 URL。
4. executor 注册通过平台定义的受信 loader 协议完成。内部 executor key 即使存在，也不暴露给 Workflow Release。
5. NodeSpec 声明为 automatic 但安装后没有 callable，或 callable 自检失败时，该节点为 `installed_not_ready`，不能进入 Worker 可执行能力集。
6. 卸载前必须检查 dependency locks、已发布 WorkflowVersion 和活动 Run；仍被引用的旧 pack 版本不能静默删除。

Connector pack 采用相同包级身份与摘要规则，但 `provides` 中主要声明 connector、credential contract、OperationSpec 和 QuerySpec。

## 13. Worker 与 Bridge 能力证明

### 13.1 Worker heartbeat

```json
{
  "worker_id": "execution-worker-01",
  "engine_version": "2.0.0",
  "packs": [
    {
      "pack_id": "textile.microscopy-workbook",
      "pack_version": "2.3.1",
      "distribution_digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ],
  "nodes": [
    {
      "type": "microscopy.original_record.render",
      "type_version": 1,
      "contract_digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "implementation_digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
      "ready": true
    }
  ]
}
```

调度规则：

- NodeRun 必须保存或可通过运行快照得到精确 contract/implementation binding。
- `claim_next_node` 必须以新鲜 heartbeat 能力过滤候选节点；不能先领取后才发现 executor 缺失。
- Worker 必须同时匹配 engine version、type、type_version、contract digest 和 implementation digest。
- 可执行 `connector.query` 的 Worker 还必须在 heartbeat `queries[]` 中上报 `connector_id/query/contract_version/query_digest/implementation_digest/ready`；调度器必须同时匹配通用 node binding 与 QuerySpec binding。
- heartbeat 过期后不再领取新节点；已领取节点仍按 lease/attempt 规则处理。
- 若无兼容 Worker，节点保持未领取并暴露稳定诊断 `node_capability_unavailable`；不得退回同 type 的其它版本或实现。
- 滚动升级期间允许新旧 pack Worker 并存。活动 Run 固定旧 digest；旧 Worker/side-by-side pack 应保留到相关运行排空。

### 13.2 Bridge capability

Bridge 心跳/claim 请求应动态上报：

```json
{
  "bridge_id": "legacy-writer-01",
  "connector_id": "legacy_fibrecheck",
  "pack_version": "1.8.0",
  "implementation_digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef",
  "operations": [
    {
      "operation": "special_wool.image_upload",
      "contract_version": 1,
      "operation_digest": "0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef"
    }
  ]
}
```

服务端只返回完全匹配的 approved operation。账号作用域、全局写入容量、credential revision 和源文件重新核验仍属于 claim 前强制条件。

## 14. 与 Workflow Release v2 的契约

可移植 release 中节点实例保持精简：

```json
{
  "id": "generate-record",
  "type": "microscopy.original_record.render",
  "type_version": 1,
  "name": "生成微观形貌原始记录",
  "config": {
    "staging_root_slot": "execution_staging",
    "record_family": "microscopy",
    "template_asset": "microscopy-original-record-v4"
  },
  "input_mapping": {
    "inspection_number": "$.inputs.inspection_number",
    "selected_images": "$.nodes.select-images.output.selected_images",
    "record_fields": "$.nodes.record-input.output.record_fields",
    "project": "$.nodes.match-project.output.matched_project"
  },
  "runtime_policy": {
    "retry": {"max_attempts": 2, "backoff": "exponential"}
  }
}
```

Workflow Release 的依赖字段约定：

- `dependencies.engine.version_range`
- `dependencies.node_types[] = {type, type_version, contract_digest, implementation_digest?}`
- `dependencies.packs[] = {pack_id, version_range, distribution_digest?, required_on}`
- `dependencies.connectors[] = {connector_id, version_range, distribution_digest?, operations[], queries[]}`
- `resources.root_slots/credential_slots/role_slots/rule_slots`

约束：

1. 每个节点实例必须在 `dependencies.node_types` 中有且只有一个匹配项。
2. release 必须为每个 node type、Connector OperationSpec 和 QuerySpec 给出 `contract_digest`，保证跨环境解析到同一执行契约；目标环境无精确匹配时导入失败，禁止自动替换。
3. pack/Connector `distribution_digest` 与 node `implementation_digest` 是可选的字节级强 pin；无论是否强 pin，导入解析都必须生成 `dependency_lock`，固定精确 pack version、distribution/contract/implementation digest。
4. NodeSpec requirements 的并集必须被 release dependencies/resources 覆盖；未声明依赖是错误，未使用声明是 warning。
5. release 不能覆盖 `execution`、schemas、ports、requirements、side-effect、policy limits、suspension 或 executor owner。
6. deployment binding 中的物理 root、credential ID、role ID、rule revision 不进入 portable release；发布记录和 Run snapshot 固定实际绑定及 revision。
7. 导入 NodeSpec 或能力包不等于导入 Workflow Release。工作流 JSON 永远不能触发代码安装。

## 15. 分阶段校验

| 阶段 | 强制校验 | 产物/失败行为 |
|---|---|---|
| Pack install | manifest/schema/digest、engine range、类型所有权、executor 自检、资产摘要 | 原子安装；任一失败则包不可见 |
| Release import | release schema、NodeSpec/OperationSpec/QuerySpec 解析、effective schema、config/映射、资源/资产/connector/pack 依赖、禁止 secret/绝对路径/任意 executor | 生成 import report；缺依赖保持未发布，不部分激活 |
| Deployment binding | 每个逻辑槽位存在、访问权限、credential/role/rule contract 与 revision | 生成 environment binding receipt |
| Publish | 固定 dependency lock、contract checksum、NodeSpec/asset/operation digest；deprecated/retired 策略 | 原子生成不可变 WorkflowVersion |
| Run creation | input/global schema、published lock 可用、资源 binding 仍有效、NodeSpec/pack 未被撤除 | 固定 definition、dependency、resource 快照 |
| Pre-dispatch | Worker 精确能力、映射结果 input schema、资源权限、policy bounds | 不兼容 Worker 不可 claim；输入错误确定性失败 |
| Completion | output schema、receipt/verification、lease/fence token | 只有全部通过才 `succeeded` |
| Human resume | task revision、表单 schema、output schema、权限 | 重复/迟到提交冲突，不覆盖新 revision |
| External resume | Bridge identity、operation digest、stage、write boundary、receipt schema/verifier | 未知结果保持 fence 并进入 reconciliation |

瞬时健康与已安装依赖应区分：缺少已安装 NodeSpec/pack/OperationSpec/QuerySpec 或 executor 未就绪是发布 blocker；Worker/Bridge 暂时离线是部署健康 warning。运行启动策略可以选择立即拒绝或排队等待，但无论选择哪种都不得把节点交给不兼容执行器。Bridge 离线不应使一个已经正确发布的外部流程失去定义有效性。

## 16. 规范性完整示例：automatic 节点

```json
{
  "schema_version": "2.0",
  "type": "microscopy.original_record.render",
  "type_version": 1,
  "name": "生成微观形貌原始记录",
  "category": "Excel",
  "description": "把任务字段和 1 至 10 张电镜图片写入版本化模板",
  "execution": {
    "kind": "automatic",
    "owner": {
      "kind": "capability_pack",
      "pack_id": "textile.microscopy-workbook"
    },
    "result_mode": "object"
  },
  "config_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
      "staging_root_slot": {
        "type": "string",
        "pattern": "^[a-z][a-z0-9_-]{0,63}$"
      },
      "record_family": {
        "type": "string",
        "enum": ["microscopy", "cross_section"]
      },
      "template_asset": {
        "type": "string",
        "const": "microscopy-original-record-v4"
      }
    },
    "required": ["staging_root_slot", "record_family", "template_asset"],
    "additionalProperties": false
  },
  "input_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
      "inspection_number": {"type": "string", "minLength": 1},
      "selected_images": {
        "type": "array",
        "minItems": 1,
        "maxItems": 10,
        "items": {
          "type": "object",
          "properties": {
            "artifact_id": {"type": "string"},
            "content_sha256": {
              "type": "string",
              "pattern": "^[0-9a-f]{64}$"
            }
          },
          "required": ["artifact_id", "content_sha256"],
          "additionalProperties": false
        }
      },
      "record_fields": {
        "type": "object",
        "properties": {
          "sample_name": {"type": "string", "minLength": 1}
        },
        "required": ["sample_name"],
        "additionalProperties": false
      },
      "project": {
        "type": "object",
        "properties": {
          "project_key": {"type": "string"}
        },
        "required": ["project_key"],
        "additionalProperties": false
      }
    },
    "required": [
      "inspection_number",
      "selected_images",
      "record_fields",
      "project"
    ],
    "additionalProperties": false
  },
  "output_schema": {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {
      "artifact": {
        "type": "object",
        "properties": {
          "artifact_id": {"type": "string"},
          "root_slot": {"type": "string"},
          "relative_path": {"type": "string"},
          "content_sha256": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          }
        },
        "required": [
          "artifact_id",
          "root_slot",
          "relative_path",
          "content_sha256"
        ],
        "additionalProperties": false
      },
      "verification": {
        "type": "object",
        "properties": {
          "verified": {"const": true},
          "artifact_digest": {
            "type": "string",
            "pattern": "^[0-9a-f]{64}$"
          }
        },
        "required": ["verified", "artifact_digest"],
        "additionalProperties": false
      },
      "image_count": {"type": "integer", "minimum": 1, "maximum": 10}
    },
    "required": ["artifact", "verification", "image_count"],
    "additionalProperties": false
  },
  "schema_bindings": {
    "input": {"source": "node_spec"},
    "output": {"source": "node_spec"}
  },
  "ui_schema": {
    "palette": {"group": "Excel", "icon": "workbook", "tone": "red"},
    "inspector": {
      "field_order": [
        "/staging_root_slot",
        "/record_family",
        "/template_asset"
      ],
      "widgets": {
        "/staging_root_slot": {"widget": "root-slot-picker"},
        "/record_family": {"widget": "select"},
        "/template_asset": {"widget": "asset-picker"}
      }
    },
    "mapping": {"widget": "typed-variable-picker"}
  },
  "ports": {
    "inputs": [
      {"id": "in", "min_edges": 1, "max_edges": null, "default": true}
    ],
    "outputs": [
      {"id": "out", "min_edges": 1, "max_edges": null, "default": true}
    ],
    "condition_mode": "none"
  },
  "requirements": {
    "resources": {
      "root_slots": [
        {
          "config_pointer": "/staging_root_slot",
          "access": "write",
          "required": true
        }
      ],
      "credential_slots": [],
      "role_slots": [],
      "rule_slots": []
    },
    "capabilities": [
      {
        "capability_id": "workbook.biff8.render",
        "version_range": ">=1.4.0 <2.0.0"
      }
    ],
    "connectors": [],
    "assets": [
      {
        "config_pointer": "/template_asset",
        "allowed_kinds": ["workbook_template"],
        "allowed_media_types": ["application/vnd.ms-excel"],
        "required": true
      }
    ]
  },
  "side_effect": {
    "class": "reversible_local_write",
    "idempotency": "engine_key",
    "cancellation": "cooperative",
    "receipt": "optional",
    "unknown_outcome": "fail"
  },
  "policies": {
    "retry": {
      "mode": "idempotent",
      "default_max_attempts": 2,
      "max_attempts_limit": 3,
      "backoff": {
        "strategy": "exponential",
        "initial_seconds": 1,
        "max_seconds": 10
      },
      "retryable_error_codes": ["worker_lease_expired", "temporary_io"]
    },
    "timeout": {
      "execution_seconds_default": 300,
      "execution_seconds_max": 900
    },
    "error": {
      "default_action": "fail_run",
      "allowed_actions": ["fail_run"]
    }
  },
  "suspension": null,
  "lifecycle": {
    "state": "active",
    "publish_mode": "allowed",
    "replacement": null,
    "migration_ids": []
  }
}
```

示例中的每个 object 都已封闭。真实领域字段可用同文档 `$defs` 复用，但新增字段仍必须按 4.2 的兼容/破坏性规则更新 contract digest 或 type version，不得恢复为开放 object。

## 17. 生命周期、迁移与废弃

```json
{
  "lifecycle": {
    "state": "deprecated",
    "publish_mode": "allowed_with_warning",
    "deprecated_since_pack": "3.1.0",
    "replacement": {
      "type": "human.form",
      "type_version": 1
    },
    "migration_ids": ["human-input-v1-to-human-form-v1"]
  }
}
```

`state`：

- `active`：允许新建和发布；
- `deprecated`：现有草稿/版本/运行可继续，新建和发布显示警告；
- `retired`：禁止新建和新发布，只允许历史查看和活动 Run 使用被锁定的旧实现。

`publish_mode`：`allowed | allowed_with_warning | test_only | blocked`，替代当前两个布尔标志难以表达的组合。

迁移规则：

1. migration implementation 属于受信 pack，以逻辑 `migration_id` 注册，不能从 Workflow JSON 提供代码路径。
2. 自动迁移只针对 draft/import document，输入必须是精确旧 type/version/contract digest，输出必须通过新 NodeSpec。
3. 迁移必须生成结构化 diff、warning 和新 checksum；发布前由设计者确认。
4. 已发布 WorkflowVersion 和活动 Run 的 definition/dependency/resource snapshot 永不原地修改。
5. 若旧 executor 不再可用，只能让运行等待兼容 Worker、恢复旧 pack，或通过单独审批的运行迁移流程处理；禁止静默改用新 digest。

## 18. 从当前 registry 迁移的建议步骤

本节直接使用[39 节点迁移矩阵](./current-node-migration-matrix.md)的 P0–P5，避免再建一套不同的 Phase 序列。

### P0：契约冻结

- 导出 39 个当前 NodeType 事实快照、9 份内置流程和副作用 golden receipts。
- 生成 contract completeness 报告：未声明 config/input/output、开放对象、硬编码 root/credential/role/asset 与 executor 缺失。

### P1：NodeSpec v2 与精确能力基座

- 建立 NodeSpec v2 loader 和 v1 compatibility adapter，先不改变旧快照行为。
- 先上线 pack/Connector manifest、API metadata registry 与 Worker executable registry 分离、exact heartbeat/claim 和 dependency lock；这是后续通用 external node 的前置条件。
- 完成 effective input/output schema 编译与运行闸门，并把 resource/asset/connector requirements 从中央字典迁入 NodeSpec。

### P2：基础节点收敛

- 使用 `execution.kind` 替换 HUMAN/EXTERNAL 类型集合，但 P2 只启用通用 human suspension；旧 external 类型继续走 compatibility router。
- 以 `human.form/select/approval`、`flow.*`、`data.*` 与受限文件/工作簿原语取代 `task_kind`、固定节点 ID 与隐藏 flag。

### P3：领域能力拆分

- 按迁移矩阵拆分文件查询、模板渲染和领域守卫；通用底座不得替代显微/再生纤维 wrapper。
- 新类型从 `type_version=1` 起步；只有保留原 type 且发生 breaking change 才递增到 2。

### P4：Connector 化

- 在 P1 的 manifest/lock/exact claim 基座已验收后，再启用 `external.operation@1 + OperationSpec`，取代 preparer 字典并保留 fence/stage/receipt/reconciliation 全部语义。
- 只读外部查询限定为同步/缓存 `connector.query@1`；异步 Bridge 查询需单独内核设计。

### P5：设计器与 v1 退场

- Node Inspector 按 config/UI/effective schema 渲染，映射改为强类型变量选择器。
- 只有迁移矩阵中的快照引用、回放、回滚和真实受控环境门槛全部通过，才停止注册 v1 executor。

## 19. 验收标准

- 新增一个使用既有 NodeSpec/OperationSpec 的工作流，只分发 Workflow Release JSON 和必要资产，不改 backend/frontend/Worker。
- 未声明 config 或 input_mapping key 在导入/发布阶段被准确指出。
- executor 返回不符合 output schema 的数据时，节点不会成功，也不会污染下游。
- 新增 human NodeSpec 不需要修改 `engine.py` 类型集合。
- 新增 Connector operation 不需要修改核心 API 的 Literal、stage 表或 preparer 字典。
- 异构 Worker 并存时，节点只由精确匹配 lock digest 的 Worker 领取。
- 人工/外部节点 suspend 时已释放 Worker lease，resume 对重复和迟到结果幂等拒绝。
- 外部写入越过 write boundary 后结果未知，仍然进入 `reconciliation_required`，绝不自动重试。
- deprecated/retired 节点不会破坏既有 WorkflowVersion 和活动 Run。
- 工作流、NodeSpec、pack、Connector 和环境 binding 的 checksum/digest 可在审计记录中串联追踪。

## 20. 待评审问题与建议答案

1. **同一 type_version 的兼容 NodeSpec 更新如何保留？**
   建议以不同 `contract_digest + pack_version` 并存；作者选择当前首选版本，发布和运行锁定精确 digest。

2. **Run 是否锁定 implementation digest？**
   建议锁定。代价是升级期间需 side-by-side Worker 或保留旧 pack，但能避免长时间人工暂停前后运行不同代码。

3. **发布时 Worker/Bridge 必须在线吗？**
   建议“已安装依赖和 binding”是 blocker，“瞬时 heartbeat 离线”是健康 warning；运行启动策略另行配置。任何情况下都不能错误降级到不兼容执行器。

4. **v2 第一阶段是否实现错误分支？**
   建议不实现，只支持 `fail_run`。先完成契约、能力调度和 suspend/resume，再设计独立 error edge 语义。

5. **UI 扩展能否随能力包携带代码？**
   建议默认只允许平台 widget allowlist；确有复杂 UI 时作为受信安装包更新，不允许由 Workflow JSON 动态注入。

6. **能力包是否支持热加载？**
   建议第一阶段采用“安装后滚动重启 Worker”，配合多版本 Worker；不要在同一 Python 进程中卸载/替换已执行模块。

7. **Connector operation 粒度如何定？**
   建议一个 operation 对应一个可独立 fencing、claim、receipt 和 reconciliation 的远端业务事务，不能把整个工作流封装成单个万能 operation。

8. **哪些字段不进入 contract digest？**
   建议仅排除纯展示 metadata/ui；任何默认值、资源要求、策略或行为提示只要影响执行，就必须进入摘要。

9. **“只有新增外部系统才更新”是否绝对成立？**
   不绝对。既有节点组合变化只发 JSON；新外部协议发 Connector Pack；新本地算法、原生库或文件格式发 Capability Pack；只有新增内核状态语义时才更新核心应用。

## 21. 本设计明确不做的事

- 不允许 Workflow Release 下载或执行任意 Python、JavaScript、PowerShell、.NET assembly 或 shell command。
- 不把现有安全 external operation 简化成通用 HTTP POST。
- 不把数据库连接、物理路径、账号或 secret 嵌入 NodeSpec/Workflow JSON。
- 不在导入流程时自动安装未知代码包。
- 不原地修改已发布 WorkflowVersion 或正在运行的 definition/dependency snapshot。
- 不用一个万能“脚本节点”替代强类型业务节点和 Connector operation。
