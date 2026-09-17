# P3 第二批：电镜、纸纤维共享服务与直接接口

日期：2026-09-17。基线：`cca2c4d`（P2 接替、第一批简化、P3 再生纤首批）。本批抽取服务并提供直接 API，旧工作流调用同一实现；不新增表、队列或批准步骤。

## 本批交付

- 电镜：索引图片查询、任务快照组合、候选领域校验分开。共享微观形貌和横截面两个 `record_family`，保留项目规则和已安装模板支持的图片数量。
- 纸纤维：本地文件查询/字段读取、任务快照组合、候选领域校验分开。Worker 等待任务快照时只轮询快照，复用本次文件查询结果；规则版本改变则重新查询。不增加跨请求缓存。
- 原始记录：字段构造和工作簿渲染从旧执行器抽出。API 可直接下载含图片的 `.xls`，旧节点继续注册制品并复用同请求产物。
- 登记记录：API 和旧节点共用模板渲染。移除复制后的重复模板摘要检查，保留源模板身份及最终单元格/BIFF 格式重读。
- 修复 `.xls` 图片边缘舍入误拦截：真实 LibreOffice 保存后，相邻图片出现 0.03 mm 的坐标舍入。重叠判定允许最多 0.1 mm 的边缘误差，仍拒绝真实重叠、比例失真和越界；不改变布局或工作簿内容。

下载接口在调用 LibreOffice 前释放数据库事务，生成文件只放在临时目录，成功或失败均清理；不创建 Run、Artifact、人工任务或检务操作。原始图片仍以查询返回的文件身份定位，文件改变时提示重新选择。

## 直接 API

沿用执行系统登录、`workflow.read` 权限及 `X-CSRF-Token`。四个请求均为 POST：

| 路径（统一前缀 `/api/execution/v1`） | 输入 | 返回 |
| --- | --- | --- |
| `/microscopy/query` | `inspection_number`；可选 `record_family=microscopy\|cross_section`、`match_rule` | 图片、目录、任务缓存状态、规则版本、`record_choices`、`supported_image_counts` |
| `/paper-fiber/query` | `inspection_number`；可选 `limit`（1–6，默认 6）、`match_rule` | 候选、W32/M32、100% 判断、项目身份和任务缓存状态 |
| `/microscopy/render/original-record` | 通用记录字段、`sample_name`、`images` | 原始记录 `.xls` 下载，包含所选图片 |
| `/microscopy/render/check-record` | 通用记录字段、`image_count` | 检验登记 `.xls` 下载，包含最终值及旧采集器隐藏字段 |

通用记录字段：`inspection_number`、`record_family`（默认 `microscopy`）、必填布尔值 `judgement_required`；可选 `sample_identification`、`judgement_basis`、`indicator_requirement`、`test_result`、`judgement`、`remark`。需要判定时，判定依据、指标要求、测试结果和判定必须填写；无需判定时相关单元格置空。

调用示例：先向 `/microscopy/query` 提交：

```json
{"inspection_number":"26W006701","record_family":"microscopy"}
```

把响应中选定的 `images` 直接传给 `/microscopy/render/original-record`，并填写 `sample_name`、`judgement_required` 和必要记录字段即可下载；不必启动工作流或重复读取任务单。每个图片引用包含查询返回的 `id/root_id/relative_path/fingerprint`，调用方无需自行计算摘要。登记文件使用同一组记录字段及选图数量调用 `/microscopy/render/check-record`。

查询仅使用索引/本地文件及持久化任务快照。快照未就绪时，复用现有刷新请求并立即返回 `task_cache_state=pending`，调用方稍后重查；API 不等待 Windows Bridge。渲染使用调用方提供的已确认字段，可独立调用；下载不会自动写入检务系统。

模板数量来自现有安装资产：微观形貌支持 1、2、3、5、6、7、10 张；横截面支持 1、2、3 张。原始记录生成服务仍需要现有 LibreOffice/UNO 运行环境；直接查询、纸纤维读取和登记文件渲染不要求启动 LibreOffice。

## 版本与验证

本批只将 `textile.execution-v1-compat` 升至 **2.1.2** 并刷新资源摘要。Engine **2.2.0**、协议 **2.1**、Kernel **2.1.2**、再生纤 Pack **1.0.0** 保持。累计 **20 份原生契约、59 个能力绑定、57 ready、6/9 条完整迁移候选**；本批没有把 API 包装成新的原生节点。

验证记录保存在 `.tmp/workflow-p3-domains-20260917/verification.json`：

- 后端 **715 项测试、117 个子测试通过**；6 项跳过，1 项依赖宿主机 UNO 的旧面积识别测试未执行。本批 21 个 API/领域用例覆盖等待、规则修订、下载、释放数据库事务、失败清理及无额外 Run/制品。
- 隔离 Linux/LibreOffice 容器 **2 项 UNO 集成测试通过**，覆盖 1、2、5、10 张非方形图片，以及微观形貌 5 张、横截面 3 张相邻图片的舍入回归。
- 与 `cca2c4d` 的旧实现比较：**10 组登记模板逐字节一致**；**5 组原始记录**的字段、图片摘要、坐标和打印信息一致；**4 组纸纤维 `.xls/.xlsx` 输入**的查询与节点输出一致。原始记录有两组在旧版因边缘舍入被误拦截，新版通过，生成内容不变。
- 6 个 Pack 摘要、契约快照、3 份示例及 `git diff --check` 通过；冻结 P1 快照不变。

本地验证不等于现场历史语料、生产共享目录、Windows Bridge 或检务系统验收。没有部署或测量人工与系统的现场端到端耗时；本批无前端、数据库迁移或调度状态机改动。

## 下一步

1. P3 收尾：给已抽出的电镜/纸纤维服务补齐原生 NodeSpec、资源绑定和迁移适配；业务表单集中必要输入，复用现有选择器。涉及外部写入的完整业务候选随 P4 验收，不把当前 6/9 提前记为 9/9。
2. P4：共用现有队列提供 Connector 查询/操作 API，从一种记录的精确读取、更正及未知结果核对开始，再覆盖其余写入；不增加逐次批准。
3. P5：NodeSpec 配置/画布与少量常用业务组合；完成引用审计和业务回放后逐项移除旧执行器。

`.twr`、Release 内置 fixture 执行器和完整绑定界面继续单独登记。
