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
   cleanly remove itself is a defect users pay for later);
5. **the machine PATH** — a whole second cycle (install with the PATH task
   off, the same add-to-PATH upgrade twice, an upgrade and an uninstall over a
   PATH put back into the state an earlier build left it in) counting the
   installer-owned segments in the REGISTRY PATH after each step. Nothing above
   can see this defect: the app installs, runs and uninstalls perfectly while
   leaving one more copy of the CLI directory on the machine PATH per upgrade
   and a dead one behind at the end (#281). A user-owned lookalike segment is
   seeded before the cycle and must come back out of it byte for byte.

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
from collections.abc import Iterator, Sequence
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

#: The machine PATH the optional add-to-PATH task appends to — the same key
#: anastomosis.iss writes (its EnvKey). Always read from the REGISTRY: a
#: registry write never reaches an already-running process, so os.environ here
#: would report the PATH this job inherited, not the one the installer left.
_ENV_KEY = r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"
_ENV_PATH_VALUE = "Path"

#: The directory the task puts on PATH ({app}\cli in anastomosis.iss).
_CLI_SUBDIR = "cli"

#: The installer-owned marker anastomosis.iss writes ([Registry]) and both of
#: its repair paths gate on. Read in BOTH registry views, and reported beside
#: every measurement: a step gated on this marker does NOTHING when it is
#: missing, and "was the marker there, and in which view" is the first question
#: a failure of those steps asks. It went unanswered once already.
_MARKER_KEY = r"Software\Anastomosis"
_MARKER_VALUE = "PathAdded"

#: One installer log per step of the PATH cycle, kept apart so no run can
#: overwrite the evidence of the run before it.
_PATH_LOG_NOPATH = "anastomosis-install-nopath.log"
_PATH_LOG_UPGRADE = "anastomosis-install-upgrade-{attempt}.log"
_PATH_LOG_REPAIR = "anastomosis-install-repair.log"
_PATH_LOG_UNINSTALL = "anastomosis-uninstall-repair.log"

#: How much of a log a failure carries with it. Bounded because the point is a
#: diagnosis a reader can take in, not the whole install transcript.
_LOG_TAIL_LINES = 40

#: The DOM expectations shared with the Linux GUI lane (tests/gui_e2e).
_EXPECTATIONS = _ROOT / "tests" / "gui_e2e" / "expectations.py"

#: The GUI's own opt-in for the debugging port (anastomosis.gui.shell). It is
#: the ONLY route that works here: pywebview always sets WebView2's
#: AdditionalBrowserArguments itself, and WebView2 ignores the environment
#: variable below whenever the host app supplies that property. The shell turns
#: this into pywebview's REMOTE_DEBUGGING_PORT setting, which pywebview appends
#: to the arguments it is already passing.
_GUI_DEBUG_PORT_ENV = "ANAST_GUI_REMOTE_DEBUGGING_PORT"
#: WebView2's documented switch variable — set alongside for a non-pywebview
#: host (it costs nothing), but never relied on: see above for why it is inert
#: for THIS app.
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

# Coming-alive budgets, one per thing being waited for (milliseconds). One
# 30-second budget used to cover all three, and it failed a release gate on a
# slow runner with no diff to blame: the same commit passed on re-run, and the
# failing runner was ~43s slower before it gave up (#270). A gate that can go
# red without a change causing it costs a cycle of trust every time, and this
# is the only step that installs the real installer and drives the real
# WebView2 window.
#
# Three budgets rather than one, because the failure message is the point: a
# timeout now says WHICH of the three never happened, and the log prints how
# long each took even when they pass — so the margin shrinking is visible while
# it is still fine, rather than discovered when it runs out.
#
# Sized for a cold VM moments after installing 1.67 GB across 2585 files, not
# for a warm one. Raising a budget does not weaken what is asserted; it only
# changes how long the step is willing to wait for it, and every assertion
# below is unchanged.
#: The page appearing in the attached WebView2 at all.
_PAGE_TIMEOUT_MS = 60_000
#: The bridge reporting live — pywebview + WebView2 cold start, the expensive one.
_BRIDGE_TIMEOUT_MS = 90_000
#: The first info() round-trip, once the bridge is already live.
_INFO_TIMEOUT_MS = 30_000


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


def install(
    installer: Path,
    *,
    merge_tasks: str | None = None,
    log_name: str = "anastomosis-install.log",
) -> None:
    """Install silently, the way a scripted rollout would.

    ``merge_tasks`` goes through as Inno's ``/MERGETASKS`` — the switch a
    scripted rollout uses to choose optional tasks, and the only way to select
    (``addtopath``) or deselect (``!addtopath``) the add-to-PATH task without a
    wizard. Left unset the install takes the task defaults, which is what a user
    double-clicking the installer gets.

    ``log_name`` is a parameter because the PATH cycle below installs four
    times: one fixed name meant each run overwrote the log of the run before it,
    so the only failure whose evidence survived was the last one.
    """
    log = _temp_dir() / log_name
    command = [
        str(installer),
        "/VERYSILENT",
        "/SUPPRESSMSGBOXES",
        "/NORESTART",
        "/SP-",
        f"/LOG={log}",
    ]
    if merge_tasks is not None:
        command.append(f"/MERGETASKS={merge_tasks}")
    print(f"installing {installer.name} silently (tasks: {merge_tasks or 'default'}; log: {log})")
    try:
        result = _run(command, timeout=_INSTALL_TIMEOUT)
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


def measure_footprint() -> None:
    """Report the on-disk cost of the install — the number users actually pay.

    The installer's download size is public on the Releases page, but what it
    expands TO was never measured anywhere. Printed here (and into the CI job
    summary when available) so every build records it; informational only — a
    size regression is a review conversation, not a broken install.

    Which is why nothing in here raises. It runs inside ``step()``, and
    ``step()`` records any exception as a FAIL, so an unreadable file or a
    locked directory partway through a walk failed the whole release over a
    measurement that was never a gate. A number nobody could take is reported
    as one nobody could take.
    """
    app = _app_dir()
    try:
        files = [p for p in app.rglob("*") if p.is_file()]
        total = sum(p.stat().st_size for p in files)
    except OSError as exc:
        print(f"installed footprint: not measurable ({type(exc).__name__})")
        return
    line = f"installed footprint: {total / 1_000_000_000:.2f} GB across {len(files)} files ({app})"
    print(line)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        try:
            with open(summary, "a", encoding="utf-8") as fh:
                fh.write(f"**{line}**\n")
        except OSError as exc:
            print(f"(could not write the job summary: {type(exc).__name__})")


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
    # The app's own opt-in: the shell routes this into pywebview's
    # REMOTE_DEBUGGING_PORT setting, which is what actually reaches WebView2's
    # Chromium. The WebView2 switch variable is set alongside for completeness
    # only — a pywebview host sets AdditionalBrowserArguments itself, and
    # WebView2 then ignores that variable entirely.
    env[_GUI_DEBUG_PORT_ENV] = str(_CDP_PORT)
    env[_WEBVIEW2_ARGS_ENV] = f"--remote-debugging-port={_CDP_PORT}"
    temp = _temp_dir()
    stdout_log = temp / _GUI_STDOUT_LOG
    stderr_log = temp / _GUI_STDERR_LOG
    print(f"launching {gui_exe} with {_GUI_DEBUG_PORT_ENV}={env[_GUI_DEBUG_PORT_ENV]}")
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
            _await_liveness(page)
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


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


#: The app's two machine liveness signals, in the order they can happen, each
#: with the budget for what it is actually waiting for. Mirrors expectations.py's
#: contract: the bridge reports live, and then info() has answered — the About
#: popover carries a non-empty data-version only after the real round-trip.
_LIVENESS_STEPS: tuple[tuple[str, str, int], ...] = (
    (
        "the bridge going live",
        "() => document.documentElement.dataset.bridge === 'live'",
        _BRIDGE_TIMEOUT_MS,
    ),
    (
        "the first info() round-trip",
        "() => !!(document.querySelector('#about-version')"
        " && document.querySelector('#about-version').dataset.version)",
        _INFO_TIMEOUT_MS,
    ),
)


def _liveness_state(page: Page) -> str:
    """What the page says about itself, for a timeout message.

    A wait that ends with "Timeout 30000ms exceeded" says the budget ran out
    and nothing else — the reader cannot tell a hung bridge from a slow one, so
    the whole failure reads as noise. This reports the state the page was
    actually in when the budget ran out. Defensive: a page that cannot be
    evaluated at all is itself the diagnosis, and must not replace the real
    failure with an error of its own.
    """
    try:
        return str(
            page.evaluate(
                "() => `bridge=${document.documentElement.dataset.bridge ?? 'unset'}"
                " about-version=${document.querySelector('#about-version')"
                " ? (document.querySelector('#about-version').dataset.version || 'empty')"
                " : 'absent'}`"
            )
        )
    except Exception as exc:
        return f"the page could not be read ({type(exc).__name__})"


def _await_liveness(page: Page) -> None:
    """Wait for each liveness signal in turn, and say how long each took.

    One compound predicate under one budget used to cover both, so a timeout
    named neither and the elapsed time was invisible until it was too long.
    Waiting for them separately costs nothing — they happen in this order
    anyway — and buys a failure message that names the signal that never
    arrived, plus a pass that shows how much of its budget it used.
    """
    from playwright.sync_api import TimeoutError as PlaywrightTimeout

    for label, predicate, budget_ms in _LIVENESS_STEPS:
        started = time.monotonic()
        try:
            page.wait_for_function(predicate, timeout=budget_ms)
        except PlaywrightTimeout:
            raise SmokeFailure(
                f"{label} never happened within {budget_ms // 1000}s ({_liveness_state(page)})"
            ) from None
        print(f"  {label}: {_elapsed_ms(started)} ms of {budget_ms} ms")


def _dashboard_page(browser: Browser) -> Page:
    """The WebView2 page showing index.html (the window's only document)."""
    started = time.monotonic()
    deadline = started + _PAGE_TIMEOUT_MS / 1000
    while time.monotonic() < deadline:
        pages = [page for context in browser.contexts for page in context.pages]
        for page in pages:
            if page.url.endswith("index.html"):
                print(f"  page attached after {_elapsed_ms(started)} ms")
                return page
        if pages:
            print(
                f"  page attached after {_elapsed_ms(started)} ms (no index.html; took the first)"
            )
            return pages[0]
        time.sleep(1)
    raise SmokeFailure(f"the attached WebView2 exposed no page within {_PAGE_TIMEOUT_MS // 1000}s")


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


def uninstall(*, log_name: str = "anastomosis-uninstall.log") -> None:
    """Uninstall silently and prove the machine is clean afterwards.

    Logged, and to a name the caller chooses, for the same reason the installs
    are: the uninstaller is where the PATH segment is stripped, so when the
    count afterwards is wrong its log is the only account of what it decided.
    """
    uninstaller = _uninstaller()
    log = _temp_dir() / log_name
    print(f"uninstalling with {uninstaller} (log: {log})")
    try:
        result = _run(
            [
                str(uninstaller),
                "/VERYSILENT",
                "/SUPPRESSMSGBOXES",
                "/NORESTART",
                f"/LOG={log}",
            ],
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


# --- 5. the machine PATH count matrix ---------------------------------------


#: The sequence issue #281 reproduces, and the number of installer-owned PATH
#: segments the machine must hold after each step of it. The shipped 0.7.0
#: installer scored 0, 1, 2, 4, 4 here; each of its three defects lands on a
#: different step, which is why the steps are separate:
#:
#: * step 3 — the idempotence Check compared the literal ``{app}\cli`` (Inno
#:   does not expand constants in a Check parameter) against a PATH holding the
#:   expanded directory, so it never matched and every upgrade appended again;
#: * step 4 — nothing collapsed the duplicates already on a machine the shipped
#:   installer had upgraded, so the repair never happened;
#: * step 5 — the uninstall removed the first occurrence it found and left the
#:   rest on the machine for good.
#:
#: Steps 4 and 5 run over a PATH deliberately put back into the state the
#: shipped installer left (see ``_duplicate_owned_segment``): once the Check
#: works, no install can produce the duplicate the other two guards exist for,
#: so the only way to exercise them is to hand them one.
_PATH_MATRIX: tuple[tuple[str, int], ...] = (
    ("the install with the PATH task off", 0),
    ("the first add-to-PATH upgrade", 1),
    ("the second identical add-to-PATH upgrade", 1),
    ("an upgrade over a PATH an earlier build duplicated", 1),
    ("the uninstall, over a PATH an earlier build duplicated", 0),
)


def _cli_dir() -> str:
    """The directory the add-to-PATH task appends ({app}\\cli in anastomosis.iss)."""
    return str(_app_dir() / _CLI_SUBDIR)


def _user_path_segments() -> list[str]:
    """Segments a user could plausibly already have, seeded before the cycle.

    Both are near-misses on purpose: ``...\\cli2`` is a sibling whose name starts
    with the directory we own, and ``...\\cli\\bin`` is a child of it. A check
    that matched on substring rather than on whole delimited segments would
    treat either as the installer's and delete it on uninstall — which is a
    user's PATH edited by an installer that had no business touching it.
    """
    cli = _cli_dir()
    return [f"{cli}2", str(Path(cli, "bin"))]


def _path_segments(value: str) -> list[str]:
    """A PATH value as its ``;``-delimited segments, empty ones included.

    The empties are kept because they are part of the value the machine had: a
    PATH that ended in ``;`` must still end in ``;`` afterwards, and dropping
    them here would let an installer that quietly tidied someone else's PATH
    pass this test.
    """
    return value.split(";")


def _owns(segment: str, directory: str) -> bool:
    """Whether this PATH segment is one the installer wrote.

    The ownership test anastomosis.iss applies, and no wider: the whole segment
    spells the directory the way the installer writes it, case-folded because
    Windows paths are. Deliberately not a substring or prefix test, and
    deliberately not trailing-slash tolerant — the installer writes the
    directory without one, so a segment carrying one is somebody else's.
    """
    return segment.upper() == directory.upper()


def _count_owned(value: str, directory: str) -> int:
    return sum(1 for segment in _path_segments(value) if _owns(segment, directory))


def _unowned(value: str, directory: str) -> list[str]:
    return [segment for segment in _path_segments(value) if not _owns(segment, directory)]


def _render(segments: Sequence[str]) -> str:
    """PATH segments for an operator to read. Quoted rather than repr'd: repr
    doubles every backslash in a Windows path, so the one line the reader has to
    compare against their own PATH arrives in a spelling that is not theirs."""
    return ", ".join(f"'{segment}'" for segment in segments)


def _matrix_problems(observed: Sequence[int]) -> list[str]:
    """Every step of the matrix whose owned-segment count is not the required one."""
    if len(observed) != len(_PATH_MATRIX):
        return [f"expected {len(_PATH_MATRIX)} PATH measurements, got {len(observed)}"]
    return [
        f"after {label}: {seen} owned PATH segment(s), expected {expected}"
        for (label, expected), seen in zip(_PATH_MATRIX, observed, strict=True)
        if seen != expected
    ]


def _preservation_problems(before: str, after: str, directory: str) -> list[str]:
    """What the cycle did to the parts of PATH the installer does not own.

    The whole unowned remainder is compared, in order and byte for byte, rather
    than only the seeded segments: the failure worth catching is an installer
    rewriting a machine PATH value it only meant to read one entry out of.
    """
    kept_before = _unowned(before, directory)
    kept_after = _unowned(after, directory)
    if kept_before == kept_after:
        return []
    problems: list[str] = []
    lost = [segment for segment in kept_before if segment not in kept_after]
    gained = [segment for segment in kept_after if segment not in kept_before]
    if lost:
        problems.append(f"the cycle removed PATH segments it does not own: {_render(lost)}")
    if gained:
        problems.append(f"the cycle left PATH segments behind: {_render(gained)}")
    if not problems:
        problems.append(
            "the unowned PATH segments changed order or repetition: "
            f"{_render(kept_before)} -> {_render(kept_after)}"
        )
    return problems


def _machine_path() -> str:
    """The machine PATH as the registry holds it."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ENV_KEY, 0, winreg.KEY_READ) as key:
        value, _kind = winreg.QueryValueEx(key, _ENV_PATH_VALUE)
    return str(value)


def _write_machine_path(value: str) -> None:
    """Put a PATH value back as REG_EXPAND_SZ — the type Windows and
    anastomosis.iss both write. Rewriting it as REG_SZ would freeze whatever
    ``%VAR%`` another program left in there into today's expansion."""
    import winreg

    with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, _ENV_KEY, 0, winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, _ENV_PATH_VALUE, 0, winreg.REG_EXPAND_SZ, value)


def _marker_state() -> str:
    """The ownership marker as EACH registry view sees it.

    Both views, always, because a [Registry] entry and a [Code] read need not
    land in the same one on 64-bit Windows, and a marker written where the
    installer cannot read it looks exactly like a marker that was never
    written. Naming the view turns that pair of guesses into an observation.
    """
    import winreg

    seen: list[str] = []
    for label, view in (
        ("64-bit", winreg.KEY_WOW64_64KEY),
        ("32-bit", winreg.KEY_WOW64_32KEY),
    ):
        try:
            with winreg.OpenKey(
                winreg.HKEY_LOCAL_MACHINE, _MARKER_KEY, 0, winreg.KEY_READ | view
            ) as key:
                value, _kind = winreg.QueryValueEx(key, _MARKER_VALUE)
        except OSError as exc:
            seen.append(f"{label} view: absent ({type(exc).__name__})")
        else:
            seen.append(f"{label} view: {value}")
    return "; ".join(seen)


def _dump_tail(label: str, path: Path, lines: int = _LOG_TAIL_LINES) -> None:
    """Print the end of one installer log, between markers.

    Bounded to a tail because the decisions this step is accused of getting
    wrong are made at the end of a run, and an unbounded install transcript
    buries them. An absent file says so: "the uninstaller wrote no log" is
    itself a finding, and printing nothing would hide it.
    """
    print(f"----- {label}: {path} (last {lines} lines) -----")
    if not path.is_file():
        print("(absent)")
    else:
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError as exc:
            print(f"(unreadable: {type(exc).__name__})")
        else:
            tail = text.splitlines()[-lines:]
            print("\n".join(tail) if tail else "(empty)")
    print(f"----- end of {label} -----")


def _dump_path_diagnostics() -> None:
    """What the failed PATH cycle left behind: the marker, then the two logs.

    Only ever called from the failure path. Defensive by design, the same rule
    the GUI dump follows: a diagnostic must never replace the real failure with
    an error of its own — and this one reads the registry, on a machine where
    something about the registry has just gone wrong.
    """
    try:
        print(f"ownership marker {_MARKER_KEY}\\{_MARKER_VALUE} -> {_marker_state()}")
        _dump_tail("the repair upgrade's installer log", _temp_dir() / _PATH_LOG_REPAIR)
        _dump_tail("the uninstaller log", _temp_dir() / _PATH_LOG_UNINSTALL)
    except Exception as exc:
        print(f"warning: the PATH diagnostics could not be read ({type(exc).__name__})")


def _measure(index: int, directory: str) -> int:
    """One step of the matrix, measured and reported as it happens.

    Everything a reader needs on one line: which step, what it counted, what it
    owed, and the marker state. A count on its own cannot say WHY a step
    failed — the two repair steps do nothing at all when the marker is missing,
    and that is exactly how they failed the first time this shipped: three
    passing counts, two failing ones, and no way to tell a broken collapse from
    an absent gate. So the marker rides along on the passing steps too, where a
    reader can see it was there all the way up to the step that lost it.
    """
    label, expected = _PATH_MATRIX[index]
    count = _count_owned(_machine_path(), directory)
    print(f"  after {label}: {count} owned segment(s), expected {expected}")
    print(f"    marker {_marker_state()}")
    return count


def _duplicate_owned_segment(directory: str) -> None:
    """Put the machine PATH back into the state the shipped installer left it in.

    Not a fake: the marker that establishes ownership was written by the real
    installer on the upgrade before this, and the extra segment is spelled
    exactly the way it spells one. It has to be injected because the fixed
    installer cannot produce it — which is the whole difficulty of testing a
    repair, and the reason the collapse and the remove-all would otherwise ship
    having never run.
    """
    _write_machine_path(f"{_machine_path()};{directory}")


def check_path_matrix(installer: Path) -> None:
    """Install, upgrade, repair, uninstall — counting what lands on the machine PATH.

    Every other step here passes on an installer with this defect: it installs,
    the app runs, the payload comes off again, and the only trace is one more
    copy of the CLI directory on the machine PATH per upgrade and a dead one
    left behind at the end (#281). So the evidence is a count, read from the
    registry after each step of ``_PATH_MATRIX``, plus proof that the segments
    the installer does not own came through the whole cycle untouched.

    The PATH found on entry is restored on the way out even when a step fails: a
    smoke test that edits the machine PATH of the runner it is running on and
    does not put it back breaks every step after it. The restore itself reports
    rather than raises — it runs while an earlier failure is unwinding, and a
    diagnostic must never replace the failure it is describing.

    Any failure takes the marker state and the tails of the two logs with it.
    This step runs where nobody can go and look afterwards, and the first time
    it went red it reported five numbers and nothing that explained them.
    """
    directory = _cli_dir()
    original = _machine_path()
    before = after = original
    observed: list[int] = []
    print(f"the machine PATH holds {len(_path_segments(original))} segment(s) before seeding")
    try:
        try:
            _write_machine_path(";".join([original, *_user_path_segments()]))
            before = _machine_path()
            install(installer, merge_tasks="!addtopath", log_name=_PATH_LOG_NOPATH)
            observed.append(_measure(len(observed), directory))
            for attempt in (1, 2):
                install(
                    installer,
                    merge_tasks="addtopath",
                    log_name=_PATH_LOG_UPGRADE.format(attempt=attempt),
                )
                observed.append(_measure(len(observed), directory))
            # The remaining two steps are the repair: hand the installer the
            # PATH an earlier build left, once before an upgrade and once
            # before the uninstall, and require each to come back to the right
            # count.
            _duplicate_owned_segment(directory)
            install(installer, merge_tasks="addtopath", log_name=_PATH_LOG_REPAIR)
            observed.append(_measure(len(observed), directory))
            _duplicate_owned_segment(directory)
            uninstall(log_name=_PATH_LOG_UNINSTALL)
            after = _machine_path()
            observed.append(_measure(len(observed), directory))
            problems = _matrix_problems(observed)
            problems.extend(_preservation_problems(before, after, directory))
            if problems:
                raise SmokeFailure("; ".join(problems))
        except Exception:
            _dump_path_diagnostics()
            raise
    finally:
        try:
            _write_machine_path(original)
        except OSError as exc:
            print(f"warning: the machine PATH was not restored ({type(exc).__name__})")
    print(f"the seeded user segments survived the cycle: {_render(_user_path_segments())}")


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

    if installer is not None and not _failures:
        with step("installed layout"):
            check_layout()
        with step("installed footprint"):
            measure_footprint()
        with step("installed 'anast doctor'"):
            check_installed_doctor()
        with step("the dashboard renders in the shipped WebView2 window"):
            check_gui_window()
        # The uninstall runs even if the GUI step failed: leaving the runner
        # dirty would poison a re-run, and a broken uninstall is its own finding.
        with step("silent uninstall"):
            uninstall()
        # Its own install -> upgrade -> upgrade -> repair -> uninstall cycle,
        # starting from the clean machine the uninstall above just proved it left.
        with step("the machine PATH gains one owned segment and loses it again"):
            check_path_matrix(installer)

    if _failures:
        print(f"\nSMOKE FAILED: {len(_failures)} step(s): {_failures}")
        return 1
    print(
        "\nSMOKE PASSED: install -> layout -> doctor -> GUI window -> uninstall"
        " -> machine PATH matrix"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
