@echo off
rem 더블클릭하면 이 폴더에서 관리 프로그램(manage.py)을 실행
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (python manage.py) else (py manage.py)
pause
