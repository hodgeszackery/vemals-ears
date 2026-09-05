# Configure only the VE caption worker. Existing relay state stays in place.
# Run -CheckOnly for a read-only source/dependency preflight.
[CmdletBinding()]
param(
    [string]$PythonPath,
    [string]$NssmPath,
    [string]$StateDirectory = (Join-Path $env:ProgramData 'VemalsEars'),
    [string]$UserLocalAppData = $env:LOCALAPPDATA,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot))
$required = @('Vemals-Ears.pyw', 'GuildHub/worker_launcher.py',
    'VoiceCaptions/voice_captions.py', 'VoiceCaptions/discord_adapter/package-lock.json')
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $relative) -PathType Leaf)) {
        throw "Incomplete VE checkout: $relative"
    }
}
if (-not $PythonPath) {
    $candidate = Get-ChildItem -LiteralPath (Join-Path $env:LOCALAPPDATA 'Python') `
        -Recurse -Filter python.exe -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($candidate) { $PythonPath = $candidate.FullName }
    else { $PythonPath = (Get-Command python -ErrorAction SilentlyContinue).Source }
}
if (-not $PythonPath -or $PythonPath -match 'WindowsApps' -or
    -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Pass -PythonPath with a real python.exe, not a WindowsApps shim.'
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
if (-not [IO.Path]::IsPathRooted($StateDirectory) -or
    -not [IO.Path]::IsPathRooted($UserLocalAppData)) {
    throw 'StateDirectory and UserLocalAppData must be absolute paths.'
}
$StateDirectory = [IO.Path]::GetFullPath($StateDirectory)
$UserLocalAppData = [IO.Path]::GetFullPath($UserLocalAppData)

# Pass the checkout as argv, never interpolate it into Python source.
& $PythonPath -I -B -c 'import sys; sys.path.insert(0, sys.argv[1]); import websockets; from GuildHub import worker_launcher' $repoRoot
if ($LASTEXITCODE -ne 0) { throw 'VE worker Python imports failed.' }
$nodePath = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $nodePath) { throw 'Node.js is required for the Discord adapter.' }
$adapterRoot = Join-Path $repoRoot 'VoiceCaptions/discord_adapter'
Push-Location -LiteralPath $adapterRoot
try {
    & $nodePath --input-type=module -e "const [major, minor] = process.versions.node.split('.').map(Number); if (major < 22 || (major === 22 && minor < 12)) { console.error('Node.js 22.12 or newer is required.'); process.exit(1); } await import('opusscript'); await import('@discordjs/voice'); await import('discord.js');"
    if ($LASTEXITCODE -ne 0) { throw 'Run npm ci in VoiceCaptions/discord_adapter first.' }
} finally { Pop-Location }
if ($CheckOnly) {
    Write-Host "VE worker source and dependencies verified: $repoRoot"
    return
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Service configuration requires an elevated PowerShell.'
}
if (-not $NssmPath) { $NssmPath = (Get-Command nssm -ErrorAction SilentlyContinue).Source }
if (-not $NssmPath -or -not (Test-Path -LiteralPath $NssmPath -PathType Leaf)) {
    throw 'Pass -NssmPath with the installed nssm.exe.'
}
if (-not (Test-Path -LiteralPath (Join-Path $StateDirectory 'relay.env') -PathType Leaf)) {
    throw 'Shared relay state is missing. Configure the guild host first.'
}
$serviceName = 'VemalsEarsWorker'
$logs = Join-Path $StateDirectory 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    & $NssmPath @Arguments | Out-Null
    if ($LASTEXITCODE -ne 0) { throw "NSSM $($Arguments[0]) failed for the VE worker." }
}
$existing = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
if ($existing -and $existing.Status -ne 'Stopped') {
    Stop-Service -Name $serviceName -ErrorAction Stop
    (Get-Service -Name $serviceName).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
}
if (-not $existing) { Invoke-Nssm install $serviceName $PythonPath }
Invoke-Nssm set $serviceName Application $PythonPath
Invoke-Nssm set $serviceName AppParameters '-m GuildHub.worker_launcher'
Invoke-Nssm set $serviceName AppDirectory $repoRoot
Invoke-Nssm set $serviceName AppStdout (Join-Path $logs 'worker.log')
Invoke-Nssm set $serviceName AppStderr (Join-Path $logs 'worker.log')
Invoke-Nssm set $serviceName AppRotateFiles 1
Invoke-Nssm set $serviceName AppRotateBytes 1048576
Invoke-Nssm set $serviceName AppExit Default Restart
Invoke-Nssm set $serviceName AppExit 3 Exit
Invoke-Nssm set $serviceName AppRestartDelay 30000
Invoke-Nssm set $serviceName Start SERVICE_AUTO_START
Invoke-Nssm set $serviceName DisplayName "Vemal's Ears Caption Worker"
Invoke-Nssm set $serviceName Description 'Vemal''s Ears - consent-gated Discord captions and speech recognition.'
Invoke-Nssm set $serviceName AppEnvironmentExtra "VEMALS_EARS_STATE_DIR=$StateDirectory" "LOCALAPPDATA=$UserLocalAppData"
$actualDirectory = (& $NssmPath get $serviceName AppDirectory | Out-String).Trim()
if ($LASTEXITCODE -ne 0 -or [IO.Path]::GetFullPath($actualDirectory) -ne $repoRoot) {
    throw 'Worker source registration did not match the VE checkout; service remains stopped.'
}
Start-Service -Name $serviceName -ErrorAction Stop
(Get-Service -Name $serviceName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
Write-Host "Configured $serviceName from $repoRoot. Verify worker health and live captions before promoting Downtime."
