[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$IsccPath = "",
    [string]$OutputDirectory = "",
    [switch]$Signed,
    [switch]$TestBuild,
    # Build an unsigned installer intended to be handed to real members.
    # Signing remains the goal; until the certificate exists this is the
    # honest way to ship, and it has to be an explicit flag rather than a
    # default so nobody publishes an unsigned build by accident.
    [switch]$AllowUnsigned,
    [string]$SigningMetadataPath = "",
    [string]$ExpectedPublisher = "",
    # Which signer holds the key - see sign_file.ps1. "artifact" is Azure
    # Artifact Signing; "store" is any certificate in the current user's
    # Windows store, which is how hardware tokens and cloud HSMs present.
    [ValidateSet("artifact", "store")]
    [string]$SigningProvider = "artifact",
    [string]$SigningThumbprint = "",
    [string]$SigningTimestampUrl = "",
    # Proof builds only: sign with a certificate nobody trusts, to prove
    # the whole pipeline before a real one is bought. The output is named
    # so it can never be mistaken for a release.
    [switch]$AllowUntrustedSigner,
    [string]$SigningInvocationLog = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$installerRoot = $PSScriptRoot
$repoRoot = (Resolve-Path -LiteralPath (Join-Path $installerRoot "..")).Path
$lockPath = Join-Path $installerRoot "runtime-lock.json"
$lock = Get-Content -Raw -LiteralPath $lockPath | ConvertFrom-Json
$runtimeSourcesLockPath = Join-Path $installerRoot "runtime-sources.lock.json"
$runtimeSourcesLockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath $runtimeSourcesLockPath).Hash

# Argument validation comes before any tool is probed: a wrong flag
# combination should fail in milliseconds, and be testable on a machine
# that has no Inno Setup at all.
if ($Signed -and $TestBuild) {
    throw "Test builds are intentionally unsigned and cannot use -Signed."
}
if ($AllowUnsigned -and $Signed) {
    throw "-AllowUnsigned describes a build with no signature; drop one of the two."
}
if ($AllowUnsigned -and $TestBuild) {
    throw "-TestBuild uses an isolated test location and cannot select the distribution filename."
}
if ($AllowUntrustedSigner -and -not $Signed) {
    throw "-AllowUntrustedSigner only means something for a -Signed proof build."
}
if ($AllowUntrustedSigner -and $SigningProvider -ne "store") {
    throw "A proof build signs from the certificate store; -AllowUntrustedSigner requires -SigningProvider store."
}

if ($Signed) {
    if ([string]::IsNullOrWhiteSpace($ExpectedPublisher)) {
        throw "The exact validated publisher subject is required for signed builds."
    }
    switch ($SigningProvider) {
        "artifact" {
            if ([string]::IsNullOrWhiteSpace($SigningMetadataPath) -or
                -not (Test-Path -LiteralPath $SigningMetadataPath -PathType Leaf)) {
                throw "A temporary Artifact Signing metadata file is required for signed builds."
            }
        }
        "store" {
            if ($SigningThumbprint -notmatch '^[0-9A-Fa-f]{40}$') {
                throw "A 40-hex-digit certificate thumbprint is required for -SigningProvider store."
            }
        }
    }
}

if ([string]::IsNullOrWhiteSpace($Version)) {
    $versionLine = Get-Content -LiteralPath (Join-Path $repoRoot "GuildHub\version.py") |
        Where-Object { $_ -like 'CLIENT_VERSION = *' } |
        Select-Object -First 1
    if ([string]::IsNullOrWhiteSpace($versionLine)) {
        throw "GuildHub/version.py does not declare an installer version."
    }
    $Version = ($versionLine -split "=", 2)[1].Trim().Trim('"')
}
if ($Version -notmatch '^\d+\.\d+\.\d+(?:\.\d+)?$') {
    throw "Version must contain three or four numeric components."
}

