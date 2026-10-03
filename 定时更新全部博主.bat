@echo off
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "LOGDIR=%~dp0logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\auto_update_log.txt"
set "FAILNOTE=%LOGDIR%\AUTO_UPDATE_FAILED_read_me.txt"
rem ---- keep the log bounded: past ~5MB roll it aside as .1 (one old copy) ----
if exist "%LOG%" for %%A in ("%LOG%") do if %%~zA GTR 5242880 move /y "%LOG%" "%LOG%.1" >nul
echo [%date% %time%] Start update all >> "%LOG%"
call "%~dp0tools\find_python.bat"
if not defined PYTHON (
    echo [%date% %time%] ERROR: no usable python found, update did NOT run >> "%LOG%"
    echo [%date% %time%] Searched: BBDOWN_PYTHON, python_paths.txt, local folder, PATH, common install dirs >> "%LOG%"
    > "%FAILNOTE%" echo AUTO UPDATE DID NOT RUN - no usable python was found.
    >> "%FAILNOTE%" echo Time: %date% %time%
    >> "%FAILNOTE%" echo.
    >> "%FAILNOTE%" echo A real python (pyvenv.cfg or python*.dll next to it) with
    >> "%FAILNOTE%" echo the "requests" module is required.
    >> "%FAILNOTE%" echo The python used before is gone: moved, renamed, or deleted.
    >> "%FAILNOTE%" echo.
    >> "%FAILNOTE%" echo Fix: install Python, or set BBDOWN_PYTHON to python.exe,
    >> "%FAILNOTE%" echo      or add its path to tools\python_paths.txt,
    >> "%FAILNOTE%" echo      then run the download manager once to confirm.
    >> "%FAILNOTE%" echo Details: auto_update_log.txt
    echo [%date% %time%] Finished with exit code 9 >> "%LOG%"
    exit /b 9
)
if exist "%FAILNOTE%" del "%FAILNOTE%"
rem delayed expansion: a plain %PYTHON% would run commands embedded in it
setlocal EnableDelayedExpansion
>> "%LOG%" echo [!date! !time!] using python: !PYTHON!  [from: !PYTHON_SOURCE!]
endlocal
"%PYTHON%" "%~dp0tools\BBDown-manager.py" --all >> "%LOG%" 2>&1
set "EC=%errorlevel%"
echo [%date% %time%] Finished with exit code %EC% >> "%LOG%"
exit /b %EC%
