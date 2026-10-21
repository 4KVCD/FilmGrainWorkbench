"""Drive the real main window through the whole workflow on the small test clip."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from afgs1wb.gui.jobs import running  # noqa: E402


@pytest.fixture(scope="module")
def qapp():
    return QApplication.instance() or QApplication([])


def wait_for(qapp, condition, timeout=120):
    end = time.time() + timeout
    while time.time() < end:
        qapp.processEvents()
        if condition():
            return
        time.sleep(0.02)
    raise AssertionError("timed out")


def test_full_workflow(qapp, tools, need, grainy_source, grainy_clip, tmp_path):
    need("ffmpeg", "ffprobe", "mkvmerge")
    import afgs1wb.fullscan as fullscan
    from afgs1wb.gui.main_window import MainWindow
    from afgs1wb.gui.viewer import ENCODED, GRAIN, SOURCE

    if not fullscan.analyzer_path().is_file():
        pytest.skip("grain analyzer not built")
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    w = MainWindow(settings, tmp_path / "data")
    w.show()
    idle = lambda: not running(w.frame_job) and not w.frame_timer.isActive()

    w.source_edit.setText(str(grainy_source))
    w._load_video("source")
    w.encoded_edit.setText(str(grainy_clip))
    w._load_video("encoded")
    # The grain of the frame shown is estimated as it loads: no analysis step.
    wait_for(qapp, lambda: idle() and SOURCE in w.state.images and GRAIN in w.state.images)
    assert w.model is None and w.current_index in w.estimates
    # grain flags follow libaom's noise model: overlap on, no clipping to the limited range
    assert w.encoded.limited_range and not w.clip_check.isChecked() and w.overlap_check.isChecked()
    assert not w.plan.params_at(w.current_index).clip_to_restricted_range
    assert "Estimated from this frame" in w.tune_label.text()

    QTest.keyClick(w, Qt.Key.Key_Right)
    assert w.state.mode == GRAIN
    QTest.keyClick(w, Qt.Key.Key_Left)
    assert w.state.mode == ENCODED

    start = w.current_index
    QTest.keyClick(w, Qt.Key.Key_Period)
    # A new frame gets its own estimate. It is made in a second job, started when the
    # frame's job finishes: between the two nothing runs, so wait for the estimate itself.
    wait_for(qapp, lambda: idle() and w.current_index == start + 1 and start + 1 in w.estimates)

    # a new offset pairs other source frames: the estimates are made again
    w.offset_spin.setValue(1)
    w.offset_timer.timeout.emit()
    assert not w.estimates
    w.offset_spin.setValue(0)
    w.offset_timer.timeout.emit()
    wait_for(qapp, lambda: idle() and w.current_index in w.estimates)

    w.luma_spin.setValue(250)
    wait_for(qapp, lambda: idle() and not w.tune_timer.isActive())
    assert w.plan.params_at(w.current_index).y_points

    w.output_edit.setText(str(tmp_path / "out.mkv"))
    w._write()
    wait_for(qapp, lambda: not w._busy() and w.written is not None, timeout=300)
    assert (tmp_path / "out.mkv").is_file()
    assert w.model.kind == "every-frame"                # written from every frame, not the sample
    wait_for(qapp, idle)
    w._verify()
    wait_for(qapp, lambda: not w._busy() and "matches the preview exactly" in w.write_label.text())
    w.close()


def test_every_frame_workflow(qapp, tools, need, grainy_source, grainy_clip, tmp_path, monkeypatch):
    need("ffmpeg", "ffprobe", "mkvmerge")
    import afgs1wb.fullscan as fullscan
    from afgs1wb.gui.main_window import MainWindow
    from afgs1wb.gui.viewer import GRAIN

    if not fullscan.analyzer_path().is_file():
        pytest.skip("grain analyzer not built")
    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    w = MainWindow(settings, tmp_path / "data")
    w.show()
    idle = lambda: not running(w.frame_job) and not w.frame_timer.isActive()
    w.source_edit.setText(str(grainy_source))
    w._load_video("source")
    w.encoded_edit.setText(str(grainy_clip))
    w._load_video("encoded")
    wait_for(qapp, idle)

    # No Analyse grain first: Write analyses every frame by itself.
    assert w.model is None and w.write_button.isEnabled()
    assert "Writing first analyses all" in w.write_estimate_label.text()
    w.gpu_check.setChecked(False)
    w.output_edit.setText(str(tmp_path / "every.mkv"))
    w._write()
    wait_for(qapp, lambda: not w._busy() and w.written is not None, timeout=300)
    wait_for(qapp, lambda: idle() and GRAIN in w.state.images)
    assert "Every frame analysed" in w.analysis_label.text()
    assert "already analysed" in w.write_estimate_label.text()
    assert w.grain_timeline.isVisibleTo(w)
    assert "At this frame" in w.tune_label.text()

    w.grain_timeline.frame_clicked.emit(30)
    wait_for(qapp, lambda: idle() and w.current_index == 30)
    w._verify()
    wait_for(qapp, lambda: not w._busy() and "matches the preview exactly" in w.write_label.text())

    # reopening the same files restores the every-frame model
    w.close()
    w2 = MainWindow(settings, tmp_path / "data")
    wait_for(qapp, lambda: w2.model is not None, timeout=30)
    assert "previous every-frame analysis" in w2.analysis_label.text()
    w2.close()


def test_errors_in_the_gui_thread_are_reported(qapp, tmp_path, monkeypatch):
    # Under pythonw an exception in a Qt slot used to vanish silently.
    import sys
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QMessageBox
    from afgs1wb.gui.app import install_error_handler
    from afgs1wb.gui.main_window import MainWindow

    shown = []
    monkeypatch.setattr(QMessageBox, "critical", staticmethod(lambda *a, **k: shown.append(a[2])))
    monkeypatch.setattr(sys, "excepthook", sys.excepthook)
    w = MainWindow(QSettings(str(tmp_path / "s.ini"), QSettings.Format.IniFormat), tmp_path / "data")
    install_error_handler(w)

    def broken():
        raise ZeroDivisionError("a bug in a slot")
    QTimer.singleShot(0, broken)
    wait_for(qapp, lambda: shown, timeout=10)
    assert "a bug in a slot" in shown[0]
    assert "a bug in a slot" in (tmp_path / "data" / "errors.log").read_text(encoding="utf-8")
    assert "Unexpected error" in w.log.toPlainText()
    w.close()


def test_each_pair_keeps_its_frame_offset_and_shows_how_well_frames_match(
        qapp, tools, need, grainy_source, grainy_clip, tmp_path):
    need("ffmpeg", "ffprobe")
    from afgs1wb.gui.main_window import MainWindow
    from afgs1wb.gui.viewer import ENCODED

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    w = MainWindow(settings, tmp_path / "data")
    idle = lambda: not running(w.frame_job) and not w.frame_timer.isActive()
    w.source_edit.setText(str(grainy_source)); w._load_video("source")
    w.encoded_edit.setText(str(grainy_clip)); w._load_video("encoded")
    wait_for(qapp, lambda: idle() and ENCODED in w.state.images)
    assert "✔" in w.offset_note.text()                        # the right pairing reads as a match
    w.offset_spin.setValue(3)
    w.offset_timer.timeout.emit()
    wait_for(qapp, lambda: idle() and ENCODED in w.state.images)
    assert "⚠" in w.offset_note.text()                        # 3 frames off: flagged
    w.close()

    w2 = MainWindow(settings, tmp_path / "data")              # same pair again: its offset is back
    wait_for(qapp, lambda: w2.encoded is not None)
    assert w2.offset_spin.value() == 3
    w2.encoded_edit.setText(str(grainy_source)); w2._load_video("encoded")
    assert w2.offset_spin.value() == 0                        # another pair does not inherit it
    w2.close()


def test_a_saved_analysis_loads_in_the_background(qapp, tools, need, grainy_source, grainy_clip, tmp_path):
    # A whole-film analysis takes seconds to read; loading it must not freeze the window.
    need("ffmpeg", "ffprobe")
    import afgs1wb.fullscan as fullscan
    from afgs1wb.fullscan import TimelineModel
    from afgs1wb.gui.main_window import MainWindow
    from conftest import sample_params

    settings = QSettings(str(tmp_path / "settings.ini"), QSettings.Format.IniFormat)
    w = MainWindow(settings, tmp_path / "data")
    w.source_edit.setText(str(grainy_source)); w._load_video("source")
    w.encoded_edit.setText(str(grainy_clip)); w._load_video("encoded")
    model = TimelineModel([fullscan.Segment(0, w.encoded.frame_count, sample_params())], 10, 1, 1,
                          w.encoded.fps, w.encoded.frame_count, 3 * 3600 + 42 * 60, "every-frame")
    w._model_cache_file().parent.mkdir(parents=True)
    w._model_cache_file().write_text(model.to_json(), encoding="utf-8")
    w.close()

    w2 = MainWindow(settings, tmp_path / "data")
    wait_for(qapp, lambda: w2.encoded is not None)
    assert w2.model is None                                   # not read on the GUI thread...
    assert "Loading the saved every-frame analysis" in w2.analysis_label.text()
    wait_for(qapp, lambda: w2.model is not None)              # ...but arrives shortly after
    assert "The analysis took 3 h 42 min." in w2.analysis_label.text()
    w2.close()
