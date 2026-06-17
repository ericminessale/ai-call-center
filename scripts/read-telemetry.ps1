<#
.SYNOPSIS
    Inspect captured SignalWire AI agent telemetry from a test call.

.DESCRIPTION
    Reads the capture files written by the backend webhook handlers
    (backend/captures/) and prints:
      1. A turn-by-turn timeline of debug-level-2 events (debug-events.jsonl)
      2. Pretty-printed detail for the "interesting" events (step/context
         changes, fillers, errors, session lifecycle) — the ones that explain
         a "skips to the final step + spams fillers" symptom
      3. The most recent post-prompt summary (postprompt-latest.json)

    Requires DEBUG_WEBHOOK_ENABLED=true in .env and the agents restarted.

.PARAMETER Clear
    Delete the capture files BEFORE you place a fresh test call, so the next
    call's telemetry is isolated. Run with -Clear, place the call, then run
    again with no args to read it.

.PARAMETER Full
    Also dump high-volume events (llm_request / llm_response / conversation_add)
    in the detail section. Off by default to keep the output readable.

.EXAMPLE
    .\scripts\read-telemetry.ps1 -Clear      # wipe, then place your call
    .\scripts\read-telemetry.ps1             # read what the call produced
    .\scripts\read-telemetry.ps1 -Full       # include LLM request/response
#>
[CmdletBinding()]
param(
    [switch]$Clear,
    [switch]$Full
)

$ErrorActionPreference = 'Stop'
$capDir   = Join-Path $PSScriptRoot '..\backend\captures'
$debugLog = Join-Path $capDir 'debug-events.jsonl'
$ppLatest = Join-Path $capDir 'postprompt-latest.json'
$ppLog    = Join-Path $capDir 'postprompt.jsonl'

# Events that are pure volume at level 2 — summarised in the timeline but only
# shown in full when -Full is passed.
$noisy = @('llm_request', 'llm_response', 'conversation_add')

if ($Clear) {
    foreach ($f in @($debugLog, $ppLatest, $ppLog)) {
        if (Test-Path $f) { Remove-Item $f -Force }
    }
    Write-Host "Cleared capture files in $capDir" -ForegroundColor Yellow
    Write-Host "Now place your test call, then re-run this script with no args." -ForegroundColor Yellow
    return
}

if (-not (Test-Path $capDir)) {
    Write-Host "No captures directory yet: $capDir" -ForegroundColor Red
    Write-Host "Set DEBUG_WEBHOOK_ENABLED=true, restart, and place a call first." -ForegroundColor Red
    return
}

function Get-EventType($p) { if ($p.label) { $p.label } elseif ($p.action) { $p.action } else { 'unknown' } }

# --- 1 + 2: debug events ----------------------------------------------------
if (Test-Path $debugLog) {
    $events = Get-Content $debugLog | Where-Object { $_.Trim() } | ForEach-Object {
        try { $_ | ConvertFrom-Json } catch { $null }
    } | Where-Object { $_ }

    Write-Host "`n=== DEBUG EVENT TIMELINE ($($events.Count) events) ===" -ForegroundColor Cyan
    $i = 0
    foreach ($e in $events) {
        $i++
        $p = $e.payload
        $type = Get-EventType $p
        $ts = ([string]$e.captured_at)
        $color = switch -Regex ($type) {
            'error|warn'          { 'Red' }
            'step|context'        { 'Green' }
            'filler'              { 'Magenta' }
            'session'             { 'Cyan' }
            default               { 'Gray' }
        }
        Write-Host ("{0,3}. {1}  {2}" -f $i, $ts, $type) -ForegroundColor $color
    }

    Write-Host "`n=== INTERESTING EVENT DETAIL ===" -ForegroundColor Cyan
    foreach ($e in $events) {
        $p = $e.payload
        $type = Get-EventType $p
        if (-not $Full -and ($noisy -contains $type)) { continue }
        Write-Host "`n--- $type @ $($e.captured_at) ---" -ForegroundColor DarkCyan
        $p | ConvertTo-Json -Depth 8
    }
} else {
    Write-Host "`nNo debug-events.jsonl yet." -ForegroundColor Yellow
    Write-Host "Check: DEBUG_WEBHOOK_ENABLED=true, agents restarted, and the" -ForegroundColor Yellow
    Write-Host "agent log printed 'Debug webhook enabled (level 2) -> ...'." -ForegroundColor Yellow
}

# --- 3: post-prompt ---------------------------------------------------------
Write-Host "`n=== MOST RECENT POST-PROMPT ===" -ForegroundColor Cyan
if (Test-Path $ppLatest) {
    Get-Content $ppLatest -Raw
} else {
    Write-Host "No postprompt-latest.json yet (post-prompt fires when the AI" -ForegroundColor Yellow
    Write-Host "session ends — e.g. on transfer or hangup)." -ForegroundColor Yellow
}
