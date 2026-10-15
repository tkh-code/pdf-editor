@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem PDFをこのファイルにドラッグ＆ドロップすると、そのPDFを開いた状態で起動します
python server.py %*
if errorlevel 1 pause
