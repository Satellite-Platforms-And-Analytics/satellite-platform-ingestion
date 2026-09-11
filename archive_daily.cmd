@echo off
REM ---------------------------------------------------------------------
REM Daily archive of Supabase history to local disk. Run by Task Scheduler.
REM
REM Exists as a .cmd rather than a schtasks /TR one-liner because that
REM one-liner has to nest quotes around && and >>, and because Task
REM Scheduler does not inherit the conda environment this project runs in
REM - a bare `python` there is a different interpreter, or none at all.
REM That is the usual reason a scheduled job silently never runs.
REM
REM Interpreter resolution, in order, logged so a failure says which:
REM   1. %SATELLITE_PYTHON%      - set this to pin an exact python.exe
REM   2. conda env satellite-base
REM   3. whatever `python` is on PATH
REM ---------------------------------------------------------------------
setlocal enabledelayedexpansion
cd /d "%~dp0"

if "%SATELLITE_ARCHIVE_DIR%"=="" set "SATELLITE_ARCHIVE_DIR=D:\Databases\satellite\archive"
if not exist "%SATELLITE_ARCHIVE_DIR%" mkdir "%SATELLITE_ARCHIVE_DIR%"
set "LOG=%SATELLITE_ARCHIVE_DIR%\archive.log"

echo. >> "%LOG%"
echo ======== %DATE% %TIME% ======== >> "%LOG%"

set "PY="
if not "%SATELLITE_PYTHON%"=="" (
    set "PY=%SATELLITE_PYTHON%"
    echo interpreter: SATELLITE_PYTHON=%SATELLITE_PYTHON% >> "%LOG%"
)

if "!PY!"=="" (
    if exist "%USERPROFILE%\anaconda3\Scripts\activate.bat" (
        call "%USERPROFILE%\anaconda3\Scripts\activate.bat" satellite-base >> "%LOG%" 2>&1
        if not errorlevel 1 (
            set "PY=python"
            echo interpreter: conda env satellite-base >> "%LOG%"
        )
    )
)

if "!PY!"=="" (
    set "PY=python"
    echo interpreter: bare 'python' on PATH ^(fallback^) >> "%LOG%"
)

!PY! -c "import sys; print('using ' + sys.executable)" >> "%LOG%" 2>&1
if errorlevel 1 (
    echo FAILED: no usable python. Set SATELLITE_PYTHON to a full path. >> "%LOG%"
    exit /b 2
)

!PY! archive_to_local.py --apply >> "%LOG%" 2>&1
set RC=%ERRORLEVEL%
echo exit=%RC% >> "%LOG%"
exit /b %RC%
