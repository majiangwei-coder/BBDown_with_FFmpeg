@echo off
rem ============================================================
rem  Run the offline test suite for the download guard.
rem
rem  WARNING: keep this file 100%% ASCII. cmd.exe mis-parses
rem  batch files that contain non-ASCII bytes (same rule as
rem  find_python.bat). Chinese output comes from python instead.
rem
rem  Set GUARD_NO_PAUSE=1 to skip the "press any key" wait (for automation).
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
call "%~dp0tools\find_python.bat"
if not defined PYTHON (
    echo.
    echo No usable python found - cannot run the tests.
    echo Fix: install Python, or set BBDOWN_PYTHON to python.exe,
    echo      or add its path to find_python.bat
    echo.
    if not defined GUARD_NO_PAUSE pause
    exit /b 1
)
rem delayed expansion: a plain %PYTHON% would run commands embedded in it
setlocal EnableDelayedExpansion
echo Using python: !PYTHON!  [!PYTHON_SOURCE!]
endlocal
echo.
echo These tests are offline: no network, and they never touch the
echo real download state files or logs (everything goes to a temp dir).
echo.
"%PYTHON%" -m unittest discover -s "%~dp0tools\tests" -v
set "EC=%errorlevel%"
echo.
if "%EC%"=="0" (
    echo ALL TESTS PASSED
) else (
    echo SOME TESTS FAILED - see the details above
)
echo exit code %EC%
if not defined GUARD_NO_PAUSE pause
exit /b %EC%
