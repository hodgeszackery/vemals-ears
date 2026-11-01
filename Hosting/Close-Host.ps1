# Close only this installation's visible Host wizard; never kill Python.
[CmdletBinding()]
param([Parameter(Mandatory=$true)][string]$PythonwPath)
$ErrorActionPreference = 'Stop'
if (-not [IO.Path]::IsPathRooted($PythonwPath)) {
    throw 'The private Python window runtime must be an absolute path.'
}
$expected = [IO.Path]::GetFullPath($PythonwPath)
$targets = @(Get-Process -Name pythonw -ErrorAction SilentlyContinue | Where-Object {
    $_.MainWindowTitle -eq "Vemal's Ears - Host" -and
    $_.Path -and [IO.Path]::GetFullPath($_.Path) -eq $expected
})
foreach ($target in $targets) {
    if (-not $target.CloseMainWindow()) {
        throw 'Close the Vemal''s Ears Host window before continuing.'
    }
}
foreach ($target in $targets) {
    if (-not $target.WaitForExit(30000)) {
        throw 'The Host window is still open. Finish or cancel setup, then retry.'
    }
}
