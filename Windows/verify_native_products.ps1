[CmdletBinding()]
param(
    [Parameter(Mandatory = $true)] [string]$LaunchersDirectory,
    [Parameter(Mandatory = $true)] [string]$RuntimeDirectory
)

$ErrorActionPreference = "Stop"
Set-StrictMode -Version Latest
$launchers = (Resolve-Path -LiteralPath $LaunchersDirectory).Path
$runtime = (Resolve-Path -LiteralPath $RuntimeDirectory).Path
if (-not (Test-Path -LiteralPath (Join-Path $runtime "pythonw.exe") -PathType Leaf)) {
    throw "A complete private Python runtime is required."
}
$probe = Join-Path $PSScriptRoot "tests\NativeProductSmoke.pyw"
$tempParent = [System.IO.Path]::GetFullPath([System.IO.Path]::GetTempPath())
$stage = Join-Path $tempParent ("native-products-smoke-" + [guid]::NewGuid().ToString("N"))
New-Item -ItemType Directory -Path $stage | Out-Null
$stage = (Resolve-Path -LiteralPath $stage).Path
$products = @(
    @{ Exe = "VemalsEars.exe"; Name = "Vemal's Ears"; AppId = "VemalsEars.Companion"; Script = "app\Vemals-Ears.pyw"; Flag = "--vemals-ears-pair"; Scheme = "vemalsears" }
)
$links = @()
try {
    foreach ($product in $products) {
        $source = Join-Path $launchers $product.Exe
        $metadata = (Get-Item -LiteralPath $source).VersionInfo
        if ($metadata.OriginalFilename -cne $product.Exe -or
            $metadata.ProductName -cne $product.Name -or $metadata.FileDescription -cne $product.Name) {
            throw "The native resource identity is wrong for $($product.Exe)."
        }
        $appRoot = Join-Path $stage $product.Name
        New-Item -ItemType Directory -Path $appRoot | Out-Null
        $executable = Join-Path $appRoot $product.Exe
        Copy-Item -LiteralPath $source -Destination $executable
        $script = Join-Path $appRoot $product.Script
        New-Item -ItemType Directory -Path (Split-Path -Parent $script) -Force | Out-Null
        Copy-Item -LiteralPath $probe -Destination $script
        $moduleRoot = Join-Path (Split-Path -Parent $script) "GuildHub"
        New-Item -ItemType Directory -Path $moduleRoot | Out-Null
        foreach ($module in @("__init__.py", "windows_taskbar.py")) {
            Copy-Item -LiteralPath (Join-Path $PSScriptRoot ("..\GuildHub\" + $module)) `
                -Destination (Join-Path $moduleRoot $module)
        }
        $link = Join-Path $appRoot "runtime"
        New-Item -ItemType Junction -Path $link -Target $runtime | Out-Null
        $links += $link
        $modes = @("normal", "pair")
        if ($product.Scheme -eq "downtime") { $modes += "legacy" }
        foreach ($mode in $modes) {
            $receipt = Join-Path $appRoot ("result-" + $mode + ".json")
            $startInfo = [System.Diagnostics.ProcessStartInfo]::new()
            $startInfo.FileName = $executable
            $startInfo.UseShellExecute = $false
            $startInfo.CreateNoWindow = $true
            $startInfo.WindowStyle = [System.Diagnostics.ProcessWindowStyle]::Hidden
            $startInfo.EnvironmentVariables["NATIVE_PRODUCT_NAME"] = $product.Name
            $startInfo.EnvironmentVariables["NATIVE_PRODUCT_RESULT"] = $receipt
            $expectedArgs = @()
            if ($mode -ne "normal") {
                $uri = $product.Scheme + "://pair?code=smoke-test&source=native"
                $expectedArgs = @($product.Flag, $uri)
                if ($mode -eq "legacy") {
                    $uri = "vemalsears://pair?code=smoke-test&source=native"
                    $expectedArgs = @("--vemals-ears-pair", $uri)
                }
                $startInfo.Arguments = $expectedArgs[0] + ' "' + $uri + '"'
            }
            $process = [System.Diagnostics.Process]::Start($startInfo)
            try {
                if (-not $process.WaitForExit(30000)) {
                    $process.Kill()
                    $process.WaitForExit()
                    throw "The isolated $($product.Exe) probe timed out."
                }
                if ($process.ExitCode -ne 0) {
                    throw "The isolated $($product.Exe) failed with exit code $($process.ExitCode)."
                }
                $result = Get-Content -Raw -LiteralPath $receipt | ConvertFrom-Json
                if ($result.host_pid -ne $process.Id -or $result.window_pid -ne $process.Id) {
                    throw "$($product.Exe) did not own its Python UI process and window."
                }
                if ([System.IO.Path]::GetFileName($result.image) -cne $product.Exe -or
                    $result.app_id -cne $product.AppId -or $result.window_title -cne $product.Name) {
                    throw "$($product.Exe) reported the wrong Windows identity."
                }
                if ([System.IO.Path]::GetFileName($result.sys_executable) -cne "pythonw.exe") {
                    throw "$($product.Exe) changed the interpreter used by child helpers."
                }
                if ($result.ignore_environment -ne 1 -or $result.no_user_site -ne 1 -or
                    -not $result.dont_write_bytecode) {
                    throw "$($product.Exe) lost the isolated runtime flags."
                }
                if (($result.argv -join "`n") -cne ($expectedArgs -join "`n")) {
                    throw "$($product.Exe) changed its pairing arguments."
                }
                if ($result.taskbar.ID -cne $product.AppId -or
                    $result.taskbar.RelaunchCommand -cne ('"' + $executable + '"') -or
                    $result.taskbar.RelaunchIconResource -cne ($executable + ",0") -or
                    $result.taskbar.RelaunchDisplayNameResource -cne $product.Name -or
                    $result.taskbar.PreventPinning) {
                    throw "$($product.Exe) did not publish its native taskbar relaunch target."
                }
                Write-Output "$($product.Exe): native process/window/taskbar identity and $mode launch passed"
            }
            finally {
                $process.Dispose()
            }
        }
    }
}
finally {
    # Delete junctions themselves before any recursive cleanup. Their runtime
    # target is shared read-only with the source installation.
    foreach ($link in $links) {
        if (Test-Path -LiteralPath $link) {
            [System.IO.Directory]::Delete($link)
        }
    }
    $absoluteStage = [System.IO.Path]::GetFullPath($stage)
    $prefix = $tempParent.TrimEnd([char[]]@('\', '/')) + [System.IO.Path]::DirectorySeparatorChar
    if (-not $absoluteStage.StartsWith($prefix, [System.StringComparison]::OrdinalIgnoreCase) -or
        -not ([System.IO.Path]::GetFileName($absoluteStage)).StartsWith("native-products-smoke-")) {
        throw "Refusing cleanup outside the isolated native validation directory."
    }
    Remove-Item -LiteralPath $absoluteStage -Recurse -Force
}
