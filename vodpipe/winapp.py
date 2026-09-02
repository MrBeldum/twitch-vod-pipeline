"""Register VOD Pipeline as a Windows application.

The compiled host (`VODPipeline.exe`) is what Explorer, the Start Menu, Apps
& Features and the taskbar pin. This module builds that host with the machine's
`csc.exe` and then asks it to write the uninstall key, App Paths entry and
Start Menu shortcut (with the same AppUserModelID the process sets at launch).

Python itself is not an application Windows will group or pin; wrapping it is
the whole point.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .config import APP_ROOT
from .util import LOG

AUMID = "MrBeldum.VODPipeline"
APP_NAME = "VOD Pipeline"
HOST_NAME = "VODPipeline.exe"
CSC_HINTS = (
    r"%SystemRoot%\Microsoft.NET\Framework64\v4.0.30319\csc.exe",
    r"%SystemRoot%\Microsoft.NET\Framework\v4.0.30319\csc.exe",
)


def host_path() -> Path:
    return APP_ROOT / HOST_NAME


def find_csc() -> str | None:
    for hint in CSC_HINTS:
        candidate = Path(os.path.expandvars(hint))
        if candidate.is_file():
            return str(candidate)
    return None


def build_inputs() -> list[Path]:
    """Everything baked into the compiled host.

    `ensure_host` compares the exe against all of these. It used to compare
    against `host.cs` alone, which meant a new icon was compiled in only if the
    C# happened to change in the same breath -- so replacing the artwork and
    running `vodpipe install` produced no rebuild, no error, and the old icon.
    The icon is an input to the compile (`/win32icon`), so it has to be an input
    to the staleness check.
    """
    packaging = APP_ROOT / "packaging"
    return [packaging / "host.cs",
            packaging / "version.g.cs",
            packaging / "app.manifest",
            packaging / "vodpipe.ico",
            packaging / "VODPipeline.VisualElementsManifest.xml"]


def run_prebuild() -> None:
    """Regenerate the icons and the version file before compiling.

    Best effort: both outputs are checked in, so a machine without ffmpeg still
    builds -- it just builds what is committed. Failing an install over an icon
    would be a worse trade than shipping the previous one.
    """
    script = APP_ROOT / "packaging" / "prebuild.py"
    if not script.is_file():
        return
    done = subprocess.run([sys.executable, str(script)],
                          capture_output=True, text=True)
    if done.returncode != 0:
        LOG.warning("generated build inputs not refreshed, using the committed "
                    "ones: %s", (done.stderr or done.stdout or "").strip())


def build_host() -> Path:
    """Compile `VODPipeline.exe` next to the package. Raises on failure."""
    csc = find_csc()
    if not csc:
        raise RuntimeError(
            "csc.exe not found; .NET Framework 4 is required to compile "
            "the Windows host")
    run_prebuild()
    packaging = APP_ROOT / "packaging"
    icon = packaging / "vodpipe.ico"
    manifest = packaging / "app.manifest"
    source = packaging / "host.cs"
    version_cs = packaging / "version.g.cs"
    missing = [str(path) for path in (icon, manifest, source, version_cs)
               if not path.is_file()]
    if missing:
        raise RuntimeError("cannot build the Windows host; missing "
                           + ", ".join(missing))
    dest = host_path()
    argv = [
        csc,
        "/nologo",
        "/optimize+",
        "/target:winexe",
        "/platform:x64",
        "/reference:System.Windows.Forms.dll",
        "/reference:System.Drawing.dll",
        # csc wants a colon here. With `=` it answers `fatal error CS2007:
        # Unrecognized option: '/win32icon=...'`, which meant `vodpipe install`
        # could not compile the host at all -- the exe in the tree had been
        # built by packaging/build.cmd, which had it right, and `ensure_host`
        # never asked for a rebuild, so nothing surfaced the breakage.
        f"/win32icon:{icon}",
        f"/win32manifest:{manifest}",
        f"/out:{dest}",
        str(source),
        str(version_cs),
    ]
    LOG.info("compiling Windows host: %s", dest)
    completed = subprocess.run(argv, capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stdout or "") + (completed.stderr or "")
        raise RuntimeError(f"csc failed ({completed.returncode}): {detail.strip()}")
    tile_src = packaging / "VODPipeline.VisualElementsManifest.xml"
    tile_dest = APP_ROOT / "VODPipeline.VisualElementsManifest.xml"
    if tile_src.is_file():
        tile_dest.write_bytes(tile_src.read_bytes())
    return dest


def ensure_host() -> Path:
    """The compiled host, rebuilt if any of its inputs is newer than it."""
    dest = host_path()
    if not dest.is_file():
        return build_host()
    built = dest.stat().st_mtime
    present = [path for path in build_inputs() if path.is_file()]
    if present and all(built >= path.stat().st_mtime for path in present):
        return dest
    return build_host()


def _run_host(*flags: str) -> None:
    exe = ensure_host()
    completed = subprocess.run([str(exe), *flags], capture_output=True, text=True)
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "").strip()
        raise RuntimeError(detail or f"{exe.name} {' '.join(flags)} failed "
                           f"({completed.returncode})")


def install() -> Path:
    """Build the host if needed and register it with Windows."""
    if os.name != "nt":
        raise RuntimeError("Windows app registration is only available on Windows")
    exe = ensure_host()
    _run_host("--install")
    return exe


def uninstall() -> None:
    if os.name != "nt":
        raise RuntimeError("Windows app registration is only available on Windows")
    exe = host_path()
    if exe.is_file():
        completed = subprocess.run([str(exe), "--uninstall"],
                                   capture_output=True, text=True)
        if completed.returncode != 0:
            LOG.warning("host uninstall exited %s", completed.returncode)
        return
    _python_uninstall_fallback()


def _python_uninstall_fallback() -> None:
    """Drop the registry keys and shortcut if the host exe is already gone."""
    try:
        import winreg
    except ImportError:
        return
    for key in (
        r"Software\Microsoft\Windows\CurrentVersion\Uninstall\VODPipeline",
        r"Software\Microsoft\Windows\CurrentVersion\App Paths\VODPipeline.exe",
        r"Software\Classes\Applications\VODPipeline.exe",
    ):
        try:
            winreg.DeleteKey(winreg.HKEY_CURRENT_USER, key)
        except OSError:
            pass
    programs = Path(os.environ.get("APPDATA", "")) / (
        "Microsoft/Windows/Start Menu/Programs/VOD Pipeline.lnk")
    try:
        programs.unlink(missing_ok=True)
    except OSError:
        pass


def set_app_user_model_id(app_id: str = AUMID) -> None:
    """Tell Windows this python process is VOD Pipeline, not pythonw.exe.

    Has to run before any HWND is created. The compiled host also sets this
    on itself; this covers `pythonw -m vodpipe app` without the host.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception:
        LOG.debug("could not set AppUserModelID", exc_info=True)
