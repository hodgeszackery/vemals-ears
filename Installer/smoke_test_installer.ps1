[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$InstallerPath
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$InstallerPath = [System.IO.Path]::GetFullPath($InstallerPath)
if (-not (Test-Path -LiteralPath $InstallerPath -PathType Leaf)) {
    throw "The test installer does not exist."
}
if ([System.IO.Path]::GetFileName($InstallerPath) -cne "Vemals-Ears-Setup-TEST-UNSIGNED.exe") {
    throw "The smoke test accepts only the isolated TEST-UNSIGNED installer."
}

function Read-RegistryValue {
    param(
        [Parameter(Mandatory = $true)] [string]$Subkey,
        [Parameter(Mandatory = $true)] [AllowEmptyString()] [string]$Name
    )
    $key = [Microsoft.Win32.Registry]::CurrentUser.OpenSubKey($Subkey, $false)
    if ($null -eq $key) {
        return "<missing-key>"
    }
    try {
        $value = $key.GetValue($Name, $null, [Microsoft.Win32.RegistryValueOptions]::DoNotExpandEnvironmentNames)
        if ($null -eq $value) {
            return "<missing-value>"
        }
        return [string]$value
    }
    finally {
        $key.Dispose()
    }
}

function Get-ProductionRegistrySnapshot {
    return @(
        Read-RegistryValue -Subkey "Software\Classes\vemalsears" -Name ""
        Read-RegistryValue -Subkey "Software\Classes\vemalsears" -Name "URL Protocol"
        Read-RegistryValue -Subkey "Software\Classes\vemalsears\shell\open\command" -Name ""
        Read-RegistryValue -Subkey "Software\VemalsEars\Installer" -Name "Version"
        Read-RegistryValue -Subkey "Software\Microsoft\Windows\CurrentVersion\Run" -Name "Vemal's Ears"
        Read-RegistryValue -Subkey "Software\Microsoft\Windows\CurrentVersion\Run" -Name "Downtime"
        Read-RegistryValue -Subkey "Software\Microsoft\Windows\CurrentVersion\Run" -Name "WoW Companion"
    )
}

function Assert-ProductionRegistryUnchanged {
    # AllowEmptyString because a snapshot legitimately contains one: the
    # "URL Protocol" value under Software\Classes\vemalsears is empty by
    # convention, which is how Windows marks a protocol handler. Without
    # it this check refused its own input on exactly the machines where
    # Vemal's Ears is registered - the only ones worth testing on.
    param([Parameter(Mandatory = $true)] [AllowEmptyString()] [string[]]$Before)
    $after = @(Get-ProductionRegistrySnapshot)
    if ([string]::Join("`n", $Before) -cne [string]::Join("`n", $after)) {
        throw "The isolated installer changed production Vemal's Ears registry state."
    }
}

function ConvertTo-WindowsArgument {
    param(
        [Parameter(Mandatory = $true)] [AllowEmptyString()] [string]$Value
    )
    if ($Value -match '"') {
        throw "Refusing to quote an argument containing a double quote: $Value"
    }
    if ($Value -eq "") { return '""' }
    if ($Value -notmatch '\s') { return $Value }
    # A value ending in backslashes needs them doubled, or the last one
    # escapes the closing quote and the installer receives a mangled path.
    $trailing = [regex]::Match($Value, '\\*$').Value
    return '"' + $Value + $trailing + '"'
}

function Invoke-WaitingProcess {
    param(
        [Parameter(Mandatory = $true)] [string]$FilePath,
        [Parameter(Mandatory = $true)] [string[]]$Arguments
    )
    $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
    $startInfo.FileName = $FilePath
    $startInfo.UseShellExecute = $false
    # ProcessStartInfo.ArgumentList arrived with .NET Core. Windows
    # PowerShell 5.1 runs on .NET Framework, which has only the single
    # Arguments string - and reading the absent property under
    # Set-StrictMode threw before the installer was ever launched, so this
    # smoke test could not run on the very platform it tests.
    if ($startInfo.PSObject.Properties.Name -contains "ArgumentList") {
        foreach ($argument in $Arguments) {
            $startInfo.ArgumentList.Add($argument)
        }
    }
    else {
        $quoted = @($Arguments | ForEach-Object { ConvertTo-WindowsArgument $_ })
        $startInfo.Arguments = ($quoted -join " ")
    }
    $process = [System.Diagnostics.Process]::Start($startInfo)
    if ($null -eq $process) {
        throw "Windows could not start $FilePath."
    }
    try {
        $process.WaitForExit()
        return $process.ExitCode
    }
    finally {
        $process.Dispose()
    }
}

function Remove-TemporaryFile {
    param([Parameter(Mandatory = $true)] [string]$Path)
    $deadline = [DateTime]::UtcNow.AddSeconds(5)
    while (Test-Path -LiteralPath $Path -PathType Leaf) {
        try {
            [System.IO.File]::Delete($Path)
        }
        catch [System.IO.IOException] {
            if ([DateTime]::UtcNow -ge $deadline) {
                throw
            }
            Start-Sleep -Milliseconds 100
        }
    }
}

$localAppData = [Environment]::GetFolderPath(
    [Environment+SpecialFolder]::LocalApplicationData
)
$tempRoot = [System.IO.Path]::GetFullPath((Join-Path $localAppData "Temp"))
[System.IO.Directory]::CreateDirectory($tempRoot) | Out-Null
$testRoot = [System.IO.Path]::GetFullPath(
    (Join-Path $tempRoot ("Vemals Ears Installer Smoke-" + [guid]::NewGuid().ToString("N")))
)
$tempPrefix = $tempRoot.TrimEnd('\') + '\'
if (-not $testRoot.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
    throw "The smoke-test install path is outside the temporary directory."
}
$installLog = $testRoot + "-install.log"
$uninstallLog = $testRoot + "-uninstall.log"
$beforeRegistry = @(Get-ProductionRegistrySnapshot)
$installed = $false

try {
    $installExitCode = Invoke-WaitingProcess -FilePath $InstallerPath -Arguments @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/DIR=$testRoot",
        "/LOG=$installLog"
    )
    if ($installExitCode -ne 0) {
        throw "The isolated installer returned exit code $installExitCode."
    }
    $installed = $true
    Assert-ProductionRegistryUnchanged -Before $beforeRegistry

    $privatePython = Join-Path $testRoot "runtime\python.exe"
    $launcher = Join-Path $testRoot "VemalsEars.exe"
    $companion = Join-Path $testRoot "app\Vemals-Ears.pyw"
    $memberEdition = Join-Path $testRoot "app\vemals-ears-member.json"
    $manifestPath = Join-Path $testRoot "payload-manifest.json"
    $license = Join-Path $testRoot "app\LICENSE"
    $sourceInstructions = Join-Path $testRoot "app\docs\SOURCE.txt"
    $sourceArchive = Join-Path $testRoot "app\Source\vemals-ears-source.zip"
    $dependencyNotices = Join-Path $testRoot "app\Installer\THIRD-PARTY-NOTICES.txt"
    $innoLicense = Join-Path $testRoot "app\Installer\INNO-SETUP-LICENSE.txt"
    foreach ($required in @($privatePython, $launcher, $companion, $memberEdition, $manifestPath,
                             $license, $sourceInstructions, $sourceArchive, $dependencyNotices, $innoLicense)) {
        if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
            throw "The isolated install is missing $required."
        }
    }

    & $privatePython -I -c "import tkinter, websockets, PIL, win32api, win32gui; tkinter.Tcl()"
    if ($LASTEXITCODE -ne 0) {
        throw "The installed private runtime failed its relocated import check."
    }

    $obsoleteAppFile = Join-Path $testRoot "app\obsolete-host-module.py"
    $obsoleteRuntimeFile = Join-Path $testRoot "runtime\obsolete-runtime.dll"
    [System.IO.File]::WriteAllText($obsoleteAppFile, "obsolete`n", [System.Text.Encoding]::ASCII)
    [System.IO.File]::WriteAllText($obsoleteRuntimeFile, "obsolete`n", [System.Text.Encoding]::ASCII)
    $upgradeExitCode = Invoke-WaitingProcess -FilePath $InstallerPath -Arguments @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        "/DIR=$testRoot",
        "/LOG=$installLog"
    )
    if ($upgradeExitCode -ne 0) {
        throw "The isolated reinstall returned exit code $upgradeExitCode."
    }
    foreach ($obsolete in @($obsoleteAppFile, $obsoleteRuntimeFile)) {
        if (Test-Path -LiteralPath $obsolete) {
            throw "The isolated reinstall retained an obsolete payload file."
        }
    }
    Assert-ProductionRegistryUnchanged -Before $beforeRegistry

    $memberClient = Join-Path $testRoot "app\GuildHub\client.py"
    $clientExitCode = Invoke-WaitingProcess -FilePath $privatePython -Arguments @(
        "-E",
        "-s",
        "-B",
        $memberClient,
        "--help"
    )
    if ($clientExitCode -ne 0) {
        throw "The installed thin caption client failed its private-runtime import check."
    }
    $companionExitCode = Invoke-WaitingProcess -FilePath $privatePython -Arguments @(
        "-E",
        "-s",
        "-B",
        "-c",
        "import runpy, sys; runpy.run_path(sys.argv[1], run_name='installer_smoke')",
        $companion
    )
    if ($companionExitCode -ne 0) {
        throw "The installed unified companion failed its private-runtime import check."
    }

    $manifest = Get-Content -Raw -LiteralPath $manifestPath | ConvertFrom-Json
    if (-not $manifest.generic -or $manifest.audience -ne "member" -or $manifest.guild_configuration_embedded) {
        throw "The installed payload is not marked as generic."
    }
    if ($manifest.PSObject.Properties.Name -contains "update_trust") {
        throw "An unsigned test build must not authorize automatic installer execution."
    }
    foreach ($record in $manifest.app_files) {
        $relative = [string]$record.path
        if ($relative.Contains("..") -or $relative.StartsWith("/") -or $relative.StartsWith("\")) {
            throw "The installed payload manifest contains an unsafe path."
        }
        $installedFile = Join-Path (Join-Path $testRoot "app") ($relative.Replace('/', '\'))
        $actualHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $installedFile).Hash.ToLowerInvariant()
        if ($actualHash -cne [string]$record.sha256) {
            throw "The installed payload hash does not match for $relative."
        }
    }

    foreach ($forbidden in @(
        "app\GuildHub\server.py",
        "app\GuildHub\host.py",
        "app\GuildHub\publisher.py",
        "app\GuildHub\store.py",
        "app\GearHelper\Tools\gearhelper_auto.py",
        "app\GearHelper\Tools\raidbots_import.py",
        "app\BossTalents\Tools\boss_talents_auto.py",
        "app\VoiceCaptions\discord_adapter",
        "app\VoiceCaptions\stt"
    )) {
        if (Test-Path -LiteralPath (Join-Path $testRoot $forbidden)) {
            throw "The member installer contains a host-only component: $forbidden"
        }
    }

    $savedCompanion = $companion + ".smoke-original"
    [System.IO.File]::Move($companion, $savedCompanion)
    $marker = Join-Path $testRoot "launcher-contract.txt"
    $stub = @'
import os
import sys
from pathlib import Path

Path(__file__).resolve().parent.parent.joinpath("launcher-contract.txt").write_text(
    str(os.getpid()) + "\n" + "\n".join(sys.argv[1:]) + "\n",
    encoding="ascii",
)
'@
    [System.IO.File]::WriteAllText($companion, $stub, [System.Text.Encoding]::ASCII)
    $pairingUri = "vemalsears://pair?code=smoke-test&source=installer"
    $canaryRoot = Join-Path $testRoot "pythonpath-canary"
    [System.IO.Directory]::CreateDirectory($canaryRoot) | Out-Null
    [System.IO.File]::WriteAllText(
        (Join-Path $canaryRoot "sitecustomize.py"),
        "import os`nos._exit(91)`n",
        [System.Text.Encoding]::ASCII
    )
    $oldPythonPath = $env:PYTHONPATH
    try {
        $env:PYTHONPATH = $canaryRoot
        $launchExitCode = Invoke-WaitingProcess -FilePath $launcher -Arguments @(
            "--vemals-ears-pair",
            $pairingUri
        )
    }
    finally {
        $env:PYTHONPATH = $oldPythonPath
    }
    if ($launchExitCode -ne 0) {
        throw "The installed launcher rejected a valid pairing link."
    }
    $deadline = [DateTime]::UtcNow.AddSeconds(10)
    while (-not (Test-Path -LiteralPath $marker -PathType Leaf) -and [DateTime]::UtcNow -lt $deadline) {
        Start-Sleep -Milliseconds 100
    }
    if (-not (Test-Path -LiteralPath $marker -PathType Leaf)) {
        throw "The installed launcher did not pass the pairing link to the companion."
    }
    $received = @(Get-Content -LiteralPath $marker)
    if (
        $received.Count -ne 3 -or
        $received[1] -cne "--vemals-ears-pair" -or
        $received[2] -cne $pairingUri
    ) {
        throw "The installed launcher changed the pairing arguments."
    }
    $captionChildPid = 0
    if (-not [int]::TryParse($received[0], [ref]$captionChildPid)) {
        throw "The installed launcher contract did not report its child process."
    }
    $childDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while (
        $null -ne (Get-Process -Id $captionChildPid -ErrorAction SilentlyContinue) -and
        [DateTime]::UtcNow -lt $childDeadline
    ) {
        Start-Sleep -Milliseconds 100
    }
    if ($null -ne (Get-Process -Id $captionChildPid -ErrorAction SilentlyContinue)) {
        throw "The installed launcher's child process did not exit after its contract check."
    }
    [System.IO.File]::Delete($companion)
    [System.IO.File]::Move($savedCompanion, $companion)
    [System.IO.File]::Delete($marker)
    [System.IO.File]::Delete((Join-Path $canaryRoot "sitecustomize.py"))
    [System.IO.Directory]::Delete($canaryRoot, $false)

    $uninstaller = Join-Path $testRoot "unins000.exe"
    $uninstallExitCode = Invoke-WaitingProcess -FilePath $uninstaller -Arguments @(
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/LOG=$uninstallLog"
    )
    if ($uninstallExitCode -ne 0) {
        throw "The isolated uninstaller returned exit code $uninstallExitCode."
    }
    $installed = $false
    $removeDeadline = [DateTime]::UtcNow.AddSeconds(10)
    while ((Test-Path -LiteralPath $testRoot) -and [DateTime]::UtcNow -lt $removeDeadline) {
        Start-Sleep -Milliseconds 100
    }
    if (Test-Path -LiteralPath $testRoot) {
        throw "The isolated uninstaller left its install directory behind."
    }
    Assert-ProductionRegistryUnchanged -Before $beforeRegistry
    Write-Output "Isolated installer smoke test passed."
}
finally {
    if ($installed) {
        $uninstaller = Join-Path $testRoot "unins000.exe"
        if (Test-Path -LiteralPath $uninstaller -PathType Leaf) {
            Invoke-WaitingProcess -FilePath $uninstaller -Arguments @(
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART"
            ) | Out-Null
        }
    }
    if (Test-Path -LiteralPath $testRoot) {
        $resolved = [System.IO.Path]::GetFullPath($testRoot)
        if ($resolved.StartsWith($tempPrefix, [System.StringComparison]::OrdinalIgnoreCase)) {
            Remove-Item -LiteralPath $resolved -Recurse -Force
        }
    }
    foreach ($log in @($installLog, $uninstallLog)) {
        if (Test-Path -LiteralPath $log -PathType Leaf) {
            Remove-TemporaryFile -Path $log
        }
    }
}
