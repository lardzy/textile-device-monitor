# Connector 独立更新与接口调用

Engine 2.5 增加启动时的 Python entry point：`textile.execution.adapters`。适配包工厂返回 `AdapterPackage(api_version=1)`，声明自己的 Pack manifest、资源目录、查询、操作准备、源数据核验、Bridge 载荷、回执核验及对账处理函数。源代码和契约摘要随 wheel 固定；缺失生命周期处理器的包不能加载。

业务 API 与工作流共用 `connector.query` / `external.operation` 的同一契约。当前检务 9 个操作均支持 `POST /api/execution/v1/connector-operations`。上传/复核/登记类请求在顶层提供 `inspection_number`，`input` 使用能力目录中的操作 schema。文件与前序操作通过明确标识传入；直接调用不创建 Workflow、Run 或 NodeRun。文件制品限定为调用者和当前样品，前序操作校验所有者、样品及已完成回执；已有 Run 内的来源约束保持原样。

新适配器提供接收 `db, input_data` 的查询或操作准备函数。操作准备只产生数据，实际写入仍由适配器的 Bridge 完成。主系统继续负责幂等、领取租约、阶段、写入结果不明时的停放、回执及 Run 恢复；无需为一个系统增加新的中心分派表。不同 Connector 可使用独立凭据 `system_key=connector_id`，旧检务继续使用 `legacy_inspection`。外部操作冻结适配包实现摘要，升级不会把等待中的操作静默交给另一实现。

参考包在 `adapters/example/`，仅用于离线示例与集成测试，没有真实系统写入。其 `lab.example` 名称也验证了带点号的 Connector ID。构建与安装方式：

```sh
# 在项目根目录，使用已有 uv Python 环境执行封存检查。
uv run python tools/execution_adapter_package.py adapters/example/textile_example_adapter/manifest.json
uv build adapters/example --wheel --out-dir .tmp/adapter-wheels
uv pip install --target runtime/execution-runtime/adapters .tmp/adapter-wheels/textile_example_adapter-1.0.0-py3-none-any.whl
```

更新包源码/版本后，先检查并用 `--update` 重算 manifest，再构建。实际部署使用对应运行平台的 Python 环境安装依赖。Docker 的 API 与 Worker 共用 `/data/execution-runtime/adapters`；本机设置 `EXECUTION_ADAPTER_PATH`。完成安装后一起重启 API/Worker。Windows Bridge 的协议实现由相应适配器单独部署，不随工作流 JSON 分发。这里没有自动下载、在线安装市场或热替换。

安装更新可保留旧版本服务运行至旧操作完成，或先排空操作再更新；如果精确实现不可用，待处理操作保持待处理并返回版本不匹配。普通 JSON 分发与运行时精确冻结是两个不同层次。

验证：真实构建并在隔离目录安装 wheel，独立进程发现包并完成查询；API 提交 → 持久队列 → Bridge 领取 → 分阶段回执已使用离线适配器通过。纸纤维独立上传/复核/登记共用现有服务通过。未安装到生产目录，也未访问真实检务写入。
