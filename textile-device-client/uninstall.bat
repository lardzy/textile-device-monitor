@echo off
chcp 65001 >nul
echo ========================================
echo   纺织品检测设备客户端 - 卸载程序
echo ========================================
echo.

set INSTALL_DIR=C:\Program Files\TextileDeviceClient

echo [1/4] 停止运行中的客户端...
taskkill /F /IM textile-device-client.exe >nul 2>&1

echo [2/4] 删除开机自启动...
reg delete "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" ^
    /v "TextileDeviceClient" ^
    /f >nul 2>&1

echo [3/4] 删除桌面快捷方式...
del "%USERPROFILE%\Desktop\纺织品检测设备客户端.lnk" >nul 2>&1

echo [4/4] 删除安装目录...
if exist "%INSTALL_DIR%" (
    rmdir /s /q "%INSTALL_DIR%"
    echo 已删除: %INSTALL_DIR%
) else (
    echo 安装目录不存在
)

echo.
echo ========================================
echo   卸载完成！
echo ========================================
echo.
pause
