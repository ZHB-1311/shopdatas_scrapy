@echo off
chcp 65001 >nul
cd /d %~dp0
echo 启动立创商城数据采集系统...
echo 浏览器访问: http://127.0.0.1:8123
python -m lcsc_scraper.webapp
pause
