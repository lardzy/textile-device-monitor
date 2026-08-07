# 安装目录 BridgeConfig.psd1 的模板。构建时原样打包，首次安装时仅当不存在才复制。
# 管理员按现场环境修改；机密（账号、口令、令牌）放在 bridge.env，不要写进本文件。
@{
    # 执行系统服务端 API（保持 http，不使用 https）
    ApiBase             = 'http://192.168.105.82/api/execution/v1'

    # 两个桥实例的稳定编号（同一时刻各自只允许一个进程运行）
    WriteBridgeId       = 'legacy-write-bridge-01'
    SnapshotBridgeId    = 'task-snapshot-bridge-01'
    PollSeconds         = 15

    # root_id -> 本机/UNC 路径映射。
    # execution_staging 必须与 docker compose 中 EXECUTION_RUNTIME_HOST_PATH
    # 指向同一个目录（桥与容器同机时为宿主机目录，异机时用 UNC）。
    ExecutionStagingPath  = 'D:\textile-monitor\runtime\execution-runtime'
    PaperFiberRecordsPath = '\\192.168.105.82\材料检测中心\10特纤\02-检验\08-其他\2022-纸、纸板和纸浆纤维鉴别分析'

    # 上传/登记需要用 SMB 凭据连通的共享（bridge.env 中 SMB_USER_B / SMB_PASS_B）
    SmbTargets = @(
        '\\192.168.105.82\fibrecheckfile$',
        '\\192.168.105.82\材料检测中心'
    )

    # 任务快照桥（只读 Oracle）
    OracleDataSource        = '192.168.105.106/orcl'
    OracleCredentialProfile = 'WebService.dll.config:PanYuJianWu'

    # Writer 可执行文件 SHA-256 钉值；每次运行前校验，不一致拒绝启动。
    # 升级安装包时由 Build-BridgePackage.ps1 重新生成。
    WriterHashes = @{
        'FibreCheckWriter.exe'            = 'REPLACE_BY_BUILD'
        'FibreCheckFinalEntryWriter.exe'  = 'REPLACE_BY_BUILD'
    }
}
