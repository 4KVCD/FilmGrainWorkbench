"""Build the programs the app runs into bin/, then the app and the Windows download.

    python build.py                    # everything, ending with build/FilmGrainWorkbench-<version>-win64.zip
    python build.py --only analyzer    # bin/afgs1_grain_analyzer.exe (gcc, cmake, ninja, git)
    python build.py --only vvc         # bin/vvencapp.exe, bin/vvdecapp.exe (Visual Studio 2022, cmake, git)
    python build.py --only package     # the app and the zip, with what is in bin/ (Visual Studio 2022, Qt 6)
    python build.py --aom-src DIR --aom-build DIR    # reuse an existing libaom build for the analyzer
    python build.py --qt DIR           # Qt's folder, for the app's first build

The programs use different compilers on purpose: the analyzer is built with
gcc like libaom's own Release build (its floating-point results must match
libaom's), VVenC and vvdec with MSVC as their projects support. Each part is
its own script (analyzer/build.py, vvc/build_vvc_tools.ps1,
packaging/package.py); this one runs them. Any Python 3.11 or newer will do:
only its standard library is used.
"""

from __future__ import annotations

from pathlib import Path
import argparse
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def build_analyzer(args: argparse.Namespace) -> int:
    command = [sys.executable, str(ROOT / "analyzer" / "build.py")]
    if args.aom_src:
        command += ["--aom-src", str(args.aom_src)]
    if args.aom_build:
        command += ["--aom-build", str(args.aom_build)]
    return subprocess.run(command).returncode


def build_vvc() -> int:
    shell = shutil.which("pwsh") or shutil.which("powershell")
    if not shell:
        print("PowerShell not found: vvc/build_vvc_tools.ps1 needs it", file=sys.stderr)
        return 1
    script = ROOT / "vvc" / "build_vvc_tools.ps1"
    return subprocess.run([shell, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File", str(script)]).returncode


def build_package(args: argparse.Namespace) -> int:
    command = [sys.executable, str(ROOT / "packaging" / "package.py")]
    if args.qt:
        command += ["--qt", str(args.qt)]
    return subprocess.run(command).returncode


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--only", choices=("analyzer", "vvc", "package"), help="build just this part")
    parser.add_argument("--aom-src", type=Path, help="libaom source folder to reuse (analyzer)")
    parser.add_argument("--aom-build", type=Path, help="libaom build folder to reuse (analyzer)")
    parser.add_argument("--qt", type=Path, help="Qt's folder, for the app's first build (package)")
    args = parser.parse_args()
    failed = []
    if args.only in (None, "analyzer") and build_analyzer(args) != 0:
        failed.append("analyzer")
    if args.only in (None, "vvc") and build_vvc() != 0:
        failed.append("vvc")
    if not failed and args.only in (None, "package") and build_package(args) != 0:
        failed.append("package")
    if failed:
        print("failed: " + ", ".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
