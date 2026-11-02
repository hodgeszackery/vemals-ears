#ifndef AppVersion
  #error AppVersion must be supplied by build_host_installer.py
#endif
#ifndef StageRoot
  #error StageRoot must be supplied by build_host_installer.py
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by build_host_installer.py
#endif

#ifdef TestBuild
  #define AppName "Vemal's Ears Host Installer Test"
  #define OutputName "Vemals-Ears-Host-Setup-TEST-UNSIGNED"
#else
  #define AppName "Vemal's Ears Host"
  #define OutputName "Vemals-Ears-Host-Setup"
#endif

[Setup]
#ifdef TestBuild
AppId={{609E72F0-BF40-42B1-95CF-FC47664E880C}
DefaultDirName={localappdata}\Temp\VemalsEarsHost-Installer-Test-Manual
PrivilegesRequired=lowest
#else
AppId={{16B97A98-EE13-43DE-BF19-012B5AA81EB8}
DefaultDirName={autopf}\VemalsEarsHost
PrivilegesRequired=admin
#endif
AppName={#AppName}
AppVersion={#AppVersion}
AppVerName={#AppName} {#AppVersion}
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoCompany=Zackery Hodges
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
AppPublisher=Zackery Hodges
AppPublisherURL=https://github.com/hodgeszackery/vemals-ears
DefaultGroupName={#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
UsePreviousAppDir=no
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#StageRoot}\VemalsEars.ico
UninstallDisplayIcon={app}\app\Windows\VemalsEars.ico
LicenseFile={#StageRoot}\app\LICENSE
Compression=lzma2/max
SolidCompression=yes
WizardStyle=modern dynamic
WizardSizePercent=110
CloseApplications=force
RestartApplications=no
ChangesAssociations=no
SetupLogging=yes
SignedUninstaller=no

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
Source: "{#StageRoot}\runtime\*"; DestDir: "{app}\runtime"; Excludes: "Source\*"; Flags: ignoreversion recursesubdirs createallsubdirs
; Source archives are already compressed. Preserve the actual artifacts, not
; only a receipt that relies on an unrelated public member download.
Source: "{#StageRoot}\runtime\Source\*"; DestDir: "{app}\runtime\Source"; Flags: ignoreversion recursesubdirs createallsubdirs nocompression
; Do not exclude Source here: Inno matches recursively and npm's chalk also
; has a source/ directory that must remain in the executable tool payload.
Source: "{#StageRoot}\tools\*"; DestDir: "{app}\tools"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageRoot}\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
; Pinned Vulkan whisper.cpp runtime (assets.lock.json whisper_vulkan) with its
; license. The runtime installer reads it from {app}\speech-engine. Already a
; ZIP, so it is stored without recompression.
Source: "{#StageRoot}\speech-engine\*"; DestDir: "{app}\speech-engine"; Flags: ignoreversion nocompression
Source: "{#StageRoot}\host-install.json"; DestDir: "{app}"; Flags: ignoreversion

#ifndef TestBuild
[Tasks]
Name: "desktopicon"; Description: "Create a desktop shortcut"; GroupDescription: "Shortcuts:"; Flags: checkedonce

[Icons]
Name: "{group}\Vemal's Ears Host"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-E -s -B -m Hosting.wizard --state-directory ""{commonappdata}\VemalsEarsHost"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\Windows\VemalsEars.ico"; AppUserModelID: "VemalsEars.Host"
Name: "{commondesktop}\Vemal's Ears Host"; Filename: "{app}\runtime\pythonw.exe"; Parameters: "-E -s -B -m Hosting.wizard --state-directory ""{commonappdata}\VemalsEarsHost"""; WorkingDir: "{app}\app"; IconFilename: "{app}\app\Windows\VemalsEars.ico"; AppUserModelID: "VemalsEars.Host"; Tasks: desktopicon
Name: "{group}\Uninstall Vemal's Ears Host"; Filename: "{uninstallexe}"; Flags: preventpinning

[Run]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-E -s -B -m Hosting.wizard --state-directory ""{commonappdata}\VemalsEarsHost"""; WorkingDir: "{app}\app"; Description: "Open Vemal's Ears Host setup"; Flags: nowait postinstall skipifsilent
#endif

[UninstallDelete]
; Runtime preparation can create adapter dependencies after installation.
; Only this product's install trees are removed. ProgramData state is kept.
Type: filesandordirs; Name: "{app}\app"; Check: UninstallTargetValidated
Type: filesandordirs; Name: "{app}\runtime"; Check: UninstallTargetValidated
Type: filesandordirs; Name: "{app}\tools"; Check: UninstallTargetValidated
Type: filesandordirs; Name: "{app}\speech-engine"; Check: UninstallTargetValidated
Type: files; Name: "{app}\host-resume.json"; Check: UninstallTargetValidated

[Code]
var
  ValidatedUninstall: Boolean;
  ResumeAfterInstall: Boolean;
  ResumeFailed: Boolean;

function InstallPathIsSafe(): Boolean;
var
  ActualPath, ExpectedPath, Suffix: String;
begin
  ActualPath := RemoveBackslashUnlessRoot(ExpandConstant('{app}'));
#ifdef TestBuild
  ExpectedPath := ExpandConstant('{localappdata}\Temp\VemalsEarsHost-Installer-Test-');
  if CompareText(Copy(ActualPath, 1, Length(ExpectedPath)), ExpectedPath) <> 0 then
  begin
    Result := False;
    Exit;
  end;
  Suffix := Copy(ActualPath, Length(ExpectedPath) + 1, MaxInt);
  Result := (Suffix <> '') and (Length(Suffix) <= 80) and
            (Pos('\', Suffix) = 0) and (Pos('/', Suffix) = 0);
#else
  ExpectedPath := RemoveBackslashUnlessRoot(ExpandConstant('{autopf}\VemalsEarsHost'));
  Result := CompareText(ActualPath, ExpectedPath) = 0;
#endif
end;

function ExistingInstallOwned(): Boolean;
var
  Marker: AnsiString;
begin
  Result := LoadStringFromFile(ExpandConstant('{app}\host-install.json'), Marker) and
            (Pos('"product": "vemals-ears-host"', Marker) > 0);
#ifdef TestBuild
  Result := Result and (Pos('"test_build": true', Marker) > 0);
#else
  Result := Result and (Pos('"test_build": false', Marker) > 0);
#endif
end;

function RunOwnedControl(Action: String): Boolean;
var
  Python, Arguments: String;
  ResultCode: Integer;
begin
#ifdef TestBuild
  { Test installers never invoke the service manager, even when elevated. }
  Result := True;
#else
  Python := ExpandConstant('{app}\runtime\python.exe');
  Result := FileExists(Python) and FileExists(ExpandConstant('{app}\app\Hosting\control.py'));
  if not Result then
    Exit;
  Arguments := '-E -s -B -m Hosting.control --state-directory "' +
    ExpandConstant('{commonappdata}\VemalsEarsHost') + '" ' + Action +
    ' --install-root "' + ExpandConstant('{app}') + '"';
  Result := Exec(Python, Arguments, ExpandConstant('{app}\app'), SW_HIDE,
                 ewWaitUntilTerminated, ResultCode);
  if Result then
    Result := ResultCode = 0;
#endif
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not InstallPathIsSafe() then
  begin
    Result := 'The installation location does not belong to Vemal''s Ears Host.';
    Exit;
  end;
  if DirExists(ExpandConstant('{app}')) then
  begin
    if not ExistingInstallOwned() then
    begin
      Result := 'This folder is not a recognized Vemal''s Ears Host installation.';
      Exit;
    end;
    if not RunOwnedControl('installer-stop') then
      Result := 'The existing host services could not be stopped safely. Open Vemal''s Ears Host and resolve its service status before updating.'
    else
      ResumeAfterInstall := True;
  end;
end;

procedure CurStepChanged(CurStep: TSetupStep);
begin
#ifndef TestBuild
  if (CurStep = ssPostInstall) and ResumeAfterInstall then
  begin
    if not RunOwnedControl('installer-resume') then
    begin
      ResumeFailed := True;
      Log('Host files installed, but previously running host services could not be resumed.');
      SuppressibleMsgBox('The host update was installed, but its services could not be resumed. Open Vemal''s Ears Host to check the status and start the host.', mbError, MB_OK, IDOK);
    end;
  end;
#endif
end;

function GetCustomSetupExitCode(): Integer;
begin
  if ResumeFailed then
    Result := 12
  else
    Result := 0;
end;

function InitializeUninstall(): Boolean;
begin
  ValidatedUninstall := False;
  Result := InstallPathIsSafe() and ExistingInstallOwned();
  if Result then
    Result := RunOwnedControl('installer-remove');
  if not Result then
  begin
    MsgBox('The host installation or service ownership could not be verified. Uninstall stopped; private host state has been kept.', mbError, MB_OK);
    Exit;
  end;
  ValidatedUninstall := True;
end;

function UninstallTargetValidated(): Boolean;
begin
  Result := ValidatedUninstall and InstallPathIsSafe();
end;
