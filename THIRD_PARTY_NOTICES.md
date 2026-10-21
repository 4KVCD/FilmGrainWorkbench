# Third-party software

Film Grain Workbench's own code is under the MIT license (`LICENSE`). The
Windows download also contains the programs and libraries below, each under
its own license; the license texts are in the `licenses` folder.

## Programs in `bin`

| Program | What it is | License |
|---|---|---|
| `vvencapp.exe` | [VVenC](https://github.com/fraunhoferhhi/vvenc), Fraunhofer HHI, built from pull request 765 (commit 6b212fd) | BSD 3-Clause Clear (`VVenC-LICENSE.txt`) |
| `vvdecapp.exe` | [vvdec](https://github.com/fraunhoferhhi/vvdec), Fraunhofer HHI, commit 6f6bb29 | BSD 3-Clause Clear (`vvdec-LICENSE.txt`) |
| `afgs1_grain_analyzer.exe` | This project's grain analyzer (`analyzer/`), linked with [libaom](https://aomedia.googlesource.com/aom) commit c8aad2b and, through GCC (MinGW-w64), the MinGW-w64 runtime and winpthreads | this project: MIT; libaom: BSD 2-Clause and the AOMedia Patent License 1.0 (`libaom-LICENSE.txt`, `libaom-PATENTS.txt`); MinGW-w64: `mingw-w64-runtime-COPYING.txt`, `mingw-w64-winpthreads-COPYING.txt` |

## Libraries of the app itself

| Library | License |
|---|---|
| [Qt 6](https://www.qt.io/) and [Qt for Python (PySide6, Shiboken6)](https://doc.qt.io/qtforpython-6/) 6.11.2 | LGPL v3 (`lgpl-3.0.txt`, which builds on `gpl-3.0.txt`). The Qt and PySide6 libraries are separate files in the `_internal` folder and can be replaced with other builds of the same version. Source: https://download.qt.io/official_releases/QtForPython/ and https://code.qt.io/ |
| [NumPy](https://numpy.org/) 2.5.3, with OpenBLAS | BSD 3-Clause; the bundled OpenBLAS, LAPACK and GCC runtime notices are in `python/numpy-LICENSE.txt` |
| [PyAV](https://pyav.basswood-io.com/) 18.1.0 | BSD 3-Clause (`python/av-LICENSE.txt`) |
| FFmpeg 8.1.2 libraries shipped with PyAV, with libx264, libx265 and other codec libraries | GPL v2 or later as built (`gpl-2.0.txt`). Build scripts and the versions of every library: https://github.com/PyAV-Org/pyav-ffmpeg; FFmpeg source: https://ffmpeg.org/download.html |
| Python 3.14 runtime | PSF License (`python/python-LICENSE.txt`) |
| [PyInstaller](https://pyinstaller.org/) bootloader | GPL v2 with an exception that allows it to be used with programs under any license |

## Not included

ffmpeg, ffprobe and mkvmerge (MKVToolNix) are separate programs the app runs;
they are not part of the download.
