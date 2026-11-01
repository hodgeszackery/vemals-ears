"""Exercise the Windows remote service plan against an in-memory SCM fake."""

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
class RemoteServiceInstallTests(unittest.TestCase):
    def test_remote_install_never_queries_mutates_or_depends_on_another_service(self):
        with tempfile.TemporaryDirectory() as folder:
            temporary = Path(folder)
            harness = temporary / "remote install witness.ps1"
            harness.write_text(r'''
param([string]$Source, [string]$StateDirectory)
$ErrorActionPreference = 'Stop'
$tokens = $null; $parseErrors = $null
$ast = [Management.Automation.Language.Parser]::ParseFile($Source, [ref]$tokens, [ref]$parseErrors)
if ($parseErrors.Count) { throw 'Installer parse failed.' }
$definitions = $ast.FindAll({ param($node) $node -is [Management.Automation.Language.FunctionDefinitionAst] -and $node.Name -in @('Get-HostServiceDefinitions', 'Assert-HostServiceOwnership', 'Install-RegisteredHostServices') }, $true)
if ($definitions.Count -ne 3) { throw 'Expected installer functions missing.' }
foreach ($definition in $definitions) { Invoke-Expression $definition.Extent.Text }
$script:events = [Collections.Generic.List[string]]::new()
$script:queries = [Collections.Generic.List[string]]::new()
$script:registry = @{}
$script:fail = $false
function Check-Name {
    param([string]$Name)
    if ($Name -ne 'VemalsEarsHostWorker') { throw ('Touched unrelated service: ' + $Name) }
}
function New-Worker {
    $value = [pscustomobject]@{ Name='VemalsEarsHostWorker'; Status='Stopped'; Settings=@{} }
    Add-Member -InputObject $value -MemberType ScriptMethod -Name WaitForStatus -Value {
        param($Expected, $Timeout)
        if ($this.Status -ne $Expected) { throw 'Wrong synthetic service status.' }
    }
    return $value
}
function Get-Service {
    [CmdletBinding()] param([string]$Name)
    Check-Name $Name
    $script:queries.Add($Name)
    return $script:registry[$Name]
}
function Start-Service {
    [CmdletBinding()] param([string]$Name)
    Check-Name $Name
    $script:events.Add('start ' + $Name)
    $script:registry[$Name].Status = 'Running'
}
function Stop-Service {
    [CmdletBinding()] param([string]$Name)
    Check-Name $Name
    $script:events.Add('stop ' + $Name)
    $script:registry[$Name].Status = 'Stopped'
}
function Invoke-Nssm {
    param([Parameter(ValueFromRemainingArguments=$true)][string[]]$Arguments)
    $action, $name = $Arguments[0], $Arguments[1]
    Check-Name $name
    if ($Arguments -contains 'VemalsEarsHostRelay') { throw 'Remote worker depends on a local relay.' }
    $script:events.Add(($Arguments -join ' '))
    if ($action -eq 'install') {
        $script:registry[$name] = New-Worker
    } elseif ($action -eq 'set') {
        if ($script:fail -and $Arguments[2] -eq 'AppParameters') { throw 'Synthetic registration failure.' }
        $script:registry[$name].Settings[$Arguments[2]] = $Arguments[3]
    } elseif ($action -eq 'reset') {
        [void]$script:registry[$name].Settings.Remove($Arguments[2])
    } elseif ($action -eq 'stop') {
        $script:registry[$name].Status = 'Stopped'
    } elseif ($action -eq 'remove') {
        [void]$script:registry.Remove($name)
    } else { throw 'Unexpected synthetic NSSM command.' }
}
function Get-NssmValue {
    param([string]$Name, [string]$Setting)
    Check-Name $Name
    return $script:registry[$Name].Settings[$Setting]
}
$root = [IO.Path]::GetFullPath($StateDirectory)
$python = [IO.Path]::GetFullPath((Join-Path $root 'private runtime/python.exe'))
$services = @(Get-HostServiceDefinitions -HostMode 'remote-worker' -StateDirectory $root)
if ($services.Count -ne 1 -or $services[0].Name -ne 'VemalsEarsHostWorker') { throw 'Wrong remote service plan.' }
if ($services[0].Parameters -notmatch '--host-mode remote-worker worker$') { throw 'Missing explicit remote ownership argument.' }
$arguments = @{ Services=$services; PythonPath=$python; RepoRoot=$root; StateDirectory=$root; NodePath='C:/Program Files/Node/node.exe'; HostMode='remote-worker' }
Assert-HostServiceOwnership -Services $services -PythonPath $python -RepoRoot $root
Install-RegisteredHostServices @arguments
if ($script:registry.Count -ne 1 -or $script:registry['VemalsEarsHostWorker'].Status -ne 'Running') { throw 'Worker did not start.' }
if (-not $script:events.Contains('reset VemalsEarsHostWorker DependOnService')) { throw 'Relay dependency was not cleared.' }
$script:events.Clear()
Assert-HostServiceOwnership -Services $services -PythonPath $python -RepoRoot $root
Install-RegisteredHostServices @arguments
if (-not $script:events.Contains('stop VemalsEarsHostWorker')) { throw 'Existing owned worker was not stopped for reconfiguration.' }

# A standalone registration using the same folder is not an upgrade.
$script:registry['VemalsEarsHostWorker'].Settings['AppParameters'] = $services[0].Parameters.Replace('--host-mode remote-worker ', '')
$script:events.Clear()
try { Assert-HostServiceOwnership -Services $services -PythonPath $python -RepoRoot $root; throw 'Expected ownership rejection.' }
catch { if ($_.ToString() -notmatch 'belongs to another configuration') { throw } }
if ($script:events.Count) { throw 'Ownership rejection changed a service.' }

# A partial new registration is removed, and retry can start cleanly.
$script:registry.Clear()
$script:fail = $true
try { Install-RegisteredHostServices @arguments; throw 'Expected configuration failure.' }
catch { if ($_.ToString() -notmatch 'Synthetic registration failure') { throw } }
if ($script:registry.Count) { throw 'New failed worker registration remained.' }
$script:fail = $false
Install-RegisteredHostServices @arguments
@{ names=@($script:queries | Select-Object -Unique); running=$script:registry['VemalsEarsHostWorker'].Status; ownership='rejected'; rollback='passed' } | ConvertTo-Json -Compress
'''.lstrip(), encoding="ascii")
            result = subprocess.run([
                POWERSHELL, "-NoProfile", "-File", str(harness),
                str(ROOT / "Hosting/Install-Host.ps1"), str(temporary / "host state"),
            ], capture_output=True, text=True, timeout=30,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            self.assertEqual(0, result.returncode, result.stdout + result.stderr)
            proof = json.loads(result.stdout)
            self.assertEqual(["VemalsEarsHostWorker"], proof["names"])
            self.assertEqual("Running", proof["running"])
            self.assertEqual("rejected", proof["ownership"])
            self.assertEqual("passed", proof["rollback"])


if __name__ == "__main__":
    unittest.main()
