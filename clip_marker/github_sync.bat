@echo off
echo ======================================
echo  ClipMarker - GitHub 同期
echo ======================================
echo.

cd /d "%~dp0"

echo [1/5] 既存の .git を削除中...
if exist ".git" rmdir /s /q .git

echo [2/5] git 初期化中...
git init -b main

echo [3/5] リモートを追加中...
git remote add origin https://github.com/yamagucho/ClipMarker.git

echo [4/5] ファイルをステージング・コミット中...
git add .
git commit -m "Initial commit: ClipMarker source"

echo [5/5] GitHub に push 中...
git push -u origin main

echo.
echo 完了！ https://github.com/yamagucho/ClipMarker を確認してください。
echo.
pause
