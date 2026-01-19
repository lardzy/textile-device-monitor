@echo off
chcp 65001 >nul
echo ========================================
echo   纺织品检测设备客户端 - 开发环境安装
echo ========================================
echo.

cd /d "%~dp0"

echo 检查 Python 版本...
python --version
if errorlevel 1 (
    echo [错误] Python 未安装或未添加到 PATH
    echo 请安装 Python 3.11+ 并添加到系统 PATH
    pause
    exit /b 1
)

echo.
echo [1/3] 创建 Python 虚拟环境...
python -m venv venv
if errorlevel 1 (
    echo [错误] 创建虚拟环境失败
    pause
    exit /b 1
)

echo [2/3] 激活虚拟环境...
call venv\Scripts\activate.bat

echo [3/3] 安装依赖包...
pip install --upgrade pip
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple
if errorlevel 1 (
    echo [错误] 依赖包安装失败
    pause
    exit /b 1
)

echo.
echo ========================================
echo   开发环境安装完成！
echo ========================================
echo.
echo 使用以下命令启动开发环境：
echo   1. 双击 start.bat
echo   2. 或运行: python main.py
echo.
echo 打包命令：
echo   1. 先运行: pip install pyinstaller
echo   2. 然后运行: python build.py
echo.
pause