if ([string]::IsNullOrWhiteSpace($OutputDirectory)) {
    $OutputDirectory = Join-Path $repoRoot "dist"
}
$OutputDirectory = [System.IO.Path]::GetFullPath($OutputDirectory)
[System.IO.Directory]::CreateDirectory($OutputDirectory) | Out-Null

if ([string]::IsNullOrWhiteSpace($IsccPath)) {
    $IsccPath = "C:\Program Files\Inno Setup 7\ISCC.exe"
}
if (-not (Test-Path -LiteralPath $IsccPath -PathType Leaf)) {
    throw "Inno Setup 7.1.0 is required. Pass its ISCC.exe path with -IsccPath."
}
$isccVersion = (& $IsccPath --version | Select-Object -First 1).Trim()
if ($isccVersion -cne $lock.inno_setup.version) {
    throw "ISCC.exe must be the locked Inno Setup version $($lock.inno_setup.version)."
}


$cacheRoot = Join-Path $installerRoot "cache"
[System.IO.Directory]::CreateDirectory($cacheRoot) | Out-Null
$runName = "run-" + (Get-Date -Format "yyyyMMddHHmmss") + "-" + [guid]::NewGuid().ToString("N")
$runRoot = Join-Path (Join-Path $installerRoot "build") $runName
$stageRoot = Join-Path $runRoot "stage"
[System.IO.Directory]::CreateDirectory($stageRoot) | Out-Null

function Get-LockedDownload {
    param(
        [Parameter(Mandatory = $true)] [string]$Url,
        [Parameter(Mandatory = $true)] [string]$Sha256,
        [Parameter(Mandatory = $true)] [string]$FileName
    )
    $target = Join-Path $cacheRoot $FileName
    $needsDownload = $true
    if (Test-Path -LiteralPath $target -PathType Leaf) {
        $existing = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
        $needsDownload = $existing -cne $Sha256
    }
    if ($needsDownload) {
        Invoke-WebRequest -Uri $Url -OutFile $target
    }
    $actual = (Get-FileHash -Algorithm SHA256 -LiteralPath $target).Hash.ToLowerInvariant()
    if ($actual -cne $Sha256) {
        throw "Hash verification failed for $FileName."
    }
    return $target
}

function Expand-LockedZip {
    param(
        [Parameter(Mandatory = $true)] [string]$ArchivePath,
        [Parameter(Mandatory = $true)] [string]$DestinationPath
    )
    Add-Type -AssemblyName System.IO.Compression.FileSystem
    $destination = [System.IO.Path]::GetFullPath($DestinationPath)
    [System.IO.Directory]::CreateDirectory($destination) | Out-Null
    $prefix = $destination.TrimEnd('\') + '\'
    $archive = [System.IO.Compression.ZipFile]::OpenRead($ArchivePath)
    try {
        foreach ($entry in $archive.Entries) {
            $target = [System.IO.Path]::GetFullPath((Join-Path $destination $entry.FullName))
            if (-not $target.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase)) {
                throw "The locked runtime archive contains an unsafe path."
            }
            if ([string]::IsNullOrEmpty($entry.Name)) {
                [System.IO.Directory]::CreateDirectory($target) | Out-Null
                continue
            }
            [System.IO.Directory]::CreateDirectory([System.IO.Path]::GetDirectoryName($target)) | Out-Null
            [System.IO.Compression.ZipFileExtensions]::ExtractToFile($entry, $target, $false)
        }
    }
    finally {
        $archive.Dispose()
    }
}

$pythonArchive = Get-LockedDownload `
    -Url $lock.python.url `
    -Sha256 $lock.python.sha256 `
    -FileName ("python-" + $lock.python.version + "-amd64.zip")
$runtimeRoot = Join-Path $stageRoot "runtime"
Expand-LockedZip -ArchivePath $pythonArchive -DestinationPath $runtimeRoot

$wheelPaths = @()
foreach ($wheel in $lock.wheels) {
    $wheelPaths += Get-LockedDownload `
        -Url $wheel.url `
        -Sha256 $wheel.sha256 `
        -FileName $wheel.filename
}

