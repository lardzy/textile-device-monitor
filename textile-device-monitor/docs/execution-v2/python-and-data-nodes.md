# 通用数据与 Python 节点约定

`data.transform@1` 一次处理多步 `select / project / map / filter / sort / unique / limit`，避免每个字段一个调度节点。`fields` 中的 `{"path":"#/sample/name","default":"未命名"}` 使用 JSON Pointer，`{"value":...}` 表示字面量。`data.validate@1` 使用内联 JSON Schema，默认失败终止，也可输出 `valid/errors` 供分支使用。字段计算、文本格式化及复杂列表逻辑使用 Python。

`data.python@1` 配置 `runtime_version: 1`、`code`、封闭对象 `input_schema`、`output_schema`。函数唯一入口：

```python
import statistics

def main(inputs):
    return {"mean": statistics.mean(inputs["diameters"])}
```

输入来自普通节点映射；输出必须为 JSON 对象并符合 schema。支持 `json/math/re/datetime/decimal/statistics/collections/itertools`，普通容器、循环、函数、异常和列表推导式。不提供文件、网络、数据库、凭据或应用模块；这些交给文件节点和 Connector。代码随 Release JSON 冻结，无需为字段公式重发应用包。

每次调用独立进程，清空环境、隔离解释器搜索路径；默认 5 秒，可设 1–30 秒；源码 64 KiB，输入/输出各 1 MiB，打印缓冲 16 KiB（不持久记录）。Linux 限制 512 MiB 地址空间及 30 秒 CPU，其他平台仍有父进程超时。它用于受信任流程作者的计算，不声称是隔离恶意代码的操作系统沙箱。不支持第三方依赖、后台任务或持久状态。为可回放性，作者应只根据输入计算，避免读取当前时间。

`workbook.render@1` 通过配置的模板引用、字段映射和图片位置生成独立制品，保存后重读字段。模板引用为 `root_slot + relative_path + sha256`，设计器选择文件时自动填写；发布时绑定目录。支持 XLSX 字段及图片；XLS 复用原有 BIFF 补丁，保留公式及缓存，限单工作表字段写入。复杂 XLS 图片继续使用显微底层渲染节点。不会改写源模板，重试返回已有且内容未变的制品。

显微节点 `@2` 增加完整 `family_profile`，包含项目别名、方法、名称、标题、图片数、模板映射；设计器从旧版本展开默认配置，编辑后的 JSON 自带这些值。纸纤维的工作表、结果单元格和匹配条件随规则 definition 分发。既有领域 `@1` 合同和旧算法继续保留；新字段/任意版式可组合 `data.python / human.form / workbook.render`。检务远端登记协议仍由适配器确认。
