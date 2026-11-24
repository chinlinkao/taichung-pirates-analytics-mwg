@echo off
chcp 65001 >nul
cd /d %~dp0

echo ==========================================================
echo [1/3] 正在執行數據管線 (刷新當前賽事戰績並校驗入庫)...
echo ==========================================================
echo.

call .venv\Scripts\python.exe -u run_pipeline.py --auto-current

if errorlevel 1 (
    echo.
    echo [!] 數據管線執行失敗，請檢查錯誤訊息，取消同步至雲端。
    echo.
    pause
    exit /b 1
)

echo.
echo ==========================================================
echo [2/3] 本機數據庫更新成功！準備推送至 GitHub...
echo ==========================================================
echo.

git add .
git diff-index --quiet HEAD
if errorlevel 1 (
    git commit -m "update: 本機一鍵同步最新賽事數據"
    if errorlevel 1 (
        echo.
        echo [!] Git 提交失敗，取消推送。
        echo.
        pause
        exit /b 1
    )
)
git pull --rebase origin main
if errorlevel 1 (
    echo.
    echo [!] Git 拉取合併失敗，請確認是否有衝突。
    echo.
    pause
    exit /b 1
)
git push origin main

if errorlevel 1 (
    echo.
    echo [!] Git 推送失敗，請確認網路連線或 GitHub 權限。
    echo.
    pause
    exit /b 1
)

echo.
echo ==========================================================
echo [3/3] 全流程完成！
echo 數據與靜態 JSON 已推送至 GitHub，將自動觸發線上看板更新！
echo [新版看板] https://chinlinkao.github.io/taichung-pirates-analytics-mwg/
echo ==========================================================
echo.
pause
