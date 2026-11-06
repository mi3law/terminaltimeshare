@echo off
rem ===========================================================================
rem  AI Terminal Timeshare - kiosk watchdog (SPEC section 6, process model)
rem
rem  The Startup-folder shortcut points HERE, with "Run: Minimized".
rem  Launches the agent windowless and relaunches it 5 s after ANY exit, so a
rem  dead agent fails CLOSED. Stops looping only on these agent exit codes:
rem     3 = another copy is already running (it was told to show its status)
rem     4 = teacher chose Quit, or config.json is missing/invalid
rem  Needs the Python launcher (pyw), installed by the python.org installer.
rem ===========================================================================
setlocal
cd /d "%~dp0"

:loop
start "" /wait pyw -3 "%~dp0agent.py"
set RC=%ERRORLEVEL%
if "%RC%"=="3" goto :done
if "%RC%"=="4" goto :done
rem ~5 s pause (ping works even when the console has no keyboard input)
ping -n 6 127.0.0.1 >nul
goto loop

:done
endlocal
exit /b 0
