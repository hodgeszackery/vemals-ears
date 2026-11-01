"""Service setup cleans up only registrations it created during a failed run."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class HostServiceRollbackTests(unittest.TestCase):
    def test_failed_new_registration_is_removed_and_preexisting_service_survives(self):
        # Extract just the registration function and replace every SCM/NSSM
        # entry point with an in-memory fake. No real service command can run.
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder)
            script = temporary / "rollback.ps1"
            script.write_text(r'''
param([string]$Source, [string]$StateDirectory)
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Installer parse failed.' }
$definition = $ast.Find({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -eq 'Install-RegisteredHostServices' }, $true)
if (-not $definition) { throw 'Registration function was not found.' }
Invoke-Expression $definition.Extent.Text
$script:registry = @{}
$script:events = [Collections.Generic.List[string]]::new()
$script:fail = $true
function New-FakeService {
    param([string]$Name)
    $value = [pscustomobject]@{ Name=$Name; Status='Stopped'; Settings=@{} }
    Add-Member -InputObject $value -MemberType ScriptMethod -Name WaitForStatus -Value {
        param($Expected, $Timeout)
        if ($this.Status -ne $Expected) { throw 'Unexpected fake service state.' }
    }
    return $value
}
function Get-Service {
    [CmdletBinding()] param([string]$Name)
    return $script:registry[$Name]
}
function Start-Service {
    [CmdletBinding()] param([string]$Name)
    $script:registry[$Name].Status = 'Running'
}
function Stop-Service {
    [CmdletBinding()] param([string]$Name)
    $script:registry[$Name].Status = 'Stopped'
}
function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    $action, $name = $Arguments[0], $Arguments[1]
    if ($name -notin @('VemalsEarsHostRelay', 'VemalsEarsHostWorker')) { throw 'Unexpected service name.' }
    if ($action -eq 'install') {
        if ($script:registry.ContainsKey($name)) { throw 'Installed over an existing fake service.' }
        $script:registry[$name] = New-FakeService $name
    } elseif ($action -eq 'set') {
        if ($script:fail -and $name -eq 'VemalsEarsHostWorker' -and $Arguments[2] -eq 'AppParameters') {
            throw 'Injected configuration failure after successful install.'
        }
        $script:registry[$name].Settings[$Arguments[2]] = $Arguments[3]
    } elseif ($action -eq 'stop') {
        $script:events.Add('stop ' + $name)
        $script:registry[$name].Status = 'Stopped'
    } elseif ($action -eq 'remove') {
        $script:events.Add('remove ' + $name)
        [void]$script:registry.Remove($name)
    } else { throw 'Unexpected fake NSSM command.' }
}
function Get-NssmValue {
    param([string]$Name, [string]$Setting)
    return $script:registry[$Name].Settings[$Setting]
}
$services = @(
    @{ Name='VemalsEarsHostRelay'; Role='relay'; Title='Relay'; Parameters='relay parameters' },
    @{ Name='VemalsEarsHostWorker'; Role='worker'; Title='Worker'; Parameters='worker parameters' }
)
$arguments = @{ Services=$services; PythonPath='C:/Program Files/Python/python.exe'; RepoRoot=[IO.Path]::GetFullPath($StateDirectory); StateDirectory=$StateDirectory; NodePath='C:/Program Files/Node/node.exe' }
try { Install-RegisteredHostServices @arguments; throw 'Expected failure did not occur.' }
catch { if ($_.ToString() -notmatch 'Injected configuration failure') { throw } }
if ($script:registry.Count -ne 0) { throw 'New incomplete services survived rollback.' }
$firstRemoval = @($script:events | Where-Object { $_ -like 'remove *' })
if (($firstRemoval -join ',') -ne 'remove VemalsEarsHostWorker,remove VemalsEarsHostRelay') { throw 'Rollback order was incorrect.' }
$script:fail = $false
Install-RegisteredHostServices @arguments
if ($script:registry.Count -ne 2 -or $script:registry['VemalsEarsHostWorker'].Status -ne 'Running') { throw 'Retry did not complete.' }

$script:registry = @{ VemalsEarsHostRelay = (New-FakeService 'VemalsEarsHostRelay') }
$script:registry['VemalsEarsHostRelay'].Status = 'Running'
$script:events.Clear()
$script:fail = $true
try { Install-RegisteredHostServices @arguments; throw 'Expected failure did not occur.' }
catch { if ($_.ToString() -notmatch 'Injected configuration failure') { throw } }
if ($script:registry.Count -ne 1 -or -not $script:registry.ContainsKey('VemalsEarsHostRelay')) { throw 'Preexisting service was removed.' }
$secondRemoval = @($script:events | Where-Object { $_ -like 'remove *' })
if (($secondRemoval -join ',') -ne 'remove VemalsEarsHostWorker') { throw 'Rollback included a preexisting service.' }
@{ first=$firstRemoval; second=$secondRemoval; retry='passed' } | ConvertTo-Json -Compress
'''.lstrip(), encoding="ascii")
            result = subprocess.run(
                [POWERSHELL, "-NoProfile", "-File", str(script),
                 str(ROOT / "Hosting/Install-Host.ps1"), str(temporary / "state")],
                cwd=temporary, capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            proof = json.loads(result.stdout)
            self.assertEqual(["remove VemalsEarsHostWorker", "remove VemalsEarsHostRelay"], proof["first"])
            self.assertEqual(["remove VemalsEarsHostWorker"], proof["second"])
            self.assertEqual("passed", proof["retry"])


if __name__ == "__main__":
    unittest.main()
