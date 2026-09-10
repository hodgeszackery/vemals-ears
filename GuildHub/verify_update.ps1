[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)]
    [string]$FilePath,

    [Parameter(Mandatory = $true)]
    [string]$ExpectedPublisher
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

if (-not (Test-Path -LiteralPath $FilePath -PathType Leaf)) {
    throw "The update installer is missing."
}
if ([string]::IsNullOrWhiteSpace($ExpectedPublisher)) {
    throw "The trusted update publisher is missing."
}

$signature = Get-AuthenticodeSignature -LiteralPath $FilePath
if ($signature.Status -ne [System.Management.Automation.SignatureStatus]::Valid) {
    throw "The update installer does not have a valid Authenticode signature."
}
if ($null -eq $signature.SignerCertificate -or
    $signature.SignerCertificate.Subject -cne $ExpectedPublisher) {
    throw "The update installer was signed by an unexpected publisher."
}
if ($null -eq $signature.TimeStamperCertificate) {
    throw "The update installer does not have an RFC3161 timestamp."
}

$version = (Get-Item -LiteralPath $FilePath).VersionInfo
if ($version.ProductName -cne "Vemal's Ears" -or
    $version.FileDescription -cne "Vemal's Ears Setup") {
    throw "The signed file is not a Vemal's Ears installer."
}
