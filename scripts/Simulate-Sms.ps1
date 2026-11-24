[CmdletBinding()]
param(
    [string]$Api = 'http://localhost:8000',
    [string]$ApiKey = $env:SERVICEPILOT_API_KEY,
    [string]$Sender = '+12065550101',
    [string[]]$Text,
    [ValidateRange(5, 900)][int]$TimeoutSeconds = 150,
    [ValidateRange(1, 900)][int]$WatchSeconds = 60
)

$ErrorActionPreference = 'Stop'
if (-not $ApiKey) { throw 'Set SERVICEPILOT_API_KEY or pass -ApiKey.' }
$Api = $Api.TrimEnd('/')
$headers = @{ 'X-API-Key' = $ApiKey }
$inboxUrl = "$Api/simulations/sms?sender_phone=$([Uri]::EscapeDataString($Sender))&include_all=true"
$observed = @{}
$script:lastState = ''

function Show-Inbox {
    param([switch]$Baseline, [switch]$History)
    $inbox = Invoke-RestMethod $inboxUrl -Headers $headers
    $state = if (-not $inbox.conversation_id) { 'No conversation for this sender yet.' }
             elseif ($inbox.owner -eq 'human') { 'A staff member owns this conversation. No automatic AI reply is expected.' }
             elseif ($inbox.waiting_for_staff) { 'Waiting for staff.' }
             else { 'Service Advisor is active.' }
    if ($state -ne $script:lastState) {
        Write-Host $state
        $script:lastState = $state
    }
    # Status changes are displayed separately from delivered text. Preserve server
    # order for status rows and display delivered SMS in actual delivery order.
    $outbound = $inbox.outbound | Sort-Object -Stable { [bool]$_.delivered_at }, {
        if ($_.delivered_at) { [DateTimeOffset]$_.delivered_at }
    }
    foreach ($message in $outbound) {
        $status = "$($message.status) $($message.error_code)".Trim()
        if ($Baseline -and $message.status -eq 'delivered') {
            $observed[$message.id] = $status
            continue
        }
        if ($History) {
            if ($message.status -ne 'delivered') { continue }
        } elseif ($observed[$message.id] -eq $status) { continue }
        $observed[$message.id] = $status
        $source = switch ($message.source) {
            'staff' { 'Staff' }
            'advisor_followup' { 'Advisor follow-up' }
            default { 'Advisor' }
        }
        if ($message.status -eq 'delivered') { Write-Host "$source > $($message.body)" }
        Write-Host "Delivery $status | $($message.id)"
    }
    return $inbox
}

function Invoke-SimulatedTurn([string]$Body) {
    $payload = @{
        sender_phone = $Sender
        body = $Body
        external_message_id = [guid]::NewGuid().ToString()
    } | ConvertTo-Json -Compress
    $saved = Invoke-RestMethod "$Api/simulations/sms" -Method Post -Headers $headers -ContentType 'application/json' -Body $payload
    Write-Host "Inbound $($saved.message_id)"
    $inbox = Show-Inbox
    if ($saved.processing -ne 'queued') {
        Write-Host "Processing $($saved.processing)"
        return
    }
    if ($inbox.owner -eq 'human') { return }

    $thread = "$Api/simulations/sms/$($saved.conversation_id)/messages"
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $run = Invoke-RestMethod "$thread/$($saved.message_id)/agent" -Headers $headers
        $inbox = Show-Inbox
        if ($inbox.owner -eq 'human') { return }
        if ($run.status -eq 'failed' -and -not $run.next_attempt_at) {
            throw "Agent attempt $($run.attempts) ended with $($run.error_code)."
        }
        if ($run.status -eq 'completed') {
            if (-not $run.reply) { Write-Host 'No immediate reply.'; return }
            $status = $observed[$run.reply.id]
            if ($status -eq 'delivered') { return }
            if ($status -match '^(failed|undelivered|uncertain)') {
                throw "Simulated delivery ended with $status."
            }
        }
        Start-Sleep -Milliseconds 750
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    throw 'No delivery before the timeout. Check the worker or use /watch for late replies.'
}

Show-Inbox -Baseline | Out-Null
Show-Inbox | Out-Null
if ($Text.Count) {
    foreach ($line in $Text) { Invoke-SimulatedTurn $line }
} else {
    while ($true) {
        $line = Read-Host 'Customer (/refresh, /watch, /history, or Enter to exit)'
        if ([string]::IsNullOrWhiteSpace($line)) { break }
        try {
            switch ($line.Trim()) {
                '/refresh' { Show-Inbox | Out-Null }
                '/history' { Show-Inbox -History | Out-Null }
                '/watch' {
                    $until = [DateTimeOffset]::UtcNow.AddSeconds($WatchSeconds)
                    Write-Host "Watching for $WatchSeconds seconds."
                    do {
                        Show-Inbox | Out-Null
                        if ([DateTimeOffset]::UtcNow -ge $until) { break }
                        Start-Sleep -Seconds 1
                    } while ($true)
                }
                default { Invoke-SimulatedTurn $line }
            }
        } catch {
            Write-Host $_.Exception.Message
        }
    }
}
