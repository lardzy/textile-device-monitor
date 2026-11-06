# 首次安装的普通参数默认值，配置中心保存到 ProgramData。
@{
    # 默认与服务端同机，管理员可在界面选择实际服务端及发布端口。
    ApiBase             = 'http://127.0.0.1/api/execution/v1'

    PollSeconds         = 2

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
}
