# 构建机路径与物料配置。默认值对应当前 Parallels Windows 11 构建机；
# 换机器/换源码包时只需改这里或同名覆盖文件，不要改 Build-BridgePackage.ps1。
@{
    # 安装包版本号（写入 version.auto.iss、manifest 与安装包文件名）
    # 1.0.1：ops 运行日志按天落盘 + 注册后立即启动计划任务（c6b1ca9）
    PackageVersion    = '1.0.1'

    # 执行系统源码树（含 tools\*；writer 已编译产物在其 out\ 下）
    RepoSourceRoot    = 'C:\Users\lishuyang\Downloads\textile-device-monitor-cdde9ec\textile-device-monitor'

    # 旧仓库根（.tmp 下的第三方物料：FibreCheck 冻结客户端、oracle-ic）
    LegacyRepoRoot    = 'C:\Users\lishuyang\Downloads\textile-device-monitor'
    FibreCheckDir     = 'C:\Users\lishuyang\Downloads\textile-device-monitor\.tmp\FibreCheck'
    OracleIcX64Dir    = 'C:\Users\lishuyang\Downloads\textile-device-monitor\.tmp\oracle-ic\instantclient_19_31'

    # x86 Oracle.DataAccess.dll（非托管 ODP.NET 4.112.2.50，Writer 运行依赖）。
    # odac32 xcopy 包不含该文件，默认从构建机 GAC_32 复制（构建后即随包携带，
    # 生产机无需安装 ODAC/GAC）。
    OracleDataAccessDll = 'C:\Windows\Microsoft.NET\assembly\GAC_32\Oracle.DataAccess\v4.0_4.112.2.50__89b483f429c47342\oracle.dataaccess.dll'

    # 随包携带的 CPython 运行时（x64，完整安装目录）
    PythonRuntimeDir  = 'C:\Users\lishuyang\AppData\Roaming\uv\python\cpython-3.12.13-windows-x86_64-none'

    # Writer 源可执行文件 SHA-256 钉值：与本次验收通过的编译产物绑定。
    # 重新编译 Writer 后必须显式更新此处的值，否则构建失败。
    WriterSourceHashes = @{
        'FibreCheckWriter.exe'           = 'e99da7ea028c93dad5f13934e7c5a0f90525092264b199cbb9e80d8b9a633108'
        'FibreCheckFinalEntryWriter.exe' = '29e5a8e4569b7a8f42320255ac80e3fe83e75872e4988ae249726dc39a86cf9e'
    }

    # probe 离线依赖打包时的 pip 源（构建机需要能访问；--no-input）
    PipIndexUrl       = 'https://pypi.org/simple'

    # 用于引导 pip 的 Python：uv 版 CPython 的 ensurepip 被拒绝（externally-managed），
    # 因此默认使用构建机上已装好 pip 的 probe-venv（与随包 Python 同为 3.12 x64，
    # --target 产物可直接共用）。
    PipBootstrapPython = 'C:\Users\lishuyang\Downloads\textile-device-monitor-cdde9ec\.tmp\probe-venv\Scripts\python.exe'
}
