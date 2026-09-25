"""Drive the real main window through the whole workflow on the small test clip."""

import os
import time

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtTest import QTest  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402


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
    idle = lambda: not (w.frame_job and w.frame_job.isRunning()) and not w.frame_timer.isActive()

    w.source_edit.setText(str(grainy_source))
    w._load_video("source")
    w.encoded_edit.setText(str(grainy_clip))
    w._load_video("encoded")
    # The grain of the frame shown is estimated as it loads: no analysis step.
    wait_for(qapp, lambda: idle() and SOURCE in w.state.images and GRAIN in w.state.images)
    assert w.model is None and w.current_index in w.estimates
    assert "Estimated from this frame" in w.tune_label.text()

    QTest.keyClick(w, Qt.Key.Key_Right)
    assert w.state.mode == GRAIN
    QTest.keyClick(w, Qt.Key.Key_Left)
    assert w.state.mode == ENCODED

    start = w.current_index
    QTest.keyClick(w, Qt.Key.Key_Period)
    wait_for(qapp, lambda: idle() and w.current_index == start + 1)
    assert start + 1 in w.estimates                     # a new frame gets its own estimate

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
    idle = lambda: not (w.frame_job and w.frame_job.isRunning()) and not w.frame_timer.isActive()
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
