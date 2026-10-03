@echo off
rem ============================================================
rem  Helper for the download manager bat and the auto-update bat.
rem  Finds a python that can really run BBDown-manager.py.
rem
rem  Usage from another bat (note: keep the quotes):
rem      call "%~dp0find_python.bat"
rem      if not defined PYTHON ( ...no python, handle the failure... )
rem      "%PYTHON%" "%~dp0BBDown-manager.py" ...
rem
rem  Result:
rem      PYTHON        = full path of the interpreter   (empty if none)
rem      PYTHON_SOURCE = where it was found             (for the log)
rem      return code   = 0 found / 1 not found
rem
rem  A candidate is accepted only if the file exists, is not a directory,
rem  is bigger than 1KB, LOOKS LIKE a real install (a pyvenv.cfg or a
rem  python*.dll next to it -- checked before the exe is ever executed, so
rem  a stray python.exe dropped into a searched folder is skipped), and it
rem  really runs python: it must import what BBDown needs and then end with
rem  the exact exit code we asked for (sys.exit(37)). A plain success code
rem  (0) is NOT accepted -- some programs return 0 for any arguments.
rem  If python is installed somewhere else later, either set the env var
rem  BBDOWN_PYTHON to its python.exe, or add one line to
rem  tools\python_paths.txt (a personal file, not in git: one path per
rem  line, # starts a comment). That file plus the search order below is
rem  the whole fallback mechanism.
rem  BBDOWN_PYTHON wins over everything, so only point it at a python you
rem  installed yourself: whoever can set that variable decides which
rem  program runs as "python" for this project.
rem
rem  WARNING: keep this file 100%% ASCII. cmd.exe mis-parses batch files
rem  that contain non-ASCII bytes when the console code page is not
rem  UTF-8, and that would silently break the automatic update.
rem ============================================================

set "PYTHON="
set "PYTHON_SOURCE="

rem Delayed expansion stays on for the whole script. Candidate paths are read
rem as !CAND!, which cmd expands only AFTER the line has been parsed, so a
rem quote or an & inside the value cannot start a new command. The result is
rem copied back out at :found (endlocal would otherwise erase it).
setlocal EnableDelayedExpansion

rem ---- 0) explicit override: set BBDOWN_PYTHON to a python.exe path ----
rem The value is handed to the checker in a VARIABLE, never as an argument.
rem Passing it as an argument is unsafe twice over: "%VAR%" is expanded while
rem the line is being parsed, and "!VAR!" is expanded just before CALL, which
rem then re-parses the finished line -- either way one double quote inside the
rem value ends the quote and the rest of the line runs as commands.
if defined BBDOWN_PYTHON (
    set "CAND=!BBDOWN_PYTHON!"
    set "SRC=BBDOWN_PYTHON"
    call :trypy_var
)
if defined PYTHON goto :found

rem ---- 1) extra candidates from python_paths.txt (optional personal file) ----
rem One path per line: either a python.exe, or a folder (a folder also gets
rem <folder>\python.exe and <folder>\python\python.exe tried). # starts a
rem comment. The line is read into BASE and checked through :try_paths ->
rem :trypy_var, never through "call :trypy", so a quote or an & inside the
rem line cannot be re-parsed as a command (same rule as BBDOWN_PYTHON).
set "BASE="
if exist "%~dp0python_paths.txt" (
    for /f "usebackq eol=# delims=" %%P in ("%~dp0python_paths.txt") do (
        set "BASE=%%~P"
        call :try_paths
    )
)
set "BASE="
if defined PYTHON goto :found

rem ---- 2) portable python shipped next to this script: python\python.exe ----
call :trypy "%~dp0python\python.exe" "local python folder"
if defined PYTHON goto :found

rem ---- 3) python on PATH (the 0-byte store stub is skipped automatically) ----
for /f "delims=" %%P in ('where.exe python 2^>nul') do if not defined PYTHON (
    call :trypy "%%~fP" "PATH"
)
if defined PYTHON goto :found

rem ---- 4) common install locations ----
for /d %%D in ("%LOCALAPPDATA%\Programs\Python\*") do if not defined PYTHON (
    call :trypy "%%~fD\python.exe" "user install"
)
for /d %%D in ("C:\Python*") do if not defined PYTHON (
    call :trypy "%%~fD\python.exe" "C:\Python"
)
for /d %%D in ("C:\Program Files\Python*") do if not defined PYTHON (
    call :trypy "%%~fD\python.exe" "Program Files"
)

:found
set "PYDIR="
set "PYOK="
set "PYSIZE="
rem copy the result out of the setlocal scope, then answer the caller
set "PY_TMP=!PYTHON!"
set "SRC_TMP=!PYTHON_SOURCE!"
endlocal & set "PYTHON=%PY_TMP%" & set "PYTHON_SOURCE=%SRC_TMP%"
if defined PYTHON exit /b 0
exit /b 1

rem ---- check one line of python_paths.txt: the path itself, then the
rem ---- two common folder layouts. BASE is read with !BASE! inside
rem ---- :trypy_var only, so nothing here is re-parsed as a command.
:try_paths
if defined PYTHON goto :eof
if not defined BASE goto :eof
set "CAND=!BASE!"
set "SRC=python_paths.txt"
call :trypy_var
set "CAND=!BASE!\python.exe"
call :trypy_var
set "CAND=!BASE!\python\python.exe"
call :trypy_var
goto :eof

rem ---- check a single candidate given as an argument ----
rem Only used for paths this script builds itself. The BBDOWN_PYTHON value
rem goes through :trypy_var instead -- see the note near the top.
:trypy
set "CAND=%~1"
set "SRC=%~2"
call :trypy_var
goto :eof

rem ---- check the candidate in %CAND% (label in %SRC%) ----
:trypy_var
if defined PYTHON goto :eof
if not defined CAND goto :eof
if not exist "!CAND!" goto :eof
if exist "!CAND!\" goto :eof
set "PYSIZE="
set "PYDIR="
for %%A in ("!CAND!") do set "PYSIZE=%%~zA"
for %%A in ("!CAND!") do set "PYDIR=%%~dpA"
if not defined PYSIZE goto :eof
if !PYSIZE! LSS 1024 goto :eof
rem structural sanity: a real python has pyvenv.cfg or python*.dll next to it.
rem this runs BEFORE the candidate is executed, so a planted exe is skipped.
set "PYOK="
if exist "!PYDIR!pyvenv.cfg" set "PYOK=1"
if exist "!PYDIR!python*.dll" set "PYOK=1"
if not defined PYOK goto :eof
rem A success code is not enough: some programs (an attrib.exe renamed to
rem python.exe, for one) return 0 no matter what arguments they are given.
rem So ask the candidate to import what BBDown needs and then finish with one
rem exact exit code, and accept that code and nothing else. A real python that
rem cannot import requests/msvcrt dies with code 1, and a program that is not
rem python has no way to produce 37 on demand.
"!CAND!" -c "import requests, msvcrt, sys; sys.exit(37)" >nul 2>&1
if not errorlevel 37 goto :eof
if errorlevel 38 goto :eof
set "PYTHON=!CAND!"
set "PYTHON_SOURCE=!SRC!"
goto :eof
