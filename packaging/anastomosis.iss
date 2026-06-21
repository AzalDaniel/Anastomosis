; Anastomosis Windows installer (Inno Setup 6).
;
; Built by .github/workflows/windows-package.yml, which compiles two Nuitka
; --mode=standalone builds (the windowed GUI app + the console CLI), downloads
; the WebView2 bootstrapper, and invokes:  ISCC /DAppVersion=<x.y.z> anastomosis.iss
;
; What it does: installs the GUI app + the `anast` CLI under {app}, adds a
; Start-menu shortcut + uninstaller, optionally puts the CLI on PATH, and — only
; if the Edge WebView2 Runtime is absent — silently installs it (most Windows
; 10/11 machines already have it, so this is normally a no-op). Unsigned by
; default; pass /DSignTool=<name> to sign (the seam below).

#ifndef AppVersion
  #define AppVersion "0.0.0"
#endif

[Setup]
AppId={{A1B2C3D4-0E5F-4A6B-9C7D-ANASTOMOSIS01}}
AppName=Anastomosis
AppVersion={#AppVersion}
AppPublisher=Anastomosis
AppPublisherURL=https://github.com/AzalDaniel/Anastomosis
DefaultDirName={autopf}\Anastomosis
DefaultGroupName=Anastomosis
UninstallDisplayName=Anastomosis
DisableProgramGroupPage=yes
ArchitecturesAllowed=x64compatible
ArchitecturesInstallIn64BitMode=x64compatible
Compression=lzma2/max
SolidCompression=yes
OutputDir=dist\installer
OutputBaseFilename=Anastomosis-Setup-{#AppVersion}
WizardStyle=modern
; Signing seam: a no-op unless ISCC is invoked with /DSignTool=<configured tool>.
#ifdef SignTool
SignTool={#SignTool}
#endif

[Languages]
Name: "english"; MessagesFile: "compiler:Default.isl"

[Tasks]
Name: "addtopath"; Description: "Add the 'anast' command-line tool to PATH"; \
  GroupDescription: "Command-line access:"; Flags: unchecked

[Files]
; The windowed GUI app and the console CLI — each a self-contained Nuitka dist
; (Python runtime + bundled Chromium + all data assets).
Source: "dist\Anastomosis\*"; DestDir: "{app}\gui"; \
  Flags: recursesubdirs createallsubdirs ignoreversion
Source: "dist\anast\*"; DestDir: "{app}\cli"; \
  Flags: recursesubdirs createallsubdirs ignoreversion
; The WebView2 bootstrapper (downloaded at build time), staged for the [Run] step.
Source: "build\MicrosoftEdgeWebview2Setup.exe"; DestDir: "{tmp}"; Flags: deleteafterinstall

[Icons]
Name: "{group}\Anastomosis"; Filename: "{app}\gui\Anastomosis.exe"
Name: "{group}\Uninstall Anastomosis"; Filename: "{uninstallexe}"

[Registry]
; Append the CLI directory to the per-user PATH (idempotent; only if the task is
; checked). Per-user keeps the install non-elevating; the canonical NeedsAddPath
; check avoids duplicate entries.
Root: HKCU; Subkey: "Environment"; ValueType: expandsz; ValueName: "Path"; \
  ValueData: "{olddata};{app}\cli"; Tasks: addtopath; Check: NeedsAddPath('{app}\cli')

[Run]
; Install the Edge WebView2 Runtime only when it is not already present (the GUI
; renders through it). On a machine that already has it this never runs.
Filename: "{tmp}\MicrosoftEdgeWebview2Setup.exe"; Parameters: "/silent /install"; \
  StatusMsg: "Installing the Microsoft Edge WebView2 Runtime (if needed)..."; \
  Check: not WebView2Installed; Flags: waituntilterminated
; Offer to launch the app at the end (skipped on a silent install).
Filename: "{app}\gui\Anastomosis.exe"; Description: "Launch Anastomosis"; \
  Flags: nowait postinstall skipifsilent

[Code]
function WebView2Installed(): Boolean;
var
  pv: string;
begin
  // Per Microsoft's distribution doc, the runtime is present when one of these
  // EdgeUpdate "Clients" keys holds a 'pv' version greater than 0.0.0.0. Check
  // the machine-wide 64-bit (WOW6432Node) + native + per-user views so the
  // detection holds regardless of install scope or registry redirection.
  Result :=
    (RegQueryStringValue(HKLM,
      'SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', pv) and (pv <> '') and (pv <> '0.0.0.0')) or
    (RegQueryStringValue(HKLM,
      'SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', pv) and (pv <> '') and (pv <> '0.0.0.0')) or
    (RegQueryStringValue(HKCU,
      'Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}',
      'pv', pv) and (pv <> '') and (pv <> '0.0.0.0'));
end;

function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
begin
  if not RegQueryStringValue(HKCU, 'Environment', 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  // Idempotent: only add if the dir is not already a PATH segment.
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;
