@echo off
rem Double-click to open the alert settings program (manage.py) in this folder
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (python manage.py) else (py manage.py)
pause
