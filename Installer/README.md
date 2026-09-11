# Vemal's Ears Windows installer

This directory builds one generic, signed Windows installer for Vemal's Ears.
The same installer can be served by the project or mirrored by any guild. A
guild mirror is a download location, not a signing or update trust root.

The installer is per-user and does not require administrator access. It installs
under `%LOCALAPPDATA%\Programs\VemalsEars`, registers the `vemalsears:` pairing
protocol in HKCU, and includes a private x64 Python runtime. Members do not need
to install Python separately. Pairing supplies the guild-specific server
configuration after installation; no guild endpoint, token, or device secret is
embedded in the installer.

## Member behavior

- Windows 10 version 1809 or newer, or Windows 11, on x64 hardware is required.
- Running a newer signed installer upgrades the existing per-user installation.
- The installer rejects downgrades below the recorded installed version.
- Vemal's Ears and Downtime can be installed and run side by side. Their
  startup entries, pairing credentials, windows, and updates are independent.
- The caption window runs inside VemalsEars.exe, so Windows and Task Manager
  show the Vemal's Ears process. The private Python runtime remains available
  to its child helpers without becoming the main window's process name.
- Uninstall removes the local Vemal's Ears device credential and local pairing
  and overlay state. A guild administrator must separately revoke the device on
  the guild server when access should end immediately.
- Updating or uninstalling Vemal's Ears never removes Downtime's state or job
  credential. A running older VE release that still uses the legacy shared
  window lock may need to be closed from its tray before updating.
- The installed member edition has no Start or Stop service controls. It keeps
  its caption connection available automatically and never starts or restarts
  local simulation services.
- SimulationCraft, Raidbots import tooling, and Warcraft Logs service
  credentials are not installed on member computers.
- This installer intentionally remains caption-only. The hosted Gear Helper,
  Great Vault, and Boss Talents forwarding client ships separately as
  `downtime-lite.zip`; it is not staged or installed by this signed EXE.
- The member app can stage an advertised update in the background. It may run
  that installer unattended only when Authenticode is valid and the exact
  signer subject appears in the installed `payload-manifest.json` under
  `update_trust.authenticode_publishers`.
- Official signed builds generate that publisher list from the same exact
  `ExpectedPublisher` value checked against the finished installer. Unsigned,
  test, source-rebuilt, and proof-certificate builds omit `update_trust`, so a
  download hash by itself never grants unattended execution trust.
- An unattended update invokes Setup with the explicit `/VEAUTOUPDATE` switch.
  Setup still owns closing the running companion in `PrepareToInstall`, and it
  relaunches the app once after a successful silent update. Ordinary silent
  installs remain non-launching.

## Local unsigned build

The default unsigned build is labeled for development:

```powershell
./Installer/build_installer.ps1
```

The result is `dist/Vemals-Ears-Setup-UNSIGNED.exe`. The build downloads the
exact Python runtime and wheels recorded in `runtime-lock.json`, verifies every
SHA-256 digest, stages only the explicit application allowlist, compiles the
native launcher, and uses the locked Inno Setup version. The installer omits
the large archives under `runtime/Source` so existing clients can download it,
but retains their exact `MANIFEST.json` and `README.txt` receipt.

Unsigned builds deliberately contain no automatic-execution trust metadata.
They can still be installed manually, but the app will not silently run a
future installer on the authority of an unsigned build.

This label describes the project's release process, not a restriction on your
GPL rights. You may rebuild, modify and redistribute under GPL-3.0-only. Use
`-AllowUnsigned` to deliberately select the normal release filename for an
unsigned build. Do not represent an unsigned or modified build as an official
signed release. Matching client source and instructions are included in
`app/Source/vemals-ears-source.zip` and `app/docs/SOURCE.txt` after installation.
Recipients do not need the private GitHub repository or publisher's signing
credentials to rebuild; see `docs/SOURCE.txt`.

The exact source-complete member ZIP, including every upstream runtime source
archive, is public at the versioned URL in `docs/SOURCE.txt`. Keep that ZIP
available for as long as the matching installer is distributed.

Inno Setup 7.1.0 and the Visual Studio x64 C++ Build Tools must be installed for
a local build. The release workflow downloads and verifies the locked Inno
Setup installer itself.

For the isolated install/reinstall/uninstall check, build the special test
installer and run its smoke harness:

