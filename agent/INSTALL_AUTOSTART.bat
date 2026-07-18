@echo off
:: ═══════════════════════════════════════════════════════════════════════
:: Laptop Diagnostics Agent — Silent Auto-Start Installer
::
:: RUN THIS AS ADMINISTRATOR to install as a Windows Scheduled Task.
:: After install the agent runs silently at startup — no window, no Python.
::
:: STEP 1: Edit DIAG_SERVER_URL and DIAG_API_KEY below
:: STEP 2: Right-click this file → Run as administrator
:: ═══════════════════════════════════════════════════════════════════════

:: ── SET YOUR VALUES HERE ─────────────────────────────────────────────────────
set DIAG_SERVER_URL=http://YOUR_SERVER_IP:5000
set DIAG_API_KEY=YOUR_AGENT_API_KEY_HERE
set DIAG_INTERVAL_MIN=10
:: ─────────────────────────────────────────────────────────────────────────────

if "%DIAG_SERVER_URL%"=="http://YOUR_SERVER_IP:5000" (
    echo ERROR: Edit DIAG_SERVER_URL and DIAG_API_KEY in this file first.
    pause
    exit /b 1
)

echo Installing Laptop Diagnostics Agent as scheduled task...
echo Server: %DIAG_SERVER_URL%
echo.

powershell.exe -NonInteractive -ExecutionPolicy Bypass ^
    -File "%~dp0LaptopDiagAgent.ps1" ^
    -Install ^
    -ServerUrl "%DIAG_SERVER_URL%" ^
    -ApiKey "%DIAG_API_KEY%" ^
    -IntervalMinutes %DIAG_INTERVAL_MIN%

echo.
echo Starting agent now...
powershell.exe -Command "Start-ScheduledTask -TaskName 'LaptopDiagnosticsAgent'"

echo.
echo ✅ Done. The agent is now running silently in the background.
echo    It will start automatically every time this PC boots.
echo    Check your dashboard at: %DIAG_SERVER_URL%
echo.
echo    To stop:   sc stop LaptopDiagnosticsAgent
echo    To remove: Run INSTALL_AUTOSTART.bat again (it will offer to uninstall)
echo.
pause
