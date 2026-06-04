@echo off
echo ======================================
echo  ClipMarker - Windows EXE ビルド
echo ======================================
echo.

cd /d "%~dp0"

echo [1/3] 依存パッケージをインストール中...
pip install -r requirements.txt
pip install pyinstaller

echo.
echo [2/3] EXEをビルド中...
pyinstaller ^
  --onefile ^
  --noconsole ^
  --name "ClipMarker" ^
  --icon=icon.ico ^
  app.py

echo.
echo [3/3] 完了！
echo dist\ClipMarker.exe が生成されました。
echo.
pause
