#ifndef AppVersion
  #error AppVersion must be supplied by build_installer.ps1
#endif
#ifndef StageRoot
  #error StageRoot must be supplied by build_installer.ps1
#endif
#ifndef OutputDir
  #error OutputDir must be supplied by build_installer.ps1
#endif

#ifdef TestBuild
  #define AppName "Vemal's Ears Installer Test"
#else
  #define AppName "Vemal's Ears"
#endif
#define AppExeName "VemalsEars.exe"
#define AppMutexName "Local\VemalsEars.Companion"
; The fixed per-user location belongs only to the caption application.
#define FixedAppFolder "{localappdata}\Programs\VemalsEars"

#ifdef TestBuild
  #define OutputName "Vemals-Ears-Setup-TEST-UNSIGNED"
#else
  #if defined(ProofUntrusted)
    ; Signed for real, through the whole pipeline, by a certificate nobody
    ; trusts. It proves the plumbing before a certificate is bought, and
    ; the name identifies it as a proof rather than an official release.
    #define OutputName "Vemals-Ears-Setup-PROOF-UNTRUSTED"
  #elif defined(SignedBuild) || defined(DistributeUnsigned)
    ; DistributeUnsigned is a deliberate choice made at build time: the
    ; guild's members are handed this link directly by someone they know,
    ; and a file called "UNSIGNED" is more alarming to them than the
    ; SmartScreen prompt it is warning about. Signing is still the goal;
    ; this only stops the filename from doing the frightening.
    #define OutputName "Vemals-Ears-Setup"
  #else
    #define OutputName "Vemals-Ears-Setup-UNSIGNED"
  #endif
#endif

