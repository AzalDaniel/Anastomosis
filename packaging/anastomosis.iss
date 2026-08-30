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
; hive, not the user who will run `anast`. NeedsAddPath expands the constant it
; is handed before comparing — Inno does not expand constants in a Check
; parameter, and the literal '{app}\cli' matched no PATH on earth, so every
; upgrade appended the directory again; CurStepChanged collapses what earlier
; builds already duplicated and CurUninstallStepChanged strips every occurrence
; on uninstall.
Root: HKLM; Subkey: "SYSTEM\CurrentControlSet\Control\Session Manager\Environment"; \
  ValueType: expandsz; ValueName: "Path"; ValueData: "{olddata};{app}\cli"; \
  Tasks: addtopath; Check: NeedsAddPath('{app}\cli')
; Installer-owned marker: written under the SAME task + Check conditions as the
; PATH append above, so it exists IFF this installer actually added the segment
; (not when the task was unchecked, nor when the dir was already on PATH).
; That "SAME Check" is load-bearing and is NOT the same as "asks the same
; question twice": Inno evaluates a Check immediately before processing its own
; entry, in order, so by the time this one is reached the entry above has
; already appended the directory. NeedsAddPath therefore answers once per run
; and hands both entries that one answer — see the memo in [Code]. A second live
; lookup here reads "already on PATH", skips the marker, and leaves an installer
; that added a segment it can never prove it owns.
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
// CurStepChanged collapses anything an earlier build duplicated, and
// CurUninstallStepChanged strips it back out. The ownership marker is written
// under the same conditions as the append, so all three agree on which segment
// is ours to touch.
const
  EnvKey = 'SYSTEM\CurrentControlSet\Control\Session Manager\Environment';
  OwnerKey = 'Software\Anastomosis';
  OwnerValue = 'PathAdded';

var
  // The add-to-PATH decision, made once per run and shared by both [Registry]
  // entries. NeedsAddPath owns them and explains why they exist.
  PathAddDecided: Boolean;
  PathAddNeeded: Boolean;

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

// --- the one PATH segment this installer owns -------------------------------

function CliDir(): string;
begin
  // The directory the [Registry] entry appends, expanded — the one segment this
  // installer may edit. NeedsAddPath is handed the same constant as a Check
  // parameter instead, so the .iss keeps saying out loud, on the entry itself,
  // which directory that entry means.
  Result := ExpandConstant('{app}\cli');
end;

// Whether the CLI directory is already reachable from PATH.
//
// Trailing-slash policy, one half of it: a PATH directory keeps its identity
// across a trailing backslash — 'X\cli' and 'X\cli\' send `anast` to the same
// place — so a user who already spelled it the second way must not be given a
// second segment. The other half is in RewriteOwnedSegments, which is spelling-
// exact: we decline to ADD generously and only ever REMOVE what we wrote.
function PathHasDir(Value, Dir: string): Boolean;
var
  Bounded, Bare: string;
