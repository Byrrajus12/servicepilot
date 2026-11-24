param(
    [Parameter(Mandatory)] [string] $Dealership,
    [Parameter(Mandatory)] [string] $Username,
    [string] $BaseUrl = 'http://127.0.0.1:8000'
)

$ErrorActionPreference = 'Stop'
$base = $BaseUrl.TrimEnd('/') + '/staff'
$password = Read-Host -MaskInput 'Employee password'
$login = Invoke-RestMethod -Method Post -Uri "$base/sessions" -ContentType 'application/json' -Body (
    @{ dealership = $Dealership; username = $Username; password = $password } | ConvertTo-Json
)
Remove-Variable password
$headers = @{ Authorization = "Bearer $($login.access_token)" }

function Invoke-Staff {
    param([string] $Path, [string] $Method = 'Get', [hashtable] $Body)
    $request = @{ Uri = "$base$Path"; Method = $Method; Headers = $headers }
    if ($null -ne $Body) {
        $request.ContentType = 'application/json'
        $request.Body = $Body | ConvertTo-Json
    }
    Invoke-RestMethod @request
}

function Select-StaffItem {
    param([string] $Path, [string] $Label, [scriptblock] $Show)
    $pageSize = 10
    $offset = 0
    $separator = if ($Path.Contains('?')) { '&' } else { '?' }
    while ($true) {
        $items = @(foreach ($row in (Invoke-Staff "$Path${separator}limit=$pageSize&offset=$offset")) {
            if ($null -ne $row) { $row }
        })
        if (-not $items.Count) {
            if ($offset -eq 0) { Write-Host "No $Label found."; return }
            Write-Host "No more $Label."
            $offset -= $pageSize
            continue
        }
        for ($index = 0; $index -lt $items.Count; $index++) {
            & $Show ($index + 1) $items[$index]
        }
        $choice = Read-Host 'Select a number, n for next page, p for previous, or Enter to cancel'
        if ([string]::IsNullOrWhiteSpace($choice)) { return }
        if ($choice -eq 'n' -and $items.Count -eq $pageSize) { $offset += $pageSize; continue }
        if ($choice -eq 'p' -and $offset -gt 0) { $offset -= $pageSize; continue }
        $number = 0
        if ([int]::TryParse($choice, [ref] $number) -and $number -ge 1 -and $number -le $items.Count) {
            return $items[$number - 1]
        }
        Write-Host 'Use a number on the current page or an available page command.'
    }
}

function Select-StaffThread {
    Select-StaffItem '/conversations' 'conversations' {
        param($Number, $Item)
        $detail = Invoke-Staff "/conversations/$($Item.id)"
        $customer = $detail.context.customer.name
        if (-not $customer) { $customer = 'Unresolved customer' }
        $vehicle = @($detail.context.vehicles | ForEach-Object { "$($_.model_year) $($_.make) $($_.model)" }) -join ', '
        $recent = @(Invoke-Staff "/conversations/$($Item.id)/messages?limit=1")
        $preview = ([string] $recent[0].body) -replace '\s+', ' '
        if ($preview.Length -gt 70) { $preview = $preview.Substring(0, 67) + '...' }
        $assigned = if (-not $Item.assigned_employee_id) { 'unassigned' }
                    elseif ($Item.assigned_employee_id -eq $me.id) { 'assigned to you' }
                    else { 'assigned to another employee' }
        $waiting = if ($Item.waiting_for_staff) { ' | waiting for staff' } else { '' }
        Write-Host "$Number. $customer $vehicle | $($Item.owner) | $assigned$waiting"
        if ($preview) { Write-Host "   $($recent[0].direction) - $preview" }
    }
}

