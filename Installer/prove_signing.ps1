[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$OutputDirectory = "",
    [string]$IsccPath = ""
)

<#
Prove the signing pipeline end to end without buying anything.

Creates a throwaway self-signed code-signing certificate in the current
user's store with a NON-EXPORTABLE key - the same shape a hardware token
or cloud HSM presents - builds a real installer through the ordinary
build_installer.ps1 path with -SigningProvider store, checks that Inno
Setup handed exactly three files to the signer (launcher, uninstaller,
setup), verifies every signature is by that certificate, and removes the
certificate again whether or not anything failed.

The output is Vemals-Ears-Setup-PROOF-UNTRUSTED.exe. It is signed by a
certificate nobody trusts and must never be distributed; it exists to
show that the day a real certificate is in the store, signing a release
is one thumbprint away.
#>

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest

$installerRoot = $PSScriptRoot
$subject = "CN=Downtime Signing Proof"
$log = Join-Path ([System.IO.Path]::GetTempPath()) (
    "downtime-signing-proof-" + [guid]::NewGuid().ToString("n") + ".log"
)

$certificate = New-SelfSignedCertificate `
    -Type CodeSigningCert `
    -Subject $subject `
    -CertStoreLocation "Cert:\CurrentUser\My" `
    -NotAfter (Get-Date).AddDays(1) `
    -KeyExportPolicy NonExportable `
    -KeyUsage DigitalSignature
$thumbprint = $certificate.Thumbprint
$storePath = "Cert:\CurrentUser\My\" + $thumbprint
Write-Output "Proof certificate $thumbprint created (non-exportable, expires in one day)."

try {
    $buildArguments = @{
        Signed                = $true
        SigningProvider       = "store"
        SigningThumbprint     = $thumbprint
        SigningTimestampUrl   = "none"
        AllowUntrustedSigner  = $true
        ExpectedPublisher     = $subject
        SigningInvocationLog  = $log
    }
    if (-not [string]::IsNullOrWhiteSpace($Version)) { $buildArguments.Version = $Version }
    if (-not [string]::IsNullOrWhiteSpace($OutputDirectory)) { $buildArguments.OutputDirectory = $OutputDirectory }
    if (-not [string]::IsNullOrWhiteSpace($IsccPath)) { $buildArguments.IsccPath = $IsccPath }

    & (Join-Path $installerRoot "build_installer.ps1") @buildArguments

    if (-not (Test-Path -LiteralPath $log -PathType Leaf)) {
        throw "The signer was never invoked - Inno Setup did not call sign_file.ps1 at all."
    }
    $signed = @(Get-Content -LiteralPath $log)
    if ($signed.Count -ne 3) {
        throw ("Expected Inno Setup to hand exactly 3 files to the signer " +
            "(launcher, uninstaller, installer); it handed " + $signed.Count +
            ": " + ($signed -join ", "))
    }
    $names = @($signed | ForEach-Object { [System.IO.Path]::GetFileName($_) })
    foreach ($expected in @("VemalsEars.exe", "Vemals-Ears-Setup-PROOF-UNTRUSTED.exe")) {
        if ($names -notcontains $expected) {
            throw "The signer never saw ${expected}. It saw: $($names -join ', ')"
        }
    }
    $uninstaller = @($names | Where-Object { $_ -notin @("VemalsEars.exe", "Vemals-Ears-Setup-PROOF-UNTRUSTED.exe") })
    if ($uninstaller.Count -ne 1) {
        throw "Expected exactly one uninstaller stub to be signed; saw: $($names -join ', ')"
    }

    Write-Output ""
    Write-Output "PROOF COMPLETE"
    Write-Output "  signer invoked 3 times: launcher, uninstaller ($($uninstaller[0])), installer"
    Write-Output "  every signature made by $thumbprint"
    Write-Output "  the key was never exportable and no password was ever typed"
    Write-Output "  output is PROOF-UNTRUSTED and must not be distributed"
}
finally {
    if (Test-Path -LiteralPath $storePath) {
        Remove-Item -LiteralPath $storePath -Force
    }
    # Verifying the signature makes Windows' chain engine cache the
    # self-signed signer in another user store (it turned up in
    # CurrentUser\CA on 2026-09-03). Sweep every store by thumbprint, or
    # a "removed" certificate is still on the machine.
    # The recursive listing yields store containers as well as
    # certificates, and under strict mode reading .Thumbprint on a
    # container is an exception rather than $null - which is how the
    # first version of this sweep aborted and left a copy behind.
    $certificateType = [System.Security.Cryptography.X509Certificates.X509Certificate2]
    Get-ChildItem Cert:\CurrentUser -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_ -is $certificateType -and $_.Thumbprint -eq $thumbprint } |
        ForEach-Object { Remove-Item -LiteralPath $_.PSPath -Force }
    if (Test-Path -LiteralPath $log) {
        Remove-Item -LiteralPath $log -Force
    }
    $remaining = @(Get-ChildItem Cert:\CurrentUser -Recurse -ErrorAction SilentlyContinue |
        Where-Object { $_ -is $certificateType -and $_.Thumbprint -eq $thumbprint })
    if ($remaining.Count -gt 0) {
        Write-Error "The proof certificate $thumbprint is STILL in Cert:\CurrentUser - remove it by hand."
    }
    else {
        Write-Output "Proof certificate $thumbprint removed from every user store."
    }
}
