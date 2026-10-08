@echo off
chcp 65001 >nul
cd /d "%~dp0.."
echo Starting LCSC / HQchip / IcKey data collector...
echo Open http://127.0.0.1:8123 in your browser
py -3.12 -m lcsc_scraper.webapp
pause
