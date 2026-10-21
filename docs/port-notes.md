# Notes from the C++ port

The Python app was ported to C++ (2026-10-21) with its behaviour kept, and
checked against it: the unit references in `tests/reference` were made by
the Python app's own functions, and 22 real-file checks on a 4K film gave
identical results. What looked wrong or improvable during the port was
noted here and done after it.

## Noted during the port

| # | Note | Outcome |
| --- | --- | --- |
| 1 | Export grain table padded Cr's AR coefficients by Cb's count | Fixed (d080fef), with a test |
| 2 | The grain strip's sigma used one seed, the tune label's three | Fixed: the same three seeds (d080fef) |
| 3 | The packaged Python app once ignored a close request | Not seen in the C++ app: every walkthrough and the unzipped package close on the first request |
| 4 | Python probed videos on the window's thread | Done in the port: probes run on job threads |
| 5 | Raw frames were read into a buffer, then copied | Read straight into the planes (923094e) |
| 6 | Write's preflight kept 7 full frames per video and place | Keeps a quarter-size luma: 295 → 85 MB at 4K (923094e) |
| 7 | The plan and the AFGS1 templates kept every segment of a film | Capped at 4,096 (923094e) |
| 8 | The grain estimate passes its table through a temp file | Kept: changing the analyzer (gcc/libaom) to save ~1 ms on a ~1 s estimate is not worth it |
| 9 | frame_stats computes the variance as E[d²] − mean² | Kept: the sums are exact (integers over powers of two) and the mean is ~0 |
| 10 | The synthetic HDR probe videos carry no transfer tags | Open: the reference files are fixed data now; an HDR probe case would be written by hand |
| 11 | Check installation's job has no parent | Kept: closing the dialog early is safe; the app's exit ends the job and its programs |
| 12 | vvdecapp's version line starts with its path | Fixed (d080fef) |

## Bug check after the port

Found by the review of the code and by running the app (fgw_walkthrough)
on real files, then fixed (see the bug-check commit):

- Write failed whenever mkvmerge exited with a warning (exit code 1): the
  exit code was lost on the way.
- Closing the window while a frame loaded could start the queued frame's
  job, which was then destroyed running (an abort at exit).
- A job reads as running until its result is delivered; before, a timer in
  between could start a second job in its place.
- Results of work started on other videos (a frame, a grain estimate) were
  shown, or kept as estimates, for the videos chosen since.
- The Browse buttons stayed enabled during Write and Detect.
- The preview kept an every-frame analysis after the offset changed, and
  the saved analysis was keyed without the offset and the crop.
- VVC tab: a scene encoded before the black bars were known kept them; an
  earlier scene's encode held up the next source's black bar detection;
  without ffmpeg/ffprobe it tried to run an empty program path.
- An encode Write cannot read (a raw .266, an MPEG-TS) failed only after
  the hours-long analysis; it is now refused at once.

## Open

- Write runs even when no place of the preflight matches (a wholly wrong
  offset: every place "unclear" at a low PSNR). Refusing needs a threshold
  for pixel PSNR on grainy film; to be decided.
- A VVC encode in MPEG-TS does not load in the AV1 tab (the probe gives no
  usable frame rate or duration).
