# Install the standalone relay and NVIDIA caption worker as Windows services.
# -CheckOnly validates local prerequisites without changing services or files.
[CmdletBinding()]
param(
    [Parameter(Mandatory=$true)][string]$StateDirectory,
    [Parameter(Mandatory=$true)][string]$PythonPath,
    [string]$NssmPath,
    [switch]$CheckOnly
)

$ErrorActionPreference = 'Stop'
$repoRoot = [IO.Path]::GetFullPath((Split-Path $PSScriptRoot))
$required = @('Hosting/service.py', 'GuildHub/server.py', 'GuildHub/host.py',
    'VoiceCaptions/voice_captions.py', 'VoiceCaptions/discord_adapter/package-lock.json')
foreach ($relative in $required) {
    if (-not (Test-Path -LiteralPath (Join-Path $repoRoot $relative) -PathType Leaf)) {
        throw "Incomplete host package: $relative"
    }
}
if (-not [IO.Path]::IsPathRooted($StateDirectory) -or
    $StateDirectory.IndexOf('"') -ge 0 -or $StateDirectory -match '[\x00-\x1f]') {
    throw 'StateDirectory must be an absolute path without quotes or control characters.'
}
$StateDirectory = [IO.Path]::GetFullPath($StateDirectory).TrimEnd('\', '/')
if ($StateDirectory.Length -le 3) { throw 'Choose a dedicated host state directory.' }
if (-not [IO.Path]::IsPathRooted($PythonPath) -or $PythonPath -match 'WindowsApps' -or
    -not (Test-Path -LiteralPath $PythonPath -PathType Leaf)) {
    throw 'Pass -PythonPath with an absolute real python.exe, not a WindowsApps shim.'
}
$PythonPath = (Resolve-Path -LiteralPath $PythonPath).Path
$nodePath = (Get-Command node -ErrorAction SilentlyContinue).Source
if (-not $nodePath) { throw 'Install Node.js 22.12 or newer first.' }

& $PythonPath -I -B -c "import sys; sys.path.insert(0, sys.argv[1]); from GuildHub import server, host; from Hosting.service import main; sys.exit(main(['--state-directory', sys.argv[2], '--check-only', 'relay']))" $repoRoot $StateDirectory
if ($LASTEXITCODE -ne 0) { throw 'Host source, Python dependencies, or configuration preflight failed.' }
& $PythonPath -I -B -c "import sys; sys.path.insert(0, sys.argv[1]); from Hosting.service import main; sys.exit(main(['--state-directory', sys.argv[2], '--check-only', 'worker']))" $repoRoot $StateDirectory
if ($LASTEXITCODE -ne 0) { throw 'NVIDIA speech runtime preflight failed. Run host setup prepare first.' }
$runningRelay = Get-Service -Name VemalsEarsHostRelay -ErrorAction SilentlyContinue
if (-not $runningRelay -or $runningRelay.Status -ne 'Running') {
    & $PythonPath -I -B -c "import sys; sys.path.insert(0, sys.argv[1]); from Hosting.service import main; sys.exit(main(['--state-directory', sys.argv[2], '--check-only', '--check-port', 'relay']))" $repoRoot $StateDirectory
    if ($LASTEXITCODE -ne 0) { throw 'The relay port is unavailable. No services changed.' }
}
Push-Location -LiteralPath (Join-Path $repoRoot 'VoiceCaptions/discord_adapter')
try {
    & $nodePath --input-type=module -e "const [major, minor] = process.versions.node.split('.').map(Number); if (major < 22 || (major === 22 && minor < 12)) { console.error('Node.js 22.12 or newer is required.'); process.exit(1); } await import('opusscript'); await import('@discordjs/voice'); await import('discord.js');"
    if ($LASTEXITCODE -ne 0) { throw 'Run the host runtime setup to install Discord dependencies.' }
} finally { Pop-Location }
if ($CheckOnly) {
    Write-Host 'Host source, dependencies, and configuration verified. No services changed.'
    return
}

$identity = [Security.Principal.WindowsIdentity]::GetCurrent()
$principal = [Security.Principal.WindowsPrincipal]::new($identity)
if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
    throw 'Run service installation from an elevated PowerShell.'
}
if (-not $NssmPath) { $NssmPath = (Get-Command nssm -ErrorAction SilentlyContinue).Source }
if (-not $NssmPath -or -not (Test-Path -LiteralPath $NssmPath -PathType Leaf)) {
    throw 'Pass -NssmPath with the installed nssm.exe.'
}
$NssmPath = (Resolve-Path -LiteralPath $NssmPath).Path
function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)
    # Windows PowerShell's native binder strips quotes inside arguments.
    # Quote each argv value explicitly using the Windows CRT parsing rules.
    $escaped = [Text.StringBuilder]::new()
    [void]$escaped.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') {
            $backslashes++
        } elseif ($character -eq '"') {
            [void]$escaped.Append(('\' * (2 * $backslashes + 1)))
            [void]$escaped.Append('"')
            $backslashes = 0
        } else {
            [void]$escaped.Append(('\' * $backslashes))
            [void]$escaped.Append($character)
            $backslashes = 0
        }
    }
    [void]$escaped.Append(('\' * (2 * $backslashes)))
    [void]$escaped.Append('"')
    return $escaped.ToString()
}
function Invoke-NativeProcess {
    param([string]$FilePath, [string[]]$ArgumentList)
    $startInfo = [Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.Arguments = (($ArgumentList | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $startInfo.UseShellExecute = $false
    $startInfo.CreateNoWindow = $true
    $startInfo.RedirectStandardOutput = $true
    $startInfo.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new()
    $process.StartInfo = $startInfo
    try {
        if (-not $process.Start()) { throw 'Could not start the service configuration tool.' }
        $stdoutTask = $process.StandardOutput.ReadToEndAsync()
        $stderrTask = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit(30000)) {
            $process.Kill()
            throw 'Service configuration tool timed out.'
        }
        return [pscustomobject]@{
            ExitCode = $process.ExitCode
            StandardOutput = $stdoutTask.Result
            StandardError = $stderrTask.Result
        }
    } finally { $process.Dispose() }
}
function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    $result = Invoke-NativeProcess -FilePath $NssmPath -ArgumentList $Arguments
    if ($result.ExitCode -ne 0) { throw "NSSM $($Arguments[0]) failed; inspect the service configuration." }
}
function Get-NssmValue {
    param([string]$ServiceName, [string]$Setting)
    $result = Invoke-NativeProcess -FilePath $NssmPath -ArgumentList @('get', $ServiceName, $Setting)
    if ($result.ExitCode -ne 0) { throw "Could not read $ServiceName service configuration." }
    return $result.StandardOutput.Trim()
}
$services = @(
    @{ Name = 'VemalsEarsHostRelay'; Role = 'relay'; Title = "Vemal's Ears Host Relay" },
    @{ Name = 'VemalsEarsHostWorker'; Role = 'worker'; Title = "Vemal's Ears Host Captions" }
)
# A service name belonging to a different state directory is not an upgrade.
# Validate both registrations before stopping either process.
foreach ($service in $services) {
    $service.Parameters = '-B -m Hosting.service --state-directory "' + $StateDirectory + '" ' + $service.Role
    $existing = Get-Service -Name $service.Name -ErrorAction SilentlyContinue
    if ($existing) {
        $parameters = Get-NssmValue $service.Name AppParameters
        if ($parameters -ne $service.Parameters) {
            throw "Service $($service.Name) belongs to another configuration; no services changed."
        }
    }
}
$logs = Join-Path $StateDirectory 'logs'
New-Item -ItemType Directory -Force -Path $logs | Out-Null
foreach ($service in @($services[1], $services[0])) {
    $existing = Get-Service -Name $service.Name -ErrorAction SilentlyContinue
    if ($existing -and $existing.Status -ne 'Stopped') {
        Stop-Service -Name $service.Name -ErrorAction Stop
        (Get-Service -Name $service.Name).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
}
foreach ($service in $services) {
    $existing = Get-Service -Name $service.Name -ErrorAction SilentlyContinue
    if (-not $existing) { Invoke-Nssm install $service.Name $PythonPath }
    Invoke-Nssm set $service.Name Application $PythonPath
    Invoke-Nssm set $service.Name AppParameters $service.Parameters
    Invoke-Nssm set $service.Name AppDirectory $repoRoot
    Invoke-Nssm set $service.Name AppStdout (Join-Path $logs ($service.Role + '.log'))
    Invoke-Nssm set $service.Name AppStderr (Join-Path $logs ($service.Role + '.log'))
    Invoke-Nssm set $service.Name AppRotateFiles 1
    Invoke-Nssm set $service.Name AppRotateBytes 1048576
    Invoke-Nssm set $service.Name AppExit Default Restart
    Invoke-Nssm set $service.Name AppExit 3 Exit
    Invoke-Nssm set $service.Name AppRestartDelay 30000
    Invoke-Nssm set $service.Name AppStopMethodConsole 15000
    Invoke-Nssm set $service.Name Start SERVICE_DELAYED_AUTO_START
    Invoke-Nssm set $service.Name DisplayName $service.Title
    Invoke-Nssm set $service.Name AppEnvironmentExtra ('PATH=' + (Split-Path $nodePath) + ';' + $env:PATH)
    $actualDirectory = Get-NssmValue $service.Name AppDirectory
    if ([IO.Path]::GetFullPath($actualDirectory) -ne $repoRoot) {
        throw "Service $($service.Name) has the wrong source directory; services remain stopped."
    }
}
Invoke-Nssm set VemalsEarsHostWorker DependOnService VemalsEarsHostRelay
foreach ($service in $services) {
    Start-Service -Name $service.Name -ErrorAction Stop
    (Get-Service -Name $service.Name).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
}
Write-Host 'Host services started. Verify /healthz, Discord pairing, and captions with the guide.'
