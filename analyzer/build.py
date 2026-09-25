"""Build bin/afgs1_grain_analyzer.exe against libaom.

    python analyzer/build.py                      # clone + build libaom into third_party/
    python analyzer/build.py --aom-src DIR --aom-build DIR   # reuse an existing libaom build

Needs git, cmake, ninja and gcc (MinGW-w64 "posix" threads) on PATH.
"""

from __future__ import annotations

from pathlib import Path
import argparse
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
AOM_URL = "https://aomedia.googlesource.com/aom"
AOM_COMMIT = "c8aad2b"          # libaom main, 2026-09-23 (the tested version)


def run(cmd: list[str], cwd: Path | None = None) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=cwd, check=True)


def ensure_libaom(src: Path, build: Path) -> None:
    if not (src / "aom_dsp" / "noise_model.c").is_file():
        src.parent.mkdir(parents=True, exist_ok=True)
        run(["git", "clone", AOM_URL, str(src)])
        run(["git", "checkout", AOM_COMMIT], cwd=src)
    if not (build / "libaom.a").is_file():
        build.mkdir(parents=True, exist_ok=True)
        run(["cmake", "-G", "Ninja", str(src), "-DCMAKE_BUILD_TYPE=Release",
             "-DENABLE_EXAMPLES=0", "-DENABLE_TESTS=0", "-DENABLE_DOCS=0", "-DENABLE_TOOLS=0",
             "-DENABLE_NASM=0"], cwd=build)
        run(["cmake", "--build", ".", "--target", "aom"], cwd=build)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--aom-src", type=Path, default=ROOT / "third_party" / "libaom")
    parser.add_argument("--aom-build", type=Path, default=ROOT / "third_party" / "aom-build")
    args = parser.parse_args()
    for tool in ("gcc", "cmake", "git"):
        if not shutil.which(tool):
            print(f"{tool} not found on PATH", file=sys.stderr)
            return 1
    ensure_libaom(args.aom_src, args.aom_build)
    out = ROOT / "bin" / "afgs1_grain_analyzer.exe"
    out.parent.mkdir(exist_ok=True)
    # Same optimisation level as libaom's own Release build, so floating-point
    # results match libaom's noise_model; -static avoids MinGW runtime DLLs.
    run(["gcc", "-O3", "-DNDEBUG", "-std=gnu11", "-pthread",
         f"-I{args.aom_src}", f"-I{args.aom_build}",
         str(ROOT / "analyzer" / "afgs1_grain_analyzer.c"), str(args.aom_build / "libaom.a"),
         "-lm", "-static", "-o", str(out)])
    print(f"built {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
