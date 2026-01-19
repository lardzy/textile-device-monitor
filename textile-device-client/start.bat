@echo off
chcp 65001 >nul
echo ========================================
echo   纺织品检测设备客户端 - 快速启动
echo ========================================
echo.

cd /d "%~dp0"

if not exist "venv" (
    echo [1/2] 创建 Python 虚拟环境...
    python -m venv venv
    if errorlevel 1 (
        echo [错误] 创建虚拟环境失败，请确认 Python 3.11+ 已安装
        pause
        exit /b 1
    )
)

echo [2/2] 激活虚拟环境并安装依赖...
call venv\Scripts\activate.bat
pip install -r requirements.txt -i https://pypi.tuna.tsinghua.edu.cn/simple

echo.
echo ========================================
echo   启动客户端...
echo ========================================
echo.

python main.py

pause
