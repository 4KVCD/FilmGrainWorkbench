# Film Grain Workbench

See what happens to film grain in VVC, and put it back.

- **VVC grain analyzer (VVenC):** pick a scene of a film and see it three ways:
  the source, the picture after VVenC removed the grain, and the picture with
  vvdec's film grain synthesis added back, for each of VVenC's three film grain
  analysis modes.
- **AV1 grain into VVC (AFGS1):** measure the grain a VVC encode lost with
  libaom's noise model, tune its strength on real frames, and write it into
  the encode as **AFGS1** metadata (AV1 film grain carried in ITU-T T.35 SEI
  messages), so players re-synthesise it at playback.

## Download and run

1. Download `FilmGrainWorkbench-<version>-win64.zip` from the Releases page and
   unzip it anywhere.
2. Run **Film Grain Workbench.exe**. Nothing is installed; delete the folder to
   remove it.
3. The app needs two free programs it does not include:
   - **ffmpeg and ffprobe** with VVC decoding, FFmpeg 7.1 or newer
     ([ffmpeg.org/download.html](https://ffmpeg.org/download.html), for example
     the gyan.dev "full" build). The AV1 tab's *Verify* also needs an ffmpeg
     that applies AFGS1 grain to VVC (9.0.1 does).
   - **mkvmerge** from [MKVToolNix](https://mkvtoolnix.download/), only to write
     MKVs in the AV1 tab.

   The app finds them on `PATH`, in MKVToolNix's install folder, or in its own
   `bin` folder; otherwise set them under *Settings → External programs*.
   *Help → Check installation* lists what was found.

Windows 10 or 11, 64-bit. VVenC, vvdec and the grain analyzer are included.

## VVC grain analyzer

Choose a source video and browse it for a scene: only the source is decoded
while you look. Black bars are found and cropped, as an encode would.
*Encode this scene* (E) encodes the frames around the one shown with VVenC in
all three film grain analysis modes:

| Mode | Analysis |
| --- | --- |
| `--fga 1` | VTM's analysis (JVET-AN0237), frequency filtering model |
| `--fga 2` | the same, with the grain measured from the temporal residual and calibrated scaling |
| `--fga 3` | like 2, with the auto-regressive grain model (only some decoders, such as vvdec, synthesise it) |

Each mode writes an H.274 film grain characteristics SEI message; vvdec decodes
every encode twice, without and with its grain synthesis. The selected mode is
shown as soon as it is ready; the other two are encoded in the background, so
switching modes is then instant. VVenC analyses the grain on the first picture
of every GOP (32 frames); the pictures in between carry the latest parameters.
Inside the encoded scene the views are:

1. **Source**: the source frame, cropped like the encode.
2. **VVC, grain removed**: what VVenC coded (its MCTF pre-filter and the
   quantisation remove the grain), decoded with film grain synthesis off.
3. **VVC + grain synthesis**: the same, decoded with vvdec's synthesis on.
4. **Grain only (×4)**: the grain vvdec adds.

The side panel shows the grain parameters the frame carries (read from the
bitstream, both the frequency and the auto-regressive model), how much grain
VVenC removed and vvdec adds, and the colour shift the grain causes. Inside an
encoded scene, changing the strength, QP or preset encodes the scene again;
a new scene length applies when *Encode this scene* is pressed next. HDR
sources (PQ, HLG) are encoded as HDR.

Temporary files: one scene at a time, in a folder in `%TEMP%` (for 33 frames
at 3840×1608 about 4.3 GB). Whatever is replaced is deleted as soon as nothing
reads it, the folder when the app closes, and folders left by a crash when the
app next opens.

## AV1 grain into VVC

1. **Videos.** Choose the source and the VVC encode made from it. *Detect*
   finds the frame offset if the encode was trimmed.
2. **Preview and tune.** The grain of the frame shown is estimated as it
   loads. Luma and chroma strength scale it exactly; the grain is rendered with
   the same algorithm decoders use.
3. **Write.** Every frame is analysed the way libaom's encoder does with
   `--denoise-noise-level`: one model carried through the film, updated on each
   frame and restarted when the noise changes, so the grain follows the film
   shot by shot. This takes a few hours for a 4K film on all CPU threads;
   progress is saved about once a minute, so a stopped run resumes, and a
   finished analysis is reused. Every picture then gets a complete AFGS1
   parameter set with its own seed. Audio, subtitles, chapters, attachments
   and HDR properties are carried over.
4. **Verify.** ffmpeg decodes a frame of the written file; the app checks it is
   bit-identical to the preview.

Grain flags default to libaom's: overlap on, no clipping to the limited range.
To see the grain, the player must apply AFGS1 to VVC (ffmpeg-based players
such as mpv).

## Viewer keys

| Key | Action |
| --- | --- |
| ← / → | Without / with grain |
| 1 2 3 4 | Source, without grain, with grain, grain only (×4) |
| , / . | Previous / next frame |
| PgUp / PgDn | ±1 second |
| E | Encode this scene (VVC tab) |
| wheel, + / − | Zoom (nearest-neighbour from 100 %, so single grains stay sharp) |
| 0 / F | 100 % / fit |
| drag | Pan |

*Open in window* shows the frame alone in a resizable window with the same keys.

## Troubleshooting

- *Help → Check installation* shows every program the app uses, where it was
  found and its version; *Copy* puts the report on the clipboard for a bug
  report. The same report can be written from the command line:
  `"Film Grain Workbench.exe" --check-installation report.txt`.
- Unexpected errors are written to
  `%APPDATA%\AFGS1Workbench\AFGS1 Grain Workbench\errors.log`.

## Build from source

Needs Python 3.11 or newer on Windows.

```bat
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements-build.txt
.venv\Scripts\python build.py
```

`build.py` builds the programs in `bin`, then the download in
`build\FilmGrainWorkbench-<version>-win64.zip`. One part at a time:
`--only analyzer`, `--only vvc` or `--only package`.

- The grain analyzer (`analyzer/build.py`) needs gcc, cmake, ninja and git; it
  fetches libaom and builds it with gcc, like libaom's own Release build, so its
  floating-point results match libaom's.
- vvencapp and vvdecapp (`vvc/build_vvc_tools.ps1`) need Visual Studio 2022,
  CMake and git. VVenC is fraunhoferhhi/vvenc pull request 765, which includes
  pull request 764; vvdec is its master branch.
- The package step (`packaging/package.py`) uses PyInstaller.

Run from the source with `.venv\Scripts\pythonw -m afgs1wb`. Tests:
`.venv\Scripts\python -m pip install -r requirements-dev.txt`, then
`.venv\Scripts\python -m pytest`.

## Notes on compatibility

- **Why not ffmpeg for muxing:** ffmpeg does not rebuild decode timestamps for
  VVC read from Matroska, so `ffmpeg -c copy` collapses the timestamps of any
  VVC MKV. The video track is written by the app and merged with mkvmerge.
- **ffmpeg's AFGS1 parser** reads chroma point fields that the specification
  omits when a chroma curve is empty, so "no chroma grain" is written as a
  single zero-strength point, which every parser reads the same way.
- **ffmpeg 9.0.1** fails ("File exists") when it applies grain to a stream
  whose MKV track header carries HDR mastering / light-level metadata. When the
  VVC stream already carries that metadata in SEI (VVenC encodes do), it is not
  repeated in the track header.
- VVC's own H.274 film grain SEI is not used in the AV1 tab: ffmpeg only
  applies it to 8-bit video.

## License

MIT (`LICENSE`). The download also contains third-party programs and libraries
under their own licenses; see `THIRD_PARTY_NOTICES.md`.
