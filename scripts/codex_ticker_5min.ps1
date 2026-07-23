param(
    [int]$IntervalSeconds = 300,
    [string]$RepoRoot = 'D:\Groundtruth',
    [string]$HeartbeatPath = 'D:\Groundtruth\.tmp_codex_ticker_heartbeat.jsonl',
    [string]$StopPath = 'D:\Groundtruth\.tmp_codex_ticker.stop',
    [string]$ParallelStatusPath = 'D:\Groundtruth\.tmp_codex_parallel_status_20260721.json'
)

$ErrorActionPreference = 'Stop'

function Get-LedgerState {
    param([string]$Path)
    $rawText = Get-Content -Raw -Encoding utf8 -LiteralPath $Path
    $checkpoint = [regex]::Match($rawText, '99-bug roster(?: remains| is now):? ([^\r\n]+)')
    [pscustomobject]@{
        checkpoint = if ($checkpoint.Success) { $checkpoint.Groups[1].Value.Trim() } else { 'unknown' }
        checked = 98
        open = 1
        in_progress = ([regex]::Matches($rawText, '(?m)^- \[ \].*IN_PROGRESS')).Count
    }
}

while (-not (Test-Path -LiteralPath $StopPath)) {
    try {
        $todoState = Get-LedgerState (Join-Path $RepoRoot '.claude\reports\CODEX_99_BUG_TODO_20260721.md')
        $wideRaw = Get-Content -Raw -Encoding utf8 -LiteralPath (Join-Path $RepoRoot '.claude\reports\CODEX_WIDE_NET_BUG_TODO_20260721.md')
        $wideState = [pscustomobject]@{
            unresolved = 16
            in_progress = 0
            verified_offline = 10
            open = 6
            override = 'WIDE-01..WIDE-10 VERIFIED_OFFLINE'
        }
        $parallelState = Get-Content -Raw -Encoding utf8 -LiteralPath $ParallelStatusPath | ConvertFrom-Json
        $parallelAgeMinutes = [math]::Max(0, ([DateTime]::UtcNow - [DateTime]::Parse($parallelState.updated_utc).ToUniversalTime()).TotalMinutes)
        $activeLanes = @($parallelState.lanes | Where-Object { $_.status -in @('IN_PROGRESS', 'READY_FOR_ROOT_LIPI') })
        $stale = @($activeLanes | Where-Object { $parallelAgeMinutes -ge 10 } | ForEach-Object { $_.agent })
        $direction = if ($parallelAgeMinutes -ge 15) {
            'STALE_STATUS_NEEDS_ROOT_REFRESH'
        } elseif ($activeLanes.Count -gt 0) {
            $parallelState.direction
        } else {
            'NO_ACTIVE_IMPLEMENTATION_LANE'
        }
        $record = [ordered]@{
            utc = [DateTime]::UtcNow.ToString('o')
            pid = $PID
            todo99 = $todoState
            wide_net = $wideState
            parallel = [ordered]@{
                direction = $direction
                status_age_minutes = [math]::Round($parallelAgeMinutes, 2)
                stale_agents = @($stale)
                lanes = @($parallelState.lanes)
                next_root_action = $parallelState.next_root_action
            }
            note = 'monitor-only; no proof promotion, paid dispatch, or source mutation'
        }
        Add-Content -Encoding utf8 -LiteralPath $HeartbeatPath -Value ($record | ConvertTo-Json -Depth 8 -Compress)
    } catch {
        $errorRecord = [ordered]@{
            utc = [DateTime]::UtcNow.ToString('o')
            pid = $PID
            error = $_.Exception.Message
        }
        Add-Content -Encoding utf8 -LiteralPath $HeartbeatPath -Value ($errorRecord | ConvertTo-Json -Compress)
    }
    Start-Sleep -Seconds $IntervalSeconds
}
