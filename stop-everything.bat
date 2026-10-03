@echo off
rem ============================================================
rem  Stop the downloader WITHOUT leaving half-finished videos.
rem
rem  Default behaviour - graceful stop:
rem    1) ask it to stop: write the "graceful stop request" file
rem    2) the guard and the manager see it and STOP TAKING NEW WORK,
rem       but the videos already downloading are allowed to FINISH
rem       and land complete on disk
rem    3) they exit by themselves
rem    Result: nothing half-written on disk, nothing to clean up, and
rem    it stops within roughly one video's time instead of grinding
rem    through the whole queue.
rem
rem  Only if that takes longer than --timeout minutes (default 40)
rem  does it fall back to killing the process tree and cleaning up
rem  the leftovers. --force skips the wait entirely.
rem
rem  Options:
rem    stop-everything.bat                  graceful, asks first
rem    stop-everything.bat --yes            graceful, no question
rem    stop-everything.bat --dry-run        only show what it would do
rem    stop-everything.bat --timeout 15     wait at most 15 min, then kill
rem    stop-everything.bat --force          kill now (leaves partial files)
rem    stop-everything.bat --keep-workdirs  with --force: keep temp dirs
rem
rem  Note: it only matches processes belonging to THIS copy of the
rem  project (full-path match), so a second copy elsewhere is never
rem  touched. Run that copy's own script to stop it.
rem
rem  WARNING: keep this file 100%% ASCII (cmd.exe mis-parses batch
rem  files with non-ASCII bytes). All Chinese output comes from python.
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
call "%~dp0tools\find_python.bat"
if not defined PYTHON goto :no_python

set "TOOL=%~dp0tools\stop_safely.py"
if not exist "%TOOL%" goto :no_tool

echo Using python: %PYTHON%
echo.
"%PYTHON%" "%TOOL%" %*
set "EC=%errorlevel%"
echo.
if "%EC%"=="0" echo Done - safe to start the guard again.
if "%EC%"=="1" echo Nothing was changed.
if "%EC%"=="2" echo NOT clean: some process refused to exit, so no file was deleted.
echo exit code %EC%
if not defined GUARD_NO_PAUSE pause
exit /b %EC%

:no_python
echo.
echo No usable python found - cannot stop the downloader safely.
echo Fix: install Python, or add its path to tools\find_python.bat,
echo      or set the BBDOWN_PYTHON environment variable.
echo.
echo Quick manual alternative (PowerShell, as Administrator):
echo   Get-Process python,BBDown,ffmpeg -ErrorAction SilentlyContinue ^| Stop-Process -Force
echo.
echo Note: that kills the running download, so it leaves a partial file
echo and a temp dir; the guard cleans those up on its next start.
echo.
if not defined GUARD_NO_PAUSE pause
exit /b 9

:no_tool
echo.
echo Cannot find "%~dp0tools\stop_safely.py" - is this the right folder?
echo.
if not defined GUARD_NO_PAUSE pause
exit /b 9
