param(
    [Parameter(Mandatory = $true)][string]$Database,
    [string]$Reports,
    [string]$TaskName,
    [switch]$ReplaceExisting,
    [switch]$Uninstall
)
$ErrorActionPreference = 'Stop'
$pmRoot = $PSScriptRoot
$pmDb = (Resolve-Path -LiteralPath $Database).Path
if (-not $Reports) { $Reports = Join-Path (Split-Path -Parent $pmDb) 'weekly_reports' }
$pmReportPath = [System.IO.Path]::GetFullPath($Reports)
if (-not $TaskName) {
    $pmHasher = [System.Security.Cryptography.SHA256]::Create()
    try { $pmDigest = $pmHasher.ComputeHash([System.Text.Encoding]::UTF8.GetBytes($pmDb.ToLowerInvariant())) }
    finally { $pmHasher.Dispose() }
    $pmSuffix = ([BitConverter]::ToString($pmDigest)).Replace('-', '').Substring(0, 10)
    $TaskName = 'pmmaster-' + $pmSuffix
}
if ($Uninstall) {
    Unregister-ScheduledTask -TaskName $TaskName -Confirm:$false -ErrorAction Stop
    Write-Host "Removed task: $TaskName"
    Write-Host 'Database weekly-config and archive-config settings are unchanged.'
    exit 0
}
if ((Get-ScheduledTask -TaskName $TaskName -ErrorAction SilentlyContinue) -and -not $ReplaceExisting) {
    throw "Task already exists: $TaskName. Use -ReplaceExisting only to update this report task."
}
$pmVirtualPython = Join-Path $pmRoot '.venv\Scripts\python.exe'
if (Test-Path -LiteralPath $pmVirtualPython) {
    $pmPython = $pmVirtualPython
    $pmPrefix = @()
} else {
    $pmLauncher = Get-Command py.exe -ErrorAction SilentlyContinue
    if ($pmLauncher) { $pmPython = $pmLauncher.Source; $pmPrefix = @('-3') }
    else { $pmPython = (Get-Command python.exe -ErrorAction Stop).Source; $pmPrefix = @() }
}
& $pmPython @pmPrefix -c "import sys; assert sys.version_info >= (3, 11); import openpyxl, tzdata"
if ($LASTEXITCODE -ne 0) { throw 'Install Python 3.11+ and requirements.txt first.' }
# Paths are individual quoted Windows process arguments, not executed by cmd.exe.
foreach ($pmPathValue in @($pmRoot, $pmDb, $pmReportPath)) {
    if ($pmPathValue.Contains('"')) { throw 'Paths containing double quotes are not supported.' }
}
$pmArgumentParts = @()
$pmArgumentParts += $pmPrefix
$pmArgumentParts += @('-m', 'pm_core.cli', 'weekly-tick', '--db', ('"' + $pmDb + '"'), '--out', ('"' + $pmReportPath + '"'))
$pmAction = New-ScheduledTaskAction -Execute $pmPython -Argument ($pmArgumentParts -join ' ') -WorkingDirectory $pmRoot
# A lightweight tick runs each minute; Python applies the configured IANA timezone
# and the Monday/Friday report times plus Sunday Excel archive time.
$pmTrigger = New-ScheduledTaskTrigger -Once -At (Get-Date).AddMinutes(1) -RepetitionInterval (New-TimeSpan -Minutes 1)
$pmSettings = New-ScheduledTaskSettingsSet -StartWhenAvailable -MultipleInstances IgnoreNew -ExecutionTimeLimit (New-TimeSpan -Minutes 10) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
$pmUser = [System.Security.Principal.WindowsIdentity]::GetCurrent().Name
$pmPrincipal = New-ScheduledTaskPrincipal -UserId $pmUser -LogonType Interactive -RunLevel Limited
$pmTask = New-ScheduledTask -Action $pmAction -Trigger $pmTrigger -Settings $pmSettings -Principal $pmPrincipal -Description 'pmmaster weekly reports and Sunday Excel archive.'
Register-ScheduledTask -TaskName $TaskName -InputObject $pmTask -Force:$ReplaceExisting | Out-Null
Push-Location $pmRoot
try {
    & $pmPython @pmPrefix -m pm_core.cli weekly-config --db $pmDb --enable
    if ($LASTEXITCODE -ne 0) { throw 'Task registered, but schedule enable failed. Review weekly-config before using it.' }
    & $pmPython @pmPrefix -m pm_core.cli archive-config --db $pmDb --enable
    if ($LASTEXITCODE -ne 0) { throw 'Task registered, but Sunday archive enable failed. Review archive-config.' }
} finally { Pop-Location }
Write-Host "Registered task: $TaskName"
Write-Host "Database: $pmDb"
Write-Host "Reports: $pmReportPath"
Write-Host 'Runs while this Windows user is signed in and the computer is awake. No password is stored.'
Write-Host 'Set report times with weekly-config and archive timezone/source with archive-config.'
Write-Host 'To remove this task, run this script with the same Database and -Uninstall.'
