# 内部 CA 与本机 hosts 运维手册

## 适用边界

本文档描述的是**可选 HTTPS 模式**。当前开发和生产试运行默认通过
`http://<服务器局域网IP>` 访问，不需要执行本手册，也不需要公司 IT 修改
DNS 或其它网络配置。只有在正式上线阶段决定启用
`docker-compose.https.yml` 时，才使用下面的内部 CA 方案。

可选 HTTPS 入口固定为：

```text
https://textile-monitor.internal
```

该名称只在本部门终端的本机 `hosts` 中解析，不申请公司 DNS 记录，也不改动
公司路由器、交换机、DHCP、域策略或网络防火墙。部门需要具备的权限仅限于：

- 读取并使用 Docker 服务器当前的 RFC1918 局域网 IPv4 地址；
- 在部门服务器上管理本项目文件、Docker 和本机防火墙；
- 在本部门浏览器电脑与设备电脑上以管理员身份安装根证书、维护本机
  `hosts`。

项目不要求申请固定 IP、DHCP 保留，也不要求修改服务器网卡配置。已有稳定
地址可以继续使用；如果服务器地址改变，只需在本部门终端重新运行信任安装
脚本更新本机 `hosts`，不需要联系 IT 修改 DNS 或 DHCP。

## 文件和密钥边界

离线 PKI 使用三层关系：

- 根 CA：RSA 3072，有效期 10 年；私钥仅保存在加密离线介质；
- 中间 CA：RSA 3072，有效期 5 年；私钥仅保存在受控管理员电脑；
- 服务器证书：RSA 3072，有效期 365 天；SAN 只能包含
  `textile-monitor.internal`。

仓库内只保存生成、校验和部署脚本，不保存真实证书或私钥。Docker 主机只
保存以下部署文件：

```text
fullchain.pem
privkey.pem
root-ca.pem
root-ca.cer
```

其中只有 `privkey.pem` 是服务器私钥；Docker 主机不得出现根 CA 或中间 CA
私钥。CA 工作目录和生产 TLS 目录必须位于 Git 工作区之外。

## 1. 初始化离线 CA

在受控管理员电脑上安装 OpenSSL，并以 PowerShell 执行：

```powershell
.\scripts\pki\Initialize-InternalPki.ps1 `
  -PkiDirectory "E:\TextileMonitor-Offline-PKI"
```

脚本会要求分别设置根 CA 和中间 CA 私钥口令，并输出根证书 SHA-256 指纹。
通过纸质记录或其它离线方式保存该指纹。完成后立即把根 CA 私钥移入加密离线
介质；日常签发只在受控管理员电脑使用中间 CA。

脚本会拒绝把 PKI 输出写入任何 Git 工作区。

## 2. 签发服务器证书

```powershell
.\scripts\pki\Issue-ServerCertificate.ps1 `
  -PkiDirectory "E:\TextileMonitor-Offline-PKI" `
  -OutputDirectory "E:\TextileMonitor-Issued\2026-01"
```

输出目录不含 CA 私钥。签发脚本会校验 SAN、用途、证书链、有效期、RSA
位数、证书与私钥匹配关系以及私钥权限。将该输出目录通过受控介质复制到
Docker 服务器。

## 3. 配置可选 HTTPS 环境

在宿主机为部署预检准备独立 Python 环境；离线生产环境应提前下载并核对所需
wheel，不要在每次启动时访问公网：

```powershell
python -m venv C:\TextileMonitor\deployment-venv
C:\TextileMonitor\deployment-venv\Scripts\python.exe -m pip install `
  -r .\scripts\requirements-deployment.txt
```

复制 `.env.example` 为 `.env`，至少设置：

```dotenv
PUBLIC_HOSTNAME=textile-monitor.internal
PUBLIC_ORIGIN=https://textile-monitor.internal
WEB_TRANSPORT=https
FRONTEND_BIND_ADDRESS=192.168.106.50
FRONTEND_HTTPS_PORT=443
TLS_DIR_HOST_PATH=C:/TextileMonitor/tls-active
TLS_MIN_VALID_DAYS=30
HSTS_MAX_AGE=300
MANAGEMENT_CIDRS=192.168.106.21/32,192.168.106.22/32
```

