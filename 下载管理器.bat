@echo off
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONIOENCODING=utf-8
call "%~dp0tools\find_python.bat"
if not defined PYTHON (
    echo.
    echo No usable python found - cannot start the download manager.
    echo Fix: install Python, or set BBDOWN_PYTHON to python.exe,
    echo      or add its path to tools\python_paths.txt
    echo.
    pause
    exit /b 1
)
"%PYTHON%" "%~dp0tools\BBDown-manager.py" %*
set "EC=%errorlevel%"
echo.
pause
rem pause returns 0, so report the real exit code after it: scripts and
rem scheduled tasks need to see whether the download manager failed.
exit /b %EC%