$privatePython = Join-Path $runtimeRoot "python.exe"
& $privatePython -E -s -B -m pip install --disable-pip-version-check --no-index --no-deps `
    --no-compile --no-warn-script-location $wheelPaths
if ($LASTEXITCODE -ne 0) {
    throw "The locked Python wheels could not be installed."
}
& $privatePython -E -s -B (Join-Path $repoRoot "packaging\runtime_licenses.py") $runtimeRoot --preserve
if ($LASTEXITCODE -ne 0) {
    throw "Required bundled-runtime license notices could not be preserved."
}

foreach ($relative in @("Doc", "include", "libs", "Lib\ensurepip", "Lib\idlelib", "Scripts")) {
    $target = Join-Path $runtimeRoot $relative
    if (Test-Path -LiteralPath $target) {
        [System.IO.Directory]::Delete($target, $true)
    }
}
foreach ($relative in @("Lib\site-packages\pip", "Lib\site-packages\pip-26.2.1.dist-info")) {
    $target = Join-Path $runtimeRoot $relative
    if (Test-Path -LiteralPath $target) {
        [System.IO.Directory]::Delete($target, $true)
    }
}
& $privatePython -E -s -B -c "import tkinter, websockets, PIL, win32api, win32gui; tkinter.Tcl()"
if ($LASTEXITCODE -ne 0) {
    throw "The relocated private runtime failed its import check."
}
& $privatePython -E -s -B (Join-Path $repoRoot "packaging\runtime_licenses.py") $runtimeRoot
if ($LASTEXITCODE -ne 0) {
    throw "Required bundled-runtime license notices did not survive pruning."
}
& $privatePython -E -s -B (Join-Path $repoRoot "packaging\runtime_sources.py") `
    --runtime $runtimeRoot --cache $cacheRoot
if ($LASTEXITCODE -ne 0) {
    throw "The matching upstream runtime sources could not be staged and verified."
}

Get-ChildItem -LiteralPath $runtimeRoot -Recurse -File -Filter "direct_url.json" |
    ForEach-Object { [System.IO.File]::Delete($_.FullName) }
Get-ChildItem -LiteralPath $runtimeRoot -Recurse -File -Filter "*.pyc" |
    ForEach-Object { [System.IO.File]::Delete($_.FullName) }
Get-ChildItem -LiteralPath $runtimeRoot -Recurse -Directory -Filter "__pycache__" |
    Sort-Object { $_.FullName.Length } -Descending |
    ForEach-Object { [System.IO.Directory]::Delete($_.FullName, $true) }

