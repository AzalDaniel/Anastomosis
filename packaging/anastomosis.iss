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
; Resolve relative [Files] sources + OutputDir from the REPO ROOT (this script
; lives in packaging/, but build_windows.py writes dist/ and the workflow writes
; build/ at the repo root). Inno resolves a relative SourceDir from the script's
; own directory, so ".." is the repo root.
SourceDir=..
OutputDir=dist\installer
OutputBaseFilename=Anastomosis-Setup-{#AppVersion}
WizardStyle=modern
; A per-machine install under Program Files (the "normal Windows app" layout):
; elevation is required, and the optional CLI-on-PATH task therefore writes the
; MACHINE PATH (see [Registry]) — writing the per-user HKCU PATH under elevation
; would target the elevating admin's hive, not the running user's.
PrivilegesRequired=admin
; We mutate the machine PATH (optional task), so broadcast WM_SETTINGCHANGE on
; finish; without this a new shell would not see `anast` until the next logon.
ChangesEnvironment=yes
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
; Append the CLI directory to the MACHINE PATH (idempotent; only if the task is
; checked). This is a per-machine install (elevated), so the machine PATH — not
; HKCU — is the correct target: under elevation HKCU is the elevating admin's
; hive, not the user who will run `anast`. The NeedsAddPath check avoids a
; duplicate segment; CurUninstallStepChanged strips it on uninstall.
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
  ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\cli"; \
  Tasks: addtopath; Check: NeedsAddPath('{app}\cli')

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
// The machine PATH lives here (REG_EXPAND_SZ); the optional task appends to it,
// and CurUninstallStepChanged strips it back out.
const
  EnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';

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
  if not RegQueryStringValue(HKLM, EnvKey, 'Path', OrigPath) then
  begin
    Result := True;
    exit;
  end;
  // Idempotent: only add if the dir is not already a PATH segment.
  Result := Pos(';' + Uppercase(Param) + ';', ';' + Uppercase(OrigPath) + ';') = 0;
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Cur, Seg: string;
  P: Integer;
begin
  // Strip the CLI directory we may have appended to the machine PATH. Without
  // this an "add to PATH" install would leave a dead segment behind forever.
  // Match the segment delimiter-anchored (';<dir>;' inside ';<PATH>;'), exactly
  // as NeedsAddPath does on the way in, so a sibling like '...\cli2' can never
  // be partially matched and a correct PATH is never mangled.
  if CurUninstallStep <> usUninstall then
    exit;
  if not RegQueryStringValue(HKLM, EnvKey, 'Path', Cur) then
    exit;
  Seg := ExpandConstant('{app}\cli');
  // Position of ';<dir>;' within the synthetic ';<PATH>;' (1 = at the very
  // start; >1 = a real separator precedes it).
  P := Pos(';' + Uppercase(Seg) + ';', ';' + Uppercase(Cur) + ';');
  if P = 0 then
    exit;
  if P = 1 then
    Delete(Cur, 1, Length(Seg) + 1)       // "<dir>;..." -> drop the leading entry + its separator
  else
    Delete(Cur, P - 1, Length(Seg) + 1);  // "...;<dir>..." -> drop the separator + entry
  RegWriteExpandStringValue(HKLM, EnvKey, 'Path', Cur);
end;
