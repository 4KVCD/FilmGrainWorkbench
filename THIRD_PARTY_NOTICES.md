# Third-party software

Film Grain Workbench's own code is under the MIT license (`LICENSE`). The
Windows download also contains the programs and libraries below, each under
its own license; the license texts are in the `licenses` folder.

## Programs in `bin`

| Program | What it is | License |
|---|---|---|
| `vvencapp.exe` | [VVenC](https://github.com/fraunhoferhhi/vvenc), Fraunhofer HHI, built from pull request 765 (commit a9fc370) | BSD 3-Clause Clear (`VVenC-LICENSE.txt`) |
| `vvdecapp.exe` | [vvdec](https://github.com/fraunhoferhhi/vvdec), Fraunhofer HHI, commit 6f6bb29 with pull request 450 (commit 4280979) | BSD 3-Clause Clear (`vvdec-LICENSE.txt`) |
| `afgs1_grain_analyzer.exe` | This project's grain analyzer (`analyzer/`), linked with [libaom](https://aomedia.googlesource.com/aom) commit c8aad2b and, through GCC (MinGW-w64), the MinGW-w64 runtime and winpthreads | this project: MIT; libaom: BSD 2-Clause and the AOMedia Patent License 1.0 (`libaom-LICENSE.txt`, `libaom-PATENTS.txt`); MinGW-w64: `mingw-w64-runtime-COPYING.txt`, `mingw-w64-winpthreads-COPYING.txt` |

## Libraries of the app itself

| Library | License |
|---|---|
| [Qt 6](https://www.qt.io/) 6.12.0 (Qt Core, Qt GUI, Qt Widgets and the Windows platform plugin) | LGPL v3 (`lgpl-3.0.txt`, which builds on `gpl-3.0.txt`). The Qt libraries are separate DLLs beside the app (`Qt6*.dll`, `platforms`) and can be replaced with other builds of the same version. Source: https://download.qt.io/official_releases/qt/6.12/ and https://code.qt.io/. Qt contains third-party code (such as FreeType, HarfBuzz, libpng, PCRE2 and zlib) under its own licenses, listed at https://doc.qt.io/qt-6/licenses-used-in-qt.html |
| [FFmpeg](https://ffmpeg.org/) 9.0.2 libraries: libavformat, libavcodec, libavutil, libswresample | LGPL 2.1 or later as built: no GPL or non-free parts, no external libraries (`lgpl-2.1.txt`). Separate DLLs beside the app (`avformat-63.dll`, `avcodec-63.dll`, `avutil-61.dll`, `swresample-7.dll`) that can be replaced. Source: https://ffmpeg.org/releases/ffmpeg-9.0.2.tar.xz |
| [nlohmann/json](https://github.com/nlohmann/json) 3.12.0, compiled into the app | MIT (`nlohmann-json-LICENSE.txt`) |
| Microsoft Visual C++ run-time (`msvcp140*.dll`, `vcruntime140*.dll`, beside the app and in `bin`) | Redistributable files of Visual Studio 2022, distributed under its license terms |

## Not included

ffmpeg, ffprobe and mkvmerge (MKVToolNix) are separate programs the app runs;
they are not part of the download.
