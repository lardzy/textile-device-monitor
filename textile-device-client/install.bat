@echo off
chcp 65001 >nul
echo ========================================
echo   纺织品检测设备客户端 - 安装程序
echo ========================================
echo.

if not exist "dist\textile-device-client" (
    echo [错误] 请先运行 build.py 进行打包
    echo.
    pause
    exit /b 1
)

set INSTALL_DIR=C:\Program Files\TextileDeviceClient

echo [1/5] 创建安装目录...
mkdir "%INSTALL_DIR%" 2>nul

echo [2/5] 复制文件...
xcopy /E /I /Y /Q "dist\textile-device-client" "%INSTALL_DIR%"

echo [3/5] 创建桌面快捷方式...
powershell -Command ^
    "$WshShell = New-Object -ComObject WScript.Shell; ^
    $Shortcut = $WshShell.CreateShortcut([Environment]::GetFolderPath('Desktop') + '\纺织品检测设备客户端.lnk'); ^
    $Shortcut.TargetPath = '%INSTALL_DIR%\textile-device-client.exe'; ^
    $Shortcut.WorkingDirectory = '%INSTALL_DIR%'; ^
    $Shortcut.Description = '纺织品检测设备监控系统 - 客户端'; ^
    $Shortcut.Save()"

echo [4/5] 注册开机自启动...
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" ^
    /v "TextileDeviceClient" ^
    /t REG_SZ ^
    /d "\"%INSTALL_DIR%\textile-device-client.exe\"" ^
    /f >nul

echo [5/5] 清理临时文件...
if exist "build" rmdir /s /q "build"

echo.
echo ========================================
echo   安装完成！
echo ========================================
echo.
echo 安装目录: %INSTALL_DIR%
echo 桌面快捷方式已创建
echo 开机自启动已启用
echo.
echo 如需卸载，请运行 uninstall.bat
echo.
pause
