@echo off
REM ---------------------------------------------------------------------
REM  TechPort import - one batch, run by Windows Task Scheduler.
REM
REM  WHY THIS EXISTS RATHER THAN A CLAUDE SCHEDULED TASK
REM  ===================================================
REM  A Claude scheduled task was set up on 2026-09-15 to run this hourly.
REM  It fired once, then suspended itself with `device_absent` and has not
REM  run since: a Windows update released 2026-09-08 stops Claude's
REM  workspace from mounting D:, so the task cannot reach the repository.
REM  As of 2026-09-20 the import is still at 3,000 of 19,690.
REM
REM  Waiting for someone else's bug to be fixed is not a plan. This runs
REM  on the machine that owns the files, through the scheduler that is
REM  already trusted with archive_daily.cmd, and depends on nothing
REM  outside this box.
REM
REM  WHAT IT WILL NOT DO
REM  ===================
REM  Retry, escalate, or run a second batch. The importer stops itself
REM  when the key's remaining quota falls below 200 and says so; that is
REM  correct behaviour and the next run continues from the rows already
REM  written. Two batches in one hour is how an account gets suspended,
REM  and the standing rule on this project is that no account is ever put
REM  at risk.
REM
REM  INSTALL (once). No elevation needed - this is a per-user task, and
REM  that is deliberate: a per-user task inherits the logged-on account,
REM  so it needs no stored password. It also only runs while that user is
REM  logged on, which is the trade. On a workstation that is left on,
REM  that is the right side of the trade; storing a password in the task
REM  scheduler to buy a few extra hours is not.
REM
REM    schtasks /Create /TN "TechPort import" /TR ^
REM      "D:\Projects\Satellite-Platform\satellite-platform-ingestion\techport_import.cmd" ^
REM      /SC HOURLY /ST 00:20
REM
REM  VERIFY it is installed and will fire:
REM
REM    schtasks /Query /TN "TechPort import" /V /FO LIST
REM
REM  PROVE IT RUNS before trusting the schedule - run it by hand once and
REM  read the log, rather than finding out at 00:20:
REM
REM    schtasks /Run /TN "TechPort import"
REM    Get-Content D:\Databases\satellite\archive\techport_import.log -Tail 30
REM
REM  REMOVE when the import finishes:
REM
REM    schtasks /Delete /TN "TechPort import" /F
REM ---------------------------------------------------------------------

setlocal
set REPO=D:\Projects\Satellite-Platform\satellite-platform-ingestion
set LOG=D:\Databases\satellite\archive\techport_import.log

REM The same conda environment the interactive tool uses. Genuinely
REM hardcoded - the first version of this line read
REM %USERPROFILE%\anaconda3\... directly beneath a comment claiming the
REM path was hardcoded rather than inherited. USERPROFILE is inherited.
REM Run as SYSTEM it expands to C:\Windows\system32\config\systemprofile,
REM python is not found, and this logs the same error every hour forever
REM while looking like it ran. "It works in my shell" is not evidence
REM about a task running unattended at 00:20.
set PY=C:\Users\toddl\anaconda3\envs\satellite-base\python.exe

if not exist "%PY%" (
    echo %DATE% %TIME% ERROR: python not found at %PY% >> "%LOG%"
    exit /b 1
)

cd /d "%REPO%" || (
    echo %DATE% %TIME% ERROR: cannot reach %REPO% >> "%LOG%"
    exit /b 1
)

echo. >> "%LOG%"
echo ======== %DATE% %TIME% ======== >> "%LOG%"
"%PY%" -m src.catalog.seed_techport --apply --limit 1500 >> "%LOG%" 2>&1
echo exit=%ERRORLEVEL% >> "%LOG%"

REM A finished import says so in its own words. Grep for it rather than
REM inferring from a row count this script would have to guess at.
findstr /C:"Every project in the listing is imported" "%LOG%" >nul
if %ERRORLEVEL%==0 (
    echo %DATE% %TIME% IMPORT COMPLETE - remove the task with: >> "%LOG%"
    echo     schtasks /Delete /TN "TechPort import" /F >> "%LOG%"
)

endlocal