try {
    $me = Invoke-Staff '/me'
    $selected = $null
    $selectedConsultation = $null
    while ($true) {
        if ($selected) { Write-Host "Selected conversation $($selected.label)" }
        if ($selectedConsultation) { Write-Host "Selected consultation $($selectedConsultation.question)" }
        Write-Host 'inbox  messages  consultations  answer  note  takeover  reply  handback  quit'
        $action = (Read-Host 'Staff command').Trim().ToLowerInvariant()
        if ($action -in @('', 'quit')) { break }
        try {
            if ($action -in @('consultations', 'answer')) {
                if ($action -eq 'consultations' -or -not $selectedConsultation) {
                    $item = Select-StaffItem '/consultations?status=open' 'open consultations' {
                        param($Number, $Item)
                        $detail = Invoke-Staff "/conversations/$($Item.conversation_id)"
                        $customer = $detail.context.customer.name
                        if (-not $customer) { $customer = 'Unresolved customer' }
                        Write-Host "$Number. $customer | $($Item.question)"
                    }
                    if (-not $item) { continue }
                    $detail = Invoke-Staff "/conversations/$($item.conversation_id)"
                    $label = if ($detail.context.customer.name) { $detail.context.customer.name } else { 'Unresolved customer' }
                    $selected = @{ id = $item.conversation_id; label = $label }
                    $selectedConsultation = $item
                }
                if ($action -eq 'answer') {
                    $item = $selectedConsultation
                    Write-Host "Answering $($item.question)"
                    $fact = Read-Host 'Fact approved for the customer (Enter to cancel)'
                    if (-not [string]::IsNullOrWhiteSpace($fact)) {
                        $answer = Invoke-Staff "/conversations/$($item.conversation_id)/consultations/$($item.id)/answer" Put @{
                            public_fact = $fact
                        }
                        $selectedConsultation = $null
                        Write-Host "Consultation $($answer.status)."
                    }
                }
                continue
            }
            if ($action -notin @('inbox', 'messages', 'note', 'takeover', 'reply', 'handback')) {
                Write-Host 'Choose a command from the menu.'
                continue
            }
            if ($action -eq 'inbox' -or -not $selected) {
                $item = Select-StaffThread
                if (-not $item) { continue }
                $detail = Invoke-Staff "/conversations/$($item.id)"
                $label = if ($detail.context.customer.name) { $detail.context.customer.name } else { 'Unresolved customer' }
                $selected = @{ id = $item.id; label = $label }
                if ($selectedConsultation.conversation_id -ne $item.id) { $selectedConsultation = $null }
            }
            $thread = "/conversations/$($selected.id)"
            switch ($action) {
                'messages' { Invoke-Staff "$thread/messages" | Format-List occurred_at, direction, delivery_status, body }
                'note' {
                    $note = Read-Host 'Private note (Enter to cancel)'
                    if (-not [string]::IsNullOrWhiteSpace($note)) {
                        Invoke-Staff "$thread/notes" Post @{ body = $note } | Out-Null
                        Write-Host 'Private note saved.'
                    }
                }
                'reply' {
                    $sms = Read-Host 'Customer SMS (Enter to cancel)'
                    if (-not [string]::IsNullOrWhiteSpace($sms)) {
                        Invoke-Staff "$thread/replies" Post @{ body = $sms; request_id = [guid]::NewGuid() } |
                            Format-List body
                        Write-Host 'Reply queued. Use messages to check delivery.'
                    }
                }
                { $_ -in @('takeover', 'handback') } {
                    $owner = if ($action -eq 'takeover') { 'human' } else { 'ai' }
                    $current = Invoke-Staff $thread
                    Invoke-Staff "$thread/ownership/$owner" Put @{ expected_version = $current.ownership_version } |
                        Format-List owner, ownership_version
                    if ($owner -eq 'human') { $selectedConsultation = $null }
                }
            }
        } catch {
            $status = [int] $_.Exception.Response.StatusCode
            $detail = if ($_.ErrorDetails.Message) { $_.ErrorDetails.Message } else { $_.Exception.Message }
            if ($action -eq 'answer' -and $selectedConsultation -and $status -in @(403, 404, 409)) {
                $selectedConsultation = $null
                Write-Host "Selected consultation is unavailable. Use consultations to choose another. $detail"
                continue
            }
            switch ($status) {
                401 { Write-Host 'Session expired. Restart the simulator to sign in.'; return }
                404 { $selected = $null; $selectedConsultation = $null; Write-Host 'Record unavailable. Refresh the inbox or consultations.' }
                409 { Write-Host "The record may have changed. Refresh and retry. $detail" }
                default { Write-Host $detail }
            }
        }
    }
} finally {
    try { Invoke-Staff '/sessions/current' Delete | Out-Null } catch { Write-Verbose 'Session already unavailable.' }
}
