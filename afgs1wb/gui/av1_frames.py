"""The AV1 tab's frames: decoding the encode and the source, and the grain preview on top."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from PySide6.QtGui import QImage

from .. import display, synth
from ..afgs1 import picture_seed
from ..alignment import block_means, frame_match
from ..frames import Frame, Geometry, read_frames
from ..fullscan import estimate_frame
from ..grain_params import GrainParams
from ..tuning import scale_params
from .frame_pane import timecode, to_qimage
from .jobs import Job, running
from .viewer import ENCODED, GRAIN, GRAIN_ONLY, SOURCE

PREFETCH = 4                      # frames decoded per seek while browsing
CACHE_BYTES = 800 * 1024 * 1024   # decoded frames kept for instant back/forth stepping


class FrameLoader:
    """Part of the AV1 tab (MainWindow): which frame is shown, and loading it.

    A frame loads in two steps: the decoded encode (and its source frame)
    first, then the grain, which may need a live estimate (~0.8 s at 4K).
    Uses the tab's videos, offset, plan, caches and status bar.
    """

    def _frames_per_second(self) -> int:
        return int(round(float(self.encoded.fps))) if self.encoded else 24

    def _step(self, delta: int) -> None:
        if self.encoded:
            self._go_to(max(0, min(self.encoded.frame_count - 1, self.current_index + delta)))

    def _go_to(self, index: int, force: bool = False) -> None:
        if not self.encoded:
            return
        index = max(0, min(self.encoded.frame_count - 1, int(index)))
        if index == self.current_index and not force and self.state.images:
            return
        self.current_index = index
        self.pane.show_frame(index, self.encoded.time_of(index))
        self.grain_timeline.set_current(index)
        self.frame_timer.start()

    def _caption(self, index: int) -> str:
        return f"frame {index:,} · {timecode(self.encoded.time_of(index))}"

    def _load_current_frame(self) -> None:
        self._request_frame(self.current_index)

    def _cached(self, key: tuple) -> Frame | None:
        frame = self.frame_cache.get(key)
        if frame is not None:
            self.frame_cache.move_to_end(key)
        return frame

    def _request_frame(self, index: int) -> None:
        if not self.encoded or self.tools.missing("ffmpeg"):
            return
        if running(self.frame_job):
            self.pending_index = index
            return
        self.pending_index = None
        src_info, enc_info, crop, tools = self.source, self.encoded, self.crop, self.tools
        offset = self.offset_spin.value()
        geometry = Geometry.of(enc_info)
        src_key = ("src", str(src_info.path) if src_info else "", index + offset, crop)
        enc_key = ("enc", str(enc_info.path), index)
        src_frame = self._cached(src_key) if src_info else None
        enc_frame = self._cached(enc_key)
        # The source frames either side of the paired one tell a wrong offset from a still shot.
        beside = {k: f for k in (index + offset - 1, index + offset + 1)
                  if src_info and (f := self._cached(("src", str(src_info.path), k, crop))) is not None}
        params = self.plan.params_at(index) if self.plan else None
        seed = picture_seed(self.seed_spin.value(), index)
        converter = self.converter
        # A seek into a long-GOP stream costs ~1-2 s at 4K, the next few frames
        # almost nothing: decode a small window in the direction of travel.
        backwards = index < self._last_requested
        self._last_requested = index
        start = max(0, index - PREFETCH + 1) if backwards else index
        count = min(PREFETCH, enc_info.frame_count - start)

        def work(token, progress):
            with ThreadPoolExecutor(2) as pool:
                need_src = src_info is not None and src_frame is None \
                    and 0 <= index + offset < src_info.frame_count
                s_start = max(0, start + offset - 1)          # one extra frame either side
                fs = pool.submit(read_frames, tools.ffmpeg, src_info, s_start,
                                 min(count + 2, src_info.frame_count - s_start), token, geometry, crop) \
                    if need_src else None
                fe = pool.submit(read_frames, tools.ffmpeg, enc_info, start, count, token) \
                    if enc_frame is None else None
                s_run = fs.result() if fs else []
                e_run = fe.result() if fe else []
            s = src_frame or next((f for f in s_run if f.index == index + offset), None)
            e = enc_frame or next((f for f in e_run if f.index == index), None)
            if e is None:
                if e_run and e_run[0].index > index:
                    # A file cut from a longer one starts with pictures that refer to
                    # frames cut away; the first ones cannot be decoded.
                    raise RuntimeError(f"frame {index:,} cannot be decoded: its pictures refer to frames "
                                       f"cut away before it. The first frame that can is {e_run[0].index:,}.")
                raise RuntimeError(f"frame {index:,} is past the end of the encode")
            with ThreadPoolExecutor(3) as pool:
                jobs = {ENCODED: pool.submit(lambda: to_qimage(converter.to_rgb(e.y, e.u, e.v)))}
                if s is not None:
                    jobs[SOURCE] = pool.submit(lambda: to_qimage(converter.to_rgb(s.y, s.u, s.v)))
                grain = pool.submit(self._grain_images, converter, e, params, seed) if params is not None else None
                images = {k: j.result() for k, j in jobs.items()}
                if grain:
                    images.update(grain.result())
            extra = [(("src", str(src_info.path), f.index, crop), f) for f in s_run] + \
                    [(("enc", str(enc_info.path), f.index), f) for f in e_run]
            match = None
            if src_info is not None:
                paired = index + offset
                near = {**beside, **{f.index: f for f in s_run}, **({paired: s} if s is not None else {})}
                means = {k: block_means(near[k].y) for k in (paired - 1, paired, paired + 1) if k in near}
                match = frame_match(means, block_means(e.y), paired, float((1 << geometry.bit_depth) - 1))
            return index, extra, images, (offset, match)

        job = Job("Frame", work, self)
        job.succeeded.connect(self._frame_ready)
        job.failed.connect(lambda msg: self._frame_failed(index, msg))
        job.finished.connect(self._frame_job_finished)
        self.frame_job = job
        if not self.state.images:
            self.state.clear(f"Loading frame {index:,}…")
        if not self._busy():
            self.status.label.setText(f"Loading frame {index:,}…")
        job.start()

    @staticmethod
    def _grain_images(converter: display.Converter, e: Frame, params: GrainParams,
                      seed: int) -> dict[int, QImage]:
        """The encoded frame with grain added, and the grain alone (runs on a worker thread)."""
        g = e.geometry
        y, u, v = synth.apply_grain(e.y, e.u, e.v, params, seed, g.bit_depth,
                                    g.subsampling_x, g.subsampling_y)
        return {GRAIN: to_qimage(converter.to_rgb(y, u, v)),
                GRAIN_ONLY: to_qimage(display.grain_only(e.y, y, g.bit_depth))}

    def _frame_ready(self, result) -> None:
        index, extra, images, (offset, match) = result
        for key, frame in extra:
            self.frame_cache[key] = frame
            self.frame_cache.move_to_end(key)
        budget = CACHE_BYTES
        for key in reversed(list(self.frame_cache)):
            f = self.frame_cache[key]
            budget -= f.y.nbytes + f.u.nbytes + f.v.nbytes
            if budget < 0:
                del self.frame_cache[key]
        if index != self.current_index:
            return
        if offset == self.offset_spin.value():
            self._show_match(index, match)
        # A new frame's grain is estimated in a second step, so the frame itself shows at once.
        grain_follows = GRAIN not in images and self._grain_possible(index)
        self.grain_dirty |= grain_follows
        if grain_follows and self.state.mode in (GRAIN, GRAIN_ONLY) and self.state.images:
            # Someone comparing grain keeps seeing grain: the new frame appears with its grain.
            self._held = (index, images)
        else:
            self._held = None
            self.state.set_images(images, self._caption(index))
        if not self._busy():
            self.status.label.setText(f"Frame {index:,} loaded." if not grain_follows
                                      else f"Frame {index:,}: adding the estimated grain…")
        self._update_segment_plot()
        self._update_enabled()

    def _show_match(self, index: int, match) -> None:
        """Under the offset: does the frame shown pair with the right source frame?"""
        text = ""
        if self.source and match is not None:
            verdict, same, best = match
            paired = index + self.offset_spin.value()
            if verdict == "same":
                text = (f"✔ Frame {index:,} matches source frame {paired:,} ({same:.1f} dB; the frames "
                        f"beside it {best:.1f} dB).")
            elif verdict == "neighbour":
                text = (f"⚠ Frame {index:,}: a source frame next to {paired:,} matches it better "
                        f"({best:.1f} vs {same:.1f} dB), so the offset is off by one. Try Detect.")
            elif verdict == "none":
                text = (f"⚠ Frame {index:,} does not match source frame {paired:,} or the frames beside it "
                        f"({same:.1f} dB): the frame offset looks wrong. Try Detect.")
            elif verdict == "unclear":
                text = (f"Frame {index:,}: the offset can't be checked here; the source frames around "
                        f"{paired:,} look alike ({same:.1f} dB).")
            else:
                text = f"No source frame for encoded frame {index:,} at this offset (source frame {paired:,})."
        self.offset_note.setText("\n".join(t for t in (self._detected, text) if t))

    def _src_frame(self, index: int) -> Frame | None:
        if not self.source:
            return None
        return self._cached(("src", str(self.source.path), index + self.offset_spin.value(), self.crop))

    def _grain_possible(self, index: int) -> bool:
        """Grain parameters exist for the frame, or can be estimated from its decoded source frame."""
        if not self.plan:
            return False
        return self.plan.params_at(index) is not None or \
            (self.model is None and self._src_frame(index) is not None)

    def _frame_failed(self, index: int, message: str) -> None:
        self.log(f"Could not load frame {index}: {message}")
        if index == self.current_index:
            self.state.clear(f"Could not decode frame {index:,}.\n\n{message.splitlines()[0]}")

    def _frame_job_finished(self) -> None:
        if self.pending_index is not None:
            self._request_frame(self.pending_index)
        elif self.grain_dirty:
            self._render_grain()

    def _render_grain(self) -> None:
        """Re-synthesise the grain views for the current frame after a tuning change."""
        if not (self.plan and self.encoded):
            return
        if running(self.frame_job):
            self.grain_dirty = True
            return
        self.grain_dirty = False
        enc = self._cached(("enc", str(self.encoded.path), self.current_index))
        if enc is None:
            self._request_frame(self.current_index)
            return
        index, offset = self.current_index, self.offset_spin.value()
        params, src = self.plan.params_at(index), self._src_frame(index)
        if params is None and (self.model is not None or src is None):
            self._request_frame(index)
            return
        tuning, seed = self._tuning(), picture_seed(self.seed_spin.value(), index)
        is420 = (self.encoded.subsampling_x, self.encoded.subsampling_y) == (1, 1)
        converter = self.converter

        def work(token, progress):
            raw = None
            if params is None:          # the grain of this frame: libaom's model on it (~0.8 s at 4K)
                raw = estimate_frame(src, enc, token)
            shown = params if params is not None else scale_params(raw, tuning, is420)[0]
            return index, self._grain_images(converter, enc, shown, seed), (offset, raw)

        job = Job("Grain preview", work, self)
        job.succeeded.connect(self._grain_ready)
        job.failed.connect(lambda msg: self._grain_ready((index, {}, (offset, None)),
                                                         f"Grain preview failed: {msg}"))
        job.finished.connect(self._frame_job_finished)
        self.frame_job = job
        job.start()

    def _grain_ready(self, result, error: str = "") -> None:
        index, images, (offset, raw) = result
        if error:
            self.log(error)
        if raw is not None and offset == self.offset_spin.value() and self.model is None:
            self.estimates[index] = raw
        held, self._held = self._held, None
        if index != self.current_index:
            return
        if held and held[0] == index:
            self.state.set_images({**held[1], **images}, self._caption(index))
        else:
            self.state.update_images(images)
        if not self._busy():
            self.status.label.setText(f"Frame {index:,} loaded.")
        self._update_segment_plot()
        self._update_enabled()
