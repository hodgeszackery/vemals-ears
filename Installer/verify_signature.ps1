[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedPublisher,

    # Pin the exact certificate as well as the subject. Optional in strict
    # mode; mandatory when the chain is not trusted, because then the
    # subject string is all a stranger's certificate would need to match.
    [string]$ExpectedThumbprint = "",

    # Accept a signer whose chain does not end in a trusted root. Proof
    # mode only - see sign_file.ps1. A release verify never passes this.
    [switch]$AllowUntrustedRoot
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
    throw "Signed file is missing: $FilePath"
}
if ([string]::IsNullOrWhiteSpace($ExpectedPublisher)) {
    throw "ExpectedPublisher must name the validated signing identity."
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedThumbprint) -and
    $ExpectedThumbprint -notmatch '^[0-9A-Fa-f]{40}$') {
    throw "ExpectedThumbprint must be the 40 hex digits of the signing certificate."
}
if ($AllowUntrustedRoot -and [string]::IsNullOrWhiteSpace($ExpectedThumbprint)) {
    throw "-AllowUntrustedRoot requires -ExpectedThumbprint: with no trusted chain, the thumbprint is the only thing that pins the signer."
}

$signature = Get-AuthenticodeSignature -LiteralPath $FilePath

if ($AllowUntrustedRoot) {
    # UnknownError is what Windows reports for a chain that ends in an
    # untrusted root - exactly what a self-signed proof certificate is.
    $accepted = @(
        [System.Management.Automation.SignatureStatus]::Valid,
        [System.Management.Automation.SignatureStatus]::UnknownError
    )
    # An unsigned file reports UnknownError with no certificate at all, so
    # the certificate check comes first: no certificate means no signature,
    # whatever the status word says.
    if ($null -eq $signature.SignerCertificate) {
        throw "No Authenticode signature is present on ${FilePath}: $($signature.Status)"
    }
    if ($accepted -notcontains $signature.Status) {
        throw "No Authenticode signature is present on ${FilePath}: $($signature.Status)"
    }
    if ($signature.SignerCertificate.Thumbprint -ne $ExpectedThumbprint.ToUpperInvariant()) {
        throw "Unexpected signer for ${FilePath}: $($signature.SignerCertificate.Thumbprint)"
    }
    if ($signature.SignerCertificate.Subject -cne $ExpectedPublisher) {
        throw "Unexpected publisher for ${FilePath}: $($signature.SignerCertificate.Subject)"
    }
    Write-Warning "UNTRUSTED ROOT ACCEPTED for $FilePath - proof mode only, never distribute."
    return
}

if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "Authenticode is not valid for ${FilePath}: $($signature.Status)"
}
if ($null -eq $signature.SignerCertificate) {
    throw "The signer certificate is missing for $FilePath"
}
if ($signature.SignerCertificate.Subject -cne $ExpectedPublisher) {
    throw "Unexpected publisher for ${FilePath}: $($signature.SignerCertificate.Subject)"
}
if (-not [string]::IsNullOrWhiteSpace($ExpectedThumbprint) -and
    $signature.SignerCertificate.Thumbprint -ne $ExpectedThumbprint.ToUpperInvariant()) {
    throw "Unexpected signer for ${FilePath}: $($signature.SignerCertificate.Thumbprint)"
}
if ($null -eq $signature.TimeStamperCertificate) {
    throw "The RFC3161 timestamp is missing for $FilePath"
}

$signToolCandidates = @(
    "C:\Program Files (x86)\Microsoft\ArtifactSigningClientTools\bin\signtool.exe",
    "C:\Program Files (x86)\Windows Kits\10\bin\10.0.26100.0\x64\signtool.exe"
)
$signTool = $signToolCandidates | Where-Object {
    Test-Path -LiteralPath $_ -PathType Leaf
} | Select-Object -First 1
if ([string]::IsNullOrWhiteSpace($signTool)) {
    throw "A compatible SignTool is unavailable."
}

& $signTool verify /pa /all /v $FilePath
if ($LASTEXITCODE -ne 0) {
    throw "SignTool verification failed for $FilePath"
}
