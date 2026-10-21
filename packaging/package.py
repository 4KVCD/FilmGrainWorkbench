"""Make the Windows download: build/FilmGrainWorkbench-<version>-win64.zip.

    python packaging/package.py [--qt DIR]     (or: python build.py --only package)

Builds the app (CMake, Release) and needs the programs in bin/ (build.py
builds them). --qt is Qt's folder (for example C:\\Qt\\6.12.0\\msvc2022_64),
needed only when the CMake build folder does not know it yet. The zip holds
one folder, "Film Grain Workbench":

    Film Grain Workbench.exe     the app, with Qt's, FFmpeg's and the MSVC run-time's DLLs beside it
    bin/                         vvencapp, vvdecapp, the grain analyzer (and the MSVC run-time they use)
    licenses/                    every license text; THIRD_PARTY_NOTICES.md says which is whose
    README.md, THIRD_PARTY_NOTICES.md

Only the Python standard library is needed.
"""

from __future__ import annotations

from pathlib import Path
import argparse
import re
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
APP_NAME = "Film Grain Workbench"
BUNDLED = ("vvencapp", "vvdecapp", "afgs1_grain_analyzer")
CMAKE_BUILD = ROOT / "build" / "cmake"
FFMPEG = ROOT / "native" / "ffmpeg"
# What windeployqt leaves out: translations (the app is in English), the
# compiler run-time (copied below from Visual Studio), the network module and
# software OpenGL, the shader compilers the widgets never use, the styles
# (the app uses Fusion, built into Qt Widgets) and the image formats (its one
# image, the icon, is a PNG, which Qt reads without a plugin).
WINDEPLOYQT_OPTIONS = ("--no-translations", "--no-compiler-runtime", "--no-system-d3d-compiler",
                       "--no-system-dxc-compiler", "--no-opengl-sw", "--no-network",
                       "--skip-plugin-types", "styles,imageformats", "--verbose", "0")
# The MSVC run-time DLLs the app, Qt, vvencapp and vvdecapp import.
RUNTIME_DLLS = ("msvcp140.dll", "msvcp140_1.dll", "msvcp140_2.dll", "msvcp140_atomic_wait.dll",
                "vcruntime140.dll", "vcruntime140_1.dll")


def version() -> str:
    match = re.search(r"project\(\w+ VERSION ([\d.]+)", (ROOT / "CMakeLists.txt").read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("no project VERSION in CMakeLists.txt")
    return match.group(1)


def run(command: list[str]) -> None:
    print("+", " ".join(command), flush=True)
    subprocess.run(command, cwd=ROOT, check=True)


def build_app(qt: Path | None) -> Path:
    """Builds the app; returns Qt's folder as the build knows it."""
    configure = ["cmake", "--preset", "windows"]
    if qt:
        configure.append(f"-DCMAKE_PREFIX_PATH={qt}")
    run(configure)
    run(["cmake", "--build", "--preset", "windows-release", "--target", "FilmGrainWorkbench"])
    cache = (CMAKE_BUILD / "CMakeCache.txt").read_text(encoding="utf-8")
    match = re.search(r"^Qt6_DIR:PATH=(.+)$", cache, re.MULTILINE)
    if not match:
        raise SystemExit("the build found no Qt: pass --qt <Qt folder>")
    return Path(match.group(1)).parents[2]  # <Qt>/lib/cmake/Qt6


def msvc_runtime() -> Path:
    """Visual Studio's folder of redistributable C++ run-time DLLs."""
    vswhere = Path(r"C:\Program Files (x86)\Microsoft Visual Studio\Installer\vswhere.exe")
    vs = subprocess.run([str(vswhere), "-latest", "-products", "*", "-property", "installationPath"],
                        capture_output=True, text=True, check=True).stdout.strip()
    folders = sorted(Path(vs, "VC", "Redist", "MSVC").glob("*/x64/Microsoft.VC14*.CRT"))
    if not folders:
        raise SystemExit(f"no C++ run-time redistributables under {vs}")
    return folders[-1]


def copy_runtime(source: Path, target: Path) -> None:
    for name in RUNTIME_DLLS:
        shutil.copy2(source / name, target / name)


def zip_folder(folder: Path, archive: Path) -> None:
    archive.unlink(missing_ok=True)
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as z:
        for path in sorted(folder.rglob("*")):
            z.write(path, Path(folder.name) / path.relative_to(folder))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--qt", type=Path, help="Qt's folder, if the CMake build does not know it yet")
    args = parser.parse_args()
    missing = [name for name in BUNDLED if not (ROOT / "bin" / f"{name}.exe").is_file()]
    if missing:
        print(f"not built: {', '.join(missing)} (python build.py)", file=sys.stderr)
        return 1
    qt = build_app(args.qt)
    runtime = msvc_runtime()

    app = ROOT / "build" / "package" / APP_NAME
    shutil.rmtree(app, ignore_errors=True)
    app.mkdir(parents=True)
    exe = app / f"{APP_NAME}.exe"
    shutil.copy2(CMAKE_BUILD / "src" / "app" / "Release" / f"{APP_NAME}.exe", exe)
    for dll in (FFMPEG / "bin").glob("*.dll"):
        shutil.copy2(dll, app)
    run([str(qt / "bin" / "windeployqt.exe"), *WINDEPLOYQT_OPTIONS, str(exe)])
    copy_runtime(runtime, app)

    (app / "bin").mkdir()
    for name in BUNDLED:
        shutil.copy2(ROOT / "bin" / f"{name}.exe", app / "bin")
    copy_runtime(runtime, app / "bin")  # Windows looks for a program's DLLs beside it, not beside the app

    licenses = app / "licenses"
    shutil.copytree(ROOT / "licenses", licenses)
    shutil.copyfile(ROOT / "LICENSE", licenses / "FilmGrainWorkbench-LICENSE.txt")
    shutil.copyfile(FFMPEG / "LICENSE.txt", licenses / "lgpl-2.1.txt")
    shutil.copyfile(ROOT / "third_party" / "nlohmann" / "LICENSE.MIT", licenses / "nlohmann-json-LICENSE.txt")
    for name in ("README.md", "THIRD_PARTY_NOTICES.md"):
        shutil.copyfile(ROOT / name, app / name)

    archive = ROOT / "build" / f"FilmGrainWorkbench-{version()}-win64.zip"
    zip_folder(app, archive)
    size = sum(f.stat().st_size for f in app.rglob("*") if f.is_file())
    print(f"{app}: {size / 1e6:.0f} MB\n{archive}: {archive.stat().st_size / 1e6:.0f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
