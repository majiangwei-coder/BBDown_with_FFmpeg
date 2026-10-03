@echo off
rem ============================================================
rem  Start the download guard (fills missing videos for every UP,
rem  survives rate limits by killing/resting/restarting).
rem
rem  WARNING: keep this file 100%% ASCII. cmd.exe mis-parses
rem  batch files that contain non-ASCII bytes (same rule as
rem  find_python.bat). Chinese output comes from python instead.
rem
rem  Usage (from this folder):
rem    start-guard.bat            - run until everything is filled
rem    start-guard.bat --status   - only show what is still missing
rem    start-guard.bat --help     - all options
rem
rem  Note: the real script has a Chinese file name, which cmd.exe cannot
rem  parse inside a .bat, so we call the ASCII entry point tools\guard.py.
rem  Programs live in tools\, videos in videos\, logs in logs\.
rem  Set GUARD_NO_PAUSE=1 to skip the "press any key" wait (for automation).
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
call "%~dp0tools\find_python.bat"
if not defined PYTHON (
    echo.
    echo No usable python found - cannot start the download guard.
    echo Fix: install Python, or set BBDOWN_PYTHON to python.exe,
    echo      or add its path to find_python.bat
    echo.
    if not defined GUARD_NO_PAUSE pause
    exit /b 1
)
rem Print through delayed expansion: a plain %PYTHON% here would run any
rem command embedded in the path (the log line is only cosmetic).
setlocal EnableDelayedExpansion
echo Using python: !PYTHON!  [!PYTHON_SOURCE!]
endlocal
echo.
echo Tips:  --status  = only report what is still missing
echo         --force   = take over from a guard that is already running
echo         --clean-workdirs = list leftover temp dirs of killed downloads
echo                            (add --yes to really delete them)
echo         --help    = full option list
echo.
"%PYTHON%" "%~dp0tools\guard.py" %*
set "EC=%errorlevel%"
echo.
echo guard exited (exit code %EC%)
if not defined GUARD_NO_PAUSE pause
exit /b %EC%