begin
  Bounded := ';' + Uppercase(Value) + ';';
  Bare := Uppercase(Dir);
  while (Length(Bare) > 1) and (Bare[Length(Bare)] = '\') do
    Delete(Bare, Length(Bare), 1);
  // Anchored on the ';' delimiters. An unanchored Pos would find this directory
  // inside a sibling like '...\cli2' or inside '...\cli\bin' and conclude PATH
  // already had it, so the CLI would silently never be on PATH at all.
  Result := (Pos(';' + Bare + ';', Bounded) > 0) or
            (Pos(';' + Bare + '\;', Bounded) > 0);
end;

// Rebuild Value keeping at most Keep segments spelled exactly like Dir, and
// report through Found how many it held.
//
// Everything we do not own — empty segments, siblings, a user's own lookalike,
// the separators around all of them — is copied through byte for byte. This is
// the machine PATH: every other program on the box depends on the parts of it
// that are not ours, and an installer that rewrites those has done far more
// damage than the duplicate it came to fix.
function RewriteOwnedSegments(Value, Dir: string; Keep: Integer;
  var Found: Integer): string;
var
  Rest, Seg, Wanted: string;
  P, Kept: Integer;
  First, Done, Take: Boolean;
begin
  Result := '';
  Found := 0;
  Kept := 0;
  First := True;
  Done := False;
  Rest := Value;
  Wanted := Uppercase(Dir);
  while not Done do
  begin
    P := Pos(';', Rest);
    if P = 0 then
    begin
      Seg := Rest;
      Done := True;
    end
    else
    begin
      Seg := Copy(Rest, 1, P - 1);
      Rest := Copy(Rest, P + 1, Length(Rest) - P);
    end;
    Take := True;
    if Uppercase(Seg) = Wanted then
    begin
      Found := Found + 1;
      // Past the quota: drop this occurrence, and with it the separator that
      // would have followed. Counting runs to the END of the value rather than
      // stopping at the first match — stopping is what left a dead segment
      // behind on a machine the duplicate bug had already touched.
      Take := Kept < Keep;
      if Take then
        Kept := Kept + 1;
    end;
    if Take then
    begin
      if First then
      begin
        Result := Seg;
        First := False;
      end
      else
        Result := Result + ';' + Seg;
    end;
  end;
end;

function InstallerOwnsPathSegment(): Boolean;
var
  Marker: Cardinal;
begin
  // The marker is written under the SAME task + Check conditions as the append
  // ([Registry]), so it exists IFF this installer put the segment there. Absent
  // means the segment is the user's: theirs to keep, never ours to collapse or
  // strip.
  Result := False;
  if not RegQueryDWordValue(HKLM, OwnerKey, OwnerValue, Marker) then
    exit;
  Result := Marker = 1;
end;

function NeedsAddPath(Param: string): Boolean;
var
  OrigPath: string;
begin
  // Two things to get right here, and the first fix for #281 got only one.
  //
  // Inno does NOT expand constants in a Check function's string parameter, so
  // this expands it itself. Comparing the literal '{app}\cli' against a PATH
  // that holds the expanded directory never matches, which is why the check
  // said "not there yet" every single time and every upgrade appended again.
  //
  // And the answer is computed ONCE per run, then handed to every caller. Both
  // [Registry] entries hang off this Check, and Inno evaluates a Check
  // immediately before processing its own entry, in order — so the marker
  // entry is reached only after the PATH entry above it has already appended
  // the directory. Looking the PATH up a second time answers "already there",
  // and the marker that both repair paths gate on is silently never written:
  // no collapse, and an uninstall that walks away leaving the directory on
  // PATH forever. Expanding the constant is what exposed that, because before
  // it the comparison could not match and this always returned True.
  if not PathAddDecided then
  begin
    PathAddDecided := True;
    if RegQueryStringValue(HKLM, EnvKey, 'Path', OrigPath) then
      PathAddNeeded := not PathHasDir(OrigPath, ExpandConstant(Param))
    else
      PathAddNeeded := True;
  end;
  Result := PathAddNeeded;
end;

procedure CurStepChanged(CurStep: TSetupStep);
var
  Cur, Fixed: string;
  Found: Integer;
begin
  // Repair a machine an earlier build already duplicated, before the [Registry]
  // entries are processed: ssInstall runs first, so the Check above then sees
  // the collapsed value and appends nothing. That ordering is what makes the
  // whole cycle idempotent — however many times this installer has run, and
  // however many times it runs again, exactly one owned segment survives.
  if CurStep <> ssInstall then
    exit;
  if not InstallerOwnsPathSegment() then
    exit;
  if not RegQueryStringValue(HKLM, EnvKey, 'Path', Cur) then
    exit;
  Fixed := RewriteOwnedSegments(Cur, CliDir(), 1, Found);
  // Only when there is something to collapse: ChangesEnvironment makes every
  // window on the machine handle a settings broadcast, and a no-op rewrite of
  // the machine PATH is a risk taken for nothing.
  if Found > 1 then
    RegWriteExpandStringValue(HKLM, EnvKey, 'Path', Fixed);
end;

procedure CurUninstallStepChanged(CurUninstallStep: TUninstallStep);
var
  Cur, Stripped: string;
  Found: Integer;
begin
  // Strip EVERY segment we appended. Without this an "add to PATH" install
  // leaves a dead directory on PATH forever; removing only the first match,
  // which is what this used to do, still left one behind on any machine that
  // had taken a second copy from the duplicate bug.
  if CurUninstallStep <> usUninstall then
    exit;
  if not InstallerOwnsPathSegment() then
    exit;
  if not RegQueryStringValue(HKLM, EnvKey, 'Path', Cur) then
    exit;
  Stripped := RewriteOwnedSegments(Cur, CliDir(), 0, Found);
  if Found > 0 then
    RegWriteExpandStringValue(HKLM, EnvKey, 'Path', Stripped);
end;
