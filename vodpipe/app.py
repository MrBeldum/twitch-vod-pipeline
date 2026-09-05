"""Desktop host: the same local server, in its own Chromium window.

The pipeline is a loopback HTTP service because that is the control surface
that already exists. This module does not replace it with a second UI; it
opens a dedicated Chromium (or Chrome) window pointed at that server.

Edge is never used. Chromium first, then Google Chrome, then a copy dropped
in `vendor/chromium`. Window close is process-exit: we use a private
`--user-data-dir`, so the browser process is ours, and when it dies we shut
the pipeline down. If no Chromium-family browser is installed we fall back
to the ordinary dashboard (system browser + serve_forever).

macOS always takes that fallback. A Chrome process there outlives its last
window (the app stays in the Dock until Cmd+Q), so "window closed" is not an
event we can observe, and a pipeline that keeps recording behind a closed
window is worse than a browser tab.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import threading
from pathlib import Path

from .config import APP_ROOT, DATA_ROOT, Config
from .pipeline import Pipeline
from .server import serve
from .util import LOG, setup_logging
from .winapp import AUMID, set_app_user_model_id

# Chromium and Chrome only. Edge is a Chromium fork but it is the Edge
# browser, which is not what this window is supposed to be.
_CHROMIUM_HINTS = (
    str(APP_ROOT / "vendor" / "chromium" / "chrome.exe"),
    str(APP_ROOT / "vendor" / "chromium" / "chromium.exe"),
    r"%LOCALAPPDATA%\VOD Pipeline\chromium\chrome.exe",
    r"%LOCALAPPDATA%\Chromium\Application\chrome.exe",
    r"%LOCALAPPDATA%\Chromium\Application\chromium.exe",
    r"%ProgramFiles%\Chromium\Application\chrome.exe",
    r"%ProgramFiles%\Chromium\Application\chromium.exe",
    r"%ProgramFiles(x86)%\Chromium\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Chromium\Application\chromium.exe",
)
_CHROME_HINTS = (
    r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome\Application\chrome.exe",
    r"%ProgramFiles%\Google\Chrome Beta\Application\chrome.exe",
    r"%LocalAppData%\Google\Chrome SxS\Application\chrome.exe",
)


def browser_candidates() -> list[str]:
    """Ordered Chromium-family paths. Never includes msedge."""
    out: list[str] = []
    seen: set[str] = set()
    for hint in (*_CHROMIUM_HINTS, *_CHROME_HINTS):
        expanded = os.path.expandvars(hint)
        key = os.path.normcase(expanded)
        if key not in seen:
            seen.add(key)
            out.append(expanded)
    return out


_PATH_NAMES = ("chromium", "chromium.exe", "chromium-browser", "chrome",
               "chrome.exe", "google-chrome", "google-chrome-stable")


def find_app_browser() -> str | None:
    if sys.platform == "darwin":
        return None  # see the module docstring
    for candidate in browser_candidates():
        if Path(candidate).is_file():
            return candidate
    for name in _PATH_NAMES:
        found = shutil.which(name)
        if found and "msedge" not in os.path.normcase(found):
            return found
    return None


def run_app(config: Config, *, port: int | None = None,
            open_window: bool = True) -> int:
    set_app_user_model_id(AUMID)
    log_file = DATA_ROOT / "logs" / "vodpipe-app.log"
    # The CLI already applied --verbose; adding the file handler must not undo it.
    setup_logging(log_file=log_file, verbose=LOG.level <= logging.DEBUG)
    pipeline = Pipeline(config)
    httpd = None
    browser: subprocess.Popen | None = None
    try:
        pipeline.start()
        httpd = serve(pipeline, config, port=port, open_browser=False)
        host, bound = httpd.server_address[:2]
        url = f"http://{host}:{bound}/"
        LOG.info("app listening on %s", url)

        if open_window:
            browser = _open_window(url)
            if browser is None:
                LOG.info("no Chromium/Chrome window available; opening the system browser")
                import webbrowser
                webbrowser.open(url)
                httpd.serve_forever()
                return 0
            _wait_for(browser, httpd)
        else:
            httpd.serve_forever()
        return 0
    except KeyboardInterrupt:
        print("\nstopping...", file=sys.stderr)
        return 0
    finally:
        if browser is not None and browser.poll() is None:
            try:
                browser.terminate()
            except OSError:
                pass
        try:
            if httpd is not None:
                try:
                    httpd.shutdown()
                except Exception:
                    pass
                httpd.server_close()
        finally:
            try:
                LOG.info("finishing queued work before exit...")
            finally:
                pipeline.shutdown_until_stopped()


def _open_window(url: str) -> subprocess.Popen | None:
    executable = find_app_browser()
    if not executable:
        return None
    profile = DATA_ROOT / ".app-profile"
    profile.mkdir(parents=True, exist_ok=True)
    argv = [
        executable,
        f"--app={url}",
        f"--user-data-dir={profile}",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-extensions",
        "--disable-sync",
        "--disable-features=TranslateUI,MediaRouter",
        "--window-size=1440,900",
        "--class=VODPipeline",
    ]
    LOG.info("opening Chromium window via %s", executable)
    try:
        return subprocess.Popen(argv, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError as exc:
        LOG.warning("could not launch %s: %s", executable, exc)
        return None


def _wait_for(browser: subprocess.Popen, httpd) -> None:
    """Serve HTTP until the window process exits, then stop the server."""

    def wait_browser() -> None:
        try:
            browser.wait()
        finally:
            threading.Thread(target=httpd.shutdown, daemon=True,
                             name="app-shutdown").start()

    watcher = threading.Thread(target=wait_browser, name="app-window",
                               daemon=True)
    watcher.start()
    httpd.serve_forever()
    watcher.join(timeout=5.0)