`MANAGEMENT_CIDRS` 应填写实际允许访问的地址。自主管理部门没有专属子网时，
不要直接把示例 `192.168.0.0/16` 当作部门边界；它可能覆盖公司内大量无关
电脑。优先把获准的 20 多台浏览器和设备电脑逐台写成 `/32`（例如
`192.168.106.21/32,192.168.106.22/32`），地址因 DHCP 改变时同步维护。
浏览器终端和会上报状态的设备电脑都必须列入；上面的两个地址只是格式示例，
不能原样用于生产。
该变量只是本项目 Nginx 的本地访问限制，不会改动公司网络。

Docker Desktop 可能让 Nginx 只看到 Docker 网关地址。正式启用白名单前，
先从灰度电脑发起访问并查看 Nginx `remote_addr`，确认是否保留了真实终端
地址；只有证据确认后才能据此收紧。不得为了通过门禁而使用 `0.0.0.0/0`，
也不能把“安装了根证书和 hosts”误当作访问授权。

生产入口固定使用 443。`FRONTEND_HTTPS_PORT` 的非 443 值仅供本机开发或
临时验收，生产预检会拒绝。服务器上的 443 入站白名单也只操作这台 Windows
电脑的本机防火墙；不涉及公司出口、防火墙设备或其它网络基础设施。

`FRONTEND_BIND_ADDRESS` 填服务器当前局域网 IPv4，只让 Docker 在这块地址
上发布 443；这不是修改网卡或申请固定 IP。若服务器地址变化，应同时更新
该变量、重建 `frontend`，并在终端重跑 `hosts` 脚本。生产预检会拒绝
`0.0.0.0`、回环和非 RFC1918 地址，避免无意暴露到其它 VPN 或虚拟网卡。

`DOCKER_SUBNET` 和 `FRONTEND_PROXY_IP` 只用于本机 Docker 内部网络。若默认
`172.30.0.0/24` 与服务器现有 VPN 或 Docker 网络冲突，可以在 `.env` 中改成
其它未占用私有网段，无需修改公司路由。

## 4. 部署或续签服务器证书

首次部署时活动 TLS 目录尚为空，不能先运行依赖活动证书的完整预检。先用
下面的部署脚本校验候选证书、检查 Compose 和安全配置、写入活动目录并启动
服务。由于首次部署没有可回滚的旧证书，先进入维护窗口并确保旧
`frontend` 已停止；脚本检测到“无有效旧证书但 frontend 仍在运行”会拒绝
切换：

```powershell
docker compose --env-file ".env" stop frontend
```

然后执行：

```powershell
.\scripts\pki\Deploy-ServerCertificate.ps1 `
  -CandidateTlsDirectory "E:\TextileMonitor-Issued\2026-01" `
  -ActiveTlsDirectory "C:\TextileMonitor\tls-active" `
  -ServerIp "192.168.106.50" `
  -ExpectedRootSha256 "<线下核对的 64 位 SHA-256>" `
  -ComposeProjectDirectory "C:\TextileMonitor\textile-device-monitor" `
  -ComposeFile "docker-compose.yml","docker-compose.https.yml" `
  -EnvFile "C:\TextileMonitor\textile-device-monitor\.env" `
  -PythonPath "C:\TextileMonitor\deployment-venv\Scripts\python.exe"
```

证书部署脚本默认已加载 `docker-compose.yml` 和
`docker-compose.https.yml`；上面仍显式列出两者，便于审计实际生效的编排
文件。需要额外叠加其它覆盖文件时，可继续通过 `-ComposeFile` 传入完整列表。

部署过程会备份现用证书、预检候选文件、原子替换、执行 `nginx -t`、重新加载
或重建前端，并使用根 CA、固定 SNI 和指定服务器当前 IP 进行不带 `-k`、
不使用系统代理的 HTTPS 探测。它不会查询 DNS。任一步失败都会恢复上一版
文件并保留失败现场。

