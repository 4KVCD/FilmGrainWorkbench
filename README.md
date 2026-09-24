# AFGS1 VVC Workbench

A Windows desktop GUI for comparing a grainy source with its VVC reconstruction, estimating the residual grain model with AOMedia's `noise_model`, adjusting the model strength, and writing AFGS1 registered T.35 metadata into VVC picture packets in an MKV file.

## What the metadata is

AFGS1 is carried as ITU-T T.35 registered user data (`B5 58 90 01`) inside a VVC **prefix SEI NAL unit**. The Matroska file stores the VVC bitstream packets that contain that SEI. A sidecar table is optional and is only used as an editable/exportable analysis result.

AFGS1 defines a film-grain post-processing process that can be used with different video codecs and transport mechanisms. An H.266 decoder is not required by the H.266 specification to synthesize AFGS1 grain. Playback needs a decoder or post-processing path that recognizes and applies AFGS1. FFmpeg has AFGS1/T.35 parsing support in some builds; actual display support depends on the decoder and player path.

## Requirements

- Windows 10/11
- Python 3.11 or newer
- FFmpeg and FFprobe executables
- AOMedia `noise_model` example executable built from libaom
- Optional InterDigital `vfgs` executable for a synthesized frame preview
- PySide6, PyAV 18.0 or newer, and NumPy (installed from `requirements.txt`)
- A PyAV/FFmpeg build that can demux your VVC file and mux Matroska

Useful project links:

- [AFGS1 specification](https://aomediacodec.github.io/afgs1-spec/)
- [AOMedia noise_model example](https://aomedia.googlesource.com/aom/+/refs/heads/main/examples/noise_model.c)
- [AOMedia grain table format implementation](https://aomedia.googlesource.com/aom/+/refs/heads/main/aom_dsp/grain_table.c)
- [FFmpeg downloads](https://ffmpeg.org/download.html)
- [PyAV documentation](https://pyav.basswood.io/docs/stable/)
- [PySide6 documentation](https://doc.qt.io/qtforpython-6/)
- [InterDigital Versatile Film Grain (AFGS1 synthesis/reference tooling)](https://github.com/InterDigitalInc/VersatileFilmGrain)
- [FGSEditor (film-grain table editing)](https://github.com/PingWer/FGSEditor)

## Install and launch

Open PowerShell in this folder and run:

```powershell
py -3 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python -m afgs1vvc.app
```

You can also launch `Run-AFGS1VVCWorkbench.bat` after installing the requirements. Enter the paths to `ffmpeg.exe`, `ffprobe.exe`, and `noise_model.exe` in the **External tools** tab. Add `vfgs.exe` there to enable the optional synthesized-frame preview. Paths are remembered between launches.

### Building AOM's `noise_model`

Build libaom with its examples enabled, then point the GUI to the resulting `noise_model.exe`. The current upstream AOM CMake project is linked above. A typical CMake build configuration includes `-DENABLE_EXAMPLES=ON`; the generated example executable is usually under the build's `examples` directory. Build steps vary by Visual Studio and CMake version.

### Optional VFGS preview

Clone [InterDigital's VFGS repository](https://github.com/InterDigitalInc/VersatileFilmGrain), build it with CMake (or the GCC command shown in its README), and point the GUI to `vfgs.exe`. The app passes a one-interval AOM grain table and one decoded YUV frame to VFGS, which returns a synthesized frame for visual review.

## Workflow

1. Choose the original grainy source, encoded VVC video, and required FFmpeg, FFprobe, and AOM `noise_model` paths. The output path is suggested automatically; check or change it before writing.
2. Confirm the frame offset. **Auto align** gives an initial low-resolution estimate; positive values mean the encode starts later. A side-by-side preview is optional.
3. Set the analysis sample limit and click **Analyze grain**. The busy bar and status line show the current sampling or model-estimation stage. Long clips are sampled at regular intervals; when intervals are sparse, the app seeks to the sample timestamps instead of decoding every intervening frame. The button stays disabled while analysis runs.
4. When the status says analysis is complete, adjust **Overall strength**, seed offset, grain scale shift, overlap, and range clipping. The strength control scales the AFGS1 intensity-dependent grain scaling curves. **Render VFGS preview** uses VFGS on the selected frame; the original appears on the left and the synthesized-grain result appears in the right preview pane. The time arrows step by one video frame and refresh the current preview. The progress bar indicates that rendering is running.
5. Check the output MKV path and click **Write AFGS1 MKV**. The app copies supported streams and attaches a self-contained AFGS1 prefix SEI to each VVC picture packet. Use **Save AOM grain table** if you want to keep the analyzer's editable `filmgrn1` output.

## Notes and limits

- This is an early desktop implementation. It expects the FFmpeg demuxer to expose one VVC access unit per video packet and requires PyAV's bundled FFmpeg to understand the input VVC bitstream. Unsupported stream layouts or muxing cases are reported rather than silently re-encoded.
- Source and encoded video must depict the same content. Auto alignment is an initial estimate; confirm it visually, especially for cuts, fades, variable-frame-rate video, or edits.
- The estimator compares the original to the encoded reconstruction. It works best when the VVC encode has removed most of the original grain while retaining the underlying image. Grain left in the encode reduces the residual grain the estimator sees.
- Analysis is capped by the sampled-frame limit. Long clips are sampled at a lower rate, but the generated model intervals still use timestamps in the source timeline.
- The GUI writes T.35 messages inside the VVC bitstream, not container attachments. Vanilla VVC playback does not guarantee AFGS1 synthesis. Validate output using an AFGS1-capable post-processing path before distribution.
- The synthesized preview is optional, uses VFGS, and approximates the eventual decoder output. Strength, seed, grain scale shift, and overlap are reflected; range clipping may differ.
- The encode, analysis, preview, metadata write, and output decode paths have been exercised with a short grainy source, a VVC encode, and AOM's `noise_model`. Player-side AFGS1 synthesis still depends on a decoder or post-processing path that supports AFGS1.
