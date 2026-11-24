[CmdletBinding()]
param(
    [string]$Api = 'http://localhost:8000',
    [string]$ApiKey = $env:SERVICEPILOT_API_KEY,
    [string]$Sender = '+12065550101',
    [string[]]$Text,
    [ValidateRange(5, 900)][int]$TimeoutSeconds = 150
)

$ErrorActionPreference = 'Stop'
if (-not $ApiKey) { throw 'Set SERVICEPILOT_API_KEY or pass -ApiKey.' }
$Api = $Api.TrimEnd('/')
$headers = @{ 'X-API-Key' = $ApiKey }

function Invoke-SimulatedTurn([string]$Body) {
    $payload = @{
        sender_phone = $Sender
        body = $Body
        external_message_id = [guid]::NewGuid().ToString()
    } | ConvertTo-Json -Compress
    $saved = Invoke-RestMethod "$Api/simulations/sms" -Method Post -Headers $headers -ContentType 'application/json' -Body $payload
    Write-Host "Conversation $($saved.conversation_id) | inbound $($saved.message_id)"
    if ($saved.processing -ne 'queued') {
        Write-Host "Processing $($saved.processing)"
        return
    }

    $thread = "$Api/simulations/sms/$($saved.conversation_id)/messages"
    $deadline = [DateTimeOffset]::UtcNow.AddSeconds($TimeoutSeconds)
    do {
        $run = Invoke-RestMethod "$thread/$($saved.message_id)/agent" -Headers $headers
        if ($run.status -eq 'completed') { break }
        if ($run.status -eq 'failed' -and -not $run.next_attempt_at) {
            throw "Agent attempt $($run.attempts) ended with $($run.error_code)."
        }
        Start-Sleep -Milliseconds 500
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    if ($run.status -ne 'completed') { throw 'Timed out waiting for the agent. Check the worker and poll the inbound ID above.' }

    Write-Host "Advisor $($run.reply.body)"
    do {
        $delivery = Invoke-RestMethod "$thread/$($run.reply.id)/delivery" -Headers $headers
        if ($delivery.status -notin @('pending', 'sending')) { break }
        Start-Sleep -Milliseconds 500
    } while ([DateTimeOffset]::UtcNow -lt $deadline)
    Write-Host "Delivery $($delivery.status) | attempt $($delivery.attempts) | outbound $($delivery.id)"
    if ($delivery.status -ne 'delivered') {
        throw "Simulated delivery ended at $($delivery.status) ($($delivery.error_code)). Check the outbound ID above."
    }
}

if ($Text.Count) {
    foreach ($line in $Text) { Invoke-SimulatedTurn $line }
} else {
    while ($true) {
        $line = Read-Host 'Customer (empty line to exit)'
        if (-not $line.Trim()) { break }
        Invoke-SimulatedTurn $line
    }
}
