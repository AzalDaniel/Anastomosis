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
AppId={{EEC2F7C9-06AD-4BC2-91D4-84BBAE937B98}}
AppName=Anastomosis
AppVersion={#AppVersion}
AppPublisher=Azal Daniel
AppPublisherURL=https://github.com/AzalDaniel/Anastomosis
AppSupportURL=https://github.com/AzalDaniel/Anastomosis/issues
AppUpdatesURL=https://github.com/AzalDaniel/Anastomosis/releases
AppCopyright=(c) 2026 Azal Daniel. AGPL-3.0-or-later.
DefaultDirName={autopf}\Anastomosis
DefaultGroupName=Anastomosis
UninstallDisplayName=Anastomosis
; The GUI exe supplies the Add/Remove Programs icon (installed under {app}\gui);
; the exe itself carries the multi-res mark via Nuitka --windows-icon-from-ico.
UninstallDisplayIcon={app}\gui\Anastomosis.exe
; Setup wizard branding — all three renditions derive from the one SVG
; master (assets/icon/icon.svg) via tools/make_icons.py.
SetupIconFile=assets\icon\icon.ico
WizardImageFile=assets\installer\wizard.bmp
WizardSmallImageFile=assets\installer\wizard-small.bmp
; The wizard chrome runs the native dark style (Inno >= 6.6): the same warm
; near-black ground the wizard bitmaps and the app itself use, so the install
; experience is one surface with the product, not a beige dialog handing off
; to a dark app. windows11 is the built-in custom style closest to the GUI's
; rounded, flat-control language. Colors are BGR ($BBGGRR): $10 13 17 is the
; brand ground #171310.
WizardStyle=modern dark windows11
WizardBackColor=$101317
WizardImageBackColor=$101317
; Repo-root LICENSE, resolved via SourceDir (=..) at compile time; shown on the
; wizard's license page.
LicenseFile=LICENSE
DisableProgramGroupPage=yes
; Upgrades keep the existing directory without asking again — re-running the
; installer over an install is the normal update path, and re-prompting for a
; directory it must not change is a trap.
DisableDirPage=auto
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
; Desktop shortcut, checked by default — this is a consumer app, so the standard
; "create a desktop icon" opt-out (no `unchecked` flag) matches user expectation.
Name: "desktopicon"; Description: "{cm:CreateDesktopIcon}"; \
  GroupDescription: "{cm:AdditionalIcons}"
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
; Third-party license texts the installed app redistributes (the HL7 CDA
; stylesheet is Apache-2.0; the two GUI fonts are OFL-1.1) plus the inventory
; that maps each asset to its license. The wheel ships the same set under
; dist-info/licenses/; the installer's copy lives beside the app.
Source: "THIRD_PARTY_LICENSES.md"; DestDir: "{app}\licenses"; Flags: ignoreversion
Source: "assets\licenses\*"; DestDir: "{app}\licenses"; Flags: ignoreversion

[Icons]
; AppUserModelID matches the id the GUI sets at startup (gui/shell.py), so
; taskbar pinning/grouping resolves to one stable identity.
Name: "{group}\Anastomosis"; Filename: "{app}\gui\Anastomosis.exe"; AppUserModelID: "AzalDaniel.Anastomosis"
Name: "{group}\Uninstall Anastomosis"; Filename: "{uninstallexe}"
Name: "{autodesktop}\Anastomosis"; Filename: "{app}\gui\Anastomosis.exe"; Tasks: desktopicon; AppUserModelID: "AzalDaniel.Anastomosis"

[Registry]
; Append the CLI directory to the MACHINE PATH (idempotent; only if the task is
; checked). This is a per-machine install (elevated), so the machine PATH — not
; HKCU — is the correct target: under elevation HKCU is the elevating admin's
; hive, not the user who will run `anast`. The NeedsAddPath check avoids a
; duplicate segment; CurUninstallStepChanged strips it on uninstall.
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
  ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\cli"; \
  Tasks: addtopath; Check: NeedsAddPath('{app}\cli')
; Installer-owned marker: written under the SAME task + Check conditions as the
; PATH append above, so it exists IFF this installer actually added the segment
; (not when the task was unchecked, nor when the dir was already on PATH).
; CurUninstallStepChanged consults it and strips the segment only when we own it,
; so a pre-existing/manual entry is never corrupted. uninsdeletevalue removes the
; marker on uninstall; uninsdeletekeyifempty tidies the empty key afterwards.
Root: HKLM; Subkey: "Software\Anastomosis"; ValueType: dword; \
  ValueName: "PathAdded"; ValueData: 1; \
  Tasks: addtopath; Check: NeedsAddPath('{app}\cli'); \
  Flags: uninsdeletevalue uninsdeletekeyifempty

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
  Marker: Cardinal;
begin
  // Strip the CLI directory we may have appended to the machine PATH. Without
  // this an "add to PATH" install would leave a dead segment behind forever.
  // Match the segment delimiter-anchored (';<dir>;' inside ';<PATH>;'), exactly
  // as NeedsAddPath does on the way in, so a sibling like '...\cli2' can never
  // be partially matched and a correct PATH is never mangled.
  if CurUninstallStep <> usUninstall then
    exit;
  // Gate on the installer-owned marker ([Registry]): only strip the segment if
  // WE actually added it. If the marker is absent (task unchecked, or the dir
  // already pre-existed so NeedsAddPath returned False and we never appended),
  // leave PATH untouched — the segment belongs to the user, not to us.
  if not RegQueryDWordValue(HKLM, 'Software\Anastomosis', 'PathAdded', Marker) then
    exit;
  if Marker <> 1 then
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
