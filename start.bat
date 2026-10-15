@echo off
chcp 65001 >nul
cd /d "%~dp0"
rem ネットのURLが使えないときの予備：このフォルダをPC内で配信して開く
start "" http://127.0.0.1:8765/
python -m http.server 8765 --bind 127.0.0.1
