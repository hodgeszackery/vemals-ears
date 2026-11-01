# AES only. The Python caller authenticates bundles before requesting decryption.
# Protocol: version byte, operation byte, key[32], IV[16], uint32 LE length, data.
# Sensitive data uses binary anonymous pipes, never arguments or temporary files.
$ErrorActionPreference = 'Stop'
$reader = $null; $aes = $null; $transform = $null
$key = $null; $iv = $null; $data = $null; $result = $null
$failed = $false
try {
    $reader = [IO.BinaryReader]::new([Console]::OpenStandardInput())
    if ($reader.ReadByte() -ne 1) { throw 'Invalid request.' }
    $operation = $reader.ReadByte()
    if ($operation -notin @(1, 2)) { throw 'Invalid request.' }
    $key = $reader.ReadBytes(32)
    $iv = $reader.ReadBytes(16)
    $length = $reader.ReadUInt32()
    if ($key.Length -ne 32 -or $iv.Length -ne 16 -or $length -gt 2097168) { throw 'Invalid request.' }
    if ($operation -eq 2 -and ($length -lt 16 -or ($length % 16) -ne 0)) { throw 'Invalid request.' }
    $data = $reader.ReadBytes([int]$length)
    if ($data.Length -ne $length -or $reader.BaseStream.ReadByte() -ne -1) { throw 'Invalid request.' }
    $aes = [Security.Cryptography.Aes]::Create()
    $aes.KeySize = 256
    $aes.BlockSize = 128
    $aes.Mode = [Security.Cryptography.CipherMode]::CBC
    $aes.Padding = [Security.Cryptography.PaddingMode]::PKCS7
    if ($operation -eq 1) { $transform = $aes.CreateEncryptor($key, $iv) }
    else { $transform = $aes.CreateDecryptor($key, $iv) }
    $result = $transform.TransformFinalBlock($data, 0, $data.Length)
    $output = [Console]::OpenStandardOutput()
    $output.Write($result, 0, $result.Length)
    $output.Flush()
} catch {
    # Do not emit the exception, key, payload or padding outcome.
    $failed = $true
} finally {
    if ($transform) { $transform.Dispose() }
    if ($aes) { $aes.Dispose() }
    foreach ($buffer in @($key, $iv, $data, $result)) {
        if ($null -ne $buffer) { [Array]::Clear($buffer, 0, $buffer.Length) }
    }
    if ($reader) { $reader.Dispose() }
}
if ($failed) { exit 1 }