宿主机外部探测只请求最小化的 `/health/live`，用于验证真实
TLS/SNI/HSTS；该精确路径不继承业务页面的来源白名单，避免把 Docker 网关
错误加入 `MANAGEMENT_CIDRS`。完整 `/health/ready` 由部署脚本通过前端容器
内仅回环可达的 `/backend-ready` 代理检查。所有页面、API、WSS、SSE 和外部
`/health/ready` 仍受管理地址白名单约束。

部署完成后再次执行完整探测：

```powershell
.\scripts\validate-deployment.ps1 `
  -EnvFile ".env" `
  -ComposeFile "docker-compose.yml","docker-compose.https.yml" `
  -PythonPath "C:\TextileMonitor\deployment-venv\Scripts\python.exe" `
  -ProbeAddress "192.168.106.50"
```

续签时，先对仍在使用的活动证书运行一次完整预检，再执行部署脚本；部署脚本
还会在替换后再次预检和探测。Linux/macOS 管理终端可使用等价入口：

```bash
DEPLOYMENT_PYTHON=/opt/textile-monitor/deployment-venv/bin/python \
  ./scripts/validate-deployment.sh --env-file .env \
  --compose-file docker-compose.yml \
  --compose-file docker-compose.https.yml
```

## 5. 收紧服务器本机 443 防火墙

这一步只管理部门 Docker 服务器自己的 Windows 防火墙，不修改公司网络。
先逐台整理浏览器终端和设备电脑的当前 IPv4，并执行只读审计：

```powershell
.\scripts\windows\Manage-InspectionHttpsFirewall.ps1 `
  -Mode Audit `
  -ServerIp "192.168.106.50" `
  -ManagementClientIp "192.168.106.21","192.168.106.22"
```

如果审计报告默认入站策略不是 Block，或存在会放宽 TCP 443 的竞争性 Allow
规则，脚本会拒绝应用，也不会修改这些既有规则。先从两台灰度终端访问系统，
再用 `docker compose logs frontend` 核对 Nginx 记录的 `remote_addr`。只有
它与终端实际地址逐项一致、确认 Docker 保留源 IP 后才能应用：

```powershell
.\scripts\windows\Manage-InspectionHttpsFirewall.ps1 `
  -Mode Apply `
  -ServerIp "192.168.106.50" `
  -ManagementClientIp "192.168.106.21","192.168.106.22" `
  -ObservedRemoteAddress "192.168.106.21","192.168.106.22" `
  -ExpectedClientAddress "192.168.106.21","192.168.106.22" `
  -ConfirmDockerSourceIpPreserved

.\scripts\windows\Manage-InspectionHttpsFirewall.ps1 -Mode Validate
```

若日志只显示 Docker 网关、服务器自身或其它代理地址，脚本会失败关闭；不得
把网关地址当成部门白名单。此时需要在这台部门服务器上调整本地 Docker/反向
代理部署并重新灰度，而不是申请公司网络改动。还必须从一台白名单外的授权
测试电脑验证 443 被拒绝，才能宣称“仅本部门可访问”。

恢复时仅删除本脚本清单记录的受管规则，不碰原有或公司策略：

```powershell
.\scripts\windows\Manage-InspectionHttpsFirewall.ps1 -Mode Restore
```

## 6. 部署浏览器终端

将 `root-ca.cer`、线下记录的根证书 SHA-256 指纹和
`scripts\windows` 目录复制到终端。以管理员身份运行：

```powershell
.\scripts\windows\Install-InspectionTlsTrust.ps1 `
  -RootCertificatePath ".\root-ca.cer" `
  -ExpectedRootSha256 "<线下核对的 64 位 SHA-256>" `
  -ServerIp "192.168.106.50" `
  -StageOnly
