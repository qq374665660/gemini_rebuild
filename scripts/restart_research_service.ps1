param(
    [string]$ComputerName = "192.168.0.50",
    [string]$ServiceName = "ResearchManagementService",
    [int]$WaitSeconds = 3,
    [switch]$DryRun
)

$ErrorActionPreference = "Stop"

function Show-SimilarServices {
    param([string]$TargetComputer)
    try {
        $similar = Get-Service -ComputerName $TargetComputer |
            Where-Object {
                $_.Name -match "research|django|project|gemini|waitress|wenxing" -or
                $_.DisplayName -match "Research|Django|项目|管理|课题|Gemini"
            } |
            Select-Object Name, DisplayName, Status |
            Sort-Object Name

        if ($similar) {
            Write-Host ""
            Write-Host "Similar services on ${TargetComputer}:"
            $similar | Format-Table -AutoSize
        }
    } catch {
        Write-Host "Unable to query similar services: $($_.Exception.Message)"
    }
}

try {
    $svcBefore = Get-Service -ComputerName $ComputerName -Name $ServiceName -ErrorAction Stop
} catch {
    Write-Host "Service '$ServiceName' not found on $ComputerName."
    Show-SimilarServices -TargetComputer $ComputerName
    exit 1
}

Write-Host "Target computer : $ComputerName"
Write-Host "Service name    : $ServiceName"
Write-Host "Before status   : $($svcBefore.Status)"

if ($DryRun) {
    Write-Host "[DRY-RUN] Skip restart."
    exit 0
}

if ($svcBefore.Status -eq "Stopped") {
    Start-Service -InputObject $svcBefore
} else {
    Restart-Service -InputObject $svcBefore -Force
}

Start-Sleep -Seconds ([Math]::Max($WaitSeconds, 1))
$svcAfter = Get-Service -ComputerName $ComputerName -Name $ServiceName -ErrorAction Stop

Write-Host "After status    : $($svcAfter.Status)"

if ($svcAfter.Status -ne "Running") {
    Write-Host "Restart finished, but service is not Running."
    exit 2
}

Write-Host "Restart completed successfully."
