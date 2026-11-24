@echo off
chcp 65001 >nul
cd /d "%~dp0web"

echo ==========================================================
echo 啟動 台中小海盜棒球數據庫 - MWG 現代前端本機預覽
echo ==========================================================
echo.

if not exist node_modules (
    echo [*] 首次啟動，正在安裝前端相依套件...
    call npm.cmd install
    if errorlevel 1 (
        echo.
        echo [!] 前端套件安裝失敗，請確認已安裝 Node.js 20+。
        pause
        exit /b 1
    )
)

echo [*] 正在啟動 Vite 開發伺服器並自動開啟瀏覽器 (按 Ctrl+C 可停止)...
call npm.cmd run dev -- --open
if errorlevel 1 (
    echo.
    echo [!] 啟動開發伺服器失敗，請檢視上方錯誤訊息。
    pause
    exit /b 1
)
