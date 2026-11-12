# Windows 构建的 Pack 换行修复

2026-11-12，部署基线 `9714960`。

现场 Worker 启动报 `pack distribution digest mismatch: legacy_fibrecheck.v1-compat@2.1.3`。
只读检查结果为 `raw_match=False`、`lf_match=True`、`crlf_files=13`：
该包的 12 个 Python 文件和 1 个 JSON 文件经 Windows 检出转换为 CRLF，
转换回 LF 后恰好等于已封存资源，未发现这 13 个文件有其他内容变化。

## 修复

- `.gitattributes` 为后端 Python 和 JSON 文件固定 LF。
- 后端和 Worker 共用的 Dockerfile 在复制源码后执行 `prepare_execution_packs.py`。
- 构建脚本先核验所有内置 Pack，再决定是否修复；仅当声明文本资源的 CRLF
  归一化后与原摘要完全匹配时才写入镜像。共享资源的修复计划再次联合核验。
- 其他内容差异使构建失败；不修改封存摘要，不改变运行时摘要算法。
- Excel 二进制模板、未声明资源、数据库、运行记录和共享文件均不参与转换。

已有 Windows 工作树不会仅因新增 Git 属性就立即改成 LF，因此构建脚本也覆盖
旧工作树和源码解压场景。修复发生在镜像构建目录中，不就地改写宿主机业务数据。

## 验证

- 9 项独立 unittest 通过；不加载数据库测试夹具，不访问运行库。
- 五个内置 Pack 的原始封存摘要均通过。
- 临时复制完整 `app/`，模拟 122 个 Python/JSON 文件转换为 CRLF；构建脚本
  恢复其中 39 个由 Pack 声明的文本资源，第二次执行改写数为 0。
- 恢复后的完整 Worker 能力文档与原版本逐项一致，仍为 46 个绑定，摘要为
  `6ea80e82a91c6ba64c704c27ece2191e90df2a1f720b2c6b7e77a17b4246dfe4`。
- 二进制模板及契约快照字节不变；语义改动和共享资源冲突均在写入前拒绝。
- 本机 Docker daemon 未运行，未执行实际镜像构建或生产容器切换；上述是
  隔离文件构建准备及注册表验证。现场重建后仍需确认容器健康与桥认证。

复现测试（仓库根目录）：

```bash
.venv/bin/python -m unittest discover -s textile-device-monitor/backend/build_tests -v
```

## 现场应用

先同步本次修复文件，然后在包含 `docker-compose.yml` 的内层项目目录执行。
沿用现场 `.env`、Compose 项目和生产覆盖文件。此次只需更新后端与 Worker：

```powershell
docker compose --env-file .env -f docker-compose.yml -f ../.tmp/execution-system-local-runtime/docker-compose.production.yml build backend execution-worker
docker compose --env-file .env -f docker-compose.yml -f ../.tmp/execution-system-local-runtime/docker-compose.production.yml up -d --no-deps backend execution-worker
docker compose --env-file .env -f docker-compose.yml -f ../.tmp/execution-system-local-runtime/docker-compose.production.yml ps
```

构建日志应出现 `Execution Packs verified; restored LF in ... text resource(s).`。
已为 LF 的源码显示 0 也是正常结果；随后确认 Worker 为 healthy，日志不再出现
Pack 摘要错误，Bridge 的快照领取不再拒绝 `supports_check_records`。
桥最终仍需分别显示认证成功；写入桥此前的 422 未由这次换行问题单独定因。

本次无数据库迁移、无流程重新发布要求，也不需要改动 Windows Bridge 安装包。
