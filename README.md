# AFGS1 Grain Workbench

Measure the film grain a VVC encode lost, preview it on real frames, tune its
strength, and write it back as **AFGS1** metadata (AV1 film grain carried in
ITU-T T.35 SEI messages) so the decoder re-synthesises it at playback.

## How it works

1. **Analyse.** AOM's reference estimator (`noise_model` from libaom) models the
   grain the encode removed by comparing it with the source. Two modes:
   - **Quick sample** (about a minute): short runs of frames from 16 places,
     merged into one grain model for the whole video.
   - **Every frame** (hours for a 4K film): every frame is analysed, as libaom's
     encoder does with `--denoise-noise-level`; a new parameter segment starts
     whenever the noise changes, so the grain follows the film shot by shot.
     Frames stream from ffmpeg into `noise_model` through named pipes (no
     temporary files) in parallel chunks; finished chunks are kept, so an
     interrupted run resumes. The PC is kept awake while it runs.

   Frame alignment is checked throughout; a wrong offset stops every-frame
   analysis within seconds.
2. **Tune.** The model is shown as grain strength (σ) against brightness (and,
   in every-frame mode, as a strip of grain strength over the whole film under
   the viewer; click it to jump there).
   Luma and chroma strength scale it exactly; the frame viewer shows the
   result on any frame, rendered with the same algorithm decoders use.
3. **Write.** Every picture of the encode gets a complete AFGS1 parameter set
   with its own seed. Audio, subtitles, chapters, attachments and HDR
   properties are carried over.
4. **Verify.** ffmpeg decodes a frame of the written file; the app checks it is
   bit-identical to the preview.

## Requirements

- Windows 10/11, Python 3.11+
- `pip install -r requirements.txt` (PySide6, PyAV, NumPy)
- **ffmpeg / ffprobe** with VVC decoding. FFmpeg 7.1 or newer applies AFGS1
  grain when decoding VVC.
- **noise_model** from libaom, built with `-DENABLE_EXAMPLES=ON`.
- **mkvmerge** from [MKVToolNix](https://mkvtoolnix.download/).

The app looks for these on `PATH` and in the usual install folders; set them
under *Settings → External programs* otherwise.

## Run

```bat
py -3 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
"Run AFGS1 Workbench.bat"
```

Tests: `.venv\Scripts\python -m pip install -r requirements-dev.txt` then
`.venv\Scripts\python -m pytest`.

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
