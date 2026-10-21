"""The AV1 tab's Write and Verify: analyse every frame, write the MKV, check it with ffmpeg."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import os

import numpy as np
from PySide6.QtCore import QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFileDialog, QMessageBox

from .. import synth
from ..afgs1 import picture_seed
from ..frames import read_frame
from ..fullscan import analyze_every_frame, analyzer_path, cache_key, checkpoint_frame, estimate_seconds
from ..mux import WriteReport, write_afgs1_mkv
from ..paths import missing_program_hint
from ..probe import probe
from ..tools import keep_awake
from ..tuning import GrainPlan, TimelinePlan

LONG_RUN_SECONDS = 600            # Write asks first when the analysis would take longer


@dataclass
class Written:
    """The last file written: what Verify compares against."""
    report: WriteReport
    plan: GrainPlan
    seed: int


class Writer:
    """Part of the AV1 tab (MainWindow): the every-frame analysis, Write and Verify."""
    confirm_long_runs = True          # tests turn the question before an hours-long run off

    def _fullscan_dir(self) -> Path | None:
        if not (self.source and self.encoded):
            return None
        key = cache_key(self.source, self.encoded, self.offset_spin.value(), self.crop)
        return self.data_dir / "every-frame" / key

    def _every_frame_state(self) -> tuple[str, int | None]:
        """("done" | "resume" | "new", checkpoint frame) of this pair's every-frame analysis."""
        folder = self._fullscan_dir()
        if folder and (folder / "done.json").exists() and (folder / "table.tbl").exists():
            return "done", None
        at = checkpoint_frame(folder / "checkpoint.bin") if folder else None
        return ("resume", at) if at else ("new", None)

    def _every_frame_text(self) -> str:
        if not analyzer_path().is_file():
            return f"The grain analyzer is missing. {missing_program_hint()}"
        if not (self.encoded and self.source):
            return "Writing analyses the grain of every frame, like libaom's encoder, then adds it."
        if self.encoded.codec != "vvc":
            return "This encode is not VVC, so grain metadata cannot be written into it."
        state, at = self._every_frame_state()
        if state == "done":
            return (f"Every frame is already analysed ({self.encoded.frame_count:,} frames): writing "
                    "takes a minute or two.")
        seconds = estimate_seconds(self.encoded, self.workers_spin.value(), self.source)
        h, m = divmod(int(seconds // 60), 60)
        took = f"about {h} h {m:02d} min" if h else f"about {max(1, m)} min"
        text = (f"Writing first analyses all {self.encoded.frame_count:,} frames ({took} on this PC), "
                "then adds the grain.")
        if state == "resume":
            text += f" A stopped analysis continues from frame {at:,}."
        return text + " Progress is saved about once a minute."

    def _update_estimate(self) -> None:
        self.write_estimate_label.setText(self._every_frame_text())

    def _browse_output(self) -> None:
        start = self.output_edit.text() or (str(self.encoded.path.parent) if self.encoded else "")
        path, _ = QFileDialog.getSaveFileName(self, "Write MKV", start, "Matroska video (*.mkv)")
        if path:
            if not path.lower().endswith(".mkv"):
                path += ".mkv"
            self.output_edit.setText(os.path.normpath(path))
            self._output_is_auto = False

    def _write(self) -> None:
        if not (self.source and self.encoded):
            return
        output = Path(self.output_edit.text().strip().strip('"'))
        if output.suffix.lower() != ".mkv":
            QMessageBox.warning(self, "Output", "The output must be an .mkv file.")
            return
        if output.resolve() == self.encoded.path.resolve():
            QMessageBox.warning(self, "Output", "The output would overwrite the encode. Choose another name.")
            return
        if output.exists() and QMessageBox.question(
                self, "Replace file?", f"{output.name} already exists. Replace it?") != QMessageBox.StandardButton.Yes:
            return
        src, enc, tools, crop = self.source, self.encoded, self.tools, self.crop
        offset, threads, gpu = self.offset_spin.value(), self.workers_spin.value(), self.gpu_check.isChecked()
        folder, tuning, seed = self._fullscan_dir(), self._tuning(), self.seed_spin.value()
        cache_file = self._model_cache_file()
        state, _ = self._every_frame_state()
        seconds = estimate_seconds(enc, threads, src)
        if state != "done" and seconds > LONG_RUN_SECONDS and self.confirm_long_runs:
            h, m = divmod(int(seconds // 60), 60)
            answer = QMessageBox.question(
                self, "Analyse every frame and write?",
                f"The grain metadata is measured on every frame, like libaom's encoder does. That "
                f"analyses all {enc.frame_count:,} frames first and takes about {h} h {m:02d} min, using "
                "most of the CPU. The PC is kept awake while it runs.\n\n"
                "You can keep browsing frames meanwhile. Progress is saved about once a minute; if "
                "you cancel or close the app, the next Write continues from there.")
            if answer != QMessageBox.StandardButton.Yes:
                return

        def work(token, progress):
            steps = "Step 1 of 2 · " if state != "done" else ""
            model = analyze_every_frame(tools, src, enc, offset, crop, folder,
                                        lambda f, m: progress(f, steps + m), token,
                                        threads=threads, source_hwaccel=gpu)
            plan = TimelinePlan(model, tuning)
            report = write_afgs1_mkv(tools, enc, output, plan, seed,
                                     lambda f, m: progress(f, ("Step 2 of 2 · " if steps else "") + m), token)
            # Saved here, not in done(): a whole-film analysis takes seconds to serialise.
            if cache_file:
                cache_file.parent.mkdir(parents=True, exist_ok=True)
                cache_file.write_text(model.to_json(), encoding="utf-8")
            return model, plan, report

        def done(result):
            model, plan, report = result
            # The preview now shows what was written: the every-frame parameters.
            self._set_model(model)
            self.written = Written(report, plan, seed)
            size = report.output.stat().st_size / 1e9
            also = (", ".join(report.copied) + " copied from the encode") if report.copied else \
                "the encode has nothing else to copy"
            text = (f"Wrote {report.output.name} ({size:.2f} GB): grain metadata on all "
                    f"{report.pictures:,} pictures; {also}.")
            if report.notes:
                text += " " + " ".join(report.notes)
            self.write_label.setText(text + " Use Verify to check it with ffmpeg.")
            self.status.label.setText("Written.")
            self.log(text)
            self._update_enabled()

        keep_awake(True)
        self._start_job("Writing", work, done, f"Writing {output.name}…")
        self.job.finished.connect(self._write_finished)

    def _write_finished(self) -> None:
        keep_awake(False)
        self._update_estimate()             # the analysis is saved now: Write takes minutes next time

    def _verify(self) -> None:
        if not (self.written and self.encoded):
            return
        written, enc, tools, index = self.written, self.encoded, self.tools, self.current_index

        def work(token, progress):
            progress(-1, f"Decoding frame {index:,} of the written file with ffmpeg…")
            out_info = probe(written.report.output, tools.ffprobe, token)
            theirs = read_frame(tools.ffmpeg, out_info, index, token)
            clean = read_frame(tools.ffmpeg, enc, index, token)
            if theirs.index != clean.index:
                raise RuntimeError(f"the written file gave frame {theirs.index:,} where the encode gave "
                                   f"frame {clean.index:,}")
            # Near the start of a cut file the frame asked for may not decode; the
            # first decodable one comes back, and its own grain is what to compare.
            n, g = clean.index, clean.geometry
            mine = synth.apply_grain(clean.y, clean.u, clean.v, written.plan.params_at(n),
                                     picture_seed(written.seed, n), g.bit_depth,
                                     g.subsampling_x, g.subsampling_y)
            diffs = [int(np.abs(a.astype(np.int32) - b.astype(np.int32)).max())
                     for a, b in zip(mine, (theirs.y, theirs.u, theirs.v))]
            added = [float(np.abs(x.astype(np.int32) - clean.y.astype(np.int32)).mean())
                     for x in (theirs.y, mine[0])]
            return n, diffs, added[0], added[1]

        def done(result):
            n, diffs, theirs_added, mine_added = result
            where = f"Frame {n:,}" + ("" if n == index else
                                      f" (frame {index:,} cannot be decoded on its own; this is the first "
                                      "frame that can)")
            if max(diffs) == 0:
                text = (f"✔ {where}: ffmpeg's decode of the written file matches the preview exactly "
                        + (f"(grain changes luma by {theirs_added:.2f} code values on average)." if theirs_added
                           else "(this frame gets no visible grain)."))
            elif theirs_added == 0 and mine_added > 0:
                text = (f"✖ {where}: ffmpeg decoded the written file without grain. Your ffmpeg may not apply "
                        "AFGS1 film grain to VVC (FFmpeg 9.0.1 does).")
            else:
                text = f"✖ {where}: ffmpeg differs from the preview by up to {diffs} (Y, Cb, Cr)."
            self.write_label.setText(text)
            self.status.label.setText("Verified." if text.startswith("✔") else "Verification found a difference.")
            self.log(text)

        self._start_job("Verification", work, done, "Verifying with ffmpeg…")

    def _open_folder(self) -> None:
        if self.written:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.written.report.output.parent)))
