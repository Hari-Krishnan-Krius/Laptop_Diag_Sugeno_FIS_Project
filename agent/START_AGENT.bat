@echo off
:: ═══════════════════════════════════════════════════════════════════════
:: Laptop Diagnostics Agent — Windows Launcher
::
:: REQUIREMENTS: NOTHING. No Python. No installation. 
:: Uses PowerShell which is built into every Windows machine.
::
:: STEP 1: Edit the two lines below (DIAG_SERVER_URL and DIAG_API_KEY)
:: STEP 2: Double-click this file
:: ═══════════════════════════════════════════════════════════════════════

:: ── SET YOUR VALUES HERE ─────────────────────────────────────────────────────
set DIAG_SERVER_URL=http://YOUR_SERVER_IP:5000
set DIAG_API_KEY=YOUR_AGENT_API_KEY_HERE
:: ─────────────────────────────────────────────────────────────────────────────

:: Optional — uncomment and edit if needed:
:: set DIAG_INTERVAL_MIN=10
:: set DIAG_NAME=Lab Laptop 01
:: set DIAG_EMAIL=alerts@company.com
:: set DIAG_CATEGORY=midrange

:: ── Validation ────────────────────────────────────────────────────────────────
if "%DIAG_SERVER_URL%"=="http://YOUR_SERVER_IP:5000" (
    echo.
    echo  ERROR: You need to edit START_AGENT.bat before running it.
    echo.
    echo  Open this file in Notepad and change:
    echo    DIAG_SERVER_URL  to your server IP  e.g. http://192.168.1.9:5000
    echo    DIAG_API_KEY     to the key from server .env file
    echo.
    pause
    exit /b 1
)

echo.
echo  ╔══════════════════════════════════════════════════════════╗
echo  ║  Laptop Diagnostics Agent                                ║
echo  ║  Laptop-Motherboard-Diagnostics Sugeno FIS v2.1 (Fixed)                  ║
echo  ╚══════════════════════════════════════════════════════════╝
echo.
echo  Server  : %DIAG_SERVER_URL%
echo  Hostname: %COMPUTERNAME%
echo.
echo  Starting agent using PowerShell (no Python needed)...
echo  This laptop will appear in the dashboard within seconds.
echo.
echo  !! IMPORTANT: Keep this window OPEN to stay online !!
echo  !! Close this window to stop monitoring         !!
echo.

powershell.exe -NonInteractive -ExecutionPolicy Bypass ^
    -File "%~dp0LaptopDiagAgent.ps1" ^
    -ServerUrl "%DIAG_SERVER_URL%" ^
    -ApiKey "%DIAG_API_KEY%" ^
    -IntervalMinutes "%DIAG_INTERVAL_MIN%" ^
    -Category "%DIAG_CATEGORY%" ^
    -AlertEmail "%DIAG_EMAIL%" ^
    -DisplayName "%DIAG_NAME%"

echo.
echo Agent stopped. This laptop is now OFFLINE in the dashboard.
pause