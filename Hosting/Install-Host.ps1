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

$activation = if ($CheckOnly) { 'check' } else { 'install' }
$modeOutput = & $PythonPath -I -B -c "import sys; sys.path.insert(0, sys.argv[1]); from GuildHub import host; from Hosting.service import load_configuration, validate_activation; values=load_configuration(sys.argv[2]); validate_activation(values) if sys.argv[3] == 'install' else None; print(values['VEMALS_EARS_HOST_MODE'])" $repoRoot $StateDirectory $activation
if ($LASTEXITCODE -ne 0) { throw 'Host source, configuration, or takeover confirmation preflight failed.' }
$hostMode = ($modeOutput | Out-String).Trim()
if ($hostMode -notin @('standalone', 'remote-worker')) { throw 'Unknown host mode; no services changed.' }
& $PythonPath -I -B -c "import sys; sys.path.insert(0, sys.argv[1]); from Hosting.service import main; sys.exit(main(['--state-directory', sys.argv[2], '--host-mode', sys.argv[3], '--check-only', 'worker']))" $repoRoot $StateDirectory $hostMode
if ($LASTEXITCODE -ne 0) { throw 'NVIDIA speech runtime preflight failed. Run host setup prepare first.' }
if ($hostMode -eq 'standalone') {
    & $PythonPath -I -B -c "import sys; sys.path.insert(0, sys.argv[1]); from GuildHub import server; from Hosting.service import main; sys.exit(main(['--state-directory', sys.argv[2], '--check-only', 'relay']))" $repoRoot $StateDirectory
    if ($LASTEXITCODE -ne 0) { throw 'Relay source or configuration preflight failed.' }
    $runningRelay = Get-Service -Name VemalsEarsHostRelay -ErrorAction SilentlyContinue
    if (-not $runningRelay -or $runningRelay.Status -ne 'Running') {
        & $PythonPath -I -B -c "import sys; sys.path.insert(0, sys.argv[1]); from Hosting.service import main; sys.exit(main(['--state-directory', sys.argv[2], '--check-only', '--check-port', 'relay']))" $repoRoot $StateDirectory
        if ($LASTEXITCODE -ne 0) { throw 'The relay port is unavailable. No services changed.' }
    }
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
function Get-HostServiceDefinitions {
    param([string]$HostMode, [string]$StateDirectory)
    if ($HostMode -notin @('standalone', 'remote-worker')) { throw 'Unknown host mode.' }
    $selected = @()
    if ($HostMode -eq 'standalone') {
        $selected += @{ Name = 'VemalsEarsHostRelay'; Role = 'relay'; Title = "Vemal's Ears Host Relay" }
    }
    $selected += @{ Name = 'VemalsEarsHostWorker'; Role = 'worker'; Title = "Vemal's Ears Host Captions" }
    foreach ($service in $selected) {
        $service.Parameters = '-B -m Hosting.service --state-directory "' + $StateDirectory + '" '
        if ($HostMode -eq 'remote-worker') { $service.Parameters += '--host-mode remote-worker ' }
        $service.Parameters += $service.Role
    }
    return $selected
}
function Assert-HostServiceOwnership {
    param([array]$Services, [string]$PythonPath, [string]$RepoRoot)
    # Validate all selected registrations before stopping any process.
    foreach ($service in $Services) {
        $existing = Get-Service -Name $service.Name -ErrorAction SilentlyContinue
        if ($existing) {
            $parameters = Get-NssmValue $service.Name AppParameters
            $application = Get-NssmValue $service.Name Application
            $directory = Get-NssmValue $service.Name AppDirectory
            if ($parameters -ne $service.Parameters -or
                [IO.Path]::GetFullPath($application) -ne $PythonPath -or
                [IO.Path]::GetFullPath($directory) -ne $RepoRoot) {
                throw "Service $($service.Name) belongs to another configuration; no services changed."
            }
        }
    }
}
function Install-RegisteredHostServices {
    param([array]$Services, [string]$PythonPath, [string]$RepoRoot,
          [string]$StateDirectory, [string]$NodePath, [string]$HostMode = 'standalone')
    if ($HostMode -notin @('standalone', 'remote-worker')) { throw 'Unknown host mode.' }
    if ($HostMode -eq 'remote-worker' -and
        ($Services.Count -ne 1 -or $Services[0].Name -ne 'VemalsEarsHostWorker')) {
        throw 'Remote-worker installation can manage only its caption worker.'
    }
    $created = [Collections.Generic.List[string]]::new()
    foreach ($service in $Services) {
        if ($service.Name -notin @('VemalsEarsHostRelay', 'VemalsEarsHostWorker')) {
            throw 'Unknown host service name; no services changed.'
        }
    }
    try {
        $logs = Join-Path $StateDirectory 'logs'
        New-Item -ItemType Directory -Force -Path $logs | Out-Null
        for ($index = $Services.Count - 1; $index -ge 0; $index--) {
            $service = $Services[$index]
            $existing = Get-Service -Name $service.Name -ErrorAction SilentlyContinue
            if ($existing -and $existing.Status -ne 'Stopped') {
                Stop-Service -Name $service.Name -ErrorAction Stop
                (Get-Service -Name $service.Name).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
            }
        }
        foreach ($service in $Services) {
            $existing = Get-Service -Name $service.Name -ErrorAction SilentlyContinue
            if (-not $existing) {
                Invoke-Nssm install $service.Name $PythonPath
                $created.Add($service.Name)
            }
            Invoke-Nssm set $service.Name Application $PythonPath
            Invoke-Nssm set $service.Name AppParameters $service.Parameters
            Invoke-Nssm set $service.Name AppDirectory $RepoRoot
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
            Invoke-Nssm set $service.Name AppEnvironmentExtra ('PATH=' + (Split-Path $NodePath) + ';' + $env:PATH)
            $actualDirectory = Get-NssmValue $service.Name AppDirectory
            if ([IO.Path]::GetFullPath($actualDirectory) -ne $RepoRoot) {
                throw "Service $($service.Name) has the wrong source directory; services remain stopped."
            }
        }
        if ($HostMode -eq 'remote-worker') {
            Invoke-Nssm reset VemalsEarsHostWorker DependOnService
        } else {
            Invoke-Nssm set VemalsEarsHostWorker DependOnService VemalsEarsHostRelay
        }
        foreach ($service in $Services) {
            Start-Service -Name $service.Name -ErrorAction Stop
            (Get-Service -Name $service.Name).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
        }
    } catch {
        $originalError = $_
        $cleanupFailed = $false
        # Only registrations created successfully in this invocation belong to
        # rollback. Existing services and every other installation stay intact.
        for ($index = $created.Count - 1; $index -ge 0; $index--) {
            $name = $created[$index]
            try { Invoke-Nssm stop $name } catch { }
            try { Invoke-Nssm remove $name confirm } catch { $cleanupFailed = $true }
        }
        if ($cleanupFailed) {
            throw 'Setup failed and Windows could not remove a new service registration. Check host status before retrying.'
        }
        throw $originalError
    }
}
$services = @(Get-HostServiceDefinitions -HostMode $hostMode -StateDirectory $StateDirectory)
Assert-HostServiceOwnership -Services $services -PythonPath $PythonPath -RepoRoot $repoRoot
Install-RegisteredHostServices -Services $services -PythonPath $PythonPath -RepoRoot $repoRoot -StateDirectory $StateDirectory -NodePath $nodePath -HostMode $hostMode
Write-Host 'Host services started. Verify /healthz, Discord pairing, and captions with the guide.'