$stageArguments = @(
    (Join-Path $installerRoot "stage_installer.py"),
    "--source", $repoRoot,
    "--stage", $stageRoot,
    "--version", $Version
)
if ($Signed) {
    $stageArguments += "--require-clean"
}
& $privatePython -E -s -B @stageArguments
if ($LASTEXITCODE -ne 0) {
    throw "The generic application payload could not be staged."
}
$installerBuildRoot = Join-Path $stageRoot "source\Installer"
$sourceArchivePath = Join-Path $stageRoot "app\Source\vemals-ears-source.zip"
# Compile the exact first-party source snapshot distributed in the app.
# The runtime was staged from this lock before the source snapshot was made.
$snapshotLock = Get-Content -Raw -LiteralPath (Join-Path $installerBuildRoot "runtime-lock.json") | ConvertFrom-Json
if (($lock | ConvertTo-Json -Depth 20 -Compress) -cne ($snapshotLock | ConvertTo-Json -Depth 20 -Compress)) {
    throw "The runtime lock changed while preparing the corresponding source."
}
$snapshotSourcesLockHash = (Get-FileHash -Algorithm SHA256 -LiteralPath `
    (Join-Path $installerBuildRoot "runtime-sources.lock.json")).Hash
if ($runtimeSourcesLockHash -cne $snapshotSourcesLockHash) {
    throw "The upstream source lock changed while preparing the corresponding source."
}

$versionParts = @($Version.Split('.') | ForEach-Object { [int]$_ })
while ($versionParts.Count -lt 4) {
    $versionParts += 0
}
$vswhere = "${env:ProgramFiles(x86)}\Microsoft Visual Studio\Installer\vswhere.exe"
if (-not (Test-Path -LiteralPath $vswhere -PathType Leaf)) {
    throw "Visual Studio Build Tools are required to compile the signed launcher."
}
$visualStudio = & $vswhere -latest -products * `
    -requires Microsoft.VisualStudio.Component.VC.Tools.x86.x64 `
    -property installationPath
if ([string]::IsNullOrWhiteSpace($visualStudio)) {
    throw "Visual Studio C++ Build Tools are unavailable."
}
$vcvars = Join-Path $visualStudio "VC\Auxiliary\Build\vcvars64.bat"
$resourcePath = Join-Path $stageRoot "launcher.res"
$launcherPath = Join-Path $stageRoot "VemalsEars.exe"
$rcDefines = "/dAPP_VERSION_MAJOR=$($versionParts[0]) /dAPP_VERSION_MINOR=$($versionParts[1]) /dAPP_VERSION_PATCH=$($versionParts[2]) /dAPP_VERSION_BUILD=$($versionParts[3])"
$compile = 'call "' + $vcvars + '" >nul && cd /d "' + $stageRoot +
    '" && rc.exe /nologo ' + $rcDefines + ' /fo"' + $resourcePath + '" "' +
    (Join-Path $installerBuildRoot "launcher.rc") + '" && cl.exe /nologo /O2 /MT /W4 /WX ' +
    '/Fe:"' + $launcherPath + '" "' +
    (Join-Path $installerBuildRoot "launcher.c") + '" "' + $resourcePath +
    '" /link user32.lib shell32.lib advapi32.lib /SUBSYSTEM:WINDOWS /DYNAMICBASE /NXCOMPAT /HIGHENTROPYVA'
& cmd.exe /d /s /c $compile
if ($LASTEXITCODE -ne 0 -or -not (Test-Path -LiteralPath $launcherPath -PathType Leaf)) {
    throw "The native Vemal's Ears launcher could not be compiled."
}
& $privatePython -E -s -B (Join-Path $stageRoot "source\source_distribution.py") --verify-stage $stageRoot
if ($LASTEXITCODE -ne 0) {
    throw "The final installer source, payload or runtime distribution verification failed."
}

$isccArguments = @(
    "--quiet-progress",
    "/DAppVersion=$Version",
    "/DStageRoot=$stageRoot",
    "/DOutputDir=$OutputDirectory"
)
if ($TestBuild) {
    $isccArguments += "/DTestBuild=1"
}
if ($AllowUnsigned) {
    $isccArguments += "/DDistributeUnsigned=1"
}
if ($AllowUntrustedSigner) {
    $isccArguments += "/DProofUntrusted=1"
}
if ($Signed) {
    # One command serves every file Inno Setup signs: the launcher (via
    # signonce), the uninstaller (SignedUninstaller=yes) and the setup
    # executable itself. $q and $f are Inno's placeholders for a quote
    # and the file being signed - they are literal here on purpose.
    $signScript = Join-Path $installerBuildRoot "sign_file.ps1"
    $signCommand = 'powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -File $q' +
        $signScript + '$q -Provider ' + $SigningProvider
    if ($SigningProvider -eq "artifact") {
        $signCommand += ' -MetadataPath $q' + $SigningMetadataPath + '$q'
    }
    else {
        $signCommand += ' -Thumbprint ' + $SigningThumbprint
    }
    if (-not [string]::IsNullOrWhiteSpace($SigningTimestampUrl)) {
        $signCommand += ' -TimestampUrl $q' + $SigningTimestampUrl + '$q'
    }
    if ($AllowUntrustedSigner) {
        $signCommand += ' -AllowUntrustedRoot'
    }
    if (-not [string]::IsNullOrWhiteSpace($SigningInvocationLog)) {
        $signCommand += ' -InvocationLog $q' + $SigningInvocationLog + '$q'
    }
    $signCommand += ' -FilePath $f'
    $isccArguments += "/DSignedBuild=1"
    $isccArguments += "/Sartifact=$signCommand"
}
$isccArguments += Join-Path $installerBuildRoot "VemalsEars.iss"
& $IsccPath @isccArguments
if ($LASTEXITCODE -ne 0) {
    throw "Inno Setup could not build the installer."
}

$outputName = if ($AllowUntrustedSigner) {
    # Signed by a certificate nobody trusts. Named so it can never be
    # mistaken for a release, the same way test builds are.
    "Vemals-Ears-Setup-PROOF-UNTRUSTED.exe"
}
elseif ($Signed -or $AllowUnsigned) {
    "Vemals-Ears-Setup.exe"
}
elseif ($TestBuild) {
    "Vemals-Ears-Setup-TEST-UNSIGNED.exe"
}
else {
    "Vemals-Ears-Setup-UNSIGNED.exe"
}
$installerPath = Join-Path $OutputDirectory $outputName
if (-not (Test-Path -LiteralPath $installerPath -PathType Leaf)) {
    throw "The expected installer output is missing."
}

if ($Signed) {
    $verifyScript = Join-Path $installerBuildRoot "verify_signature.ps1"
    $verifyArguments = @{ ExpectedPublisher = $ExpectedPublisher }
    if ($SigningProvider -eq "store") {
        $verifyArguments.ExpectedThumbprint = $SigningThumbprint
    }
    if ($AllowUntrustedSigner) {
        $verifyArguments.AllowUntrustedRoot = $true
    }
    & $verifyScript -FilePath $launcherPath @verifyArguments
    & $verifyScript -FilePath $installerPath @verifyArguments
    $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerPath).Hash.ToLowerInvariant()
    $checksumPath = $installerPath + ".sha256"
    [System.IO.File]::WriteAllText(
        $checksumPath,
        $digest + "  " + [System.IO.Path]::GetFileName($installerPath) + "`n",
        [System.Text.Encoding]::ASCII
    )
    if ($AllowUntrustedSigner) {
        Write-Warning "PROOF BUILD signed by an untrusted certificate - this is not an official release."
    }
    Write-Output "Signed installer: $installerPath"
    Write-Output "Post-sign checksum: $checksumPath"
}
elseif ($AllowUnsigned) {
    # A checksum for an unsigned build is not a signature and cannot
    # prove who made it. It proves the file did not change between here
    # and the relay, which is the one thing it can honestly prove.
    $digest = (Get-FileHash -Algorithm SHA256 -LiteralPath $installerPath).Hash.ToLowerInvariant()
    $checksumPath = $installerPath + ".sha256"
    [System.IO.File]::WriteAllText(
        $checksumPath,
        $digest + "  " + [System.IO.Path]::GetFileName($installerPath) + "`n",
        [System.Text.Encoding]::ASCII
    )
    Write-Output "Unsigned installer for distribution: $installerPath"
    Write-Output "Checksum: $checksumPath"
    Write-Output ""
    Write-Output "What a member will see, in this order:"
    Write-Output "  1. Their browser may warn that the file is not commonly downloaded."
    Write-Output "  2. Windows shows a blue 'Windows protected your PC' screen."
    Write-Output "     They must choose 'More info', then 'Run anyway'. There is no"
    Write-Output "     other way through it, and the button is hidden until they do."
    Write-Output "  3. No administrator prompt: it installs for the current user only."
    Write-Output ""
    Write-Output "Tell them to expect step 2 BEFORE they download. Somebody who was"
    Write-Output "warned reads it as expected; somebody who was not reads it as a virus."
}
else {
    Write-Warning "This installer is unsigned and is not an official release. Use -AllowUnsigned for the distribution filename."
    Write-Output "Unsigned test installer: $installerPath"
}
Copy-Item -LiteralPath $sourceArchivePath -Destination (Join-Path $OutputDirectory "vemals-ears-source.zip") -Force
Write-Output "Matching source archive: $(Join-Path $OutputDirectory 'vemals-ears-source.zip')"
