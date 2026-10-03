@echo off
rem ============================================================
rem  Daily unattended update - runs the DOWNLOAD GUARD, not the manager.
rem
rem  Why the guard and not the manager:
rem    The manager alone has no account-level rate-limit protection and,
rem    worse, it writes videos into the "skip" list after 2 failed attempts
rem    - and that list is permanent. Only the guard re-queues those, warms
rem    up before a round, lowers concurrency when throttled, restarts after
rem    a rest, and cleans up leftover processes and temp dirs.
rem
rem  WARNING - two cmd.exe traps this file is careful about:
rem   1) keep this file 100%% ASCII. cmd.exe mis-parses batch files that
rem      contain non-ASCII bytes. All Chinese output comes from python.
rem   2) do NOT wrap redirections in an `if ( ... )` block. Inside such a
rem      block a ")" coming from an expanded variable ends the block early
rem      and cmd dies with "was unexpected at this time." - which is exactly
rem      what happened when this folder is named with parentheses. Every
rem      branch below therefore uses `goto` instead of parentheses.
rem
rem  Exit codes (also the Windows Task Scheduler "LastTaskResult"):
rem    0    everything filled in - nothing left to download
rem    1    hit --max-rounds with work still left (not an error per se)
rem    2    needs a human: login expired, or another downloader keeps running
rem    3    several rounds in a row made no progress
rem    4    another guard/manager is already running - this run stood down
rem    9    no usable python found - the update did NOT run
rem   10    rate limited and the rest budget ran out - stopped on purpose.
rem          Nothing is broken and nothing was lost; just run it later.
rem    anything else (e.g. 3221225786 = killed/interrupted) = it was cut off
rem
rem  Set BBDOWN_MAX_ROUNDS to change the round budget (default 40).
rem ============================================================
chcp 65001 >nul
cd /d "%~dp0"
set "PYTHONIOENCODING=utf-8"
set "LOGDIR=%~dp0logs"
if not exist "%LOGDIR%" mkdir "%LOGDIR%"
set "LOG=%LOGDIR%\auto_update_log.txt"
set "FAILNOTE=%LOGDIR%\AUTO_UPDATE_FAILED_read_me.txt"
if not defined BBDOWN_MAX_ROUNDS set "BBDOWN_MAX_ROUNDS=40"

rem ---- keep the log bounded: past ~5MB roll it aside as .1 (one old copy) ----
if exist "%LOG%" for %%A in ("%LOG%") do if %%~zA GTR 5242880 move /y "%LOG%" "%LOG%.1" >nul

echo [%date% %time%] Start guarded update >> "%LOG%"

call "%~dp0tools\find_python.bat"
if not defined PYTHON goto :no_python

rem ---- last run's failure note is stale now; a fresh one is written below if needed ----
if exist "%FAILNOTE%" del "%FAILNOTE%"

rem delayed expansion: a plain %PYTHON% would run commands embedded in it
setlocal EnableDelayedExpansion
>> "%LOG%" echo [!date! !time!] using python: !PYTHON!  [from: !PYTHON_SOURCE!]
endlocal
>> "%LOG%" echo [%date% %time%] guard: --max-rounds %BBDOWN_MAX_ROUNDS% (no --force: if another guard is already running, this run stands down)

"%PYTHON%" "%~dp0tools\guard.py" --max-rounds %BBDOWN_MAX_ROUNDS% >> "%LOG%" 2>&1
set "EC=%errorlevel%"

rem ---- leave a visible note only when a human is actually needed ----
rem  Note: exit code 10 (rate limited, stopped on purpose) deliberately does NOT
rem  leave a note. Nothing is broken and nothing was lost, and it needs no human -
rem  tonight's run just gave up waiting for the limit to clear. It IS logged.
if "%EC%"=="9" goto :note_norun
if "%EC%"=="2" goto :note_manual
if "%EC%"=="3" goto :note_stalled
if "%EC%"=="4" goto :note_busy
goto :done

:no_python
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
set "EC=9"
goto :done

:note_norun
> "%FAILNOTE%" echo AUTO UPDATE DID NOT RUN (no usable python).
>> "%FAILNOTE%" echo Time: %date% %time%
>> "%FAILNOTE%" echo See auto_update_log.txt for details.
goto :done

:note_manual
> "%FAILNOTE%" echo GUARDED UPDATE NEEDS A HUMAN.
>> "%FAILNOTE%" echo Time: %date% %time%
>> "%FAILNOTE%" echo Exit code 2: login expired, or another downloader keeps running.
>> "%FAILNOTE%" echo If login expired: double-click BBDown-login.bat and scan the QR code,
>> "%FAILNOTE%" echo                      then start the guard once by hand.
>> "%FAILNOTE%" echo See logs\guard_status.txt and auto_update_log.txt.
goto :done

:note_stalled
> "%FAILNOTE%" echo GUARDED UPDATE MADE NO PROGRESS.
>> "%FAILNOTE%" echo Time: %date% %time%
>> "%FAILNOTE%" echo Exit code 3: several rounds in a row filled nothing.
>> "%FAILNOTE%" echo Usually rate limiting or an expired cookie. Check
>> "%FAILNOTE%" echo logs\guard_status.txt and logs\guard_log.txt.
goto :done

:note_busy
> "%FAILNOTE%" echo GUARDED UPDATE STOOD DOWN (another one is running).
>> "%FAILNOTE%" echo Time: %date% %time%
>> "%FAILNOTE%" echo Exit code 4: a guard or manager is already running, so this
>> "%FAILNOTE%" echo scheduled run did nothing. That is safe - but if it keeps
>> "%FAILNOTE%" echo happening every night, find out who is holding the lock.
>> "%FAILNOTE%" echo See logs\guard_status.txt.
goto :done

:done
echo [%date% %time%] guard run finished, exit code %EC% >> "%LOG%"
exit /b %EC%
