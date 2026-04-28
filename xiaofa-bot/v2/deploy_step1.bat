@echo off
echo ============================================
echo    GUI Sorter Bot - Render 部署助手
echo ============================================
echo.

cd /d C:\Users\acer\gui-sorter-bot\v2

echo [1/6] 檢查檔案...
if not exist bot_render.py (
    echo 錯誤: 找不到 bot_render.py
    pause
    exit /b 1
)
if not exist requirements.txt (
    echo 錯誤: 找不到 requirements.txt
    pause
    exit /b 1
)
if not exist render.yaml (
    echo 錯誤: 找不到 render.yaml
    pause
    exit /b 1
)
echo    ✓ 所有檔案齊全

echo.
echo [2/6] 建立 .gitignore...
(
echo .env
echo __pycache__/
echo *.pyc
echo venv/
echo .DS_Store
) > .gitignore
echo    ✓ .gitignore 已建立

echo.
echo [3/6] 初始化 Git...
git init
if errorlevel 1 (
    echo 錯誤: Git 初始化失敗
    echo 請確認已安裝 Git: https://git-scm.com/download/win
    pause
    exit /b 1
)
echo    ✓ Git 已初始化

echo.
echo [4/6] 加入檔案到 Git...
git add .
git commit -m "Initial commit - Bot with Haiku + Jina Reader"
echo    ✓ 檔案已 commit

echo.
echo ============================================
echo 接下來請手動完成以下步驟:
echo ============================================
echo.
echo 1. 打開瀏覽器到: https://github.com/new
echo 2. Repository name 輸入: gui-sorter-render
echo 3. 選擇 Private (重要!)
echo 4. 不要勾選任何初始檔案
echo 5. 點 Create repository
echo.
echo 6. 在 GitHub 頁面複製以下兩行指令並執行:
echo.
echo    git remote add origin https://github.com/你的帳號/gui-sorter-render.git
echo    git push -u origin main
echo.
echo    (把「你的帳號」改成你的 GitHub 使用者名稱)
echo.
echo 7. 完成後繼續到 Render 部署
echo    教學: Render部署教學.md
echo.
pause