```

该“预置”阶段可以在服务器切换 HTTPS 之前执行，只修改当前电脑：

1. 备份本机 `hosts`；
2. 强制核对根证书算法、用途、有效期和线下 SHA-256 指纹；
3. 安装根证书到 Windows 本机受信任根；
4. 写入服务器当前 IP 与 `textile-monitor.internal` 的本机映射；
5. 清理本机 DNS 缓存；

此时不会探测 443，也不会把清单误标为已完成。服务器 HTTPS 上线后，再用
相同参数去掉 `-StageOnly` 运行“激活”阶段；它会直连服务器 IP、绕过系统
代理，并用固定 SNI 验证证书链和 `/health/ready`。

浏览器随后应通过以下地址打开，且地址栏不得出现证书警告：

```text
https://textile-monitor.internal
```

Chrome 和 Edge 可能继承当前电脑的 Windows 代理、PAC 或本地代理软件。
因此 3 台灰度终端必须确认该主机名实际走直连；若现有本机代理支持例外列表，
只在当前终端把 `textile-monitor.internal` 加入直连/绕过项。不得要求 IT
修改公司 PAC，也不得把内部主机名经公网代理发送。客户端程序本身固定
`trust_env=false`，不继承这些代理设置。

可先运行只读检查收集当前电脑的 WinINET、Chrome/Edge 策略和 WinHTTP 代理
信号；脚本不会修改任何代理设置：

```powershell
.\scripts\windows\Test-InspectionBrowserProxy.ps1
```

回滚最近一次本机变更：

```powershell
.\scripts\windows\Restore-InspectionTlsTrust.ps1 -Confirm:$false
```

回滚依据部署清单恢复 `hosts` 和客户端配置；只在证书确由该次运行安装时才
删除根证书，不会删除终端原本已信任的同一根证书。

## 7. 迁移设备客户端

先发布兼容版客户端。旧配置首次升级时继续保留原 HTTP 地址并标记为
`compatible`，不会因为安装新版本而立即中断上报。正式切换分两次执行：
服务器上线前用客户端迁移脚本的 `-Phase Prepare` 预置根证书和本机
`hosts`；HTTPS 可用后用 `-Phase Activate -TransportSecurity compatible`
切换 URL。连续正常上报 24 小时后，再用
`-Phase Activate -TransportSecurity required` 强制加固。默认构建的 HTTP
安装包使用 `compatible`；只有显式指定 HTTPS Origin 和 CA 的安装包才默认
使用 `required`。

正式客户端安装目录中的 `admin-tools` 提供迁移封装。以管理员身份执行时
必须传入服务器 IP、根证书、Requests 使用的 PEM 和线下根指纹。迁移只有在
以下步骤全部成功后才完成：

- Windows 根证书和本机 `hosts` 安装完成；
- `config.json` 与 CA 文件均已备份并原子替换；
- 客户端先切换到 `https://textile-monitor.internal` 和 `compatible`，
  观察窗口完成后再切换为 `required`；
- 客户端重启后保持运行；
- 后端观察到该 `device_code` 的新心跳。

可信 HTTPS 激活前的预置失败会依据清单恢复未完成的本机变更。激活已开始且
安全 HTTPS 配置已经写入后，若重启或上报核验失败，脚本会停止客户端、保留
HTTPS 配置并记录 `activation_failed`，不会恢复或运行旧 HTTP 配置。旧配置
备份仅用于取证；不得通过关闭证书校验或自动回退 HTTP 恢复业务。

## 8. HSTS、续签和轮换

HSTS 按以下节奏调整 `.env` 中的 `HSTS_MAX_AGE`：

1. 首次灰度：`300`；
2. 稳定 24 小时：`86400`；
3. 连续稳定 7 天且完成续签和回滚演练：`31536000`。

不得添加 `includeSubDomains` 或 preload。

服务器证书每天检查一次，在剩余 60、30、14、7 天时告警，60 天时开始续签。
在服务器上用当前地址和线下根指纹注册每日任务：

```powershell
.\scripts\windows\Register-InspectionTlsHealthTask.ps1 `
  -ServerIp "192.168.106.50" `
  -ExpectedRootSha256 "<线下核对的 64 位 SHA-256>" `
  -DailyAt "03:00" `
  -RunNow
```

任务只解析本机 `hosts`、直连当前服务器地址、校验证书链/SAN 和
`/health/ready`，结果写入
`%ProgramData%\TextileDeviceMonitor\Logs\tls-health.jsonl`。地址变化时
更新 `.env`、终端 `hosts` 后重新注册该任务。

根 CA 轮换至少提前 12 个月，先向全部终端和客户端分发旧根与新根的重叠信任，
再更换服务器证书，最后移除旧根。

续签不会修改终端 `hosts`，也不需要公司 DNS 或网络管理员参与。
