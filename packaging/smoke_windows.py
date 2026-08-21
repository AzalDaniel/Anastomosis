"""Smoke-test the BUILT Windows installer the way a user meets it.

Run on a Windows runner after ``iscc`` produces ``dist\\installer\\*.exe``. The
build already proves the FROZEN bundles are whole (``anast doctor`` +
``Anastomosis.exe --self-check``); this proves the SHIPPED ARTIFACT is:

1. **install** — run the installer silently, exactly as a scripted rollout would
   (``/VERYSILENT /SUPPRESSMSGBOXES /NORESTART /SP-``), bounded so an elevation
   prompt fails the build instead of hanging it;
2. **layout + doctor** — the GUI and CLI exes landed where ``anastomosis.iss``
   says, and the INSTALLED ``anast doctor`` passes (every data asset and the
   bundled Chromium survived the install);
3. **the real window** — launch the installed GUI with WebView2's remote
   debugging port open, attach Playwright over CDP, and assert the dashboard
   actually RENDERED inside the shipped WebView2 runtime. The DOM expectations
   come from ``tests/gui_e2e/expectations.py`` — the same module the Linux GUI
   lane asserts on every push, so a selector cannot rot here without going red
   there first. The app's stdout/stderr are captured to files and printed on
   any failure of this step: a frozen GUI that dies on startup leaves no other
   evidence;
4. **uninstall** — run the RECORDED uninstaller silently and prove the app
   directory and the Start-menu group are gone (an installer that cannot
   cleanly remove itself is a defect users pay for later).

Every step prints ``PASS``/``FAIL``; any FAIL exits non-zero. Stdlib only, plus
Playwright (already installed in the packaging job for the render extra).
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover - typing only
    from playwright.sync_api import Browser, Page

_ROOT = Path(__file__).resolve().parent.parent

#: Where the Inno Setup build writes the installer (anastomosis.iss OutputDir).
_INSTALLER_DIR = _ROOT / "dist" / "installer"
_INSTALLER_GLOB = "Anastomosis-Setup-*.exe"

#: The install layout anastomosis.iss lays down under {autopf}\Anastomosis.
_APP_SUBDIRS = {"cli": "anast.exe", "gui": "Anastomosis.exe"}

#: The Start-menu group ([Setup] DefaultGroupName) — an admin install puts it
#: under the COMMON programs folder.
_START_MENU_GROUP = "Anastomosis"

#: The DOM expectations shared with the Linux GUI lane (tests/gui_e2e).
_EXPECTATIONS = _ROOT / "tests" / "gui_e2e" / "expectations.py"

#: WebView2 reads its extra browser switches from this environment variable.
_WEBVIEW2_ARGS_ENV = "WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"
_CDP_PORT = 9222
_CDP_URL = f"http://127.0.0.1:{_CDP_PORT}"

#: Where the launched GUI's streams are captured (under the temp dir install()
#: resolves). Never DEVNULL: a frozen app that dies on startup says why exactly
#: once, on stderr, and nobody is watching the window on a CI runner.
_GUI_STDOUT_LOG = "anastomosis-gui-stdout.log"
_GUI_STDERR_LOG = "anastomosis-gui-stderr.log"

#: The per-user folder anastomosis.gui.shell points WebView2 at, relative to
#: %LOCALAPPDATA% — searched for logs alongside the two temp roots.
_APP_LOCAL_SUBDIR = "Anastomosis"
#: Both case spellings are globbed rather than trusting the filesystem's rules.
_WEBVIEW2_LOG_GLOBS = ("*webview2*", "*WebView2*")
#: Files whose CONTENTS are printed; anything else is listed by name and size.
_TEXT_LOG_SUFFIXES = frozenset({".log", ".txt"})
#: Bounds on the failure dump, so one huge log cannot bury the rest of it.
_MAX_LOG_MATCHES = 8
_MAX_DUMP_CHARS = 20_000

# Time budgets (seconds). The installer bound matches the workflow step this
# script replaces; the GUI bound is the hard kill for a window that never paints.
_INSTALL_TIMEOUT = 300
_UNINSTALL_TIMEOUT = 300
_DOCTOR_TIMEOUT = 300
_GUI_TIMEOUT = 120
#: How long the dashboard gets to finish its bridge round-trip once attached.
_DASHBOARD_TIMEOUT_MS = 30_000


class SmokeFailure(Exception):
    """A step failed. Carries the operator-facing reason, never a traceback."""


_failures: list[str] = []


@contextmanager
def step(name: str) -> Iterator[None]:
    """Run one smoke step, printing PASS/FAIL and recording any failure."""
    print(f"--- {name}")
    try:
        yield
    except SmokeFailure as exc:
        print(f"FAIL {name}: {exc}")
        _failures.append(name)
    except Exception as exc:  # defensive: an unexpected error is still a FAIL
        print(f"FAIL {name}: unexpected {type(exc).__name__}: {exc}")
        _failures.append(name)
    else:
        print(f"PASS {name}")


def _system32(tool: str) -> str:
    """An absolute path to a Windows system tool (never a bare name on PATH)."""
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows"), "System32", tool))


def _run(cmd: list[str], *, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a command by absolute path, capturing output; never shell=True."""
    return subprocess.run(  # noqa: S603 - argv list, absolute exe path, no shell
        cmd,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _program_files() -> Path:
    return Path(os.environ.get("ProgramFiles", r"C:\Program Files"))


def _app_dir() -> Path:
    return _program_files() / "Anastomosis"


def _temp_dir() -> Path:
    """The runner's temp directory: RUNNER_TEMP, else TEMP, else the cwd.

    One resolution shared by every step that writes a diagnostic file, so the
    installer log and the captured GUI streams always land together.
    """
    return Path(os.environ.get("RUNNER_TEMP", os.environ.get("TEMP", ".")))


def _start_menu_group() -> Path:
    program_data = Path(os.environ.get("ProgramData", r"C:\ProgramData"))
    return program_data / "Microsoft" / "Windows" / "Start Menu" / "Programs" / _START_MENU_GROUP


def _locate_installer(explicit: str | None) -> Path:
    """The single built installer, or a loud failure."""
    if explicit:
        path = Path(explicit)
        if not path.is_file():
            raise SmokeFailure(f"no installer at {path}")
        return path
    candidates = sorted(_INSTALLER_DIR.glob(_INSTALLER_GLOB))
    if not candidates:
        raise SmokeFailure(f"no {_INSTALLER_GLOB} under {_INSTALLER_DIR}")
    if len(candidates) > 1:
        raise SmokeFailure(f"expected one installer, found {[p.name for p in candidates]}")
    return candidates[0]


def _load_expectations() -> ModuleType:
    """Load the shared DOM expectations module by path (no package install).

    Deliberately the SAME file ``tests/gui_e2e`` imports: one definition of what
    a live dashboard looks like, asserted by both lanes.
    """
    if not _EXPECTATIONS.is_file():
        raise SmokeFailure(
            f"the shared GUI expectations module is missing: {_EXPECTATIONS}. "
            "Lane 1 (tests/gui_e2e) and this smoke test must share it."
        )
    spec = importlib.util.spec_from_file_location("anastomosis_gui_expectations", _EXPECTATIONS)
    if spec is None or spec.loader is None:  # pragma: no cover - import machinery
        raise SmokeFailure(f"could not load {_EXPECTATIONS}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- 1. silent install ------------------------------------------------------


def install(installer: Path) -> None:
    """Install silently, the way a scripted rollout would."""
    log = _temp_dir() / "anastomosis-install.log"
    print(f"installing {installer.name} silently (log: {log})")
    try:
        result = _run(
            [
                str(installer),
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                "/SP-",
                f"/LOG={log}",
            ],
            timeout=_INSTALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _dump(log)
        raise SmokeFailure(
            f"the installer did not finish within {_INSTALL_TIMEOUT}s "
            "(an elevation prompt may be blocking it)"
        ) from None
    if result.returncode != 0:
        _dump(log)
        raise SmokeFailure(f"the installer exited with code {result.returncode}")


def _dump(log: Path) -> None:
    if log.is_file():
        print(f"----- {log} -----")
        print(log.read_text(encoding="utf-8", errors="replace"))
        print("----- end of installer log -----")


# --- 2. the installed layout + the installed doctor -------------------------


def check_layout() -> None:
    """Every exe anastomosis.iss promises is where it promised to put it."""
    app = _app_dir()
    missing = [
        str(app / subdir / exe)
        for subdir, exe in sorted(_APP_SUBDIRS.items())
        if not (app / subdir / exe).is_file()
    ]
    if missing:
        raise SmokeFailure(f"the installed layout is missing: {missing}")
    for subdir, exe in sorted(_APP_SUBDIRS.items()):
        print(f"present: {app / subdir / exe}")
    group = _start_menu_group()
    if not group.is_dir():
        raise SmokeFailure(f"no Start-menu group at {group}")
    shortcuts = sorted(p.name for p in group.glob("*.lnk"))
    if not shortcuts:
        raise SmokeFailure(f"the Start-menu group {group} has no shortcuts")
    print(f"start menu: {group} -> {shortcuts}")


def check_installed_doctor() -> None:
    """The INSTALLED CLI self-check: assets + Chromium survived the install."""
    cli = _app_dir() / "cli" / "anast.exe"
    result = _run([str(cli), "doctor"], timeout=_DOCTOR_TIMEOUT)
    print(result.stdout.strip())
    if result.stderr.strip():
        print(result.stderr.strip(), file=sys.stderr)
    if result.returncode != 0:
        raise SmokeFailure(f"the installed 'anast doctor' failed (exit {result.returncode})")


# --- 3. the real WebView2 window --------------------------------------------


def check_gui_window() -> None:
    """Launch the installed GUI and assert the dashboard rendered for real.

    Invariant: the launched process's stdout and stderr are CAPTURED to files,
    never discarded. A frozen Windows app that dies on startup says why exactly
    once — on stderr, before it exits — and nobody is watching the window on a
    CI runner, so DEVNULL here would throw away the only diagnostic the failure
    produces. On ANY failure of this step both streams are printed, together
    with any WebView2 log the launch left behind; the pass path stays quiet.
    """
    expectations = _load_expectations()
    gui_exe = _app_dir() / "gui" / "Anastomosis.exe"
    env = dict(os.environ)
    # WebView2 forwards these switches to its Chromium; this is the ONLY way to
    # get a debugging port out of an embedded WebView2 window.
    env[_WEBVIEW2_ARGS_ENV] = f"--remote-debugging-port={_CDP_PORT}"
    temp = _temp_dir()
    stdout_log = temp / _GUI_STDOUT_LOG
    stderr_log = temp / _GUI_STDERR_LOG
    print(f"launching {gui_exe} with {_WEBVIEW2_ARGS_ENV}={env[_WEBVIEW2_ARGS_ENV]}")
    print(f"capturing GUI output to {stdout_log} and {stderr_log}")
    try:
        # The `with` closes (and flushes) both captures on the way out — before
        # the failure path below reads them back.
        with stdout_log.open("wb") as out_stream, stderr_log.open("wb") as err_stream:
            process = subprocess.Popen(  # noqa: S603 - absolute exe path, argv list, no shell
                [str(gui_exe)],
                env=env,
                stdout=out_stream,
                stderr=err_stream,
            )
            try:
                _await_cdp(process)
                _assert_dashboard_rendered(expectations)
            finally:
                _kill_tree(process)
    except Exception:
        _dump_gui_diagnostics(stdout_log, stderr_log)
        raise


def _assert_dashboard_rendered(expectations: ModuleType) -> None:
    """Attach to the live WebView2 over CDP and check the shared DOM expectations."""
    from playwright.sync_api import sync_playwright

    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(_CDP_URL)
        try:
            page = _dashboard_page(browser)
            page.wait_for_function(
                "() => { const v = document.querySelector('#version');"
                " return v && v.textContent.trim() && v.textContent.trim() !== '—'; }",
                timeout=_DASHBOARD_TIMEOUT_MS,
            )
            problems = expectations.check_dashboard(page)
            for problem in problems:
                print(f"  dashboard: {problem}")
            if problems:
                raise SmokeFailure(f"{len(problems)} dashboard expectation(s) failed")
            print(f"dashboard rendered in the shipped WebView2 window ({page.url})")
        finally:
            browser.close()


def _dump_gui_diagnostics(stdout_log: Path, stderr_log: Path) -> None:
    """Print what the failed GUI launch left behind: both streams, then any log.

    Only ever called from the failure path, and only once the capture files are
    closed. Defensive by design: a diagnostic dump must never replace the real
    failure with an error of its own, so an unreadable file degrades to one
    type-named line.
    """
    try:
        _dump_section("gui stdout", stdout_log)
        _dump_section("gui stderr", stderr_log)
        for log in _webview2_logs():
            if log.suffix.lower() in _TEXT_LOG_SUFFIXES:
                _dump_section(f"webview2 log {log.name}", log)
            else:
                print(f"----- webview2 artifact: {log} ({log.stat().st_size} bytes) -----")
    except OSError as exc:
        print(f"warning: the GUI diagnostics could not be read ({type(exc).__name__})")


def _dump_section(label: str, path: Path) -> None:
    """Print one captured file between markers, bounded to its tail.

    An absent or empty file says so: "the app printed nothing before exiting"
    is itself a finding, and silently printing nothing would hide it.
    """
    print(f"----- {label}: {path} -----")
    if not path.is_file():
        print("(absent)")
    else:
        text = path.read_text(encoding="utf-8", errors="replace")
        if not text.strip():
            print("(empty)")
        elif len(text) > _MAX_DUMP_CHARS:
            print(f"(truncated: the last {_MAX_DUMP_CHARS} of {len(text)} characters)")
            print(text[-_MAX_DUMP_CHARS:])
        else:
            print(text)
    print(f"----- end of {label} -----")


def _webview2_log_roots() -> list[Path]:
    """The directories searched for WebView2/pywebview logs."""
    roots = [Path(os.environ[name]) for name in ("TEMP", "LOCALAPPDATA") if os.environ.get(name)]
    local = os.environ.get("LOCALAPPDATA")
    if local:
        roots.append(Path(local, _APP_LOCAL_SUBDIR))
    return roots


def _webview2_logs() -> list[Path]:
    """Likely pywebview/WebView2 logs, found by a deliberately shallow glob.

    Bounded on purpose: one glob per root for a matching entry, plus the
    ``*.log`` files directly inside a matching DIRECTORY. Walking
    %LOCALAPPDATA% recursively on a runner would cost minutes and bury the
    traceback this dump exists to surface.
    """
    found: list[Path] = []
    for root in _webview2_log_roots():
        for pattern in _WEBVIEW2_LOG_GLOBS:
            for entry in sorted(root.glob(pattern))[:_MAX_LOG_MATCHES]:
                if entry.is_dir():
                    found.extend(sorted(entry.glob("*.log"))[:_MAX_LOG_MATCHES])
                elif entry.is_file():
                    found.append(entry)
    # The two globs overlap on a case-insensitive filesystem; dedupe in order.
    return list(dict.fromkeys(found))[:_MAX_LOG_MATCHES]


def _await_cdp(process: subprocess.Popen[bytes]) -> None:
    """Poll the WebView2 debugging endpoint until it answers, or give up loudly."""
    deadline = time.monotonic() + _GUI_TIMEOUT
    last_error = "never answered"
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise SmokeFailure(f"the GUI exited immediately (code {process.returncode})")
        try:
            # A fixed loopback http URL, built from module constants.
            with urllib.request.urlopen(f"{_CDP_URL}/json/version", timeout=2) as response:  # noqa: S310
                body = response.read().decode("utf-8", "replace")
            print(f"webview2 debugging endpoint is up: {body.strip()[:160]}")
            return
        except (urllib.error.URLError, OSError) as exc:
            last_error = type(exc).__name__
        time.sleep(1)
    raise SmokeFailure(
        f"{_CDP_URL}/json/version did not come up within {_GUI_TIMEOUT}s ({last_error}) — "
        "the window never opened, or WebView2 ignored the debugging switch"
    )


def _dashboard_page(browser: Browser) -> Page:
    """The WebView2 page showing index.html (the window's only document)."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        pages = [page for context in browser.contexts for page in context.pages]
        for page in pages:
            if page.url.endswith("index.html"):
                return page
        if pages:
            return pages[0]
        time.sleep(1)
    raise SmokeFailure("the attached WebView2 exposed no page")


def _kill_tree(process: subprocess.Popen[bytes]) -> None:
    """Close the app: kill the process AND the WebView2 children it spawned."""
    if process.poll() is not None:
        return
    result = _run(
        [_system32("taskkill.exe"), "/PID", str(process.pid), "/T", "/F"],
        timeout=60,
    )
    if result.returncode != 0:
        process.kill()
    try:
        process.wait(timeout=30)
    except subprocess.TimeoutExpired:  # pragma: no cover - the OS refused
        print("warning: the GUI process did not exit after taskkill", file=sys.stderr)


# --- 4. silent uninstall ----------------------------------------------------


def _uninstaller() -> Path:
    """The uninstaller the install RECORDED (registry first, then the app dir).

    Inno registers the real command under the AppId's ``UninstallString``; the
    file itself is ``{app}\\unins000.exe``. Preferring the registry is what an
    "uninstall this app" flow actually does.
    """
    import winreg

    key_path = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall"
        r"\{EEC2F7C9-06AD-4BC2-91D4-84BBAE937B98}_is1"
    )
    for view in (winreg.KEY_WOW64_64KEY, winreg.KEY_WOW64_32KEY):
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, key_path, 0, winreg.KEY_READ | view
            ) as key:
                value, _kind = winreg.QueryValueEx(key, "UninstallString")
        except OSError:
            continue
        recorded = Path(str(value).strip('"'))
        if recorded.is_file():
            return recorded
    fallback = _app_dir() / "unins000.exe"
    if fallback.is_file():
        return fallback
    raise SmokeFailure("no recorded uninstaller (registry UninstallString and unins000.exe absent)")


def uninstall() -> None:
    """Uninstall silently and prove the machine is clean afterwards."""
    uninstaller = _uninstaller()
    print(f"uninstalling with {uninstaller}")
    try:
        result = _run(
            [str(uninstaller), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART"],
            timeout=_UNINSTALL_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        raise SmokeFailure(f"the uninstaller did not finish within {_UNINSTALL_TIMEOUT}s") from None
    if result.returncode != 0:
        raise SmokeFailure(f"the uninstaller exited with code {result.returncode}")

    # Inno's uninstaller returns as soon as it spawns its own copy in %TEMP%;
    # give the removal a bounded moment to actually finish.
    app = _app_dir()
    group = _start_menu_group()
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline and (_has_payload(app) or group.is_dir()):
        time.sleep(2)

    leftovers: list[str] = []
    if _has_payload(app):
        leftovers.append(f"{app} still holds installed files")
    if group.is_dir():
        leftovers.append(f"the Start-menu group {group} survived")
    if leftovers:
        raise SmokeFailure("; ".join(leftovers))
    print(f"removed: {app}")
    print(f"removed: {group}")


def _has_payload(app: Path) -> bool:
    """True while any INSTALLED file of ANY kind remains under the app directory.

    The uninstaller may legitimately leave the (empty) directory tree behind on
    a locked filesystem; what must not survive is the payload — and the payload
    is mostly NOT exes: the DLLs, fonts, bundled Chromium data, and logs the app
    wrote all count, so this asks for files, not for a suffix.
    """
    if not app.is_dir():
        return False
    return any(p.is_file() for p in app.rglob("*"))


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if sys.platform != "win32":
        print("FAIL smoke: this smoke test drives a Windows installer; run it on Windows")
        return 1

    installer: Path | None = None
    with step("locate the built installer"):
        installer = _locate_installer(args[0] if args else None)
        print(f"installer: {installer}")

    if installer is not None and not _failures:
        with step("silent install"):
            install(installer)

    if not _failures:
        with step("installed layout"):
            check_layout()
        with step("installed 'anast doctor'"):
            check_installed_doctor()
        with step("the dashboard renders in the shipped WebView2 window"):
            check_gui_window()
        # The uninstall runs even if the GUI step failed: leaving the runner
        # dirty would poison a re-run, and a broken uninstall is its own finding.
        with step("silent uninstall"):
            uninstall()

    if _failures:
        print(f"\nSMOKE FAILED: {len(_failures)} step(s): {_failures}")
        return 1
    print("\nSMOKE PASSED: install -> layout -> doctor -> GUI window -> uninstall")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
