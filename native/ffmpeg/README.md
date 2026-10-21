# FFmpeg libraries (LGPL)

The AV1 tab's Write reads the encode's VVC packets, with their timestamps,
through libavformat (`src/core/src/demux.cpp`). These are FFmpeg 9.0.2's
libraries as VideoMetricsLab builds them (its `scripts/build_ffmpeg_decoders.ps1`,
commit 136581f): LGPL (no `--enable-gpl`, no `--enable-nonfree`), no external
library, from `ffmpeg-9.0.2.tar.xz` with SHA-256
`8c3850283eb25fa026482078a04051e0be17347b09ef81a0849bec15a96e002e`.
Its demuxers include Matroska, MP4/MOV and MPEG-TS, and its parsers VVC.

| File | What | SHA-256 |
| --- | --- | --- |
| `bin/avformat-63.dll` | libavformat: demuxing | `0e42151e775556adf1d07853581c46aba8317b6fcb006068ec0d2ebd76c9a3c6` |
| `bin/avcodec-63.dll` | libavcodec (libavformat needs it) | `d140ff88b016890b5f0ce190541ca97a04dab44e6e7ded0491d960d0395d10b6` |
| `bin/avutil-61.dll` | libavutil | `a5292d2aa4e2a9a06f80c466d52089a9c4d0f2f00368b626cf62591836738277` |
| `bin/swresample-7.dll` | libswresample (libavcodec imports it) | `8f0f4d0bcae9355ce1b270b6249dd7579d893be64065e9ba56b86ed5d0c29e33` |

`include/` holds their headers and `lib/` their exports (`.def`), from which
the build makes import libraries. `LICENSE.txt` is the LGPL 2.1 they are
under; their source is FFmpeg 9.0.2's release, https://ffmpeg.org/releases/.
