; Build through BUILD.bat / scripts/package_release.py. PayloadInclude is an audited file list.
#ifndef AppVersion
  #error AppVersion must be supplied by the Windows packaging pipeline
#endif
#ifndef PayloadInclude
  #error PayloadInclude must be supplied by the Windows packaging pipeline
#endif
#ifndef AppGuid
  #define AppGuid "2E25ABE9-E3D5-4ABA-9E50-725BA75185AF"
#endif
#ifndef AppGroup
  #define AppGroup "Atelier"
#endif

[Setup]
AppId={code:ApplicationIdentity}
AppName=Atelier
AppVersion={#AppVersion}
AppVerName=Atelier {#AppVersion}
VersionInfoVersion={#AppVersion}
AppPublisher=Atelier
AppPublisherURL=https://github.com/clownfetus/Atelier
DefaultDirName={localappdata}\Atelier
DefaultGroupName={code:ApplicationGroup}
DisableProgramGroupPage=yes
UsePreviousLanguage=no
OutputBaseFilename=AtelierSetup
Compression=lzma2/fast
SolidCompression=yes
WizardStyle=modern
PrivilegesRequired=lowest
ArchitecturesAllowed=x64compatible
; Retain upstream's 32-bit installer registration view for upgrade discovery.
; The application payload is x64; install mode does not change executable bitness.
MinVersion=10.0
CloseApplications=yes
RestartApplications=no
SetupMutex=AtelierPackagingSetup
UninstallDisplayIcon={app}\Atelier.exe
UninstallDisplayName=Atelier
LicenseFile=LICENSE
SetupIconFile=icon.ico

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "desktopicon"; Description: "Create a &desktop shortcut"; GroupDescription: "Additional icons:"; Flags: unchecked

[Files]
; No wildcard over dist or Tools. Exactly the same files as the validated portable payload.
#include PayloadInclude

[Dirs]
Name: "{app}\assets\projects"; Flags: uninsneveruninstall
Name: "{app}\assets\exported"; Flags: uninsneveruninstall

[Icons]
Name: "{group}\Atelier"; Filename: "{app}\Atelier.exe"; WorkingDir: "{app}"
Name: "{userdesktop}\{code:ApplicationGroup}"; Filename: "{app}\Atelier.exe"; WorkingDir: "{app}"; Tasks: desktopicon

[InstallDelete]
; The immutable PyInstaller runtime is owned by the installer. Removed Python/native modules
; must not survive an upgrade. No parent of projects, preferences or external tools is removed.
Type: filesandordirs; Name: "{app}\_internal"

[Run]
Filename: "{app}\Atelier.exe"; Description: "Launch Atelier"; Flags: nowait postinstall skipifsilent

[Code]
function PackagingTestIdentity: String;
var
  I: Integer;
begin
  Result := Lowercase(ExpandConstant('{param:PACKAGINGTESTID|@none}'));
  if Result = '@none' then begin
    Result := '';
    Exit;
  end;
  if (Length(Result) <> 32) or (ExpandConstant('{param:DIR|}') = '') then
    RaiseException('PACKAGINGTESTID requires a 32-character hexadecimal ID and an explicit /DIR.');
  for I := 1 to Length(Result) do
    if Pos(Result[I], '0123456789abcdef') = 0 then
      RaiseException('Invalid PACKAGINGTESTID.');
end;

function ApplicationIdentity(Param: String): String;
var
  TestId: String;
begin
  TestId := PackagingTestIdentity;
  if TestId = '' then Result := '{' + '{#AppGuid}' + '}'
  else Result := 'AtelierPackagingTest-' + TestId;
end;

function ApplicationGroup(Param: String): String;
var
  TestId: String;
begin
  TestId := PackagingTestIdentity;
  if TestId = '' then Result := '{#AppGroup}'
  else Result := 'AtelierPackagingTest-' + TestId;
end;

function HasDotNet48: Boolean;
var
  Release: Cardinal;
begin
  Result := RegQueryDWordValue(HKLM32, 'SOFTWARE\Microsoft\NET Framework Setup\NDP\v4\Full', 'Release', Release)
            and (Release >= 528040);
end;

function HasWebView2: Boolean;
var
  Version: String;
  Key: String;
begin
  Key := 'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}';
  Result := ((RegQueryStringValue(HKCU, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
             (RegQueryStringValue(HKLM32, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')) or
             (RegQueryStringValue(HKLM64, Key, 'pv', Version) and (Version <> '') and (Version <> '0.0.0.0')));
end;

function PrepareToInstall(var NeedsRestart: Boolean): String;
begin
  Result := '';
  if not HasDotNet48 then begin
    Result := '.NET Framework 4.8 or newer is required. Install it from https://dotnet.microsoft.com/download/dotnet-framework and run Setup again.';
    Exit;
  end;
  if not HasWebView2 then
    Result := 'Microsoft Edge WebView2 Runtime is required. Install the Evergreen Runtime from https://developer.microsoft.com/microsoft-edge/webview2/ and run Setup again. Your existing Atelier files have not been changed.';
end;