[Setup]
#ifdef TestBuild
AppId={{876A5690-2C5B-44F2-9134-94A3A1899B9E}
#else
AppId={{6A72924D-172F-4A52-A0EC-D26A302C043A}
#endif
AppName={#AppName}
AppVersion={#AppVersion}
; Setup.exe's OWN file properties. Inno fills ProductVersion from
; AppVersion but leaves FileVersion empty unless told, so the file a
; member downloads showed a blank version in Explorer's Details tab -
; on an unsigned executable, where "who made this and which build is
; it" is the only reassurance available.
VersionInfoVersion={#AppVersion}
VersionInfoProductVersion={#AppVersion}
VersionInfoCompany=Zackery Hodges
VersionInfoProductName={#AppName}
VersionInfoDescription={#AppName} Setup
AppVerName={#AppName} {#AppVersion}
AppPublisher=Zackery Hodges
AppPublisherURL=https://github.com/hodgeszackery/downtime-addons
AppSupportURL=https://github.com/hodgeszackery/downtime-addons/issues
AppUpdatesURL=https://github.com/hodgeszackery/downtime-addons/releases
#ifdef TestBuild
DefaultDirName={localappdata}\Temp\Vemals Ears Installer Smoke-Manual
#else
DefaultDirName={#FixedAppFolder}
#endif
DefaultGroupName={#AppName}
DisableDirPage=yes
DisableProgramGroupPage=yes
UninstallDisplayIcon={app}\{#AppExeName}
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
MinVersion=10.0.17763
OutputDir={#OutputDir}
OutputBaseFilename={#OutputName}
SetupIconFile={#StageRoot}\VemalsEars.ico
Compression=lzma2/ultra64
SolidCompression=yes
WizardStyle=modern dynamic
WizardSizePercent=110
; force, not yes: install-over-a-running-copy stops asking politely.
; This setting is INSTALL-only - the uninstaller never opens a Restart
; Manager session - so the uninstall half lives in
; CloseRunningCompanion below, at usAppMutexCheck.
CloseApplications=force
RestartApplications=no
#ifdef TestBuild
ChangesAssociations=no
#else
ChangesAssociations=yes
#endif
; AppMutex is deliberately NOT declared, and re-adding it would put the
; bug back. Inno checks it at startup - before the wizard, and long
; before PrepareToInstall, where SetupCloseRunningCompanion lives - so
; declaring it meant the member was told "close all instances of it"
; by the one program that was about to close the companion for them.
; That is the whole reason updating failed.
;
; Measured on this machine against Inno 7, rather than reasoned about:
; InitializeSetup runs BEFORE the mutex check, {app} cannot be expanded
; that early (so the close cannot simply move there), and
; usAppMutexCheck still fires for the uninstaller with no AppMutex
; declared - which is what keeps CloseRunningCompanion wired.
;
; Closing beats prompting, so PrepareToInstall closes the companion and
; refuses the install only if that actually failed.
UsePreviousAppDir=no
UsePreviousGroup=yes
AllowNoIcons=yes
SetupLogging=yes
#ifdef SignedBuild
SignTool=artifact
SignedUninstaller=yes
SignToolRetryCount=3
SignToolMinimumTimeBetween=1000
#else
SignedUninstaller=no
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Files]
#ifdef SignedBuild
Source: "{#StageRoot}\VemalsEars.exe"; DestDir: "{app}"; Flags: ignoreversion signonce
#else
Source: "{#StageRoot}\VemalsEars.exe"; DestDir: "{app}"; Flags: ignoreversion
#endif
Source: "{#StageRoot}\runtime\*"; DestDir: "{app}\runtime"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageRoot}\app\*"; DestDir: "{app}\app"; Flags: ignoreversion recursesubdirs createallsubdirs
Source: "{#StageRoot}\payload-manifest.json"; DestDir: "{app}"; Flags: ignoreversion

[InstallDelete]
Type: filesandordirs; Name: "{app}\app"; Check: InstallTargetIsSafe
Type: filesandordirs; Name: "{app}\runtime"; Check: InstallTargetIsSafe

#ifndef TestBuild
[Icons]
Name: "{group}\Vemal's Ears"; Filename: "{app}\{#AppExeName}"; WorkingDir: "{app}"; IconFilename: "{app}\{#AppExeName}"; AppUserModelID: "VemalsEars.Companion"
Name: "{group}\Uninstall Vemal's Ears"; Filename: "{uninstallexe}"; Flags: preventpinning

[Registry]
Root: HKCU; Subkey: "Software\Classes\vemalsears\shell\open\command"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"" --vemals-ears-pair ""%1"""
Root: HKCU; Subkey: "Software\Classes\vemalsears\DefaultIcon"; ValueType: string; ValueName: ""; ValueData: """{app}\{#AppExeName}"",0"
Root: HKCU; Subkey: "Software\Classes\vemalsears"; ValueType: string; ValueName: ""; ValueData: "URL:Vemal's Ears Pairing Link"
Root: HKCU; Subkey: "Software\Classes\vemalsears"; ValueType: string; ValueName: "URL Protocol"; ValueData: ""
Root: HKCU; Subkey: "Software\VemalsEars\Installer"; ValueType: string; ValueName: "Version"; ValueData: "{#AppVersion}"

[Run]
Filename: "{app}\{#AppExeName}"; Description: "Open Vemal's Ears"; Flags: nowait postinstall skipifsilent
Filename: "{app}\{#AppExeName}"; Flags: nowait; Check: IsAutomaticUpdateInvocation

[UninstallRun]
Filename: "{app}\runtime\pythonw.exe"; Parameters: "-E -s -B ""{app}\app\Installer\uninstall_cleanup.py"""; WorkingDir: "{app}\app"; Flags: runhidden waituntilterminated; RunOnceId: "VemalsEarsMemberCleanup"
#endif

[Code]
const
  InstallerStateKey = 'Software\VemalsEars\Installer';
  ProtocolKey = 'Software\Classes\vemalsears';
  ProtocolDefaultIconKey = 'Software\Classes\vemalsears\DefaultIcon';
  ProtocolShellKey = 'Software\Classes\vemalsears\shell';
  ProtocolOpenKey = 'Software\Classes\vemalsears\shell\open';
  ProtocolCommandKey = 'Software\Classes\vemalsears\shell\open\command';
  RunKey = 'Software\Microsoft\Windows\CurrentVersion\Run';
  RunValueName = 'Vemal''s Ears';

function IsAutomaticUpdateInvocation(): Boolean;
var
  Index: Integer;
begin
  Result := False;
  { Only the updater supplies this exact switch. The silent check keeps a
    copied command line from creating a second interactive post-install
    launch, while an ordinary silent deployment continues to launch nothing. }
  if not WizardSilent then
    Exit;
  for Index := 1 to ParamCount do
  begin
    if CompareText(ParamStr(Index), '/VEAUTOUPDATE') = 0 then
    begin
      Result := True;
      Exit;
    end;
  end;
end;

function PathIsSafeForPayloadCleanup(InstallPath: String): Boolean;
var
  ExpectedPath: String;
  Prefix: String;
  Suffix: String;
begin
  InstallPath := RemoveBackslashUnlessRoot(InstallPath);
#ifdef TestBuild
  Prefix := RemoveBackslashUnlessRoot(
    ExpandConstant('{localappdata}\Temp\Vemals Ears Installer Smoke-')
  );
  if CompareText(Copy(InstallPath, 1, Length(Prefix)), Prefix) <> 0 then
  begin
    Result := False;
    Exit;
  end;
  Suffix := Copy(InstallPath, Length(Prefix) + 1, MaxInt);
  Result := (Suffix <> '') and (Length(Suffix) <= 80) and
            (Pos('\', Suffix) = 0) and (Pos('/', Suffix) = 0);
#else
  ExpectedPath := RemoveBackslashUnlessRoot(
    ExpandConstant('{localappdata}\Programs\VemalsEars')
  );
  Result := SameText(InstallPath, ExpectedPath);
#endif
end;

function InstallTargetIsSafe(): Boolean;
begin
  Result := PathIsSafeForPayloadCleanup(ExpandConstant('{app}'));
end;

#ifndef TestBuild
{ Setup-side twins of the uninstall externals: uninstallonly
  declarations do not exist at install time, and Pascal has no way to
  share one declaration across both contexts. }
function SetupOpenProcess(Access: DWORD; Inherit: BOOL; Pid: DWORD): THandle;
  external 'OpenProcess@kernel32.dll stdcall';
function SetupCloseHandle(Handle: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall';
function SetupQueryImageName(Handle: THandle; Flags: DWORD;
  ExeName: String; var Size: DWORD): BOOL;
  external 'QueryFullProcessImageNameW@kernel32.dll stdcall';
function SetupGetExitCode(Handle: THandle; var ExitCode: DWORD): BOOL;
  external 'GetExitCodeProcess@kernel32.dll stdcall';
function SetupFindWindowAfter(Parent: THandle; After: THandle;
  ClassName: String; WindowName: Integer): THandle;
  external 'FindWindowExW@user32.dll stdcall';
function SetupWindowProcessId(Window: THandle; var Pid: DWORD): DWORD;
  external 'GetWindowThreadProcessId@user32.dll stdcall';
function SetupPostMessage(Window: THandle; Msg: Cardinal;
  WParam: Integer; LParam: Integer): BOOL;
  external 'PostMessageW@user32.dll stdcall';

function SetupProcessIsOurs(Pid: DWORD): Boolean;
var
  ProcessHandle: THandle;
  Buffer: String;
  Size: DWORD;
  Prefix: String;
begin
  Result := False;
  ProcessHandle := SetupOpenProcess($1000, False, Pid);
  if ProcessHandle = 0 then
    Exit;
  try
    Size := 1024;
    SetLength(Buffer, Size);
    if SetupQueryImageName(ProcessHandle, 0, Buffer, Size) then
    begin
      Prefix := ExpandConstant('{app}') + '\';
      Result := CompareText(Copy(Buffer, 1, Length(Prefix)), Prefix) = 0;
    end;
  finally
    SetupCloseHandle(ProcessHandle);
  end;
end;

function SetupLegacyProcessIsOurs(ProcessHandle: THandle): Boolean;
var
  Buffer: String;
  Size: DWORD;
  ExitCode: DWORD;
begin
  Result := False;
  if not SetupGetExitCode(ProcessHandle, ExitCode) then
    Exit;
  if ExitCode <> 259 then
    Exit;
  Size := 32768;
  SetLength(Buffer, Size);
  if not SetupQueryImageName(ProcessHandle, 0, Buffer, Size) then
    Exit;
  Buffer := Copy(Buffer, 1, Size);
  { Only the old installed UI images, not an arbitrary helper under app.
    A sibling path, source checkout, or a different Python is not ours. }
  Result := SameText(Buffer, ExpandConstant('{app}\VemalsEars.exe')) or
            SameText(Buffer, ExpandConstant('{app}\runtime\pythonw.exe'));
end;

function SetupLegacyTrayForPid(Pid: DWORD): THandle;
var
  Window: THandle;
  WindowPid: DWORD;
  Count: Integer;
begin
  Result := 0;
  Window := 0;
  { New Downtime and legacy VE use this class. Never just take its first
    window: the shared lock and exact installed process select the owner. }
  for Count := 1 to 64 do
  begin
    Window := SetupFindWindowAfter(0, Window, 'DowntimeCompanionTray', 0);
    if Window = 0 then
      Exit;
    WindowPid := 0;
    if (SetupWindowProcessId(Window, WindowPid) <> 0) and (WindowPid = Pid) then
    begin
      Result := Window;
      Exit;
    end;
  end;
end;

procedure SetupCloseLegacyCompanion();
var
  LockPath: String;
  LockText: AnsiString;
  Pid: Integer;
  ProcessHandle: THandle;
  Window: THandle;
  WindowPid: DWORD;
  Waited: Integer;
begin
  if not CheckForMutexes('{#AppMutexName}') then
    Exit;
  LockPath := ExpandConstant('{localappdata}\GearHelper\companion-window.lock');
  if not LoadStringFromFile(LockPath, LockText) then
    Exit;
  Pid := StrToIntDef(Trim(String(LockText)), 0);
  if Pid <= 0 then
    Exit;
  ProcessHandle := SetupOpenProcess($1000, False, Pid);
  if ProcessHandle = 0 then
    Exit;
  try
    { Keep this handle until after the close: Windows cannot recycle its
      PID while the handle survives. Missing/ambiguous ownership fails
      closed, leaving PrepareToInstall to ask the user to exit manually. }
    if not SetupLegacyProcessIsOurs(ProcessHandle) then
      Exit;
    Window := SetupLegacyTrayForPid(Pid);
    if Window = 0 then
      Exit;
    if not LoadStringFromFile(LockPath, LockText) then
      Exit;
    if StrToIntDef(Trim(String(LockText)), 0) <> Pid then
      Exit;
    if not SetupLegacyProcessIsOurs(ProcessHandle) then
      Exit;
    WindowPid := 0;
    if (SetupWindowProcessId(Window, WindowPid) = 0) or (WindowPid <> Pid) then
      Exit;
    { The old TrayIcon.EXIT command takes the same orderly shutdown route
      as clicking Exit. Do NOT write GearHelper's shared quit file: a new
      Downtime app can be listening there at the same time. }
    if not SetupPostMessage(Window, $0111, 1024, 0) then
      Exit;
    Waited := 0;
    while CheckForMutexes('{#AppMutexName}') and (Waited < 12000) do
    begin
      Sleep(250);
      Waited := Waited + 250;
    end;
  finally
    SetupCloseHandle(ProcessHandle);
  end;
end;

procedure SetupCloseRunningCompanion();
var
  QuitRequest: String;
  LockPath: String;
  LockText: AnsiString;
  Pid: Integer;
  Waited: Integer;
  ResultCode: Integer;
begin
  { The update path: installing over a running copy closes it first, the
    same polite-then-forcible sequence the uninstaller uses, so an
    upgrade never races a live companion for the files it is replacing. }
  if not CheckForMutexes('{#AppMutexName}') then
    Exit;
  QuitRequest := ExpandConstant('{localappdata}\VemalsEars\companion-quit.request');
  LockPath := ExpandConstant('{localappdata}\VemalsEars\companion-window.lock');
  SaveStringToFile(QuitRequest, 'quit', False);
  Waited := 0;
  while FileExists(QuitRequest) and (Waited < 1000) do
  begin
    Sleep(100);
    Waited := Waited + 100;
  end;
  if not FileExists(QuitRequest) then
  begin
    Waited := 0;
    while CheckForMutexes('{#AppMutexName}') and (Waited < 12000) do
    begin
      Sleep(250);
      Waited := Waited + 250;
    end;
  end;
  if CheckForMutexes('{#AppMutexName}') then
  begin
    if LoadStringFromFile(LockPath, LockText) then
    begin
      Pid := StrToIntDef(Trim(String(LockText)), 0);
      if (Pid > 0) and SetupProcessIsOurs(Pid) then
      begin
        Exec(ExpandConstant('{sys}\taskkill.exe'),
             '/F /T /PID ' + IntToStr(Pid), '', SW_HIDE,
             ewWaitUntilTerminated, ResultCode);
        Waited := 0;
        while CheckForMutexes('{#AppMutexName}') and (Waited < 3000) do
        begin
          Sleep(250);
          Waited := Waited + 250;
        end;
      end;
    end;
  end;
  DeleteFile(QuitRequest);
  SetupCloseLegacyCompanion();
end;
#endif


function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  if not PathIsSafeForPayloadCleanup(WizardDirValue) then
  begin
    Result := 'Vemal''s Ears will install only in its fixed per-user application directory.';
    Exit;
  end;
  Result := '';
#ifndef TestBuild
  { The update path: a running companion is closed here, before any file
    is replaced. With no AppMutex declared this is the only thing between
    an upgrade and files in use, so the close is followed by a re-check -
    closing beats prompting, but a close that FAILED must stop the
    install rather than write over a live process. Refusing here costs
    the member nothing: PrepareToInstall runs before the first file is
    touched, so there is no half-installed copy to recover from.

    It runs this late on purpose. The member still has a running
    companion all the way through the wizard, so cancelling costs them
    nothing either - the same reason Xenon's agent updater lets the
    installer stop it rather than exiting first.

    TestBuild is excluded so the smoke test can never grow a kill path
    against the owner's live companion. }
  SetupCloseRunningCompanion();
  if CheckForMutexes('{#AppMutexName}') then
    { Kept on one continued line deliberately: ISPP reads a line whose
      first non-blank character is '#' as a preprocessor directive, so a
      wrapped string starting with #13#10 fails the build outright. }
    Result := 'Vemal''s Ears is still running and could not be closed ' +
      'automatically.' + #13#10 + #13#10 +
      'Close it from its tray icon, then run this installer again.';
#endif
end;

#ifndef TestBuild
function InitializeSetup(): Boolean;
var
  CurrentText: String;
  CurrentVersion: Int64;
  NewVersion: Int64;
begin
  Result := True;
  if not StrToVersion('{#AppVersion}', NewVersion) then
  begin
    MsgBox('This Vemal''s Ears installer has invalid version metadata.', mbCriticalError, MB_OK);
    Result := False;
    Exit;
  end;
  if RegQueryStringValue(HKCU, InstallerStateKey, 'Version', CurrentText) then
  begin
    if not StrToVersion(CurrentText, CurrentVersion) then
    begin
      MsgBox(
        'The installed Vemal''s Ears version record is invalid. Repair or uninstall it before continuing.',
        mbCriticalError,
        MB_OK
      );
      Result := False;
      Exit;
    end;
    if ComparePackedVersion(CurrentVersion, NewVersion) > 0 then
    begin
      MsgBox(
        'A newer version of Vemal''s Ears is already installed. This older installer will not replace it.',
        mbCriticalError,
        MB_OK
      );
      Result := False;
    end;
  end;
end;

procedure RemoveOwnedRegistryValues();
var
  CurrentValue: String;
  ExpectedValue: String;
begin
  ExpectedValue := '"' + ExpandConstant('{app}\{#AppExeName}') + '" --vemals-ears-pair "%1"';
  if RegQueryStringValue(HKCU, ProtocolCommandKey, '', CurrentValue) and
     SameText(CurrentValue, ExpectedValue) then
  begin
    RegDeleteValue(HKCU, ProtocolCommandKey, '');
    RegDeleteKeyIfEmpty(HKCU, ProtocolCommandKey);
    RegDeleteKeyIfEmpty(HKCU, ProtocolOpenKey);
    RegDeleteKeyIfEmpty(HKCU, ProtocolShellKey);

    ExpectedValue := '"' + ExpandConstant('{app}\{#AppExeName}') + '",0';
    if RegQueryStringValue(HKCU, ProtocolDefaultIconKey, '', CurrentValue) and
       SameText(CurrentValue, ExpectedValue) then
    begin
      RegDeleteValue(HKCU, ProtocolDefaultIconKey, '');
      RegDeleteKeyIfEmpty(HKCU, ProtocolDefaultIconKey);
    end;

    if RegQueryStringValue(HKCU, ProtocolKey, '', CurrentValue) and
       SameText(CurrentValue, 'URL:Vemal''s Ears Pairing Link') then
    begin
      RegDeleteValue(HKCU, ProtocolKey, '');
    end;
    if RegQueryStringValue(HKCU, ProtocolKey, 'URL Protocol', CurrentValue) and
       (CurrentValue = '') then
    begin
      RegDeleteValue(HKCU, ProtocolKey, 'URL Protocol');
    end;
    RegDeleteKeyIfEmpty(HKCU, ProtocolKey);
  end;

  ExpectedValue := '"' + ExpandConstant('{app}\{#AppExeName}') + '"';
  if RegQueryStringValue(HKCU, RunKey, RunValueName, CurrentValue) and
     SameText(CurrentValue, ExpectedValue) then
  begin
    RegDeleteValue(HKCU, RunKey, RunValueName);
  end;
  RegDeleteKeyIncludingSubkeys(HKCU, InstallerStateKey);
  RegDeleteKeyIfEmpty(HKCU, 'Software\VemalsEars');
end;

{ The uninstaller's [Code] has no Windows API by default; these three are
  the whole surface, marked uninstallonly because only
  CloseRunningCompanion uses them. }
function OpenProcess(Access: DWORD; Inherit: BOOL; Pid: DWORD): THandle;
  external 'OpenProcess@kernel32.dll stdcall uninstallonly';
function CloseHandle(Handle: THandle): BOOL;
  external 'CloseHandle@kernel32.dll stdcall uninstallonly';
function QueryFullProcessImageNameW(Handle: THandle; Flags: DWORD;
  ExeName: String; var Size: DWORD): BOOL;
  external 'QueryFullProcessImageNameW@kernel32.dll stdcall uninstallonly';

function ProcessImagePath(Pid: DWORD): String;
var
  ProcessHandle: THandle;
  Buffer: String;
  Size: DWORD;
begin
  Result := '';
  { PROCESS_QUERY_LIMITED_INFORMATION only - reading an image path needs
    no debugger rights, and asking for more would be the wrong precedent
    on a machine that runs anti-cheat software. }
  ProcessHandle := OpenProcess($1000, False, Pid);
  if ProcessHandle = 0 then
    Exit;
  try
    Size := 1024;
    SetLength(Buffer, Size);
    if QueryFullProcessImageNameW(ProcessHandle, 0, Buffer, Size) then
      Result := Copy(Buffer, 1, Size);
  finally
    CloseHandle(ProcessHandle);
  end;
end;

function ProcessIsOurs(Pid: DWORD): Boolean;
var
  ImagePath: String;
  Prefix: String;
begin
  { The OpenProcess inside ProcessImagePath pins the pid while we look,
    so a recycle between reading the lock and killing cannot redirect
    the kill at an innocent process. }
  ImagePath := ProcessImagePath(Pid);
  Prefix := ExpandConstant('{app}') + '\';
  Result := (ImagePath <> '') and
            (CompareText(Copy(ImagePath, 1, Length(Prefix)), Prefix) = 0);
end;

procedure CloseRunningCompanion();
var
  QuitRequest: String;
  LockPath: String;
  LockText: AnsiString;
  Pid: Integer;
  Waited: Integer;
  ResultCode: Integer;
begin
  if not InstallTargetIsSafe() then
    Exit;
  { Nothing running: touch nothing, ask nothing. }
  if not CheckForMutexes('{#AppMutexName}') then
    Exit;

  { POLITE, acknowledgement-gated. The app consumes this file within a
    tick when it understands it; a build that predates the request, or a
    wedged one, leaves it untouched - and that silence is the signal to
    escalate, which is why the polite budget is one second rather than a
    long wait against software that cannot answer. }
  QuitRequest := ExpandConstant('{localappdata}\VemalsEars\companion-quit.request');
  LockPath := ExpandConstant('{localappdata}\VemalsEars\companion-window.lock');
  { The content is irrelevant - the app judges freshness by the file's
    mtime and deletion is the acknowledgement. }
  SaveStringToFile(QuitRequest, 'quit', False);
  Waited := 0;
  while FileExists(QuitRequest) and (Waited < 1000) do
  begin
    Sleep(100);
    Waited := Waited + 100;
  end;

  if not FileExists(QuitRequest) then
  begin
    { Understood. Give the clean shutdown a generous window - it stops
      watcher services before exiting. }
    Waited := 0;
    while CheckForMutexes('{#AppMutexName}') and (Waited < 12000) do
    begin
      Sleep(250);
      Waited := Waited + 250;
    end;
  end;

  if CheckForMutexes('{#AppMutexName}') then
  begin
    { FORCIBLE, and scoped hard: the pid from the companion's own lock,
      and only if that pid's image lives under this install. Never by
      image name - other Python processes on the machine belong to
      software this uninstaller does not own. }
    if LoadStringFromFile(LockPath, LockText) then
    begin
      Pid := StrToIntDef(Trim(String(LockText)), 0);
      if (Pid > 0) and ProcessIsOurs(Pid) then
      begin
        Exec(ExpandConstant('{sys}\taskkill.exe'),
             '/F /T /PID ' + IntToStr(Pid), '', SW_HIDE,
             ewWaitUntilTerminated, ResultCode);
        Waited := 0;
        while CheckForMutexes('{#AppMutexName}') and (Waited < 3000) do
        begin
          Sleep(250);
          Waited := Waited + 250;
        end;
      end;
    end;
  end;
  DeleteFile(QuitRequest);
  { AppMutex is not declared, so there is no automatic Inno backstop.
    Abort from usAppMutexCheck stops uninstall before any payload or
    credentials are removed. Keep the lock so a later attempt can still
    identify this process. Older VE copies use the legacy shared lock;
    closing those manually avoids reaching into Downtime's state. }
  if CheckForMutexes('{#AppMutexName}') then
  begin
    SuppressibleMsgBox(
      'Vemal''s Ears is still running and could not be closed automatically.' + #13#10 + #13#10 +
      'Close Vemal''s Ears from its tray icon, including ' +
      'any older copy, then run the uninstaller again. No application files ' +
      'or saved connections have been removed.',
      mbError, MB_OK, IDOK);
    Abort;
  end;
  DeleteFile(LockPath);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
begin
  if CurUninstallStep = usAppMutexCheck then
  begin
    CloseRunningCompanion();
  end;
  if CurUninstallStep = usPostUninstall then
  begin
    RemoveOwnedRegistryValues();
  end;
end;
#endif
