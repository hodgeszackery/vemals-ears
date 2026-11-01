# Publish only the standalone Host downloads beside the existing member release.
# -CheckOnly validates inputs and tunnel ingress without changing live files/services.
[CmdletBinding()]
param(
    [string]$ArtifactDirectory = '',
    [string]$SidecarPath = '',
    [string]$StateDirectory = 'C:/ProgramData/VemalsEars',
    [string]$PythonPath = 'C:/Users/Shino/AppData/Local/Python/bin/python.exe',
    [string]$NssmPath = 'C:/Users/Shino/AppData/Local/Microsoft/WinGet/Packages/NSSM.NSSM_Microsoft.Winget.Source_8wekyb3d8bbwe/nssm-2.24-101-g897c7ad/win64/nssm.exe',
    [string]$CloudflaredPath = 'C:/Program Files (x86)/cloudflared/cloudflared.exe',
    [switch]$CheckOnly
)
$ErrorActionPreference = 'Stop'
$script:PublisherRoot = $PSScriptRoot
if (-not $ArtifactDirectory) { $ArtifactDirectory = Join-Path (Split-Path $script:PublisherRoot) 'dist' }
if (-not $SidecarPath) { $SidecarPath = Join-Path $script:PublisherRoot 'downloads.py' }

function ConvertTo-NativeArgument {
    param([AllowEmptyString()][string]$Value)
    $escaped = [Text.StringBuilder]::new()
    [void]$escaped.Append('"')
    $backslashes = 0
    foreach ($character in $Value.ToCharArray()) {
        if ($character -eq '\') { $backslashes++ }
        elseif ($character -eq '"') {
            [void]$escaped.Append(('\' * (2 * $backslashes + 1)))
            [void]$escaped.Append('"'); $backslashes = 0
        } else {
            [void]$escaped.Append(('\' * $backslashes))
            [void]$escaped.Append($character); $backslashes = 0
        }
    }
    [void]$escaped.Append(('\' * (2 * $backslashes)))
    [void]$escaped.Append('"')
    return $escaped.ToString()
}

function Invoke-PublishProcess {
    param([string]$FilePath, [string[]]$ArgumentList, [int]$Timeout = 30000)
    $start = [Diagnostics.ProcessStartInfo]::new()
    $start.FileName = $FilePath
    $start.Arguments = (($ArgumentList | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' ')
    $start.UseShellExecute = $false; $start.CreateNoWindow = $true
    $start.RedirectStandardOutput = $true; $start.RedirectStandardError = $true
    $process = [Diagnostics.Process]::new(); $process.StartInfo = $start
    try {
        if (-not $process.Start()) { throw 'Could not start the publication tool.' }
        $stdout = $process.StandardOutput.ReadToEndAsync()
        $stderr = $process.StandardError.ReadToEndAsync()
        if (-not $process.WaitForExit($Timeout)) {
            $process.Kill(); throw 'Publication tool timed out.'
        }
        return [pscustomobject]@{ ExitCode=$process.ExitCode; Output=$stdout.Result; Error=$stderr.Result }
    } finally { $process.Dispose() }
}

function Invoke-PublishNssm {
    param([string[]]$Arguments)
    $result = Invoke-PublishProcess $NssmPath $Arguments
    if ($result.ExitCode -ne 0) { throw ('NSSM operation failed: ' + $Arguments[0]) }
    return $result.Output.Trim()
}

function Get-PublishHash {
    param([string]$Path)
    $stream = [IO.File]::OpenRead($Path)
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { return ([BitConverter]::ToString($hasher.ComputeHash($stream))).Replace('-', '').ToLowerInvariant() }
    finally { $hasher.Dispose(); $stream.Dispose() }
}

function Assert-PublishPath {
    param([string]$Path)
    if (-not [IO.Path]::IsPathRooted($Path) -or $Path -match '["\x00-\x1f]') {
        throw 'Publication paths must be absolute and contain no quotes or control characters.'
    }
    $absolute = [IO.Path]::GetFullPath($Path)
    $part = $absolute
    while ($part) {
        if (Test-Path -LiteralPath $part) {
            if ((Get-Item -LiteralPath $part -Force).Attributes -band [IO.FileAttributes]::ReparsePoint) {
                throw 'Publication paths cannot traverse a reparse point.'
            }
        }
        $parent = Split-Path -Parent $part
        if ($parent -eq $part) { break }
        $part = $parent
    }
    return $absolute
}

function Get-HostDownloadParameters {
    param([string]$ScriptPath, [string]$DownloadDirectory)
    return '-B ' + (ConvertTo-NativeArgument $ScriptPath) + ' --directory ' +
        (ConvertTo-NativeArgument $DownloadDirectory) + ' --host 127.0.0.1 --port 8773'
}

function Add-HostDownloadIngress {
    param([byte[]]$Content)
    # Decode/re-encode losslessly; existing BOM, line endings and all unrelated
    # YAML bytes are retained. Only a fixed literal rule is ever inserted.
    $utf8 = [Text.UTF8Encoding]::new($false, $true)
    $text = $utf8.GetString($Content)
    $pattern = '(?ms)^(?<indent> +)-[ \t]+hostname:[ \t]*[''"]?ears\.xenon\.solutions[''"]?[ \t]*\r?\n(?<body>.*?)(?=^\k<indent>-[ \t]|\z)'
    $entries = [regex]::Matches($text, $pattern)
    $relay = @(); $existing = @()
    foreach ($entry in $entries) {
        $body = $entry.Groups['body'].Value
        $path = [regex]::Match($body, '(?m)^ +path:[ \t]*(?<value>[^\r\n]+)\r?$')
        $service = [regex]::Match($body, '(?m)^ +service:[ \t]*[''"]?(?<value>http://127\.0\.0\.1:\d+)[''"]?[ \t]*\r?$')
        if (-not $path.Success -and $service.Success -and $service.Groups['value'].Value -eq 'http://127.0.0.1:8770') {
            $relay += $entry
        } elseif ($path.Success -and $path.Groups['value'].Value.Trim(' ', "'", '"') -eq '^/download/vemals-ears-host(?:/(?:source|sha256))?$') {
            if (-not $service.Success -or $service.Groups['value'].Value -ne 'http://127.0.0.1:8773') {
                throw 'The Host download ingress already points somewhere else.'
            }
            $existing += $entry
        }
    }
    if ($relay.Count -ne 1 -or $existing.Count -gt 1) { throw 'Expected exactly one unqualified relay ingress for ears.xenon.solutions.' }
    if ($existing.Count -eq 1) {
        if ($existing[0].Index -gt $relay[0].Index) { throw 'The Host download ingress is shadowed by the relay rule.' }
        return ,$Content
    }
    $indent = $relay[0].Groups['indent'].Value
    $newline = if ($relay[0].Value.Contains("`r`n")) { "`r`n" } else { "`n" }
    $addition = $indent + '- hostname: ears.xenon.solutions' + $newline +
        $indent + '  path: ^/download/vemals-ears-host(?:/(?:source|sha256))?$' + $newline +
        $indent + '  service: http://127.0.0.1:8773' + $newline
    return ,$utf8.GetBytes($text.Insert($relay[0].Index, $addition))
}

function Set-PublishFile {
    param([string]$Source, [string]$Destination)
    $temporary = $Destination + '.publishing-' + [guid]::NewGuid().ToString('N')
    try {
        [IO.File]::Copy($Source, $temporary, $false)
        if (Test-Path -LiteralPath $Destination) { [IO.File]::Replace($temporary, $Destination, [NullString]::Value) }
        else { [IO.File]::Move($temporary, $Destination) }
    } finally {
        if (Test-Path -LiteralPath $temporary) { Remove-Item -LiteralPath $temporary -Force }
    }
}

function Save-PublishReceipt {
    param([string]$Path, [object]$Value)
    [IO.File]::WriteAllText($Path, (($Value | ConvertTo-Json -Depth 8) + "`n"), [Text.Encoding]::ASCII)
}

function Stop-PublishService {
    param([string]$Name)
    $current = Get-Service -Name $Name -ErrorAction SilentlyContinue
    if ($current -and $current.Status -ne 'Stopped') {
        [void](Invoke-PublishNssm @('stop', $Name))
        (Get-Service -Name $Name).WaitForStatus('Stopped', [TimeSpan]::FromSeconds(30))
    }
}

function Get-PublishAdministratorSid {
    $identity = [Security.Principal.WindowsIdentity]::GetCurrent()
    $principal = [Security.Principal.WindowsPrincipal]::new($identity)
    if (-not $principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {
        throw 'Run publication from an elevated PowerShell.'
    }
    return $identity.User.Value
}

function Assert-HostDownloadPortAvailable {
    $listener = [Net.Sockets.TcpListener]::new([Net.IPAddress]::Loopback, 8773)
    try { $listener.Start() } finally { $listener.Stop() }
}

function Wait-HostDownloadHealth {
    for ($attempt = 0; $attempt -lt 30; $attempt++) {
        try {
            $request = [Net.HttpWebRequest]::Create('http://127.0.0.1:8773/healthz')
            $request.Proxy = $null; $request.Timeout = 1000
            $response = $request.GetResponse()
            try {
                $reader = [IO.StreamReader]::new($response.GetResponseStream())
                try { $health = $reader.ReadToEnd() | ConvertFrom-Json } finally { $reader.Dispose() }
            } finally { $response.Dispose() }
            if ($health.status -eq 'ok' -and $health.service -eq 'vemals-ears-downloads') { return }
        } catch { }
        Start-Sleep -Milliseconds 500
    }
    throw 'The Host download service did not become healthy on loopback port 8773.'
}

function Invoke-HostDownloadPublication {
    $serviceName = 'VemalsEarsHostDownloads'; $tunnelName = 'VemalsEarsTunnel'
    $ArtifactDirectory = Assert-PublishPath $ArtifactDirectory
    $SidecarPath = Assert-PublishPath $SidecarPath
    $StateDirectory = Assert-PublishPath $StateDirectory
    $PythonPath = Assert-PublishPath $PythonPath
    $script:NssmPath = Assert-PublishPath $NssmPath
    $CloudflaredPath = Assert-PublishPath $CloudflaredPath
    $downloads = Assert-PublishPath (Join-Path $StateDirectory 'downloads')
    $config = Assert-PublishPath (Join-Path $StateDirectory 'tunnel-config.yml')
    foreach ($file in @($SidecarPath, $PythonPath, $NssmPath, $CloudflaredPath, $config)) {
        if (-not (Test-Path -LiteralPath $file -PathType Leaf)) { throw ('Required publication file is missing: ' + $file) }
    }
    if (-not (Test-Path -LiteralPath $downloads -PathType Container)) { throw 'The existing public downloads directory is missing.' }
    $metadataPath = Join-Path $ArtifactDirectory 'Vemals-Ears-Host-Setup.json'
    $metadata = Get-Content -LiteralPath $metadataPath -Raw | ConvertFrom-Json
    $exe = Assert-PublishPath (Join-Path $ArtifactDirectory 'Vemals-Ears-Host-Setup.exe')
    $zip = Assert-PublishPath (Join-Path $ArtifactDirectory 'VemalsEars-Host.zip')
    if ($metadata.schema -ne 1 -or $metadata.product -ne 'vemals-ears-host' -or
        $metadata.test_build -isnot [bool] -or $metadata.test_build -ne $false -or
        $metadata.sha256 -notmatch '^[0-9a-f]{64}$' -or
        (Get-Item -LiteralPath $exe).Length -ne $metadata.bytes -or
        (Get-PublishHash $exe) -ne $metadata.sha256) {
        throw 'The production Host installer does not match its product, size and SHA-256 receipt.'
    }
    $zipCheck = Invoke-PublishProcess $PythonPath @('-I', '-B', '-c', 'import sys, zipfile; z=zipfile.ZipFile(sys.argv[1]); assert z.namelist() and z.testzip() is None', $zip) 120000
    if ($zipCheck.ExitCode -ne 0) { throw 'The Host source ZIP is missing or damaged.' }
    $zipHash = Get-PublishHash $zip
    $sidecarCheck = Invoke-PublishProcess $PythonPath @('-I', '-B', $SidecarPath, '--help')
    if ($sidecarCheck.ExitCode -ne 0) { throw 'The download sidecar did not pass its command-line preflight.' }
    $runtimeHash = Get-PublishHash $SidecarPath
    $runtimeDirectory = Assert-PublishPath (Join-Path $StateDirectory ('host-downloads-runtime/' + $runtimeHash))
    $runtime = Join-Path $runtimeDirectory 'downloads.py'
    $parameters = Get-HostDownloadParameters $runtime $downloads
    $existing = Get-Service -Name $serviceName -ErrorAction SilentlyContinue
    $prior = $null
    if ($existing) {
        if ($existing.Status -notin @('Running', 'Stopped')) { throw 'Wait for the existing download service to finish its transition before publishing.' }
        $application = Invoke-PublishNssm @('get', $serviceName, 'Application')
        $oldDirectory = Invoke-PublishNssm @('get', $serviceName, 'AppDirectory')
        $oldParameters = Invoke-PublishNssm @('get', $serviceName, 'AppParameters')
        $oldRuntime = Join-Path $oldDirectory 'downloads.py'
        [void](Assert-PublishPath $oldRuntime)
        $runtimeBase = [regex]::Escape((Join-Path $StateDirectory 'host-downloads-runtime') + [IO.Path]::DirectorySeparatorChar)
        if ([IO.Path]::GetFullPath($application) -ne $PythonPath -or $oldDirectory -notmatch ('^' + $runtimeBase + '[0-9a-f]{64}$') -or
            -not (Test-Path -LiteralPath $oldRuntime -PathType Leaf) -or
            (Get-PublishHash $oldRuntime) -ne (Split-Path -Leaf $oldDirectory) -or
            $oldParameters -ne (Get-HostDownloadParameters $oldRuntime $downloads)) {
            throw 'The existing Host downloads service is not owned by this publisher; nothing changed.'
        }
        $prior = @{ directory=$oldDirectory; parameters=$oldParameters; running=($existing.Status -eq 'Running') }
    } else {
        Assert-HostDownloadPortAvailable
    }
    $tunnel = Get-Service -Name $tunnelName -ErrorAction Stop
    if ($tunnel.Status -ne 'Running') { throw 'The existing public tunnel must be running before publishing.' }
    $tunnelApplication = Invoke-PublishNssm @('get', $tunnelName, 'Application')
    $tunnelParameters = Invoke-PublishNssm @('get', $tunnelName, 'AppParameters')
    $acceptedTunnelParameters = @(('--config ' + $config + ' tunnel run vemals-ears'), ('--config "' + $config + '" tunnel run vemals-ears'),
        ((@('--config', $config, 'tunnel', 'run', 'vemals-ears') | ForEach-Object { ConvertTo-NativeArgument $_ }) -join ' '))
    if ([IO.Path]::GetFullPath($tunnelApplication) -ne $CloudflaredPath -or $tunnelParameters -notin $acceptedTunnelParameters) {
        throw 'The tunnel service application/configuration does not match this publication target.'
    }
    $beforeConfig = [IO.File]::ReadAllBytes($config)
    $configHash = Get-PublishHash $config
    $candidateBytes = Add-HostDownloadIngress $beforeConfig
    $temporaryConfig = Join-Path ([IO.Path]::GetTempPath()) ('ve-host-ingress-' + [guid]::NewGuid().ToString('N') + '.yml')
    try {
        [IO.File]::WriteAllBytes($temporaryConfig, $candidateBytes)
        $validation = Invoke-PublishProcess $CloudflaredPath @('--config', $temporaryConfig, 'tunnel', 'ingress', 'validate')
        if ($validation.ExitCode -ne 0) { throw 'Cloudflared rejected the proposed ingress. Live configuration was not changed.' }
    } finally { if (Test-Path -LiteralPath $temporaryConfig) { Remove-Item -LiteralPath $temporaryConfig -Force } }
    if ($CheckOnly) {
        Write-Host ('Publication preflight passed: ' + $metadata.bytes + ' installer bytes; SHA-256 ' + $metadata.sha256)
        Write-Host 'No live files or services changed. The existing member download and release pointer are untouched.'
        return
    }
    $administratorSid = Get-PublishAdministratorSid
    $publication = Assert-PublishPath (Join-Path $StateDirectory ('host-downloads-publications/' + (Get-Date -Format 'yyyyMMdd-HHmmss') + '-' + [guid]::NewGuid().ToString('N')))
    [void][IO.Directory]::CreateDirectory($publication)
    $acl = Invoke-PublishProcess 'icacls.exe' @($publication, '/inheritance:r', '/grant:r', ('*' + $administratorSid + ':(OI)(CI)F'), '*S-1-5-18:(OI)(CI)F', '*S-1-5-32-544:(OI)(CI)F')
    if ($acl.ExitCode -ne 0) { throw 'Could not protect the publication backup directory.' }
    $configBackup = Join-Path $publication 'tunnel-config.before.yml'
    [IO.File]::WriteAllBytes($configBackup, $beforeConfig)
    $configCandidate = Join-Path $publication 'tunnel-config.after.yml'
    [IO.File]::WriteAllBytes($configCandidate, $candidateBytes)
    $checksum = Join-Path $publication 'Vemals-Ears-Host-Setup.sha256'
    [IO.File]::WriteAllText($checksum, ($metadata.sha256 + '  Vemals-Ears-Host-Setup.exe' + "`n"), [Text.Encoding]::ASCII)
    $files = @()
    foreach ($source in @($exe, $zip, $checksum)) {
        $name = Split-Path -Leaf $source
        $target = Assert-PublishPath (Join-Path $downloads $name)
        $backup = Join-Path $publication ($name + '.before')
        $existed = Test-Path -LiteralPath $target -PathType Leaf
        if ($existed) { [IO.File]::Copy($target, $backup, $false) }
        $beforeHash = if ($existed) { Get-PublishHash $backup } else { $null }
        $sourceHash = Get-PublishHash $source
        if (($source -eq $exe -and $sourceHash -ne $metadata.sha256) -or ($source -eq $zip -and $sourceHash -ne $zipHash)) {
            throw 'A release artifact changed during publication preflight.'
        }
        $files += @{ name=$name; target=$target; source=$source; existed=$existed; backup=$backup; before_sha256=$beforeHash; sha256=$sourceHash }
    }
    $receiptPath = Join-Path $publication 'receipt.json'
    $receipt = @{ schema=1; product='vemals-ears-host-downloads'; status='prepared'; files=$files;
        config_backup=$configBackup; config_before_sha256=$configHash; config_after_sha256=(Get-PublishHash $configCandidate);
        runtime=$runtime; runtime_sha256=$runtimeHash; service_before=$prior; tunnel_service=$tunnelName }
    Save-PublishReceipt $receiptPath $receipt
    $created = $false; $serviceChanged = $false; $filesChanged = $false; $configApplied = $false
    try {
        [void][IO.Directory]::CreateDirectory($runtimeDirectory)
        if (Test-Path -LiteralPath $runtime) {
            if ((Get-PublishHash $runtime) -ne $runtimeHash) { throw 'The immutable sidecar snapshot was modified.' }
        } else { Set-PublishFile $SidecarPath $runtime }
        if ((Get-PublishHash $runtime) -ne $runtimeHash) { throw 'The sidecar changed while its immutable snapshot was being created.' }
        if ((Get-PublishHash $config) -ne $configHash) { throw 'Tunnel configuration changed during publication preflight.' }
        if ($existing -and $existing.Status -ne 'Stopped') {
            $serviceChanged = $true; Stop-PublishService $serviceName
        }
        $filesChanged = $true
        foreach ($file in $files) { Set-PublishFile $file.source $file.target }
        if (-not $existing) {
            [void](Invoke-PublishNssm @('install', $serviceName, $PythonPath)); $created = $true
        }
        $serviceChanged = $true
        [void](Invoke-PublishNssm @('set', $serviceName, 'AppDirectory', $runtimeDirectory))
        [void](Invoke-PublishNssm @('set', $serviceName, 'AppParameters', $parameters))
        if ($created) {
            [void](Invoke-PublishNssm @('set', $serviceName, 'DisplayName', "Vemal's Ears Host Downloads"))
            [void](Invoke-PublishNssm @('set', $serviceName, 'Start', 'SERVICE_AUTO_START'))
            [void](Invoke-PublishNssm @('set', $serviceName, 'AppExit', 'Default', 'Restart'))
            [void](Invoke-PublishNssm @('set', $serviceName, 'AppRestartDelay', '5000'))
        }
        [void](Invoke-PublishNssm @('start', $serviceName))
        Wait-HostDownloadHealth
        foreach ($file in $files) {
            if ((Get-PublishHash $file.target) -ne $file.sha256) { throw 'A staged public artifact failed final checksum validation.' }
        }
        if ((Get-PublishHash $config) -ne $configHash) { throw 'Tunnel configuration changed before activation.' }
        if ($receipt.config_after_sha256 -ne $configHash) {
            Set-PublishFile $configCandidate $config; $configApplied = $true
            Restart-Service -Name $tunnelName -ErrorAction Stop
            (Get-Service -Name $tunnelName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
        }
        $receipt.status = 'published'; Save-PublishReceipt $receiptPath $receipt
        Write-Host ('Host downloads published. Rollback receipt: ' + $receiptPath)
    } catch {
        $failure = $_; $rollbackErrors = [Collections.Generic.List[string]]::new()
        if ($serviceChanged) {
            try { Stop-PublishService $serviceName } catch { $rollbackErrors.Add('stop download service') }
        }
        if ($filesChanged) {
            foreach ($file in $files) {
                try {
                    if ($file.existed) { Set-PublishFile $file.backup $file.target }
                    elseif (Test-Path -LiteralPath $file.target) { Remove-Item -LiteralPath $file.target -Force }
                } catch { $rollbackErrors.Add('restore ' + $file.name) }
            }
        }
        if ($configApplied) {
            try {
                if ((Get-PublishHash $config) -ne $receipt.config_after_sha256) { throw 'Tunnel config was changed by another operation; retained for review.' }
                Set-PublishFile $configBackup $config
                Restart-Service -Name $tunnelName -ErrorAction Stop
                (Get-Service -Name $tunnelName).WaitForStatus('Running', [TimeSpan]::FromSeconds(30))
            } catch { $rollbackErrors.Add('restore tunnel configuration/service') }
        }
        if ($created) {
            try { [void](Invoke-PublishNssm @('remove', $serviceName, 'confirm')) } catch { $rollbackErrors.Add('remove new download service') }
        } elseif ($serviceChanged -and $prior) {
            try {
                [void](Invoke-PublishNssm @('set', $serviceName, 'AppDirectory', $prior.directory))
                [void](Invoke-PublishNssm @('set', $serviceName, 'AppParameters', $prior.parameters))
                if ($prior.running) { [void](Invoke-PublishNssm @('start', $serviceName)) }
            } catch { $rollbackErrors.Add('restore previous download service') }
        }
        $receipt.status = 'failed'; $receipt.rollback_errors = @($rollbackErrors)
        Save-PublishReceipt $receiptPath $receipt
        if ($rollbackErrors.Count) { throw ('Publication failed; rollback needs attention: ' + ($rollbackErrors -join ', ') + '. Receipt: ' + $receiptPath) }
        throw $failure
    }
}

# Dot sourcing loads only pure helpers for offline tests; it never publishes.
if ($MyInvocation.InvocationName -ne '.') {
    $mutex = [Threading.Mutex]::new($false, 'Global\VemalsEarsHostDownloadsPublish')
    $locked = $false
    try {
        $locked = $mutex.WaitOne(0)
        if (-not $locked) { throw 'Another Host download publication is in progress.' }
        Invoke-HostDownloadPublication
    } finally {
        if ($locked) { $mutex.ReleaseMutex() }
        $mutex.Dispose()
    }
}
