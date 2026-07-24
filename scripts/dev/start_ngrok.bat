@echo off
REM Start ngrok tunnel to nginx (port 80), write the public URL into .env,
REM and bounce the services that read it so SignalWire webhooks resolve.
REM
REM Requires: ngrok on PATH, PowerShell, docker-compose, ngrok authtoken configured
REM           (`ngrok config add-authtoken <token>` once per machine).
REM
REM The ngrok process runs in a new window so this script can exit. Close that
REM window to kill the tunnel.

setlocal

REM Operate from the repo root (this script lives in scripts/dev/), so .env
REM and the compose files resolve correctly regardless of launch directory.
cd /d "%~dp0..\.."

REM === 1. Launch ngrok in a persistent window ==============================
echo Launching ngrok on port 80...
start "ngrok http 80" cmd /k ngrok http 80

REM === 2. Wait for ngrok's local API to come up ============================
REM Using `ping` instead of `timeout` so this works from non-interactive
REM hosts (CI, Claude's shell) where timeout errors on input redirection.
echo Waiting for ngrok to initialize...
ping -n 5 127.0.0.1 >nul

REM === 3. Pull the public URL from ngrok's local API =======================
for /f "delims=" %%i in ('powershell -NoProfile -Command "try { (Invoke-RestMethod http://localhost:4040/api/tunnels -TimeoutSec 5).tunnels[0].public_url } catch { '' }"') do set NGROK_URL=%%i

if "%NGROK_URL%"=="" (
    echo.
    echo ERROR: Could not fetch ngrok URL from http://localhost:4040/api/tunnels
    echo Check the ngrok window for errors (auth, rate limit, etc.)
    exit /b 1
)

echo.
echo Tunnel: %NGROK_URL%

REM === 4. Update .env in-place =============================================
REM Line-anchored regex so we only touch the EXTERNAL_URL / AGENT_BASE_URL lines,
REM leaving other values (and any duplicates elsewhere) untouched.
powershell -NoProfile -Command ^
  "$u='%NGROK_URL%';" ^
  "$p=Join-Path (Get-Location) '.env';" ^
  "if (-not (Test-Path $p)) { Write-Host 'ERROR: .env not found'; exit 1 };" ^
  "(Get-Content $p) -replace '^EXTERNAL_URL=.*', ('EXTERNAL_URL=' + $u) -replace '^AGENT_BASE_URL=.*', ('AGENT_BASE_URL=' + $u) | Set-Content $p -Encoding ascii"

if errorlevel 1 exit /b 1

echo Updated .env:
findstr /b "EXTERNAL_URL= AGENT_BASE_URL=" .env

REM === 5. Recreate containers so the new env takes effect ==================
REM `restart` does NOT re-read env from .env; `up -d` will recreate changed ones.
echo.
REM nginx is the tunnel target (ngrok -> :80 -> nginx -> /api backend), so
REM make sure it's up alongside the services that read the new URL. If a
REM local COMPOSE_FILE overlay is set in .env (e.g. the hosted-demo override),
REM docker-compose picks it up automatically — no flags needed here.
echo Recreating nginx + backend + ai-agents with new URL...
docker-compose up -d nginx backend ai-agents

echo.
echo Done. App at http://localhost/ ^| Webhooks at %NGROK_URL%
endlocal
