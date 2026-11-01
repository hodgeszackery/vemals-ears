"""Publisher helpers are tested without services, live state or network access."""

import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "Hosting/Publish-HostDownloads.ps1"
POWERSHELL = shutil.which("powershell") or shutil.which("pwsh")


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "Windows PowerShell required")
class HostPublishTests(unittest.TestCase):
    def test_direct_entrypoint_resolves_defaults_before_read_only_preflight(self):
        # Run -File, not dot sourcing. Missing temporary state stops before any
        # service or process probe, while both computed defaults are exercised.
        with tempfile.TemporaryDirectory() as folder:
            result = subprocess.run([
                POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                "-File", str(SCRIPT), "-StateDirectory", folder,
                "-PythonPath", sys.executable, "-NssmPath", sys.executable,
                "-CloudflaredPath", sys.executable, "-CheckOnly",
            ], text=True, capture_output=True, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW)
            self.assertNotEqual(0, result.returncode)
            self.assertIn("Required publication file is missing:", result.stderr)
            self.assertIn("tunnel-config.yml", result.stderr)
            self.assertNotIn("Split-Path", result.stderr)

    def run_helper(self, body, *arguments):
        with tempfile.TemporaryDirectory() as folder:
            harness = Path(folder) / "helper.ps1"
            harness.write_text(
                "param([string]$Source, [string]$Value, [string]$Extra)\n"
                "$ErrorActionPreference = 'Stop'\n"
                ". $Source\n" + body,
                encoding="ascii",
            )
            return subprocess.run(
                [POWERSHELL, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass",
                 "-File", str(harness), str(SCRIPT), *arguments],
                text=True, capture_output=True, timeout=20,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

    def transform(self, content):
        result = self.run_helper(
            "[Convert]::ToBase64String((Add-HostDownloadIngress ([Convert]::FromBase64String($Value))))",
            base64.b64encode(content).decode("ascii"),
        )
        self.assertEqual(0, result.returncode, result.stderr)
        return base64.b64decode(result.stdout.strip())

    def test_exact_rule_preserves_every_other_config_byte_and_is_idempotent(self):
        for newline, bom in ((b"\r\n", b"\xef\xbb\xbf"), (b"\n", b"")):
            with self.subTest(newline=newline, bom=bom):
                before = bom + newline.join((
                    b"# fixture only - no credentials", b"protocol: http2", b"ingress:",
                    b"  - hostname: other.example.org", b"    service: http://127.0.0.1:8799",
                    b"  - hostname: ears.xenon.solutions", b"    service: http://127.0.0.1:8770",
                    b"  - service: http_status:404", b"",
                ))
                addition = newline.join((
                    b"  - hostname: ears.xenon.solutions",
                    b"    path: ^/download/vemals-ears-host(?:/(?:source|sha256))?$",
                    b"    service: http://127.0.0.1:8773", b"",
                ))
                expected = before.replace(b"  - hostname: ears.xenon.solutions", addition + b"  - hostname: ears.xenon.solutions")
                after = self.transform(before)
                self.assertEqual(expected, after)
                self.assertEqual(after, self.transform(after))

    def test_conflicting_ambiguous_and_shadowed_rules_fail_closed(self):
        relay = b"  - hostname: ears.xenon.solutions\n    service: http://127.0.0.1:8770\n"
        rule = (b"  - hostname: ears.xenon.solutions\n"
                b"    path: ^/download/vemals-ears-host(?:/(?:source|sha256))?$\n"
                b"    service: http://127.0.0.1:8773\n")
        for body in (relay + relay, rule.replace(b"8773", b"9999") + relay,
                     relay + rule, rule + rule + relay, b"  - service: http_status:404\n"):
            with self.subTest(body=body):
                result = self.run_helper(
                    "$null = Add-HostDownloadIngress ([Convert]::FromBase64String($Value))",
                    base64.b64encode(b"ingress:\n" + body).decode("ascii"),
                )
                self.assertNotEqual(0, result.returncode)

    def test_native_argument_roundtrip_keeps_spaces_quotes_empty_and_trailing_slashes(self):
        values = ["plain", "a path with spaces", 'a"quote', "", "C:/folder with spaces/", "ends" + chr(92)]
        encoded = base64.b64encode(json.dumps(values).encode("ascii")).decode("ascii")
        result = self.run_helper(
            "$values = [Text.Encoding]::ASCII.GetString([Convert]::FromBase64String($Extra)) | ConvertFrom-Json\n"
            "$result = Invoke-PublishProcess $Value (@('-I','-B','-c','import json,sys;print(json.dumps(sys.argv[1:]))') + $values)\n"
            "if ($result.ExitCode -ne 0) { throw 'Isolated argv helper failed.' }; $result.Output",
            sys.executable, encoded,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(values, json.loads(result.stdout))

    def test_service_parameters_are_fixed_and_pass_as_one_nssm_setting(self):
        result = self.run_helper(
            "$parameters = Get-HostDownloadParameters 'C:/private runtime/hash/downloads.py' 'C:/public files/downloads'\n"
            "function Invoke-PublishProcess { param($FilePath, [string[]]$ArgumentList); "
            "[pscustomobject]@{ ExitCode=0; Output=($ArgumentList | ConvertTo-Json -Compress) } }\n"
            "Invoke-PublishNssm @('set','VemalsEarsHostDownloads','AppParameters',$parameters)",
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual([
            "set", "VemalsEarsHostDownloads", "AppParameters",
            '-B "C:/private runtime/hash/downloads.py" --directory "C:/public files/downloads" --host 127.0.0.1 --port 8773',
        ], json.loads(result.stdout))

    def test_atomic_replacement_keeps_only_requested_file_and_no_partial(self):
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            source = directory / "new.exe"
            target = directory / "public.exe"
            unrelated = directory / "member.exe"
            source.write_bytes(b"MZ new host fixture")
            target.write_bytes(b"MZ old host fixture")
            unrelated.write_bytes(b"MZ member stays identical")
            result = self.run_helper("Set-PublishFile $Value $Extra", str(source), str(target))
            self.assertEqual(0, result.returncode, result.stderr)
            self.assertEqual(source.read_bytes(), target.read_bytes())
            self.assertEqual(b"MZ member stays identical", unrelated.read_bytes())
            self.assertEqual({source.name, target.name, unrelated.name}, {item.name for item in directory.iterdir()})

    def test_tunnel_activation_failure_rolls_back_artifacts_and_only_owned_service(self):
        # Every process/SCM/network/admin entry point is replaced before calling
        # the real transaction. Only files under TemporaryDirectory are touched.
        for had_service in (False, True):
            with self.subTest(had_service=had_service), tempfile.TemporaryDirectory() as folder:
                directory = Path(folder).resolve()
                artifacts = directory / "artifacts"
                downloads = directory / "state/downloads"
                artifacts.mkdir()
                downloads.mkdir(parents=True)
                exe = artifacts / "Vemals-Ears-Host-Setup.exe"
                exe.write_bytes(b"MZ synthetic host installer")
                (artifacts / "VemalsEars-Host.zip").write_bytes(b"source fixture")
                (artifacts / "Vemals-Ears-Host-Setup.json").write_text(json.dumps({
                    "schema": 1, "product": "vemals-ears-host", "test_build": False,
                    "bytes": exe.stat().st_size, "sha256": hashlib.sha256(exe.read_bytes()).hexdigest(),
                }), encoding="ascii")
                config = directory / "state/tunnel-config.yml"
                config.write_bytes(b"protocol: http2\ningress:\n  - hostname: ears.xenon.solutions\n    service: http://127.0.0.1:8770\n  - service: http_status:404\n")
                original_config = config.read_bytes()
                old_files = {}
                for name in ("Vemals-Ears-Host-Setup.exe", "VemalsEars-Host.zip", "Vemals-Ears-Host-Setup.sha256", "Vemals-Ears-Setup.exe"):
                    old_files[name] = ("old fixture " + name).encode("ascii")
                    (downloads / name).write_bytes(old_files[name])
                for name in ("python.exe", "nssm.exe", "cloudflared.exe", "downloads.py"):
                    (directory / name).write_text("fixture only\n", encoding="ascii")
                result = self.run_helper(r'''
$ArtifactDirectory = Join-Path $Value 'artifacts'
$StateDirectory = Join-Path $Value 'state'
$SidecarPath = Join-Path $Value 'downloads.py'
$PythonPath = Join-Path $Value 'python.exe'
$NssmPath = Join-Path $Value 'nssm.exe'
$CloudflaredPath = Join-Path $Value 'cloudflared.exe'
$script:registry = @{}
$script:settings = @{}
$script:restartCount = 0
function New-FakeService {
    param([string]$Name)
    $value = [pscustomobject]@{ Name=$Name; Status='Running' }
    Add-Member -InputObject $value -MemberType ScriptMethod -Name WaitForStatus -Value {
        param($Expected, $Timeout)
        if ($this.Status -ne $Expected) { throw 'Unexpected fake service state.' }
    }
    return $value
}
$script:registry.VemalsEarsTunnel = New-FakeService 'VemalsEarsTunnel'
if ($Extra -eq 'existing') {
    $oldBytes = [Text.Encoding]::ASCII.GetBytes('old sidecar')
    $hasher = [Security.Cryptography.SHA256]::Create()
    try { $oldHash = ([BitConverter]::ToString($hasher.ComputeHash($oldBytes))).Replace('-','').ToLowerInvariant() } finally { $hasher.Dispose() }
    $oldDirectory = Join-Path $StateDirectory ('host-downloads-runtime/' + $oldHash)
    [void][IO.Directory]::CreateDirectory($oldDirectory)
    [IO.File]::WriteAllBytes((Join-Path $oldDirectory 'downloads.py'), $oldBytes)
    $script:registry.VemalsEarsHostDownloads = New-FakeService 'VemalsEarsHostDownloads'
    $script:settings.AppDirectory = $oldDirectory
    $script:settings.AppParameters = Get-HostDownloadParameters (Join-Path $oldDirectory 'downloads.py') (Join-Path $StateDirectory 'downloads')
}
function Get-Service {
    [CmdletBinding()] param([string]$Name)
    if ($Name -notin @('VemalsEarsTunnel','VemalsEarsHostDownloads')) { throw 'Unexpected SCM access.' }
    return $script:registry[$Name]
}
function Invoke-PublishProcess {
    param($FilePath, [string[]]$ArgumentList, $Timeout)
    return [pscustomobject]@{ ExitCode=0; Output=''; Error='' }
}
function Get-PublishAdministratorSid { return 'S-1-5-21-111-222-333-1001' }
function Assert-HostDownloadPortAvailable { }
function Wait-HostDownloadHealth { }
function Invoke-PublishNssm {
    param([string[]]$Arguments)
    $action, $name = $Arguments[0], $Arguments[1]
    if ($name -eq 'VemalsEarsTunnel' -and $action -eq 'get') {
        if ($Arguments[2] -eq 'Application') { return $CloudflaredPath }
        if ($Arguments[2] -eq 'AppParameters') { return '--config ' + (Join-Path $StateDirectory 'tunnel-config.yml') + ' tunnel run vemals-ears' }
        throw 'Unexpected tunnel setting read.'
    }
    if ($name -ne 'VemalsEarsHostDownloads') { throw 'Unexpected NSSM mutation.' }
    switch ($action) {
        'get' {
            if ($Arguments[2] -eq 'Application') { return $PythonPath }
            return $script:settings[$Arguments[2]]
        }
        'install' { $script:registry[$name] = New-FakeService $name; $script:registry[$name].Status = 'Stopped' }
        'set' { $script:settings[$Arguments[2]] = $Arguments[3] }
        'start' { $script:registry[$name].Status = 'Running' }
        'stop' { $script:registry[$name].Status = 'Stopped' }
        'remove' { $script:registry.Remove($name) }
        default { throw 'Unexpected NSSM operation.' }
    }
    return ''
}
function Restart-Service {
    [CmdletBinding()] param([string]$Name)
    if ($Name -ne 'VemalsEarsTunnel') { throw 'Restarted an unrelated service.' }
    $script:restartCount++
    if ($script:restartCount -eq 1) { throw 'Injected tunnel restart failure' }
}
try { Invoke-HostDownloadPublication; throw 'Expected activation failure was absent.' }
catch { if ($_.ToString() -notmatch 'Injected tunnel restart failure') { throw } }
if ($script:restartCount -ne 2) { throw 'The original tunnel configuration was not restored.' }
if ($Extra -eq 'existing') {
    if ($script:registry.VemalsEarsHostDownloads.Status -ne 'Running' -or $script:settings.AppDirectory -ne $oldDirectory) {
        throw 'The previously running sidecar was not restored.'
    }
} elseif ($script:registry.ContainsKey('VemalsEarsHostDownloads')) { throw 'The new sidecar was not removed.' }
''', str(directory), "existing" if had_service else "new")
                self.assertEqual(0, result.returncode, result.stderr)
                self.assertEqual(original_config, config.read_bytes())
                self.assertEqual(old_files, {name: (downloads / name).read_bytes() for name in old_files})
                receipts = list((directory / "state/host-downloads-publications").glob("*/receipt.json"))
                self.assertEqual(1, len(receipts))
                receipt = json.loads(receipts[0].read_text(encoding="ascii"))
                self.assertEqual("failed", receipt["status"])
                self.assertEqual([], receipt["rollback_errors"])


if __name__ == "__main__":
    unittest.main()
