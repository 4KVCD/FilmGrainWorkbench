"""Make the Windows download: build/FilmGrainWorkbench-<version>-win64.zip.

    python packaging/package.py        (or: python build.py --only package)

Needs PyInstaller (requirements-build.txt) and the programs in bin/ (build.py
builds them). The zip holds one folder, "Film Grain Workbench":

    Film Grain Workbench.exe     the app (PyInstaller, one folder; Python and Qt in _internal)
    bin/                         vvencapp, vvdecapp, the grain analyzer
    licenses/                    every license text; THIRD_PARTY_NOTICES.md says which is whose
    README.md, THIRD_PARTY_NOTICES.md
"""

from __future__ import annotations

from importlib import metadata
from pathlib import Path
import importlib.util
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from afgs1wb import __version__  # noqa: E402

APP_NAME = "Film Grain Workbench"
BUNDLED = ("vvencapp", "vvdecapp", "afgs1_grain_analyzer")
BUILD = ROOT / "build"
RESOURCES = ROOT / "afgs1wb" / "gui" / "resources"

# Qt parts this widgets app never uses that PySide6 and Qt's plugins pull in: the network
# module (and its TLS backends), software OpenGL, Qt Quick/QML (for the virtual keyboard
# input plugin) and Qt PDF (an image format plugin). About 60 MB.
EXCLUDED_MODULES = ("PySide6.QtNetwork", "PySide6.QtQml", "PySide6.QtQuick", "PySide6.QtPdf",
                    "PySide6.QtOpenGL", "PySide6.QtVirtualKeyboard")
UNUSED_FILES = ("opengl32sw.dll", "Qt6VirtualKeyboard.dll", "Qt6Quick.dll", "Qt6Qml.dll", "Qt6QmlMeta.dll",
                "Qt6QmlModels.dll", "Qt6QmlWorkerScript.dll", "Qt6Pdf.dll", "Qt6OpenGL.dll", "Qt6Network.dll",
                "plugins/platforminputcontexts", "plugins/imageformats/qpdf.dll", "plugins/tls",
                "plugins/networkinformation")

VERSION_INFO ="""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v4}, prodvers={v4}, mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[StringFileInfo([StringTable('040904B0', [
          StringStruct('ProductName', '{name}'),
          StringStruct('FileDescription', '{name}'),
          StringStruct('FileVersion', '{version}'),
          StringStruct('ProductVersion', '{version}'),
          StringStruct('OriginalFilename', '{name}.exe'),
          StringStruct('LegalCopyright', 'MIT license; third-party notices in THIRD_PARTY_NOTICES.md')])]),
        VarFileInfo([VarStruct('Translation', [1033, 1200])])])
"""


def _python_licenses(target: Path) -> None:
    """The license files of the Python packages inside the app, and Python's own."""
    target.mkdir(parents=True, exist_ok=True)
    for package, wanted in (("numpy", "LICENSE.txt"), ("av", "LICENSE.txt")):
        files = [f for f in metadata.distribution(package).files or [] if f.name == wanted]
        if not files:
            raise SystemExit(f"no {wanted} in the {package} package")
        shutil.copyfile(files[0].locate(), target / f"{package}-LICENSE.txt")
    python_license = Path(sys.base_prefix) / "LICENSE.txt"
    if not python_license.is_file():
        raise SystemExit(f"Python's license is not at {python_license}")
    shutil.copyfile(python_license, target / "python-LICENSE.txt")


def _zip(folder: Path, archive: Path) -> None:
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(folder.rglob("*")):
            z.write(path, Path(folder.name) / path.relative_to(folder))


def main() -> int:
    if importlib.util.find_spec("PyInstaller") is None:
        print("PyInstaller is missing: pip install -r requirements-build.txt", file=sys.stderr)
        return 1
    missing = [name for name in BUNDLED if not (ROOT / "bin" / f"{name}.exe").is_file()]
    if missing:
        print(f"not built: {', '.join(missing)} (python build.py)", file=sys.stderr)
        return 1
    BUILD.mkdir(exist_ok=True)
    numbers = [int(n) for n in __version__.split(".")] + [0]
    version_file = BUILD / "version_info.txt"
    version_file.write_text(VERSION_INFO.format(v4=tuple(numbers[:4]), name=APP_NAME, version=__version__),
                            encoding="utf-8")
    dist = BUILD / "dist"
    result = subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean", "--windowed",
        "--name", APP_NAME, "--icon", str(RESOURCES / "icon.ico"), "--version-file", str(version_file),
        "--paths", str(ROOT), "--add-data", f"{RESOURCES};afgs1wb/gui/resources",
        *(arg for module in EXCLUDED_MODULES for arg in ("--exclude-module", module)),
        "--distpath", str(dist), "--workpath", str(BUILD / "pyinstaller"), "--specpath", str(BUILD),
        str(ROOT / "packaging" / "launch.py")])
    if result.returncode != 0:
        return result.returncode

    app = dist / APP_NAME
    qt = app / "_internal" / "PySide6"
    for name in UNUSED_FILES:
        path = qt / name
        if path.is_dir():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
    (app / "bin").mkdir(exist_ok=True)
    for name in BUNDLED:
        shutil.copy2(ROOT / "bin" / f"{name}.exe", app / "bin")
    licenses = app / "licenses"
    shutil.copytree(ROOT / "licenses", licenses, dirs_exist_ok=True)
    shutil.copyfile(ROOT / "LICENSE", licenses / "FilmGrainWorkbench-LICENSE.txt")
    _python_licenses(licenses / "python")
    for name in ("README.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, app / name)

    archive = BUILD / f"FilmGrainWorkbench-{__version__}-win64.zip"
    _zip(app, archive)
    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file())
    print(f"{app}: {size / 1e6:.0f} MB\n{archive}: {archive.stat().st_size / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
