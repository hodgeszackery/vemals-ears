[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,

    # Which signer holds the key.
    #
    #   artifact  Azure Artifact Signing (formerly Trusted Signing). The key
    #             lives in Microsoft's HSM and is reached through a metadata
    #             file. Requires ArtifactSigningClientTools and an account
    #             that, as of 2026-09, needs a three-year-old organisation.
    #
    #   store     Any code-signing certificate in the current user's
    #             Windows certificate store. This is how every hardware
    #             token and cloud-HSM provider presents itself on Windows:
    #             the certificate is visible, the private key never is, and
    #             the provider's KSP does the signing. It is the path an
    #             individual developer's certificate takes.
    #
    # There is deliberately no .pfx option and no password parameter. Since
    # June 2023 every CA has required the key on FIPS 140-2 hardware, so a
    # loose .pfx no longer exists to be pointed at - and a password on a
    # command line is a password in a build log.
    [ValidateSet("artifact", "store")]
    [string]$Provider = "artifact",

    [string]$MetadataPath = "",
    [string]$Thumbprint = "",

    # RFC3161 timestamp authority. Empty picks the provider's default.
    # "none" signs without a timestamp - acceptable only for a local proof,
    # because an untimestamped signature dies with its certificate.
    [string]$TimestampUrl = "",

    # Accept a signer whose chain does not end in a trusted root. This
    # exists for one purpose: proving the pipeline end to end with a
    # self-signed certificate before any real one is bought. Nothing signed
    # under it may be distributed; build_installer.ps1 names such output so
    # it cannot be mistaken for a release.
    [switch]$AllowUntrustedRoot,

    # Append one line per file signed, so a proof can count how many files
    # Inno Setup actually handed to the signer.
    [string]$InvocationLog = ""
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
    throw "Required signing file is missing: $FilePath"
}

function Find-WindowsKitSignTool {
    $pinned = "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe"
    if (Test-Path -LiteralPath $pinned -PathType Leaf) {
        return $pinned
    }
    $kits = "C:\Program Files (x86)\Windows Kits\10\bin"
    if (Test-Path -LiteralPath $kits -PathType Container) {
        $candidate = Get-ChildItem -LiteralPath $kits -Directory |
            Where-Object { $_.Name -like "10.*" } |
            Sort-Object { [version]$_.Name } -Descending |
            ForEach-Object { Join-Path $_.FullName "x64\signtool.exe" } |
            Where-Object { Test-Path -LiteralPath $_ -PathType Leaf } |
            Select-Object -First 1
        if ($candidate) {
            return $candidate
        }
    }
    throw "A Windows SDK signtool.exe is required for -Provider store."
}

switch ($Provider) {
    "artifact" {
        if ([string]::IsNullOrWhiteSpace($MetadataPath) -or
            -not (Test-Path -LiteralPath $MetadataPath -PathType Leaf)) {
            throw "-Provider artifact requires -MetadataPath naming the Artifact Signing metadata file."
        }
        $toolsRoot = "C:\Program Files (x86)\Microsoft\ArtifactSigningClientTools\bin"
        $signTool = Join-Path $toolsRoot "signtool.exe"
        $dlib = Join-Path $toolsRoot "Azure.CodeSigning.Dlib.dll"
        foreach ($required in @($signTool, $dlib)) {
            if (-not (Test-Path -LiteralPath $required -PathType Leaf)) {
                throw "Required signing file is missing: $required"
            }
        }
        if ([string]::IsNullOrWhiteSpace($TimestampUrl)) {
            $TimestampUrl = "http://timestamp.acs.microsoft.com"
        }
        $signArguments = @("sign", "/v", "/fd", "SHA256")
        if ($TimestampUrl -ne "none") {
            $signArguments += @("/tr", $TimestampUrl, "/td", "SHA256")
        }
        $signArguments += @("/dlib", $dlib, "/dmdf", $MetadataPath, $FilePath)
    }
    "store" {
        if ($Thumbprint -notmatch '^[0-9A-Fa-f]{40}$') {
            throw "-Provider store requires -Thumbprint: the 40 hex digits of a certificate in Cert:\CurrentUser\My."
        }
        $signTool = Find-WindowsKitSignTool
        if ([string]::IsNullOrWhiteSpace($TimestampUrl)) {
            $TimestampUrl = "http://timestamp.digicert.com"
        }
        $signArguments = @("sign", "/v", "/fd", "SHA256", "/sha1", $Thumbprint)
        if ($TimestampUrl -ne "none") {
            $signArguments += @("/tr", $TimestampUrl, "/td", "SHA256")
        }
        $signArguments += $FilePath
    }
}

& $signTool @signArguments
if ($LASTEXITCODE -ne 0) {
    throw "Signing failed for $FilePath (provider $Provider)"
}

if ($AllowUntrustedRoot) {
    # A self-signed chain fails /pa by design, so the proof checks the
    # thing that actually matters here: a well-formed Authenticode
    # signature made by exactly the certificate we said.
    $signature = Get-AuthenticodeSignature -LiteralPath $FilePath
    $accepted = @(
        [System.Management.Automation.SignatureStatus]::Valid,
        [System.Management.Automation.SignatureStatus]::UnknownError
    )
    if ($accepted -notcontains $signature.Status) {
        throw "No Authenticode signature landed on ${FilePath}: $($signature.Status)"
    }
    if ($null -eq $signature.SignerCertificate) {
        throw "The signer certificate is missing for $FilePath"
    }
    if ($signature.SignerCertificate.Thumbprint -ne $Thumbprint.ToUpperInvariant()) {
        throw "Signed by the wrong certificate: $($signature.SignerCertificate.Thumbprint)"
    }
    Write-Warning "UNTRUSTED ROOT ACCEPTED for $FilePath - proof mode only, never distribute."
}
else {
    & $signTool verify /pa /all /v $FilePath
    if ($LASTEXITCODE -ne 0) {
        throw "Signature verification failed for $FilePath"
    }
}

if (-not [string]::IsNullOrWhiteSpace($InvocationLog)) {
    Add-Content -LiteralPath $InvocationLog -Value $FilePath -Encoding ASCII
}