```powershell
./Installer/build_installer.ps1 -TestBuild
./Installer/smoke_test_installer.ps1 `
  -InstallerPath ./dist/Vemals-Ears-Setup-TEST-UNSIGNED.exe
```

The test build uses a separate application identity and does not register the
pairing protocol, launch the companion, remove member credentials, or change
the production installer state. The harness also verifies a path containing
spaces, private-runtime isolation, the thin member payload, stale-file cleanup
on reinstall, native launcher pairing arguments, and uninstall cleanup.

## Protected signed release

`.github/workflows/vemals-ears-windows.yml` is the project's signed-release path. It
runs for tags matching `vemals-ears-v*` and for manual workflow dispatches. The
workflow does the following in order:

1. Validates the tag against the independent version in `GuildHub/version.py`.
2. Runs installer and companion tests and completes an unsigned proof build.
3. Downloads the exact Artifact Signing client from `runtime-lock.json`, checks
   its SHA-256 digest, and installs it.
4. Uses GitHub OIDC to obtain short-lived Azure access. No client secret or
   signing certificate is stored in GitHub.
5. Signs the native launcher, generated uninstaller, and final installer with
   SHA-256 and an RFC3161 timestamp.
6. Verifies Authenticode status, the exact publisher subject, the timestamp,
   and SignTool policy before creating the checksum from the final signed bytes.
7. Writes that exact publisher subject into the installed manifest as a list,
   allowing a signed transition release to carry both publisher identities
   when a certificate identity must rotate.
8. Builds and verifies the source-complete member ZIP, gives it an immutable
   versioned filename, and passes it with `Vemals-Ears-Setup.exe`, its
   `.sha256` file, and the application source ZIP to a separate
   `contents: write` job. The unsigned proof build is never published.

A manual dispatch leaves the signed files as a protected workflow artifact. A
matching pushed tag also creates the GitHub release.

For a planned certificate identity rotation, pass each future exact subject in
`-AdditionalUpdatePublisher` while the release is still signed by the current
`ExpectedPublisher`. The current subject is always included first. The build
rejects this option for unsigned and proof-certificate installers, and rejects
blank or duplicate subjects.

## GitHub environment configuration

Create a protected GitHub environment named `windows-release`. Require a
reviewer, prevent self-review where practical, and restrict deployment to the
release tags and trusted branch used for manual dispatches.

Add these environment secrets:

- `AZURE_CLIENT_ID`
- `AZURE_TENANT_ID`
- `AZURE_SUBSCRIPTION_ID`

Add these environment variables:

- `ARTIFACT_SIGNING_ENDPOINT`
- `ARTIFACT_SIGNING_ACCOUNT`
- `ARTIFACT_SIGNING_PROFILE`
- `ARTIFACT_SIGNING_PUBLISHER_SUBJECT`

`ARTIFACT_SIGNING_PUBLISHER_SUBJECT` must exactly match the signing
certificate's full subject string as returned by PowerShell:

```powershell
(Get-AuthenticodeSignature .\Vemals-Ears-Setup.exe).SignerCertificate.Subject
```

Configure the Azure federated credential for this environment subject:

```text
repo:hodgeszackery/vemals-ears:environment:windows-release
```

Grant that identity signing access only to the intended Artifact Signing
certificate profile. The workflow creates its metadata file in the ephemeral
runner directory and never adds it to the repository or release.

## Release procedure

1. Update `CLIENT_VERSION` in `GuildHub/version.py` and merge the reviewed
   installer change.
2. Push a matching tag, for example `vemals-ears-v0.2.0`.
3. Approve the `windows-release` environment deployment.
4. Confirm the workflow's signature and post-sign checksum checks passed.
5. Distribute or mirror the installer, checksum, and matching versioned source
   files attached to the GitHub release.

Code signing proves publisher identity and detects changed installer bytes. It
does not guarantee that Microsoft SmartScreen reputation appears immediately;
new publishers and new applications can still show reputation prompts while
that reputation develops.

Microsoft's current integration documentation is at
https://learn.microsoft.com/en-us/azure/artifact-signing/how-to-signing-integrations.
Inno Setup signing behavior is documented at
https://jrsoftware.org/ishelp/topic_setup_signtool.htm and
https://jrsoftware.org/ishelp/topic_setup_signeduninstaller.htm.
