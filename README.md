# AFGS1 Grain Workbench

Measure the film grain a VVC encode lost, preview it on real frames, tune its
strength, and write it back as **AFGS1** metadata (AV1 film grain carried in
ITU-T T.35 SEI messages) so the decoder re-synthesises it at playback.

## How it works

The grain is measured with libaom's own noise model (`aom_dsp/noise_model.c`,
built into `bin/afgs1_grain_analyzer.exe` by `analyzer/build.py`), comparing
each source frame with the same encoded frame.

1. **Videos.** Choose the source and the VVC encode. *Detect* finds the frame
   offset if the encode was trimmed.
2. **Preview and tune.** The viewer estimates the grain of the frame shown as
   it loads, and again for every frame you move to. Luma and chroma strength
   scale it exactly; the grain is rendered with the same algorithm decoders use.
3. **Write.** Every frame is analysed the way libaom's encoder does with
   `--denoise-noise-level`: one model carried through the film, updated on each
   frame and restarted when the noise changes, so the grain follows the film
   shot by shot. Source and encode are decoded once, in order, by one ffmpeg
   process and paired by timestamp; the model work runs on all CPU threads.
   Progress is saved about once a minute, so a stopped run resumes, and a
   finished analysis is reused. Then every picture of the encode gets a
   complete AFGS1 parameter set with its own seed (libaom's: 7391, +3381 per
   frame). Audio, subtitles, chapters, attachments and HDR properties are
   carried over. After writing, the viewer shows the written parameters.
4. **Verify.** ffmpeg decodes a frame of the written file; the app checks it is
   bit-identical to the preview.

Grain flags default to libaom's: overlap on, no clipping to the limited range.

## Requirements

- Windows 10/11, Python 3.11+
- `pip install -r requirements.txt` (PySide6, PyAV, NumPy)
- **ffmpeg / ffprobe** with VVC decoding. FFmpeg 7.1 or newer applies AFGS1
  grain when decoding VVC.
- **mkvmerge** from [MKVToolNix](https://mkvtoolnix.download/).
- The grain analyzer: `python analyzer/build.py` (needs gcc, cmake and ninja;
  it fetches libaom).

The app looks for ffmpeg, ffprobe and mkvmerge on `PATH` and in the usual
install folders; set them under *Settings → External programs* otherwise.

## Run

```bat
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
"Run AFGS1 Workbench.bat"
```

Tests: `.venv\Scripts\python -m pip install -r requirements-dev.txt` then
`.venv\Scripts\python -m pytest`.

## VVC grain analyzer

VVC's own film grain, instead of AV1's: `"Run VVC Grain Analyzer.bat"`, or
*Window → VVC grain analyzer* in the workbench. Choose a source video and
browse it for a scene: only the source is decoded while you look. *Encode
this scene* (E) encodes the frames around the one shown with VVenC in all
three film grain analysis modes (`--fga 1`, `2`, `3`), each writing an H.274
film grain characteristics SEI message, and decodes them with vvdec twice. The
selected mode is shown as soon as it is ready; the other two are encoded in
the background, so switching modes is then instant. VVenC analyses the grain
on the first picture of every GOP (32 frames); the pictures in between carry
the latest parameters. Inside the encoded scene, the views are:

1. **Source**: the source frame, cropped like the encode.
2. **VVC, grain removed**: what VVenC coded (its MCTF pre-filter and the
   quantisation remove the grain), decoded with film grain synthesis off.
3. **VVC + grain synthesis**: the same, decoded with vvdec's synthesis on.
4. **Grain only (×4)**: the grain vvdec adds.

The side panel shows the grain parameters the frame carries (read from the
bitstream, both the frequency and the auto-regressive model), how much grain
VVenC removed and vvdec adds, and the colour shift the grain causes. Inside an
encoded scene, changing the strength, QP, preset or scene length encodes the
scene again (all three modes); outside it, nothing is encoded.

Temporary files: one scene at a time, in a folder in `%TEMP%` (for 33 frames
at 3840×1608 about 4.3 GB: the source frames once, and each mode decoded with
and without grain). Whatever is replaced is deleted as soon as nothing reads
it, the folder when the window closes, and folders left by a crash when the
next window opens.

vvencapp and vvdecapp are built by `vvc\build_vvc_tools.ps1` (Visual Studio
2022 and CMake) into `bin`: VVenC from fraunhoferhhi/vvenc pull request 765,
which includes pull request 764 (`--fga 1` VTM JVET-AN0237 port, `--fga 2`
calibrated, `--fga 3` auto-regressive model), and vvdec master.

## Viewer keys

| Key | Action |
| --- | --- |
| ← / → | Encoded / Encoded + grain |
| 1 2 3 4 | Source, Encoded, Encoded + grain, grain only (×4) |
| , / . | Previous / next frame |
| PgUp / PgDn | ±1 second |
| wheel, + / − | Zoom (nearest-neighbour from 100 %, so single grains stay sharp) |
| 0 / F | 100 % / fit |
| drag | Pan |

*Open in window* shows the frame alone in a resizable window with the same keys.

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
- Players must support AFGS1 for VVC to show the grain (ffmpeg-based players,
  mpv). VVC's own H.274 film grain SEI is not used: ffmpeg only applies it to
  8-bit video.
